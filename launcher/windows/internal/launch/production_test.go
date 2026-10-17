package launch

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestProductionWiringRejectsUnauthenticatedFrontend(t *testing.T) {
	mainPath := filepath.Join("..", "..", "cmd", "speechcraft", "main.go")
	body, err := os.ReadFile(mainPath)
	if err != nil {
		t.Fatal(err)
	}
	source := string(body)
	if strings.Contains(source, "Probe: launch.ProbeFrontend") || strings.Contains(source, "ProbeFrontend,") {
		t.Fatal("production main.go still wires the rejecting probe")
	}
	if !strings.Contains(source, "ProductionDeps(") {
		t.Fatal("production main.go does not use ProductionDeps")
	}
	plan, err := Plan(nil, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	var calls [][]string
	deps := BindProduction(plan, func(argv []string) (string, int, error) {
		calls = append(calls, argv)
		return "not-the-proof\n", 0, nil
	}, func([]string) (Process, error) {
		return &fakeProc{}, nil
	}, func(string) error { t.Fatal("browser opened"); return nil }, &bytes.Buffer{}, &bytes.Buffer{})
	if deps.StopGuest == nil || deps.Probe == nil {
		t.Fatal("production dependencies omit probe or guest stop")
	}
	if err := deps.Probe("http://127.0.0.1:3002/setup"); err == nil {
		t.Fatal("unauthenticated or mismatched frontend was accepted")
	}
	if len(calls) != 1 || !containsArg(calls[0], "--prove-frontend") {
		t.Fatal(calls)
	}
}

func TestOwnedStopUsesGuestCommand(t *testing.T) {
	plan, err := Plan(nil, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	var calls [][]string
	stop := OwnedGuestStop(plan, func(argv []string) (string, int, error) {
		calls = append(calls, argv)
		return "SpeechCraft stopped.\n", 0, nil
	})
	if err := stop(); err != nil {
		t.Fatal(err)
	}
	if len(calls) != 1 || calls[0][len(calls[0])-1] != "stop" {
		t.Fatal(calls)
	}
}

func containsArg(args []string, want string) bool {
	for _, arg := range args {
		if arg == want {
			return true
		}
	}
	return false
}
