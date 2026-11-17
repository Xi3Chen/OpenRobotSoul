package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/sipeed/picoclaw/pkg/session"
)

func TestConfiguredHTTPProviderUsesNonStreamingToolsFreeRequest(t *testing.T) {
	requests := make(chan map[string]any, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" || r.Header.Get("Authorization") != "Bearer fixture-key" {
			t.Errorf("unexpected provider route/auth: %s", r.URL.Path)
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
		}
		requests <- payload
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "fixture", "model": "fixture-model", "choices": []any{map[string]any{"index": 0, "finish_reason": "stop", "message": map[string]any{"role": "assistant", "content": `{"kind":"complete","reply":"HTTP fixture result"}`}}}})
	}))
	defer server.Close()
	path := filepath.Join(t.TempDir(), "llm.json")
	data, _ := json.Marshal(llmSettings{Provider: "openai-compatible", BaseURL: server.URL + "/v1", Model: "fixture-model", TimeoutSeconds: 5, APIKey: "fixture-key"})
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	inner, model, timeout, options, err := loadProvider(path)
	if err != nil {
		t.Fatal(err)
	}
	if options.APIMethod != defaultAPIMethod || options.ContextWindow != defaultContextWindow || options.ThinkingLevel != defaultThinkingLevel {
		t.Fatalf("unexpected dialogue options: %+v", options)
	}
	g := testGateway(t, inner)
	g.model, g.timeout = model, timeout
	response := callGateway(t, g, map[string]any{"id": "http-turn", "type": "decide", "session_key": session.BuildOpaqueSessionKey("http/operator/session"), "text": "hello"})
	if response["reply"] != "HTTP fixture result" {
		t.Fatal(response)
	}
	request := <-requests
	if request["stream"] == true {
		t.Fatal("dialogue decision used streaming")
	}
	if tools, exists := request["tools"]; exists && tools != nil {
		if list, ok := tools.([]any); !ok || len(list) != 0 {
			t.Fatal("HTTP model received tools")
		}
	}
	if len(request["messages"].([]any)) != 2 {
		t.Fatal("first turn had unexpected history")
	}
}

func TestStartupFailsForCorruptStorageInsteadOfUsingLegacy(t *testing.T) {
	home := t.TempDir()
	if err := os.WriteFile(filepath.Join(home, "dialogue.wal"), []byte("{corrupted-complete-frame}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	p := &recordingProvider{}
	if g, err := newGateway(home, p, "test-model", 0, 32768, false); err == nil {
		g.Close()
		t.Fatal("storage initialization failure silently fell back to legacy")
	}
	if len(p.snapshotCalls()) != 0 {
		t.Fatal("failed startup called a provider")
	}
}
