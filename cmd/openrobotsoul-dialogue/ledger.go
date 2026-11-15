package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/session"
)

// Every visible state transition is one checksummed, synced frame. A partial
// final line is discarded during recovery; complete corrupt frames fail closed.
// Old frames (including messages covered by summaries) are never deleted.
type walFrame struct {
	Sequence uint64          `json:"sequence"`
	ID       string          `json:"record_id"`
	Payload  json.RawMessage `json:"payload"`
	Checksum string          `json:"checksum"`
}

type messageEntry struct {
	ID      string            `json:"id"`
	TurnID  string            `json:"turn_id"`
	Message providers.Message `json:"message"`
}

type sessionState struct {
	Generation      uint64         `json:"generation"`
	SummaryRevision uint64         `json:"summary_revision"`
	Summary         string         `json:"summary"`
	Entries         []messageEntry `json:"entries"`
}

type requestRecord struct {
	Digest   string          `json:"digest"`
	Response json.RawMessage `json:"response,omitempty"`
}

type walRecord struct {
	Kind       string          `json:"kind"`
	SessionKey string          `json:"session_key,omitempty"`
	State      *sessionState   `json:"state,omitempty"`
	RequestID  string          `json:"request_id,omitempty"`
	Digest     string          `json:"digest,omitempty"`
	Response   json.RawMessage `json:"response,omitempty"`
	EventID    string          `json:"event_id,omitempty"`
	EventHash  string          `json:"event_hash,omitempty"`
}

type pendingTurn struct {
	requestID string
	base      sessionState
	added     []messageEntry
	replace   bool
	dirty     bool
	ordinal   int
}

type durableFile interface {
	Write([]byte) (int, error)
	Sync() error
	Close() error
}

type ledger struct {
	mu       sync.Mutex
	file     durableFile
	lock     *os.File
	sequence uint64
	sessions map[string]sessionState
	requests map[string]requestRecord
	events   map[string]string
	pending  map[string]*pendingTurn
	fatal    error
	closed   bool
}

var _ session.SessionStore = (*ledger)(nil)

func openLedger(home string) (*ledger, error) {
	if err := os.MkdirAll(home, 0700); err != nil {
		return nil, err
	}
	lock, err := os.OpenFile(filepath.Join(home, "dialogue.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err = lockFile(lock); err != nil {
		lock.Close()
		return nil, fmt.Errorf("dialogue home is already in use: %w", err)
	}
	f, err := os.OpenFile(filepath.Join(home, "dialogue.wal"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		lock.Close()
		return nil, err
	}
	l := &ledger{file: f, lock: lock, sessions: map[string]sessionState{}, requests: map[string]requestRecord{}, events: map[string]string{}, pending: map[string]*pendingTurn{}}
	if err = l.replay(f); err != nil {
		l.Close()
		return nil, err
	}
	return l, nil
}

func frameChecksum(sequence uint64, id string, payload []byte) string {
	h := sha256.New()
	fmt.Fprintf(h, "%d\x00%s\x00", sequence, id)
	h.Write(payload)
	return hex.EncodeToString(h.Sum(nil))
}

func digestBytes(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func (l *ledger) replay(f *os.File) error {
	r := bufio.NewReader(f)
	var offset int64
	for {
		line, err := r.ReadBytes('\n')
		if errors.Is(err, io.EOF) {
			if len(line) != 0 {
				if err = f.Truncate(offset); err != nil {
					return err
				}
				if err = f.Sync(); err != nil {
					return err
				}
			}
			_, err = f.Seek(offset, io.SeekStart)
			return err
		}
		if err != nil {
			return err
		}
		var frame walFrame
		if err = json.Unmarshal(line, &frame); err != nil {
			return fmt.Errorf("corrupt WAL at byte %d: %w", offset, err)
		}
		if frame.Sequence != l.sequence+1 || frame.ID != fmt.Sprintf("wal-%020d", frame.Sequence) || frame.Checksum != frameChecksum(frame.Sequence, frame.ID, frame.Payload) {
			return fmt.Errorf("WAL integrity check failed at byte %d", offset)
		}
		var record walRecord
		if err = json.Unmarshal(frame.Payload, &record); err != nil {
			return fmt.Errorf("invalid WAL payload: %w", err)
		}
		if err = l.apply(record); err != nil {
			return err
		}
		l.sequence = frame.Sequence
		offset += int64(len(line))
	}
}

func (l *ledger) apply(r walRecord) error {
	switch r.Kind {
	case "request_begin":
		if _, exists := l.requests[r.RequestID]; exists || r.RequestID == "" || r.Digest == "" {
			return errors.New("invalid request_begin in WAL")
		}
		l.requests[r.RequestID] = requestRecord{Digest: r.Digest}
	case "request_response":
		old, ok := l.requests[r.RequestID]
		if !ok || len(old.Response) > 0 || len(r.Response) == 0 {
			return errors.New("invalid request_response in WAL")
		}
		old.Response = append(json.RawMessage(nil), r.Response...)
		l.requests[r.RequestID] = old
	case "session_commit", "summary_commit", "session_clear":
		if r.SessionKey == "" || r.State == nil {
			return errors.New("invalid session commit in WAL")
		}
		l.sessions[r.SessionKey] = cloneState(*r.State)
		if r.EventID != "" {
			l.events[eventKey(r.SessionKey, r.EventID)] = r.EventHash
		}
	default:
		return fmt.Errorf("unknown WAL record kind %q", r.Kind)
	}
	return nil
}

func (l *ledger) healthLocked() error {
	if l.fatal != nil {
		return l.fatal
	}
	if l.closed {
		return errors.New("dialogue storage is closed")
	}
	return nil
}

func (l *ledger) Health() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.healthLocked()
}

func (l *ledger) failLocked(err error) error {
	if l.fatal == nil {
		l.fatal = fmt.Errorf("dialogue storage failed; restart required: %w", err)
	}
	return l.fatal
}

func (l *ledger) appendLocked(record walRecord) error {
	if err := l.healthLocked(); err != nil {
		return err
	}
	payload, err := json.Marshal(record)
	if err != nil {
		return l.failLocked(err)
	}
	seq := l.sequence + 1
	id := fmt.Sprintf("wal-%020d", seq)
	frame := walFrame{Sequence: seq, ID: id, Payload: payload, Checksum: frameChecksum(seq, id, payload)}
	line, err := json.Marshal(frame)
	if err != nil {
		return l.failLocked(err)
	}
	line = append(line, '\n')
	n, err := l.file.Write(line)
	if err == nil && n != len(line) {
		err = io.ErrShortWrite
	}
	if err == nil {
		err = l.file.Sync()
	}
	if err != nil {
		return l.failLocked(err)
	}
	if err = l.apply(record); err != nil {
		return l.failLocked(err)
	}
	l.sequence = seq
	return nil
}

// BeginRequest returns the exact previous response for a completed duplicate.
// An interrupted request is never reissued to the model after a process crash.
func (l *ledger) BeginRequest(id, digest string) (json.RawMessage, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if err := l.healthLocked(); err != nil {
		return nil, err
	}
	if id == "" {
		return nil, errors.New("request id is required")
	}
	if old, ok := l.requests[id]; ok {
		if old.Digest != digest {
			return nil, &codedError{"request_conflict", "request id was already used for different input"}
		}
		if len(old.Response) == 0 {
			return nil, &codedError{"request_interrupted", "request was interrupted; inspect task state before submitting a new request"}
		}
		return append(json.RawMessage(nil), old.Response...), nil
	}
	return nil, l.appendLocked(walRecord{Kind: "request_begin", RequestID: id, Digest: digest})
}

func (l *ledger) FinishRequest(id string, response json.RawMessage) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.appendLocked(walRecord{Kind: "request_response", RequestID: id, Response: response})
}

func cloneMessages(messages []providers.Message) []providers.Message {
	// Messages contain slices and maps. Deep copies keep provider mutation and
	// asynchronous summarization outside the storage transaction.
	if len(messages) == 0 {
		return nil
	}
	b, _ := json.Marshal(messages)
	var result []providers.Message
	_ = json.Unmarshal(b, &result)
	return result
}

func cloneState(s sessionState) sessionState {
	s.Entries = append([]messageEntry(nil), s.Entries...)
	messages := make([]providers.Message, len(s.Entries))
	for i := range s.Entries {
		messages[i] = s.Entries[i].Message
	}
	messages = cloneMessages(messages)
	for i := range s.Entries {
		s.Entries[i].Message = messages[i]
	}
	return s
}

func (l *ledger) snapshot(key string) sessionState {
	l.mu.Lock()
	defer l.mu.Unlock()
	return cloneState(l.sessions[key])
}

func (l *ledger) BeginTurn(key, requestID string) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if err := l.healthLocked(); err != nil {
		return err
	}
	if _, exists := l.pending[key]; exists {
		return errors.New("session already has an active transaction")
	}
	l.pending[key] = &pendingTurn{requestID: requestID, base: cloneState(l.sessions[key])}
	return nil
}

func (l *ledger) DiscardTurn(key string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.pending, key)
}

func (l *ledger) pendingLocked(key string) *pendingTurn {
	p := l.pending[key]
	if p == nil {
		l.failLocked(errors.New("session mutation outside request transaction"))
	}
	return p
}

func (l *ledger) AddMessage(key, role, content string) {
	l.AddFullMessage(key, providers.Message{Role: role, Content: content})
}

func (l *ledger) AddFullMessage(key string, message providers.Message) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.healthLocked() != nil {
		return
	}
	p := l.pendingLocked(key)
	if p == nil {
		return
	}
	p.ordinal++
	p.added = append(p.added, messageEntry{ID: fmt.Sprintf("%s/message/%d", p.requestID, p.ordinal), TurnID: p.requestID, Message: cloneMessages([]providers.Message{message})[0]})
	p.dirty = true
}

func (l *ledger) viewLocked(key string) sessionState {
	if p := l.pending[key]; p != nil {
		s := cloneState(p.base)
		s.Entries = append(s.Entries, p.added...)
		return s
	}
	return cloneState(l.sessions[key])
}

func (l *ledger) GetHistory(key string) []providers.Message {
	l.mu.Lock()
	defer l.mu.Unlock()
	s := l.viewLocked(key)
	h := make([]providers.Message, len(s.Entries))
	for i, entry := range s.Entries {
		h[i] = entry.Message
	}
	return cloneMessages(h)
}

func (l *ledger) assembleView(key string) (sessionState, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if err := l.healthLocked(); err != nil {
		return sessionState{}, err
	}
	if p := l.pending[key]; p != nil && !p.dirty {
		p.base = cloneState(l.sessions[key])
	}
	return l.viewLocked(key), nil
}

func (l *ledger) GetSummary(key string) string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.viewLocked(key).Summary
}

func (l *ledger) SetHistory(key string, history []providers.Message) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.healthLocked() != nil {
		return
	}
	p := l.pendingLocked(key)
	if p == nil {
		return
	}
	// Preserve original turn boundaries when PicoClaw restores its captured
	// history. The pending current message may already have incremented ordinal;
	// guessing boundaries from message parity would split older user/assistant
	// pairs after cancellation.
	var restored []messageEntry
	for _, candidate := range [][]messageEntry{p.base.Entries, l.sessions[key].Entries} {
		for start := 0; start+len(history) <= len(candidate) && len(history) != 0; start++ {
			match := true
			for i, message := range history {
				if candidate[start+i].Message.Role != message.Role || candidate[start+i].Message.Content != message.Content {
					match = false
					break
				}
			}
			if match {
				restored = cloneState(sessionState{Entries: candidate[start : start+len(history)]}).Entries
				break
			}
		}
		if restored != nil {
			break
		}
	}
	p.replace, p.dirty = true, true
	p.added = nil
	if len(history) != 0 && restored == nil {
		// An unfamiliar replacement is one conservative group. It can never
		// be summarized in the middle of the restored conversation.
		for _, msg := range cloneMessages(history) {
			p.ordinal++
			restored = append(restored, messageEntry{ID: fmt.Sprintf("%s/restore/%d", p.requestID, p.ordinal), TurnID: p.requestID + "/restored-history", Message: msg})
		}
	}
	p.base.Entries = restored
}

func (l *ledger) SetSummary(key, summary string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.healthLocked() != nil {
		return
	}
	p := l.pendingLocked(key)
	if p == nil {
		return
	}
	p.base.Summary = summary
	p.replace, p.dirty = true, true
}

func (l *ledger) TruncateHistory(key string, keepLast int) {
	history := l.GetHistory(key)
	if keepLast < 0 {
		keepLast = 0
	}
	if keepLast < len(history) {
		history = history[len(history)-keepLast:]
	}
	l.SetHistory(key, history)
}

func (l *ledger) Save(key string) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if err := l.healthLocked(); err != nil {
		return err
	}
	p := l.pendingLocked(key)
	if p == nil {
		return l.healthLocked()
	}
	if !p.dirty {
		return nil
	}
	state := cloneState(l.sessions[key])
	if p.replace {
		state = cloneState(p.base)
		state.Generation = l.sessions[key].Generation + 1
		state.SummaryRevision = l.sessions[key].SummaryRevision + 1
	}
	state.Entries = append(state.Entries, p.added...)
	if err := l.appendLocked(walRecord{Kind: "session_commit", SessionKey: key, State: &state}); err != nil {
		return err
	}
	p.base, p.added, p.replace, p.dirty = cloneState(state), nil, false, false
	return nil
}

func (l *ledger) ListSessions() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	keys := make([]string, 0, len(l.sessions))
	for key := range l.sessions {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func (l *ledger) ClearSession(key string) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if _, active := l.pending[key]; active {
		return errors.New("cannot clear session during a turn")
	}
	state := l.sessions[key]
	state.Generation++
	state.SummaryRevision++
	state.Entries, state.Summary = nil, ""
	return l.appendLocked(walRecord{Kind: "session_clear", SessionKey: key, State: &state})
}

func eventKey(sessionKey, eventID string) string { return sessionKey + "\x00" + eventID }

func (l *ledger) Note(key, eventID, text, userText string) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if err := l.healthLocked(); err != nil {
		return err
	}
	if eventID == "" {
		return errors.New("note event_id is required")
	}
	data, _ := json.Marshal([]string{text, userText})
	hash := digestBytes(data)
	if old, exists := l.events[eventKey(key, eventID)]; exists {
		if old != hash {
			return &codedError{"event_conflict", "event id was already used for different results"}
		}
		return nil
	}
	if _, active := l.pending[key]; active {
		return errors.New("cannot note during active turn")
	}
	state := cloneState(l.sessions[key])
	turnID := "event/" + eventID
	if userText != "" {
		state.Entries = append(state.Entries, messageEntry{ID: turnID + "/user", TurnID: turnID, Message: providers.Message{Role: "user", Content: userText}})
	}
	content, _ := json.Marshal(map[string]string{"source": "runtime", "event_id": eventID, "text": text})
	state.Entries = append(state.Entries, messageEntry{ID: turnID + "/result", TurnID: turnID, Message: providers.Message{Role: "assistant", Content: string(content)}})
	return l.appendLocked(walRecord{Kind: "session_commit", SessionKey: key, State: &state, EventID: eventID, EventHash: hash})
}

type summarySnapshot struct {
	Key             string
	Generation      uint64
	SummaryRevision uint64
	Summary         string
	Entries         []messageEntry
}

func (l *ledger) summarySnapshot(key string, keepTurns, inputBudget int) (summarySnapshot, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	s := cloneState(l.sessions[key]) // pending user/assistant writes are excluded
	cut := len(s.Entries)
	for n := 0; n < keepTurns && cut > 0; n++ {
		turn := s.Entries[cut-1].TurnID
		for cut > 0 && s.Entries[cut-1].TurnID == turn {
			cut--
		}
	}
	// A restored or interrupted fragment without an assistant terminal record
	// cannot be represented as a completed turn in a summary.
	var boundaries []int
	for start := 0; start < cut; {
		end := start + 1
		for end < cut && s.Entries[end].TurnID == s.Entries[start].TurnID {
			end++
		}
		if s.Entries[end-1].Message.Role != "assistant" {
			cut = start
			break
		}
		boundaries = append(boundaries, end)
		start = end
	}
	if len(boundaries) == 0 {
		return summarySnapshot{}, false
	}
	// Include escaped JSON, record identities, the previous summary, system
	// prompt and output reserve in the input budget. Search complete turn ends;
	// a single oversized turn remains raw and fails explicitly.
	lo, hi, fit := 0, len(boundaries)-1, 0
	for lo <= hi {
		mid := lo + (hi-lo)/2
		tokens := summaryMaxTokens + messageTokens(summaryMessages(s.Summary, s.Entries[:boundaries[mid]]))
		if tokens <= inputBudget {
			fit = boundaries[mid]
			lo = mid + 1
		} else {
			hi = mid - 1
		}
	}
	if fit == 0 {
		return summarySnapshot{}, false
	}
	cut = fit
	return summarySnapshot{Key: key, Generation: s.Generation, SummaryRevision: s.SummaryRevision, Summary: s.Summary, Entries: s.Entries[:cut]}, true
}

func (l *ledger) CommitSummary(snap summarySnapshot, summary string) (bool, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if err := l.healthLocked(); err != nil {
		return false, err
	}
	s := cloneState(l.sessions[snap.Key])
	if len(snap.Entries) == 0 || s.Generation != snap.Generation || s.SummaryRevision != snap.SummaryRevision || len(s.Entries) < len(snap.Entries) {
		return false, nil
	}
	for i, entry := range snap.Entries {
		if s.Entries[i].ID != entry.ID {
			return false, nil
		}
	}
	s.Summary, s.SummaryRevision = summary, s.SummaryRevision+1
	s.Entries = s.Entries[len(snap.Entries):]
	err := l.appendLocked(walRecord{Kind: "summary_commit", SessionKey: snap.Key, State: &s})
	return err == nil, err
}

func (l *ledger) Close() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closed {
		return nil
	}
	l.closed = true
	err := l.file.Close()
	if l.lock != nil {
		unlockFile(l.lock)
		if lockErr := l.lock.Close(); err == nil {
			err = lockErr
		}
	}
	return err
}
