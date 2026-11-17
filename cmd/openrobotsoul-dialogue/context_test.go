package main

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/sipeed/picoclaw/pkg/agent"
	"github.com/sipeed/picoclaw/pkg/providers"
)

type recordingProvider struct {
	mu           sync.Mutex
	calls        [][]providers.Message
	toolCounts   []int
	response     string
	tools        bool
	started      chan struct{}
	release      chan struct{}
	startOnce    sync.Once
	summaryFail  bool
	summaryEmpty bool
	block        bool
}

func (*recordingProvider) GetDefaultModel() string { return "test-model" }
func (p *recordingProvider) Chat(ctx context.Context, messages []providers.Message, tools []providers.ToolDefinition, _ string, _ map[string]any) (*providers.LLMResponse, error) {
	p.mu.Lock()
	p.calls = append(p.calls, cloneMessages(messages))
	p.toolCounts = append(p.toolCounts, len(tools))
	p.mu.Unlock()
	purpose, _ := ctx.Value(purposeKey{}).(callPurpose)
	if purpose == purposeSummary || p.block {
		if p.started != nil {
			p.startOnce.Do(func() { close(p.started) })
		}
		if p.release != nil {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-p.release:
			}
		}
		if p.summaryFail {
			return nil, errors.New("injected summary error")
		}
		if purpose == purposeSummary {
			if p.summaryEmpty {
				return &providers.LLMResponse{ReasoningContent: "hidden reasoning", FinishReason: "length"}, nil
			}
			return &providers.LLMResponse{Content: "summary of exactly a", FinishReason: "stop"}, nil
		}
	}
	if p.tools {
		return &providers.LLMResponse{ToolCalls: []providers.ToolCall{{ID: "unexpected", Name: "exec"}}, FinishReason: "tool_calls"}, nil
	}
	if p.response != "" {
		return &providers.LLMResponse{Content: p.response, FinishReason: "stop"}, nil
	}
	return &providers.LLMResponse{Content: `{"kind":"complete","reply":"recorded"}`, FinishReason: "stop"}, nil
}
func (p *recordingProvider) snapshotCalls() [][]providers.Message {
	p.mu.Lock()
	defer p.mu.Unlock()
	result := make([][]providers.Message, len(p.calls))
	for i := range p.calls {
		result[i] = cloneMessages(p.calls[i])
	}
	return result
}

func TestAsyncSummaryDoesNotLoseConcurrentMessages(t *testing.T) {
	l, _ := testLedger(t)
	for _, id := range []string{"a", "b", "c"} {
		appendTurn(t, l, "session", id)
	}
	p := &recordingProvider{started: make(chan struct{}), release: make(chan struct{})}
	c := newReliableContext(l, &guardedProvider{inner: p, store: l, budget: 32768}, "test-model", 32768)
	c.threshold = 6
	defer c.Close()
	if err := c.Compact(context.Background(), &agent.CompactRequest{SessionKey: "session", Reason: agent.ContextCompressReasonSummarize}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-p.started:
	case <-time.After(5 * time.Second):
		t.Fatal("summary did not start")
	}
	if err := l.Note("session", "runtime-note", "new feedback during summary", ""); err != nil {
		t.Fatal(err)
	}
	appendTurn(t, l, "session", "d")
	close(p.release)
	c.mu.Lock()
	job := c.jobs["session"]
	c.mu.Unlock()
	if job != nil {
		select {
		case <-job.done:
		case <-time.After(5 * time.Second):
			t.Fatal("summary did not complete")
		}
	}
	history := l.GetHistory("session")
	for _, text := range []string{"b user", "c user", "new feedback during summary", "d user"} {
		if !containsMessage(history, text) {
			t.Fatalf("lost %q: %+v", text, history)
		}
	}
	if l.GetSummary("session") != "summary of exactly a" {
		t.Fatal("summary did not commit")
	}
	if calls := p.snapshotCalls(); len(calls) != 1 || containsMessage(calls[0], "b user") || containsMessage(calls[0], "new feedback") {
		t.Fatal("summary snapshot exceeded its committed turn boundary")
	}
}

func TestSummaryFailureRetainsRawAndCloseCancelsWorker(t *testing.T) {
	l, _ := testLedger(t)
	for _, id := range []string{"a", "b", "c"} {
		appendTurn(t, l, "session", id)
	}
	p := &recordingProvider{summaryFail: true}
	c := newReliableContext(l, &guardedProvider{inner: p, store: l, budget: 32768}, "test-model", 32768)
	if err := c.compactNow(context.Background(), "session"); err != nil {
		t.Fatal("summary failure should use the local fallback:", err)
	}
	if len(l.GetHistory("session")) >= 6 || l.GetSummary("session") == "" || l.Health() != nil {
		t.Fatal("summary fallback did not commit a bounded context")
	}
	c.Close()
	l, _ = testLedger(t)
	for _, id := range []string{"a", "b", "c"} {
		appendTurn(t, l, "session", id)
	}
	p = &recordingProvider{started: make(chan struct{}), release: make(chan struct{})}
	c = newReliableContext(l, &guardedProvider{inner: p, store: l, budget: 32768}, "test-model", 32768)
	c.startSummary("session")
	select {
	case <-p.started:
	case <-time.After(5 * time.Second):
		t.Fatal("summary did not start")
	}
	done := make(chan struct{})
	go func() { c.Close(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("host did not cancel and wait for summary worker")
	}
	if len(l.GetHistory("session")) != 6 {
		t.Fatal("cancelled summary changed history")
	}
}

func TestEmptySummaryResponseUsesFallback(t *testing.T) {
	l, _ := testLedger(t)
	for _, id := range []string{"a", "b", "c"} {
		appendTurn(t, l, "session", id)
	}
	p := &recordingProvider{summaryEmpty: true}
	c := newReliableContext(l, &guardedProvider{inner: p, store: l, budget: 32768}, "test-model", 32768)
	defer c.Close()
	if err := c.compactNow(context.Background(), "session"); err != nil {
		t.Fatal("empty summary response should not fail compaction:", err)
	}
	if strings.TrimSpace(l.GetSummary("session")) == "" || len(l.GetHistory("session")) >= 6 {
		t.Fatal("empty summary response did not commit the bounded fallback")
	}
}

func TestGuardRejectsSilentTrimAndBudgetOverflow(t *testing.T) {
	l, _ := testLedger(t)
	p := &recordingProvider{}
	guard := &guardedProvider{inner: p, store: l, budget: 128}
	gate := &turnGate{}
	gate.setView([]providers.Message{{Role: "user", Content: "required old goal"}}, "")
	ctx := context.WithValue(context.Background(), gateKey{}, gate)
	if _, err := guard.Chat(ctx, []providers.Message{{Role: "system", Content: "policy"}, {Role: "user", Content: "new goal"}}, nil, "test", nil); err == nil {
		t.Fatal("trimmed history accepted")
	}
	gate = &turnGate{}
	gate.setView(nil, "")
	ctx = context.WithValue(context.Background(), gateKey{}, gate)
	_, err := guard.Chat(ctx, []providers.Message{{Role: "system", Content: "policy"}, {Role: "user", Content: strings.Repeat("large input ", 1000)}}, nil, "test", map[string]any{"max_tokens": 20})
	if err == nil || len(p.snapshotCalls()) != 0 {
		t.Fatal("over-budget request reached provider")
	}
}
