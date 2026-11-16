package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/sipeed/picoclaw/pkg/providers"
)

func testLedger(t *testing.T) (*ledger, string) {
	t.Helper()
	home := t.TempDir()
	l, err := openLedger(home)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = l.Close() })
	return l, home
}

func appendTurn(t *testing.T, l *ledger, key, id string) {
	t.Helper()
	if err := l.BeginTurn(key, id); err != nil {
		t.Fatal(err)
	}
	l.AddMessage(key, "user", id+" user")
	l.AddMessage(key, "assistant", id+" answer")
	if err := l.Save(key); err != nil {
		t.Fatal(err)
	}
	l.DiscardTurn(key)
}

func containsMessage(history []providers.Message, text string) bool {
	for _, message := range history {
		if strings.Contains(message.Content, text) {
			return true
		}
	}
	return false
}

func TestLedgerAtomicSaveAndRecovery(t *testing.T) {
	l, home := testLedger(t)
	appendTurn(t, l, "voice/alice", "first")
	if err := l.BeginTurn("voice/alice", "partial"); err != nil {
		t.Fatal(err)
	}
	l.AddMessage("voice/alice", "user", "UNCOMMITTED")
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := openLedger(home)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if h := reopened.GetHistory("voice/alice"); len(h) != 2 || containsMessage(h, "UNCOMMITTED") {
		t.Fatalf("recovered incomplete transaction: %+v", h)
	}
	if err := reopened.BeginTurn("voice/alice", "restore"); err != nil {
		t.Fatal(err)
	}
	reopened.SetHistory("voice/alice", []providers.Message{{Role: "user", Content: "restored"}, {Role: "assistant", Content: "reply"}})
	reopened.SetSummary("voice/alice", "atomic summary")
	// A separate recovery reader cannot run against the same journal. Its lock
	// is kernel-owned and released even if the writer process is killed.
	if other, err := openLedger(home); err == nil {
		other.Close()
		t.Fatal("second writer was permitted")
	}
	if s := reopened.snapshot("voice/alice"); len(s.Entries) != 2 || s.Summary != "" {
		t.Fatal("pending replacement leaked before Save")
	}
	if err := reopened.Save("voice/alice"); err != nil {
		t.Fatal(err)
	}
	reopened.DiscardTurn("voice/alice")
	s := reopened.snapshot("voice/alice")
	if s.Generation != 1 || s.Summary != "atomic summary" || s.Entries[0].Message.Content != "restored" {
		t.Fatalf("replacement was not atomic: %+v", s)
	}
}

func TestLedgerRepairsOnlyIncompleteTail(t *testing.T) {
	l, home := testLedger(t)
	appendTurn(t, l, "session", "first")
	l.Close()
	path := filepath.Join(home, "dialogue.wal")
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		t.Fatal(err)
	}
	f.WriteString(`{"sequence":2,"record_id":"partial`)
	f.Close()
	reopened, err := openLedger(home)
	if err != nil {
		t.Fatal(err)
	}
	appendTurn(t, reopened, "session", "second")
	reopened.Close()
	final, err := openLedger(home)
	if err != nil {
		t.Fatal(err)
	}
	if len(final.GetHistory("session")) != 4 {
		t.Fatal("repaired journal could not accept a new complete frame")
	}
	final.Close()
	data, _ := os.ReadFile(path)
	data = bytes.Replace(data, []byte("first user"), []byte("FIRST user"), 1)
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	if bad, err := openLedger(home); err == nil {
		bad.Close()
		t.Fatal("complete corrupt frame was accepted")
	}
}

type failingFile struct {
	durableFile
	short   bool
	syncErr bool
}

func (f failingFile) Write(b []byte) (int, error) {
	if f.short {
		return f.durableFile.Write(b[:len(b)/2])
	}
	return f.durableFile.Write(b)
}
func (f failingFile) Sync() error {
	if f.syncErr {
		return errors.New("injected sync failure")
	}
	return f.durableFile.Sync()
}

func TestLedgerWriteAndSyncFailuresLatch(t *testing.T) {
	for _, failure := range []string{"short", "sync"} {
		t.Run(failure, func(t *testing.T) {
			l, _ := testLedger(t)
			if err := l.BeginTurn("session", "one"); err != nil {
				t.Fatal(err)
			}
			l.AddMessage("session", "assistant", "must not release action")
			l.file = failingFile{durableFile: l.file, short: failure == "short", syncErr: failure == "sync"}
			err := l.Save("session")
			if err == nil || l.Health() == nil {
				t.Fatal("storage failure was not latched")
			}
			if failure == "short" && !errors.Is(err, io.ErrShortWrite) {
				t.Fatal(err)
			}
			if len(l.snapshot("session").Entries) != 0 {
				t.Fatal("failed transaction became visible")
			}
			if err2 := l.Save("session"); err2 != err {
				t.Fatal("unsafe writer was reused after uncertain write")
			}
		})
	}
}

func TestRequestAndRuntimeEventIdempotency(t *testing.T) {
	l, home := testLedger(t)
	if cached, err := l.BeginRequest("request-one", "hash-one"); err != nil || cached != nil {
		t.Fatal(cached, err)
	}
	response := json.RawMessage(`{"id":"request-one","kind":"act","steps":[]}`)
	if err := l.FinishRequest("request-one", response); err != nil {
		t.Fatal(err)
	}
	if _, err := l.BeginRequest("request-one", "other"); err == nil {
		t.Fatal("request conflict accepted")
	}
	if _, err := l.BeginRequest("interrupted", "hash"); err != nil {
		t.Fatal(err)
	}
	if err := l.Note("session", "event-one", "actual feedback", "/light on"); err != nil {
		t.Fatal(err)
	}
	if err := l.Note("session", "event-one", "actual feedback", "/light on"); err != nil {
		t.Fatal(err)
	}
	if err := l.Note("session", "event-one", "changed feedback", "/light on"); err == nil {
		t.Fatal("event conflict accepted")
	}
	if len(l.GetHistory("session")) != 2 {
		t.Fatal("duplicate event appended twice")
	}
	l.Close()
	r, err := openLedger(home)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	cached, err := r.BeginRequest("request-one", "hash-one")
	if err != nil || !bytes.Equal(cached, response) {
		t.Fatal("request response did not survive restart", string(cached), err)
	}
	if _, err := r.BeginRequest("interrupted", "hash"); err == nil || !strings.Contains(err.Error(), "interrupted") {
		t.Fatal("interrupted request was rerun", err)
	}
	if err := r.ClearSession("session"); err != nil {
		t.Fatal(err)
	}
	if err := r.Note("session", "event-one", "actual feedback", "/light on"); err != nil {
		t.Fatal(err)
	}
	if len(r.GetHistory("session")) != 0 {
		t.Fatal("clear allowed a historical event to be replayed")
	}
}

func TestSummaryCompareAndSwapPreservesNewTail(t *testing.T) {
	l, _ := testLedger(t)
	for _, id := range []string{"a", "b", "c"} {
		appendTurn(t, l, "session", id)
	}
	snap, ok := l.summarySnapshot("session", 2, 32768)
	if !ok || len(snap.Entries) != 2 {
		t.Fatal("summary boundary is not a complete turn")
	}
	if err := l.BeginTurn("session", "in-flight"); err != nil {
		t.Fatal(err)
	}
	l.AddMessage("session", "user", "pending original goal")
	if current, ok := l.summarySnapshot("session", 2, 32768); !ok || len(current.Entries) != 2 {
		t.Fatal("pending turn was included in summary")
	}
	if committed, err := l.CommitSummary(snap, "summary a"); err != nil || !committed {
		t.Fatal(committed, err)
	}
	l.AddMessage("session", "assistant", "pending answer")
	if err := l.Save("session"); err != nil {
		t.Fatal(err)
	}
	l.DiscardTurn("session")
	if err := l.Note("session", "new-result", "new note", ""); err != nil {
		t.Fatal(err)
	}
	history := l.GetHistory("session")
	for _, text := range []string{"b user", "c user", "pending original goal", "new note"} {
		if !containsMessage(history, text) {
			t.Fatalf("summary lost %q: %+v", text, history)
		}
	}
	if containsMessage(history, "a user") || l.GetSummary("session") != "summary a" {
		t.Fatal("summary did not cover exactly its prefix")
	}
	if committed, err := l.CommitSummary(snap, "stale"); err != nil || committed {
		t.Fatal("stale summary revision was accepted")
	}
}

func TestClearAndRestoreInvalidateSummary(t *testing.T) {
	for _, action := range []string{"clear", "restore"} {
		t.Run(action, func(t *testing.T) {
			l, _ := testLedger(t)
			for _, id := range []string{"a", "b", "c"} {
				appendTurn(t, l, "session", id)
			}
			snap, _ := l.summarySnapshot("session", 2, 32768)
			if action == "clear" {
				if err := l.ClearSession("session"); err != nil {
					t.Fatal(err)
				}
			} else {
				if err := l.BeginTurn("session", "restore"); err != nil {
					t.Fatal(err)
				}
				l.SetHistory("session", l.GetHistory("session"))
				l.SetSummary("session", "restored summary")
				if err := l.Save("session"); err != nil {
					t.Fatal(err)
				}
				l.DiscardTurn("session")
			}
			if committed, err := l.CommitSummary(snap, "old summary"); err != nil || committed {
				t.Fatal("summary survived generation change", committed, err)
			}
		})
	}
}

func TestRestoreAfterPendingUserPreservesCompletedTurnBoundaries(t *testing.T) {
	l, _ := testLedger(t)
	for _, id := range []string{"a", "b", "c"} {
		appendTurn(t, l, "session", id)
	}
	before := l.GetHistory("session")
	if err := l.BeginTurn("session", "cancelled-turn"); err != nil {
		t.Fatal(err)
	}
	l.AddMessage("session", "user", "cancel this partial user message")
	l.SetHistory("session", before)
	l.SetSummary("session", "")
	if err := l.Save("session"); err != nil {
		t.Fatal(err)
	}
	l.DiscardTurn("session")
	snap, ok := l.summarySnapshot("session", 2, 32768)
	if !ok || len(snap.Entries) != 2 || snap.Entries[0].Message.Content != "a user" || snap.Entries[1].Message.Content != "a answer" {
		t.Fatalf("restore split a completed round: %+v", snap)
	}
}

func TestSummaryDoesNotCoverIncompleteCommittedFragment(t *testing.T) {
	l, _ := testLedger(t)
	if err := l.BeginTurn("session", "fragment"); err != nil {
		t.Fatal(err)
	}
	l.AddMessage("session", "user", "incomplete")
	if err := l.Save("session"); err != nil {
		t.Fatal(err)
	}
	l.DiscardTurn("session")
	for _, id := range []string{"b", "c", "d"} {
		appendTurn(t, l, "session", id)
	}
	if _, ok := l.summarySnapshot("session", 2, 32768); ok {
		t.Fatal("incomplete user message was treated as a completed summary round")
	}
}

func TestSummarySnapshotCapsInputAtCompleteTurns(t *testing.T) {
	l, _ := testLedger(t)
	for _, id := range []string{"a", "b", "c", "d", "e"} {
		appendTurn(t, l, "session", id)
	}
	state := l.snapshot("session")
	limit := summaryMaxTokens + messageTokens(summaryMessages("", state.Entries[:4]))
	snapshot, ok := l.summarySnapshot("session", 2, limit)
	if !ok || len(snapshot.Entries) != 4 || snapshot.Entries[3].TurnID != "b" {
		t.Fatalf("budget split or exceeded complete turns: %+v", snapshot)
	}
	if summaryMaxTokens+messageTokens(summaryMessages(snapshot.Summary, snapshot.Entries)) > limit {
		t.Fatal("summary input exceeded its budget")
	}
	tooSmall := summaryMaxTokens + messageTokens(summaryMessages("", state.Entries[:2])) - 1
	if _, ok := l.summarySnapshot("session", 2, tooSmall); ok {
		t.Fatal("oversized first complete turn was split to fit")
	}
	if len(l.GetHistory("session")) != 10 {
		t.Fatal("sizing summary changed stored history")
	}
}
