package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"time"

	"github.com/sipeed/picoclaw/pkg/logger"
	"github.com/sipeed/picoclaw/pkg/providers"
)

func serve(ctx context.Context, g *gateway, input io.Reader, output io.Writer) error {
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	writer := bufio.NewWriter(output)
	for scanner.Scan() {
		if len(scanner.Bytes()) == 0 {
			continue
		}
		response, shutdown := g.Handle(ctx, scanner.Bytes())
		if _, err := writer.Write(append(response, '\n')); err != nil {
			return err
		}
		if err := writer.Flush(); err != nil {
			return err
		}
		if shutdown {
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
	}
	return scanner.Err()
}

func run() error {
	logger.DisableConsole()
	defaultHome := os.Getenv("OPENROBOTSOUL_DIALOGUE_HOME")
	if defaultHome == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return err
		}
		defaultHome = filepath.Join(home, ".openrobotsoul", "dialogue")
	}
	defaultConfig := os.Getenv("OPENROBOTSOUL_LLM_CONFIG")
	if defaultConfig == "" {
		defaultConfig = filepath.Join("config", "llm.json")
	}
	home := flag.String("home", defaultHome, "isolated dialogue journal directory")
	configPath := flag.String("config", defaultConfig, "RobotHarness LLM configuration JSON")
	fake := flag.Bool("fake-provider", false, "explicit deterministic test provider; no network or physical authorization")
	budget := flag.Int("context-window", 32768, "complete model context token budget")
	flag.Parse()
	var inner providers.LLMProvider
	var model string
	timeout := 30 * time.Second
	var err error
	if *fake {
		inner, model = fakeProvider{}, "explicit-test-provider"
	} else {
		inner, model, timeout, err = loadProvider(*configPath)
		if err != nil {
			return err
		}
	}
	g, err := newGateway(*home, inner, model, timeout, *budget, *fake)
	if err != nil {
		return err
	}
	defer g.Close()
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	return serve(ctx, g, os.Stdin, os.Stdout)
}

func main() {
	if err := run(); err != nil {
		// Startup failures are visible to a waiting protocol client as well as
		// stderr; they can never masquerade as a successful hello handshake.
		fmt.Fprintln(os.Stderr, "dialogue:", err)
		b, _ := json.Marshal(errorResponse("", err))
		fmt.Fprintln(os.Stdout, string(b))
		os.Exit(1)
	}
}
