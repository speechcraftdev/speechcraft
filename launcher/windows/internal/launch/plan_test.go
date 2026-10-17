package launch

import (
	"bytes"
	"context"
	"errors"
	"io"
	"strings"
	"sync"
	"testing"
	"time"

	"speechcraft/windows/internal/config"
	"speechcraft/windows/internal/diag"
)

func testConfig() config.Installation {
	return config.Installation{
		SchemaVersion: 1, Distribution: "Ubuntu-24.04", GuestID: "ubuntu", WSLVersion: 2, LinuxUser: "ada",
		LinuxHome: "/home/ada", LinuxCommand: "/home/ada/.local/bin/speechcraft",
		LauncherInstalled: true,
	}
}

func TestPlanForwardsSafeArgumentsWithoutAShell(t *testing.T) {
	cfg := testConfig()
	start, err := Plan(nil, cfg)
	if err != nil || !start.OpenBrowser {
		t.Fatalf("start: %#v %v", start, err)
	}
	if strings.Join(start.WSLArgs, "\x00") != strings.Join([]string{
		"--distribution", "Ubuntu-24.04", "--user", "ada", "--cd", "/home/ada", "--exec",
		"/home/ada/.local/bin/speechcraft", "--no-browser",
	}, "\x00") {
		t.Fatalf("argv %#v", start.WSLArgs)
	}
	spaced, err := Plan([]string{"--data-dir", "/home/ada/My Data/sets"}, cfg)
	if err != nil || spaced.WSLArgs[len(spaced.WSLArgs)-1] != "/home/ada/My Data/sets" {
		t.Fatal(err, spaced.WSLArgs)
	}
	unicode, err := Plan([]string{"--data-dir", "/home/ada/データ"}, cfg)
	if err != nil || unicode.WSLArgs[len(unicode.WSLArgs)-1] != "/home/ada/データ" {
		t.Fatal(unicode.WSLArgs)
	}
	status, err := Plan([]string{"status", "--json", "--data-dir", "/home/ada/data"}, cfg)
	if err != nil || status.OpenBrowser || status.Command != "status" {
		t.Fatal(status, err)
	}
	stop, err := Plan([]string{"stop"}, cfg)
	if err != nil || stop.OpenBrowser || stop.WSLArgs[len(stop.WSLArgs)-1] != "stop" {
		t.Fatal(stop, err)
	}
	help, err := Plan([]string{"--help"}, cfg)
	if err != nil || help.OpenBrowser {
		t.Fatal(help, err)
	}
	silent, err := Plan([]string{"--no-browser"}, cfg)
	if err != nil || silent.OpenBrowser {
		t.Fatal(silent, err)
	}
}

func TestPlanRejectsUnsafeArgumentsAndDistros(t *testing.T) {
	cfg := testConfig()
	for _, args := range [][]string{
		{"status; rm -rf /"},
		{"--bind", "0.0.0.0"},
		{"--data-dir", `C:\Users\ada`},
		{"--data-dir", "/mnt/c/Users/ada"},
		{"--data-dir", "/home/ada/../../etc"},
		{"--data-dir", "/home/ada/../ada/data"},
		{"--development"},
		{"start", "--no-browser"},
		{"--no-browser", "status"},
	} {
		if _, err := Plan(args, cfg); err == nil {
			t.Fatalf("accepted %#v", args)
		}
	}
	wsl1 := cfg
	wsl1.Distribution = "Debian"
	if _, err := Plan(nil, wsl1); err == nil {
		t.Fatal("debian name should still be a name; config validation allows the charset")
	}
	root := cfg
	root.LinuxUser = "root"
	root.LinuxHome = "/root"
	root.LinuxCommand = "/root/.local/bin/speechcraft"
	if _, err := Plan(nil, root); err == nil {
		t.Fatal("root config accepted")
	}
}

func TestReadyEventRejectsUntrustedURLs(t *testing.T) {
	good := `SPEECHCRAFT_READY {"event":"frontend_ready","protocol":1,"url":"http://127.0.0.1:3002/setup","route":"/setup","ready":true}`
	event, ok := ParseReady(good)
	if !ok || event.Route != "/setup" {
		t.Fatal(event, ok)
	}
	for _, line := range []string{
		"SpeechCraft is available at http://evil.example/setup",
		`SPEECHCRAFT_READY {"event":"frontend_ready","protocol":1,"url":"http://127.0.0.1:3002/setup","route":"/setup","ready":false}`,
		`SPEECHCRAFT_READY {"event":"frontend_ready","protocol":1,"url":"http://example.com/setup","route":"/setup","ready":true}`,
		`SPEECHCRAFT_READY {"event":"frontend_ready","protocol":1,"url":"http://127.0.0.1:3001/setup","route":"/setup","ready":true}`,
		`SPEECHCRAFT_READY {"event":"frontend_ready","protocol":1,"url":"http://127.0.0.1:3002/setup","route":"/setup","ready":true,"token":"secret"}`,
		`SPEECHCRAFT_READY {"event":"frontend_ready","protocol":1,"url":"http://user:pass@127.0.0.1:3002/setup","route":"/setup","ready":true}`,
	} {
		if _, ok := ParseReady(line); ok {
			t.Fatalf("accepted %s", line)
		}
	}
}

type fakeProc struct {
	out        string
	err        string
	code       int
	delay      time.Duration
	cancelled  bool
	stopped    bool
	mu         sync.Mutex
	waitOnce   sync.Once
	waitResult int
	started    chan struct{}
}

func (p *fakeProc) Stdout() io.Reader { return strings.NewReader(p.out) }
func (p *fakeProc) Stderr() io.Reader { return strings.NewReader(p.err) }
func (p *fakeProc) Wait() int {
	p.waitOnce.Do(func() {
		if p.delay > 0 {
			time.Sleep(p.delay)
		}
		p.mu.Lock()
		p.waitResult = p.code
		p.mu.Unlock()
	})
	return p.waitResult
}
func (p *fakeProc) RequestStop() {
	p.mu.Lock()
	p.stopped = true
	p.code = 130
	p.mu.Unlock()
}
func (p *fakeProc) Cancel() {
	p.mu.Lock()
	p.cancelled = true
	p.code = 130
	p.mu.Unlock()
}

func TestDispatchOpensBrowserOnceForReadyFrontend(t *testing.T) {
	var opened []string
	var argv []string
	out := &bytes.Buffer{}
	errBuf := &bytes.Buffer{}
	plan, err := Plan(nil, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	code, err := Run(context.Background(), plan, Deps{
		Start: func(args []string) (Process, error) {
			argv = append([]string(nil), args...)
			return &fakeProc{out: "booting\nSPEECHCRAFT_READY {\"event\":\"frontend_ready\",\"protocol\":1,\"url\":\"http://127.0.0.1:3002/setup\",\"route\":\"/setup\",\"ready\":true}\nSPEECHCRAFT_READY {\"event\":\"frontend_ready\",\"protocol\":1,\"url\":\"http://127.0.0.1:3002/setup\",\"route\":\"/setup\",\"ready\":true}\nSpeechCraft is available at http://127.0.0.1:3002/setup\n", code: 0}, nil
		},
		Probe: func(url string) error { return nil },
		Open:  func(url string) error { opened = append(opened, url); return nil },
		Out:   out, Err: errBuf,
	})
	if err != nil || code != 0 {
		t.Fatal(code, err)
	}
	if len(opened) != 1 || opened[0] != "http://127.0.0.1:3002/setup" {
		t.Fatalf("opened %#v", opened)
	}
	if strings.Contains(strings.Join(argv, " "), "bash -c") {
		t.Fatal(argv)
	}
	if !strings.Contains(out.String(), "booting") {
		t.Fatal(out.String())
	}
}

func TestNoBrowserAndUnreachableFrontendDoNotOpen(t *testing.T) {
	opened := 0
	plan, err := Plan([]string{"--no-browser"}, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	_, err = Run(context.Background(), plan, Deps{
		Start: func([]string) (Process, error) {
			return &fakeProc{out: "SPEECHCRAFT_READY {\"event\":\"frontend_ready\",\"protocol\":1,\"url\":\"http://127.0.0.1:3002/login\",\"route\":\"/login\",\"ready\":true}\n"}, nil
		},
		Probe: func(string) error { t.Fatal("probed in no-browser mode"); return nil },
		Open:  func(string) error { opened++; return nil },
		Out:   &bytes.Buffer{}, Err: &bytes.Buffer{},
	})
	if err != nil || opened != 0 {
		t.Fatal(err, opened)
	}
	launch, _ := Plan(nil, testConfig())
	errOut := &bytes.Buffer{}
	code, err := Run(context.Background(), launch, Deps{
		Start: func([]string) (Process, error) {
			return &fakeProc{out: "SPEECHCRAFT_READY {\"event\":\"frontend_ready\",\"protocol\":1,\"url\":\"http://127.0.0.1:3002/setup\",\"route\":\"/setup\",\"ready\":true}\n", delay: 20 * time.Millisecond}, nil
		},
		Probe: func(string) error { return errors.New("connection refused") },
		Open:  func(string) error { opened++; return nil },
		Out:   &bytes.Buffer{}, Err: errOut,
	})
	if err != nil || code != 0 || opened != 0 {
		t.Fatalf("code %d opened %d err %v", code, opened, err)
	}
	if !strings.Contains(errOut.String(), "localhost forwarding") || !strings.Contains(errOut.String(), "mirrored") {
		t.Fatal(errOut.String())
	}
	if strings.Contains(errOut.String(), "0.0.0.0") {
		t.Fatal("diagnostic suggested a public bind")
	}
}

func TestBrowserOpenFailureLeavesTheGuestRunning(t *testing.T) {
	plan, _ := Plan(nil, testConfig())
	errOut := &bytes.Buffer{}
	proc := &fakeProc{out: "SPEECHCRAFT_READY {\"event\":\"frontend_ready\",\"protocol\":1,\"url\":\"http://127.0.0.1:3002/setup\",\"route\":\"/setup\",\"ready\":true}\nSpeechCraft is available at http://127.0.0.1:3002/setup\n", code: 0}
	code, err := Run(context.Background(), plan, Deps{
		Start: func([]string) (Process, error) { return proc, nil },
		Probe: func(string) error { return nil },
		Open:  func(string) error { return errors.New("no display") },
		Out:   &bytes.Buffer{}, Err: errOut,
	})
	if err != nil || code != 0 || proc.cancelled {
		t.Fatal(code, err, proc.cancelled)
	}
	if !strings.Contains(errOut.String(), "still running") {
		t.Fatal(errOut.String())
	}
}

func TestGuestExitAndWslStartupAreDistinct(t *testing.T) {
	plan, _ := Plan([]string{"status"}, testConfig())
	code, err := Run(context.Background(), plan, Deps{
		Start: func([]string) (Process, error) {
			return &fakeProc{err: "speechcraft: controller unavailable\n", code: 7}, nil
		},
		Out: &bytes.Buffer{}, Err: &bytes.Buffer{},
	})
	if err != nil || code != 7 {
		t.Fatal(code, err)
	}
	code, err = Run(context.Background(), plan, Deps{
		Start: func([]string) (Process, error) { return &fakeProc{err: "There is no distribution\n", code: 1}, nil },
		Out:   &bytes.Buffer{}, Err: &bytes.Buffer{},
	})
	var diagnostic *diag.Error
	if code != diag.ExitWSLStart || !errors.As(err, &diagnostic) || diagnostic.Code != "WSL_START_FAILED" {
		t.Fatal(code, err)
	}
	code, err = Run(context.Background(), plan, Deps{
		Start: func([]string) (Process, error) { return nil, errors.New("wsl.exe missing") },
		Out:   &bytes.Buffer{}, Err: &bytes.Buffer{},
	})
	if code != diag.ExitIntegration {
		t.Fatal(code, err)
	}
}

func TestCancelForwardsToTheGuestProcess(t *testing.T) {
	plan, _ := Plan(nil, testConfig())
	proc := &fakeProc{delay: time.Second, code: 0}
	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan struct{})
	go func() {
		Run(ctx, plan, Deps{
			Start: func([]string) (Process, error) { return proc, nil },
			Probe: func(string) error { return nil },
			Open:  func(string) error { return nil },
			Out:   &bytes.Buffer{}, Err: &bytes.Buffer{},
		})
		close(finished)
	}()
	time.Sleep(30 * time.Millisecond)
	cancel()
	select {
	case <-finished:
	case <-time.After(2 * time.Second):
		t.Fatal("cancel did not unblock")
	}
	proc.mu.Lock()
	stopped := proc.stopped
	cancelled := proc.cancelled
	proc.mu.Unlock()
	if !stopped || cancelled {
		t.Fatal("ctrl+c killed the transport instead of requesting a stop")
	}
}

func TestRepeatedLaunchReusesTheSameArgumentPlan(t *testing.T) {
	cfg := testConfig()
	first, err := Plan(nil, cfg)
	if err != nil {
		t.Fatal(err)
	}
	second, err := Plan(nil, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(first.WSLArgs, " ") != strings.Join(second.WSLArgs, " ") {
		t.Fatal(first.WSLArgs, second.WSLArgs)
	}
}
