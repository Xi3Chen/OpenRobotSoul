package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/sipeed/picoclaw/pkg/agent"
	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/session"
)

func testGateway(t *testing.T, provider providers.LLMProvider) *gateway {
	t.Helper()
	t.Setenv("PICOCLAW_HOME", t.TempDir())
	t.Setenv("PICOCLAW_BUILTIN_SKILLS", t.TempDir())
	g, err := newGateway(t.TempDir(), provider, "test-model", 5*time.Second, 32768, false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(g.Close)
	return g
}

func callGateway(t *testing.T, g *gateway, payload map[string]any) map[string]any {
	t.Helper()
	data, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	response, _ := g.Handle(context.Background(), data)
	var result map[string]any
	if err := json.Unmarshal(response, &result); err != nil {
		t.Fatalf("invalid response %q: %v", response, err)
	}
	if result["id"] != payload["id"] || result["version"] != float64(1) {
		t.Fatalf("protocol correlation missing: %+v", result)
	}
	return result
}

func TestGatewaySelectsContextAndKeepsIdentityIsolation(t *testing.T) {
	p := &recordingProvider{}
	g := testGateway(t, p)
	if agent.NewPipeline(g.loop).ContextManager != g.context {
		t.Fatal("manager silently fell back")
	}
	if g.loop.GetRegistry().GetDefaultAgent().Tools.Count() != 0 {
		t.Fatal("nonempty runtime tools")
	}
	keyA, keyB := session.BuildOpaqueSessionKey("voice/alice/conversation"), session.BuildOpaqueSessionKey("dashboard/alice/conversation")
	hello := callGateway(t, g, map[string]any{"id": "hello", "type": "hello", "version": 1})
	if hello["ready"] != true || hello["tools"] != float64(0) {
		t.Fatal(hello)
	}
	first := map[string]any{"id": "first", "type": "decide", "session_key": keyA, "text": "VOICE-SECRET", "sensors": map[string]string{"marker": "sensor-one"}}
	response := callGateway(t, g, first)
	if response["kind"] != "complete" {
		t.Fatal(response)
	}
	if len(g.store.GetHistory(keyA)) != 2 {
		t.Fatal("Ingest double-wrote a message")
	}
	duplicate := callGateway(t, g, first)
	if duplicate["reply"] != response["reply"] || len(p.snapshotCalls()) != 1 {
		t.Fatal("duplicate request reached model")
	}
	conflict := callGateway(t, g, map[string]any{"id": "first", "type": "decide", "session_key": keyA, "text": "different"})
	if conflict["error_code"] != "request_conflict" {
		t.Fatal(conflict)
	}
	callGateway(t, g, map[string]any{"id": "second", "type": "decide", "session_key": keyA, "text": "follow up", "sensors": map[string]string{"marker": "sensor-two"}})
	calls := p.snapshotCalls()
	if !containsMessage(calls[1], "VOICE-SECRET") || !containsMessage(calls[1], "sensor-two") || containsMessage(calls[1], "sensor-one") {
		t.Fatal("history or fresh contributor metadata missing")
	}
	callGateway(t, g, map[string]any{"id": "separate", "type": "decide", "session_key": keyB, "text": "dashboard message"})
	calls = p.snapshotCalls()
	if containsMessage(calls[2], "VOICE-SECRET") || len(g.store.GetHistory(keyB)) != 2 {
		t.Fatal("entry point histories were merged")
	}
	bad := callGateway(t, g, map[string]any{"id": "bare", "type": "decide", "session_key": "voice", "text": "bare key"})
	if bad["error_code"] != "invalid_session" || len(p.snapshotCalls()) != 3 {
		t.Fatal("bare session reached agent route")
	}
	for _, count := range p.toolCounts {
		if count != 0 {
			t.Fatal("provider received tool definitions")
		}
	}
}

func TestGatewayRuntimeNotesReviewAndClear(t *testing.T) {
	p := &recordingProvider{}
	g := testGateway(t, p)
	key := session.BuildOpaqueSessionKey("voice/operator/c1")
	callGateway(t, g, map[string]any{"id": "d1", "type": "decide", "session_key": key, "text": "original goal"})
	for _, id := range []string{"n1", "n2"} {
		r := callGateway(t, g, map[string]any{"id": id, "type": "note", "session_key": key, "event_id": "feedback-001", "text": "actual command result", "user_text": "/light on"})
		if r["recorded"] != true {
			t.Fatal(r)
		}
	}
	if len(g.store.GetHistory(key)) != 4 {
		t.Fatal("runtime event was duplicated")
	}
	r := callGateway(t, g, map[string]any{"id": "runtime", "type": "resume", "session_key": key, "text": "action finished"})
	if r["kind"] != "complete" {
		t.Fatal(r)
	}
	calls := p.snapshotCalls()
	if !containsMessage(calls[1], "actual command result") {
		t.Fatal("feedback absent from next context")
	}
	last := calls[1][len(calls[1])-1].Content
	if !strings.Contains(last, `"source":"runtime"`) {
		t.Fatalf("resume became human text: %s", last)
	}
	before := len(g.store.GetHistory(key))
	p.response = `{"verdict":"allow","rationale":"explicit bounded command"}`
	r = callGateway(t, g, map[string]any{"id": "review", "type": "review", "skill": "light", "parameters": map[string]any{"on": true}, "user_input": "turn on"})
	if r["verdict"] != "allow" || len(g.store.GetHistory(key)) != before {
		t.Fatal("review modified dialogue history", r)
	}
	calls = p.snapshotCalls()
	if len(calls[len(calls)-1]) != 2 || containsMessage(calls[len(calls)-1], "original goal") {
		t.Fatal("review read dialogue history")
	}
	p.response = ""
	r = callGateway(t, g, map[string]any{"id": "clear", "type": "clear", "session_key": key})
	if r["cleared"] != true || len(g.store.GetHistory(key)) != 0 {
		t.Fatal(r)
	}
}

func TestUnexpectedToolCallsNeverBecomeExecutableHistory(t *testing.T) {
	for _, content := range []string{"native", "json", "bad-json"} {
		t.Run(content, func(t *testing.T) {
			p := &recordingProvider{tools: content == "native"}
			if content == "json" {
				p.response = `{"kind":"act","steps":[{"skill":"light","parameters":{}}],"tool_calls":[{"name":"exec"}]}`
			}
			if content == "bad-json" {
				p.response = "unstructured model reply"
			}
			g := testGateway(t, p)
			key := session.BuildOpaqueSessionKey("guard/session")
			r := callGateway(t, g, map[string]any{"id": "bad", "type": "decide", "session_key": key, "text": "tool attempt"})
			if r["rejected"] != true || r["kind"] != nil || len(p.snapshotCalls()) != 1 {
				t.Fatal("guard did not stop provider path", r)
			}
			if len(g.store.GetHistory(key)) != 0 {
				t.Fatal("failed decision left partial history")
			}
			if g.store.Health() != nil {
				t.Fatal("model error incorrectly poisoned storage")
			}
		})
	}
}

type failAtSyncFile struct {
	durableFile
	count  int
	failAt int
}

func (f *failAtSyncFile) Sync() error {
	f.count++
	if f.count == f.failAt {
		return errors.New("injected persistence failure")
	}
	return f.durableFile.Sync()
}

func TestPersistenceFailureCannotReleaseAction(t *testing.T) {
	for _, failAt := range []int{1, 2, 3} {
		t.Run(fmt.Sprint(failAt), func(t *testing.T) {
			p := &recordingProvider{response: `{"kind":"act","steps":[{"skill":"light","parameters":{"on":true}}]}`}
			g := testGateway(t, p)
			g.store.file = &failAtSyncFile{durableFile: g.store.file, failAt: failAt}
			r := callGateway(t, g, map[string]any{"id": "action", "type": "decide", "session_key": session.BuildOpaqueSessionKey("failure/session"), "text": "turn on"})
			if r["rejected"] != true || r["kind"] != nil || r["steps"] != nil {
				t.Fatalf("persistence failure released an action at sync %d: %+v", failAt, r)
			}
			if failAt == 1 && len(p.snapshotCalls()) != 0 {
				t.Fatal("model called after request persistence failure")
			}
		})
	}
}

func TestCancellationDuringModelCallDiscardsPartialTurn(t *testing.T) {
	p := &recordingProvider{started: make(chan struct{}), release: make(chan struct{}), block: true}
	g := testGateway(t, p)
	key := session.BuildOpaqueSessionKey("cancel/session")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan json.RawMessage)
	go func() {
		response, _ := g.Handle(ctx, []byte(fmt.Sprintf(`{"id":"cancel-me","type":"decide","session_key":%q,"text":"goal"}`, key)))
		done <- response
	}()
	select {
	case <-p.started:
	case <-time.After(5 * time.Second):
		t.Fatal("provider did not start")
	}
	cancel()
	select {
	case response := <-done:
		var r map[string]any
		json.Unmarshal(response, &r)
		if r["rejected"] != true {
			t.Fatal(string(response))
		}
	case <-time.After(5 * time.Second):
		t.Fatal("model cancellation did not stop turn")
	}
	if len(g.store.GetHistory(key)) != 0 {
		t.Fatal("cancelled turn persisted a partial user message")
	}
}

func TestGateway256RuntimeNotesCompactInBatchesAndKeepNewestTail(t *testing.T) {
	p := &recordingProvider{started: make(chan struct{}), release: make(chan struct{})}
	g := testGateway(t, p)
	g.timeout = 20 * time.Second
	key := session.BuildOpaqueSessionKey("timeline/256-notes/operator")
	for step := 0; step < 256; step++ {
		feedback := make([]map[string]any, 0, 3)
		for _, status := range []string{"accepted", "running", "succeeded"} {
			feedback = append(feedback, map[string]any{
				"feedback_id": fmt.Sprintf("task-timeline:step-%03d:%s", step, status),
				"command_id":  fmt.Sprintf("timeline-command-%03d", step), "task_id": "timeline-task", "trace_id": "timeline-trace",
				"status": status, "timestamp": "2026-11-15T12:00:00+00:00", "message": "Registered flashlight adapter returned its local device read-back",
				"evidence": map[string]any{"condition_met": true, "outcome_known": true, "enabled": step%2 == 0},
			})
		}
		text, _ := json.Marshal(map[string]any{"skill": "flashlight", "status": "completed", "feedback": feedback, "marker": fmt.Sprintf("new-tail-%03d", step)})
		r := callGateway(t, g, map[string]any{"id": fmt.Sprintf("note-request-%03d", step), "type": "note", "session_key": key,
			"event_id": fmt.Sprintf("timeline-step-%03d", step), "text": string(text)})
		if r["recorded"] != true {
			t.Fatalf("note %d was not durably acknowledged: %+v", step, r)
		}
	}
	select {
	case <-p.started:
	case <-time.After(5 * time.Second):
		t.Fatal("note threshold did not start background summary")
	}
	if tokens := stateTokens(g.store.snapshot(key)); tokens <= g.provider.budget {
		t.Fatalf("fixture did not overflow a model input budget: %d", tokens)
	}
	close(p.release)
	r := callGateway(t, g, map[string]any{"id": "after-256-notes", "type": "decide", "session_key": key, "text": "summarize the actual timeline result"})
	if r["kind"] != "complete" {
		t.Fatalf("backlog prevented the next ordinary decision: %+v", r)
	}
	summaries, decisions := 0, 0
	for _, messages := range p.snapshotCalls() {
		if strings.HasPrefix(messages[0].Content, "Summarize the supplied completed dialogue turns") {
			summaries++
			if summaryMaxTokens+messageTokens(messages) > g.provider.budget {
				t.Fatal("summary batch exceeded provider context window")
			}
		} else {
			decisions++
			if !containsMessage(messages, "new-tail-254") || !containsMessage(messages, "new-tail-255") {
				t.Fatal("new runtime tail was lost before the next decision")
			}
			if messageTokens(messages)+8192 > g.provider.budget {
				t.Fatal("decision context exceeded its full input/output budget")
			}
		}
	}
	if summaries < 2 || summaries > maxSummaryBatches || decisions != 1 {
		t.Fatalf("unexpected model call shape summaries=%d decisions=%d", summaries, decisions)
	}
	if !containsMessage(g.store.GetHistory(key), "new-tail-255") {
		t.Fatal("latest complete runtime turn was deleted")
	}
}

func TestOversizedFreshPromptDoesNotRepeatedlySummarizeHistory(t *testing.T) {
	p := &recordingProvider{}
	g := testGateway(t, p)
	key := session.BuildOpaqueSessionKey("oversized/current-prompt")
	for _, id := range []string{"old-a", "old-b", "old-c"} {
		appendTurn(t, g.store, key, id)
	}
	r := callGateway(t, g, map[string]any{"id": "oversized", "type": "decide", "session_key": key, "text": "ordinary goal",
		"task_context": map[string]any{"input": strings.Repeat("fresh mandatory metadata ", 20000)}})
	if r["error_code"] != "context_budget" || r["rejected"] != true {
		t.Fatal(r)
	}
	if len(p.snapshotCalls()) != 0 || len(g.store.GetHistory(key)) != 6 {
		t.Fatal("oversized fresh prompt triggered useless summary calls or dropped history")
	}
}

type residentTestProcess struct {
	command *exec.Cmd
	input   io.WriteCloser
	scanner *bufio.Scanner
	stderr  *bytes.Buffer
	cancel  context.CancelFunc
}

func startResident(t *testing.T, binary, home string) *residentTestProcess {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	command := exec.CommandContext(ctx, binary, "--fake-provider", "--home", home)
	stdin, err := command.StdinPipe()
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	stdout, err := command.StdoutPipe()
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	stderr := &bytes.Buffer{}
	command.Stderr = stderr
	if err = command.Start(); err != nil {
		cancel()
		t.Fatal(err)
	}
	p := &residentTestProcess{command: command, input: stdin, scanner: bufio.NewScanner(stdout), stderr: stderr, cancel: cancel}
	t.Cleanup(func() { cancel(); _ = command.Wait() })
	return p
}

func (p *residentTestProcess) request(t *testing.T, payload map[string]any) map[string]any {
	t.Helper()
	b, _ := json.Marshal(payload)
	if _, err := p.input.Write(append(b, '\n')); err != nil {
		t.Fatal(err)
	}
	if !p.scanner.Scan() {
		t.Fatal("resident ended before response", p.scanner.Err())
	}
	var r map[string]any
	if err := json.Unmarshal(p.scanner.Bytes(), &r); err != nil {
		t.Fatalf("stdout was not JSONL: %q", p.scanner.Text())
	}
	if r["id"] != payload["id"] || r["version"] != float64(1) {
		t.Fatalf("response correlation mismatch: %+v", r)
	}
	return r
}

func TestResidentSubprocessRestartAndInterruptedRequest(t *testing.T) {
	if testing.Short() {
		t.Skip("native subprocess build")
	}
	binary := filepath.Join(t.TempDir(), "dialogue")
	if runtime.GOOS == "windows" {
		binary += ".exe"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	if output, err := exec.CommandContext(ctx, "go", "build", "-mod=readonly", "-o", binary, ".").CombinedOutput(); err != nil {
		t.Fatalf("build resident: %v\n%s", err, output)
	}
	home := t.TempDir()
	key := session.BuildOpaqueSessionKey("voice/robot/alice/c1")
	p := startResident(t, binary, home)
	if r := p.request(t, map[string]any{"id": "hello-1", "type": "hello", "version": 1}); r["ready"] != true || r["fake_provider"] != true {
		t.Fatal(r)
	}
	first := map[string]any{"id": "first", "type": "decide", "session_key": key, "text": "original goal"}
	if r := p.request(t, first); r["history_messages"] != float64(0) {
		t.Fatal(r)
	}
	if r := p.request(t, map[string]any{"id": "note-1", "type": "note", "session_key": key, "event_id": "feedback-id", "text": "real result"}); r["recorded"] != true {
		t.Fatal(r)
	}
	if r := p.request(t, map[string]any{"id": "shutdown-1", "type": "shutdown"}); r["closed"] != true {
		t.Fatal(r)
	}
	if err := p.command.Wait(); err != nil {
		t.Fatal(err, p.stderr.String())
	}
	p.cancel()
	p = startResident(t, binary, home)
	if r := p.request(t, first); r["history_messages"] != float64(0) {
		t.Fatal("duplicate changed response after restart", r)
	}
	if r := p.request(t, map[string]any{"id": "second", "type": "resume", "session_key": key, "text": "new runtime fact"}); r["history_messages"] != float64(3) {
		t.Fatal("history did not survive restart", r)
	}
	if r := p.request(t, map[string]any{"id": "other", "type": "decide", "session_key": session.BuildOpaqueSessionKey("dashboard/robot/bob/c1"), "text": "isolated"}); r["history_messages"] != float64(0) {
		t.Fatal("other user read history", r)
	}
	// Kill an actual process after request_begin is durable, before any model
	// decision exists. Recovery must refuse to regenerate that request ID.
	interrupted := map[string]any{"id": "interrupt", "type": "decide", "session_key": key, "text": "__wait_for_cancel__"}
	b, _ := json.Marshal(interrupted)
	if _, err := p.input.Write(append(b, '\n')); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		data, _ := os.ReadFile(filepath.Join(home, "dialogue.wal"))
		if bytes.Contains(data, []byte(`"request_id":"interrupt"`)) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("request start was not persisted")
		}
		time.Sleep(5 * time.Millisecond)
	}
	if err := p.command.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	_ = p.command.Wait()
	p.cancel()
	p = startResident(t, binary, home)
	if r := p.request(t, interrupted); r["error_code"] != "request_interrupted" {
		t.Fatal("interrupted decision was regenerated", r)
	}
	if r := p.request(t, map[string]any{"id": "after-kill", "type": "decide", "session_key": key, "text": "check recovered state"}); r["history_messages"] != float64(5) {
		t.Fatal("incomplete turn leaked into recovery", r)
	}
	p.request(t, map[string]any{"id": "shutdown-final", "type": "shutdown"})
	if err := p.command.Wait(); err != nil {
		t.Fatal(err, p.stderr.String())
	}
}
