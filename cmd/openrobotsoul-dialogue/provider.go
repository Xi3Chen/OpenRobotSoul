package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/sipeed/picoclaw/pkg/agent"
	"github.com/sipeed/picoclaw/pkg/config"
	"github.com/sipeed/picoclaw/pkg/providers"
)

type codedError struct{ Code, Message string }

func (e *codedError) Error() string { return e.Message }

type llmSettings struct {
	Provider       string `json:"provider"`
	BaseURL        string `json:"base_url"`
	Model          string `json:"model"`
	TimeoutSeconds int    `json:"timeout_seconds"`
	APIKey         string `json:"api_key"`
}

func loadProvider(path string) (providers.LLMProvider, string, time.Duration, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, "", 0, fmt.Errorf("read LLM configuration: %w", err)
	}
	var settings llmSettings
	if err = json.Unmarshal(content, &settings); err != nil {
		return nil, "", 0, fmt.Errorf("invalid LLM configuration: %w", err)
	}
	if settings.TimeoutSeconds == 0 {
		settings.TimeoutSeconds = 30
	}
	if settings.TimeoutSeconds < 1 || settings.TimeoutSeconds > 180 {
		return nil, "", 0, errors.New("timeout_seconds must be between 1 and 180")
	}
	settings.Model = strings.TrimSpace(settings.Model)
	if settings.Model == "" {
		return nil, "", 0, errors.New("LLM model is required")
	}
	providerName := strings.ToLower(strings.TrimSpace(settings.Provider))
	if providerName == "" || providerName == "openai-compatible" {
		providerName = "openai"
	}
	// The dialogue process uses exactly this provider/config snapshot. Model
	// changes require a controlled process restart; no fallback or CLI provider.
	switch providerName {
	case "openai", "anthropic", "gemini", "ollama", "deepseek", "openrouter", "groq", "zhipu", "qwen":
	default:
		return nil, "", 0, fmt.Errorf("unsupported dialogue provider %q", providerName)
	}
	modelConfig := &config.ModelConfig{ModelName: "dialogue", Provider: providerName, Model: settings.Model, APIBase: strings.TrimRight(settings.BaseURL, "/"), RequestTimeout: settings.TimeoutSeconds, APIKeys: config.SimpleSecureStrings(settings.APIKey)}
	provider, model, err := providers.CreateProviderFromConfig(modelConfig)
	return provider, model, time.Duration(settings.TimeoutSeconds) * time.Second, err
}

// Intentionally implements only LLMProvider. Optional streaming, search and
// stateful provider interfaces cannot bypass the checks in Chat.
type guardedProvider struct {
	inner  providers.LLMProvider
	store  *ledger
	budget int
}

func (p *guardedProvider) GetDefaultModel() string { return p.inner.GetDefaultModel() }

func (p *guardedProvider) Chat(ctx context.Context, messages []providers.Message, tools []providers.ToolDefinition, model string, options map[string]any) (*providers.LLMResponse, error) {
	purpose, _ := ctx.Value(purposeKey{}).(callPurpose)
	if purpose == "" {
		purpose = purposeDecision
	}
	gate := gateFrom(ctx)
	deny := func(err error) (*providers.LLMResponse, error) {
		if purpose == purposeDecision {
			gate.fail(err)
		}
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return deny(err)
	}
	if err := p.store.Health(); err != nil {
		return deny(err)
	}
	if len(tools) != 0 {
		return deny(&codedError{"tools_forbidden", "dialogue tools must be empty"})
	}
	if purpose == purposeDecision {
		if err := gate.verify(messages); err != nil {
			return deny(err)
		}
	}
	reserve := 1024
	if v, ok := options["max_tokens"].(int); ok && v > 0 {
		reserve = v
	}
	tokens := reserve
	for _, message := range messages {
		tokens += agent.EstimateMessageTokens(message)
	}
	if tokens > p.budget {
		return deny(&codedError{"context_budget", "complete dialogue context exceeds the model budget; history was retained"})
	}
	response, err := p.inner.Chat(ctx, cloneMessages(messages), nil, model, options)
	if err != nil {
		return deny(err)
	}
	if err = ctx.Err(); err != nil {
		return deny(err)
	}
	if err = p.store.Health(); err != nil {
		return deny(err)
	}
	if response == nil {
		return deny(errors.New("provider returned no response"))
	}
	if len(response.ToolCalls) != 0 {
		return deny(&codedError{"tools_forbidden", "model returned an unexpected tool call"})
	}
	if response.FinishReason == "length" || response.FinishReason == "max_tokens" {
		return deny(&codedError{"incomplete_response", "model response exceeded its output budget"})
	}
	if purpose == purposeDecision {
		if _, err = parseDecision(response.Content); err != nil {
			return deny(err)
		}
	} else if purpose == purposeReview {
		if _, err = parseReview(response.Content); err != nil {
			return deny(err)
		}
	}
	return response, nil
}

func parseObject(content string) (map[string]any, error) {
	var result map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(content)), &result); err != nil || result == nil {
		return nil, &codedError{"invalid_response", "model must return one JSON object"}
	}
	if calls, exists := result["tool_calls"]; exists && calls != nil {
		if a, ok := calls.([]any); !ok || len(a) != 0 {
			return nil, &codedError{"tools_forbidden", "model JSON contains an unexpected tool call"}
		}
	}
	return result, nil
}

func nonemptyString(value any) bool { s, ok := value.(string); return ok && strings.TrimSpace(s) != "" }

func parseDecision(content string) (map[string]any, error) {
	r, err := parseObject(content)
	if err != nil {
		return nil, err
	}
	if kind, ok := r["kind"].(string); ok {
		valid := false
		switch kind {
		case "act":
			steps, ok := r["steps"].([]any)
			valid = ok && len(steps) > 0 && len(steps) <= 32
		case "run_rule":
			_, valid = r["rule"].(map[string]any)
		case "wait":
			_, valid = r["wait"].(map[string]any)
		case "complete", "clarify":
			valid = nonemptyString(r["reply"])
		case "reject":
			valid = nonemptyString(r["reason"]) || nonemptyString(r["reply"])
		case "coding_task":
			valid = nonemptyString(r["coding_task"])
		}
		if !valid {
			return nil, &codedError{"invalid_response", "model returned an invalid decision kind or required fields"}
		}
		return r, nil
	}
	if r["rejected"] == true || nonemptyString(r["coding_task"]) {
		return r, nil
	}
	if nonemptyString(r["skill"]) {
		if _, ok := r["parameters"].(map[string]any); ok {
			return r, nil
		}
	}
	if plan, ok := r["plan"].([]any); ok && len(plan) > 0 && len(plan) <= 32 {
		return r, nil
	}
	return nil, &codedError{"invalid_response", "model did not return a supported decision"}
}

func parseReview(content string) (map[string]any, error) {
	r, err := parseObject(content)
	if err != nil {
		return nil, err
	}
	switch r["verdict"] {
	case "allow", "deny", "uncertain":
	default:
		return nil, &codedError{"invalid_response", "review verdict must be allow, deny or uncertain"}
	}
	if !nonemptyString(r["rationale"]) {
		return nil, &codedError{"invalid_response", "review rationale is required"}
	}
	return r, nil
}

// This explicit opt-in provider is for local protocol and recovery tests.
// A JSON decision in text is echoed; ordinary text yields a complete reply.
// It is never selected as a fallback for unavailable production credentials.
type fakeProvider struct{}

func (fakeProvider) GetDefaultModel() string { return "explicit-test-provider" }
func (fakeProvider) Chat(ctx context.Context, messages []providers.Message, _ []providers.ToolDefinition, _ string, _ map[string]any) (*providers.LLMResponse, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	purpose, _ := ctx.Value(purposeKey{}).(callPurpose)
	if purpose == purposeSummary {
		return &providers.LLMResponse{Content: "Completed dialogue turns are archived in the durable journal. Preserve current task feedback as authoritative.", FinishReason: "stop"}, nil
	}
	if purpose == purposeReview {
		return &providers.LLMResponse{Content: `{"verdict":"uncertain","rationale":"Explicit test provider does not authorize physical actions"}`, FinishReason: "stop"}, nil
	}
	text := messages[len(messages)-1].Content
	var input struct {
		Text string `json:"text"`
	}
	if json.Unmarshal([]byte(text), &input) == nil {
		text = input.Text
	}
	if text == "__tool_call__" {
		return &providers.LLMResponse{ToolCalls: []providers.ToolCall{{ID: "forbidden", Name: "exec"}}}, nil
	}
	if text == "__wait_for_cancel__" {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if _, err := parseDecision(text); err == nil {
		return &providers.LLMResponse{Content: text, FinishReason: "stop"}, nil
	}
	content, _ := json.Marshal(map[string]any{"kind": "complete", "reply": text, "history_messages": len(messages) - 2})
	return &providers.LLMResponse{Content: string(content), FinishReason: "stop"}, nil
}
