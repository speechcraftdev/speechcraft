//go:build windows

package launch

import (
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"unsafe"

	"speechcraft/windows/internal/diag"
)

func FindWSL(getenv func(string) string) (string, error) {
	root := getenv("SystemRoot")
	if root == "" {
		root = `C:\Windows`
	}
	candidate := filepath.Join(root, "System32", "wsl.exe")
	info, err := os.Stat(candidate)
	if err != nil || info.IsDir() {
		return "", diag.New("WSL_NOT_FOUND", "wsl.exe was not found in Windows System32. Install WSL2 before running SpeechCraft. SpeechCraft will not install a distribution automatically.", diag.ExitIntegration)
	}
	return candidate, nil
}

type commandProcess struct {
	cmd    *exec.Cmd
	stdout io.Reader
	stderr io.Reader
}

func StartWSL(wsl string, argv []string) (Process, error) {
	cmd := exec.Command(wsl, argv...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, err
	}
	cmd.Stdin = nil
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return &commandProcess{cmd: cmd, stdout: stdout, stderr: stderr}, nil
}

func (p *commandProcess) Stdout() io.Reader { return p.stdout }
func (p *commandProcess) Stderr() io.Reader { return p.stderr }
func (p *commandProcess) Wait() int {
	err := p.cmd.Wait()
	if err == nil {
		return 0
	}
	var exit *exec.ExitError
	if errorsAsExit(err, &exit) {
		return exit.ExitCode()
	}
	return diag.ExitWSLStart
}
func (p *commandProcess) RequestStop() {
	if p.cmd.Process != nil {
		_ = p.cmd.Process.Signal(os.Interrupt)
	}
}
func (p *commandProcess) Cancel() {
	if p.cmd.Process != nil {
		_ = p.cmd.Process.Kill()
	}
}

func errorsAsExit(err error, target **exec.ExitError) bool {
	if typed, ok := err.(*exec.ExitError); ok {
		*target = typed
		return true
	}
	return false
}

func OpenDefaultBrowser(rawURL string) error {
	var route string
	switch rawURL {
	case "http://127.0.0.1:3002/login":
		route = "/login"
	default:
		return diag.New("URL_REJECTED", "refusing to open a non-SpeechCraft loopback URL", diag.ExitArgument)
	}
	if err := ValidateFrontendURL(rawURL, route); err != nil {
		return err
	}
	shell32 := syscall.NewLazyDLL("shell32.dll")
	proc := shell32.NewProc("ShellExecuteW")
	verb, err := syscall.UTF16PtrFromString("open")
	if err != nil {
		return err
	}
	target, err := syscall.UTF16PtrFromString(rawURL)
	if err != nil {
		return err
	}
	result, _, callErr := proc.Call(0, uintptr(unsafe.Pointer(verb)), uintptr(unsafe.Pointer(target)), 0, 0, 1)
	if result <= 32 {
		return diag.New("BROWSER_OPEN_FAILED", callErr.Error(), diag.ExitOK)
	}
	return nil
}
