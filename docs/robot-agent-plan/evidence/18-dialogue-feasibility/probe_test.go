package dialogueprobe

import (
	"context"
	"fmt"
	"github.com/sipeed/picoclaw/pkg/agent"
	"github.com/sipeed/picoclaw/pkg/bus"
	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/session"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeProvider struct {
	mu               sync.Mutex
	requests         [][]providers.Message
	toolCounts       []int
	tool             bool
	summarizeStarted chan struct{}
	summarizeRelease chan struct{}
}

func (p *fakeProvider) GetDefaultModel() string { return "test-model" }
func (p *fakeProvider) Chat(ctx context.Context, m []providers.Message, tools []providers.ToolDefinition, model string, options map[string]any) (*providers.LLMResponse, error) {
	p.mu.Lock()
	p.requests = append(p.requests, append([]providers.Message(nil), m...))
	p.toolCounts = append(p.toolCounts, len(tools))
	p.mu.Unlock()
	if len(m) == 1 && strings.Contains(m[0].Content, "Provide a concise summary") && p.summarizeStarted != nil {
		close(p.summarizeStarted)
		select {
		case <-p.summarizeRelease:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return &providers.LLMResponse{Content: "SUMMARY_OF_A_ONLY", FinishReason: "stop"}, nil
	}
	if p.tool {
		return &providers.LLMResponse{ToolCalls: []providers.ToolCall{{ID: "bad-call", Name: "exec", Arguments: map[string]any{"command": "echo SHOULD_NOT_EXECUTE"}}}, FinishReason: "tool_calls"}, nil
	}
	return &providers.LLMResponse{Content: `{"rejected":true,"reason":"FAKE-REASON"}`, FinishReason: "stop"}, nil
}
func (p *fakeProvider) last() []providers.Message {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]providers.Message(nil), p.requests[len(p.requests)-1]...)
}
func contains(m []providers.Message, s string) bool {
	for _, x := range m {
		if strings.Contains(x.Content, s) {
			return true
		}
	}
	return false
}
func cfgFor(w string) *config.Config {
	z := 0.0
	return &config.Config{Agents: config.AgentsConfig{Defaults: config.AgentDefaults{Workspace: w, ModelName: "test-model", MaxTokens: 1024, ContextWindow: 32000, Temperature: &z, MaxToolIterations: 1, SummarizeMessageThreshold: 1000, SummarizeTokenPercent: 99}}}
}
func makeLoop(t *testing.T, cfg *config.Config, p *fakeProvider) *agent.AgentLoop {
	t.Helper()
	t.Setenv("PICOCLAW_HOME", t.TempDir())
	b := bus.NewMessageBus()
	a := agent.NewAgentLoop(cfg, b, p)
	t.Cleanup(func() { a.Close(); b.Close() })
	if got := a.GetRegistry().GetDefaultAgent().Tools.Count(); got != 0 {
		t.Logf("UNEXPECTED nonzero tool registry = %d; tools=%v", got, a.GetRegistry().GetDefaultAgent().Tools.List())
	}
	return a
}
func call(t *testing.T, a *agent.AgentLoop, s, k string) string {
	t.Helper()
	c, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	out, err := a.ProcessDirect(c, s, k)
	if err != nil {
		t.Fatal(err)
	}
	return out
}
func TestDialogue(t *testing.T) {
	t.Setenv("PICOCLAW_BUILTIN_SKILLS", t.TempDir())
	w := t.TempDir()
	p := &fakeProvider{}
	cfg := cfgFor(w)
	a := makeLoop(t, cfg, p)
	k := session.BuildOpaqueSessionKey("agent:main:voice:operator1:conversation1")
	call(t, a, "FIRST-ORIGINAL", k)
	call(t, a, "SECOND-FOLLOWUP", k)
	if !contains(p.last(), "FIRST-ORIGINAL") || !contains(p.last(), "FAKE-REASON") {
		t.Fatal("follow-up misses history")
	}
	a.GetRegistry().GetDefaultAgent().Sessions.AddMessage(k, "assistant", "NOTE-EXECUTION-FAILED")
	if err := a.GetRegistry().GetDefaultAgent().Sessions.Save(k); err != nil {
		t.Fatal(err)
	}
	a.Close()
	p2 := &fakeProvider{}
	a2 := makeLoop(t, cfg, p2)
	call(t, a2, "THIRD-AFTER-RESTART", k)
	if !contains(p2.last(), "NOTE-EXECUTION-FAILED") || !contains(p2.last(), "FIRST-ORIGINAL") {
		t.Fatal("restart misses note/history")
	}
	t.Log("PASS: external module links AgentLoop; explicit session two-round history and note+Save survive reopen")
}
func TestBareKeysCollide(t *testing.T) {
	t.Setenv("PICOCLAW_BUILTIN_SKILLS", t.TempDir())
	p := &fakeProvider{}
	a := makeLoop(t, cfgFor(t.TempDir()), p)
	call(t, a, "VOICE-ONLY-SECRET", "voice")
	call(t, a, "CALL-ONLY-INPUT", "call")
	t.Logf("bare voice leaked into call request = %v; raw voice history len=%d, raw call len=%d", contains(p.last(), "VOICE-ONLY-SECRET"), len(a.GetRegistry().GetDefaultAgent().Sessions.GetHistory("voice")), len(a.GetRegistry().GetDefaultAgent().Sessions.GetHistory("call")))
	if !contains(p.last(), "VOICE-ONLY-SECRET") {
		t.Fatal("expected inspected routing collision did not reproduce")
	}
}
func TestUnexpectedToolCall(t *testing.T) {
	t.Setenv("PICOCLAW_BUILTIN_SKILLS", t.TempDir())
	p := &fakeProvider{tool: true}
	a := makeLoop(t, cfgFor(t.TempDir()), p)
	k := session.BuildOpaqueSessionKey("agent:main:voice:test")
	out := call(t, a, "tool attack", k)
	h := a.GetRegistry().GetDefaultAgent().Sessions.GetHistory(k)
	t.Logf("tool-call response returned as success string=%q; history=%+v; provider tool defs=%v", out, h, p.toolCounts)
	t.Logf("final tool registry=%v", a.GetRegistry().GetDefaultAgent().Tools.List())
}

type sensorContributor struct{ value *string }

func (s sensorContributor) PromptSource() agent.PromptSourceDescriptor {
	return agent.PromptSourceDescriptor{ID: "robot:observations", Owner: "robot", Allowed: []agent.PromptPlacement{{Layer: agent.PromptLayerContext, Slot: agent.PromptSlotRuntime}}}
}
func (s sensorContributor) ContributePrompt(context.Context, agent.PromptBuildRequest) ([]agent.PromptPart, error) {
	return []agent.PromptPart{{ID: "robot.observation", Layer: agent.PromptLayerContext, Slot: agent.PromptSlotRuntime, Source: agent.PromptSource{ID: "robot:observations"}, Content: *s.value}}, nil
}
func TestDynamicContributor(t *testing.T) {
	t.Setenv("PICOCLAW_BUILTIN_SKILLS", t.TempDir())
	p := &fakeProvider{}
	a := makeLoop(t, cfgFor(t.TempDir()), p)
	value := "SENSOR-TIME-1"
	if err := a.GetRegistry().GetDefaultAgent().ContextBuilder.RegisterPromptContributor(sensorContributor{&value}); err != nil {
		t.Fatal(err)
	}
	k := session.BuildOpaqueSessionKey("agent:main:voice:dynamic")
	call(t, a, "first", k)
	if !contains(p.last(), value) {
		t.Fatal("first sensor absent")
	}
	value = "SENSOR-TIME-2"
	call(t, a, "second", k)
	if !contains(p.last(), value) {
		t.Fatal("updated sensor absent")
	}
	t.Log("public RegisterPromptContributor inserts fresh dynamic system context")
}
func TestSummaryAppendLossRepro(t *testing.T) {
	t.Setenv("PICOCLAW_BUILTIN_SKILLS", t.TempDir())
	p := &fakeProvider{summarizeStarted: make(chan struct{}), summarizeRelease: make(chan struct{})}
	cfg := cfgFor(t.TempDir())
	cfg.Agents.Defaults.SummarizeMessageThreshold = 5
	a := makeLoop(t, cfg, p)
	k := session.BuildOpaqueSessionKey("agent:main:voice:summary-race")
	call(t, a, "TURN-A", k)
	call(t, a, "TURN-B-UNSUMMARIZED", k)
	call(t, a, "TURN-C", k)
	select {
	case <-p.summarizeStarted:
	case <-time.After(5 * time.Second):
		t.Fatal("summary never started")
	}
	store := a.GetRegistry().GetDefaultAgent().Sessions
	store.AddMessage(k, "assistant", "NEW-NOTE-DURING-SUMMARY")
	close(p.summarizeRelease)
	for n := 0; n < 100; n++ {
		if store.GetSummary(k) == "SUMMARY_OF_A_ONLY" {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	time.Sleep(20 * time.Millisecond)
	h := store.GetHistory(k)
	summary := store.GetSummary(k)
	t.Logf("summary=%q; post-append history=%+v", summary, h)
	if summary != "SUMMARY_OF_A_ONLY" {
		t.Fatal("summary not completed")
	}
	if contains(h, "TURN-B-UNSUMMARIZED") {
		t.Fatal("expected loss did not reproduce")
	}
	t.Log("REPRODUCED: new note shifts keepLast=4 truncation and deletes TURN-B user text not covered by summary")
}

var _ = fmt.Sprintf

type guardProvider struct{ inner *fakeProvider }

func (p guardProvider) GetDefaultModel() string { return p.inner.GetDefaultModel() }
func (p guardProvider) Chat(ctx context.Context, m []providers.Message, tls []providers.ToolDefinition, model string, opts map[string]any) (*providers.LLMResponse, error) {
	r, err := p.inner.Chat(ctx, m, nil, model, opts)
	if err != nil {
		return nil, err
	}
	if len(r.ToolCalls) > 0 {
		return nil, fmt.Errorf("dialogue rejects unexpected ToolCalls")
	}
	return r, nil
}
func TestNoToolsFrontmatterAndProviderGuard(t *testing.T) {
	t.Setenv("PICOCLAW_BUILTIN_SKILLS", t.TempDir())
	t.Setenv("PICOCLAW_HOME", t.TempDir())
	w := t.TempDir()
	if err := os.WriteFile(filepath.Join(w, "AGENT.md"), []byte("---\ntools: []\n---\nDialogue returns JSON only.\n"), 0600); err != nil {
		t.Fatal(err)
	}
	cfg := cfgFor(w)
	p := &fakeProvider{}
	b := bus.NewMessageBus()
	a := agent.NewAgentLoop(cfg, b, guardProvider{p})
	defer a.Close()
	defer b.Close()
	inst := a.GetRegistry().GetDefaultAgent()
	if inst.Tools.Count() != 0 {
		t.Fatalf("AGENT.md tools=[] did not deny all: %v", inst.Tools.List())
	}
	if _, ok := inst.Tools.Get("reaction"); ok {
		t.Fatal("reaction remains callable")
	}
	k := session.BuildOpaqueSessionKey("agent:main:voice:no-tools")
	call(t, a, "NORMAL", k)
	if len(p.toolCounts) != 1 || p.toolCounts[0] != 0 {
		t.Fatalf("model saw tools: %v", p.toolCounts)
	}
	p.tool = true
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	out, err := a.ProcessDirect(ctx, "ATTACK", k)
	t.Logf("guarded unexpected toolcalls response=%q err=%v toolCounts=%v", out, err, p.toolCounts)
	for _, m := range inst.Sessions.GetHistory(k) {
		if m.Role == "tool" || len(m.ToolCalls) > 0 {
			t.Fatalf("guard allowed tool path: %+v", m)
		}
	}
	t.Log("PASS: controlled AGENT.md tools=[] creates zero registry/model tools; provider guard prevents tool-call history/execution")
}
