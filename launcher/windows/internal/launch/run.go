package launch

import (
	"bufio"
	"context"
	"io"
	"strings"
	"sync"
	"sync/atomic"

	"speechcraft/windows/internal/diag"
)

type Process interface {
	Stdout() io.Reader
	Stderr() io.Reader
	Wait() int
	RequestStop()
	Cancel()
}

type Deps struct {
	Start     func(argv []string) (Process, error)
	Probe     func(url string) error
	Open      func(url string) error
	StopGuest func() error
	Out       io.Writer
	Err       io.Writer
}

func Run(ctx context.Context, plan PlanResult, deps Deps) (int, error) {
	proc, err := deps.Start(plan.WSLArgs)
	if err != nil {
		return diag.ExitIntegration, diag.New("WSL_START_FAILED", err.Error(), diag.ExitIntegration)
	}
	done := make(chan struct{})
	defer close(done)
	var owned atomic.Bool
	go func() {
		select {
		case <-ctx.Done():
			if owned.Load() && deps.StopGuest != nil {
				if err := deps.StopGuest(); err == nil {
					proc.RequestStop()
					return
				}
			}
			if !owned.Load() {
				proc.RequestStop()
				return
			}
			proc.Cancel()
		case <-done:
		}
	}()

	var mu sync.Mutex
	guest := false
	mark := func(line string) {
		if event, ok := ParseReady(line); ok && event.Owned {
			owned.Store(true)
		}
		if !SawGuest(line) {
			return
		}
		mu.Lock()
		guest = true
		mu.Unlock()
	}
	ready := make(chan string, 1)
	stdoutDone := make(chan struct{})
	var copyWG sync.WaitGroup
	copyWG.Add(2)
	go func() {
		defer copyWG.Done()
		defer close(stdoutDone)
		copyLines(proc.Stdout(), deps.Out, func(line string) {
			mark(line)
			if !plan.OpenBrowser {
				return
			}
			event, ok := ParseReady(line)
			if !ok {
				return
			}
			select {
			case ready <- event.URL:
			default:
			}
		})
	}()
	go func() {
		defer copyWG.Done()
		copyLines(proc.Stderr(), deps.Err, mark)
	}()

	exitCh := make(chan int, 1)
	go func() { exitCh <- proc.Wait() }()

	openURL := func(url string) {
		if probeErr := deps.Probe(url); probeErr != nil {
			io.WriteString(deps.Err, diag.Networking(url)+"\n")
			return
		}
		if openErr := deps.Open(url); openErr != nil {
			io.WriteString(deps.Err, "Could not open the Windows browser. SpeechCraft is still running at "+url+"\n")
		}
	}
	var code int
	if plan.OpenBrowser {
		select {
		case url := <-ready:
			openURL(url)
			code = <-exitCh
		case <-stdoutDone:
			select {
			case url := <-ready:
				openURL(url)
			default:
			}
			code = <-exitCh
		}
	} else {
		code = <-exitCh
	}
	copyWG.Wait()
	mu.Lock()
	saw := guest
	mu.Unlock()
	if code != 0 && !saw {
		return diag.ExitWSLStart, diag.New("WSL_START_FAILED", "WSL exited before the Linux application produced output", diag.ExitWSLStart)
	}
	return code, nil
}

func copyLines(r io.Reader, w io.Writer, onLine func(string)) {
	if r == nil {
		return
	}
	reader := bufio.NewReaderSize(r, 64*1024)
	for {
		line, err := reader.ReadString('\n')
		if line != "" {
			if w != nil {
				io.WriteString(w, line)
			}
			onLine(strings.TrimRight(line, "\r\n"))
		}
		if err != nil {
			return
		}
	}
}
