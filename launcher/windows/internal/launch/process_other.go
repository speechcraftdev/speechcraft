//go:build !windows

package launch

import (
	"errors"
	"io"

	"speechcraft/windows/internal/diag"
)

func FindWSL(func(string) string) (string, error) {
	return "", diag.New("WSL_NOT_FOUND", "wsl.exe is only available on Windows.", diag.ExitIntegration)
}

type unavailableProcess struct{}

func StartWSL(string, []string) (Process, error) {
	return nil, errors.New("WSL process launch is only available on Windows")
}

func (unavailableProcess) Stdout() io.Reader { return nil }
func (unavailableProcess) Stderr() io.Reader { return nil }
func (unavailableProcess) Wait() int         { return diag.ExitIntegration }
func (unavailableProcess) RequestStop()      {}
func (unavailableProcess) Cancel()           {}

func OpenDefaultBrowser(rawURL string) error {
	var route string
	switch rawURL {
	case "http://127.0.0.1:3002/setup":
		route = "/setup"
	case "http://127.0.0.1:3002/login":
		route = "/login"
	default:
		return diag.New("URL_REJECTED", "refusing to open a non-SpeechCraft loopback URL", diag.ExitArgument)
	}
	if err := ValidateFrontendURL(rawURL, route); err != nil {
		return err
	}
	return diag.New("BROWSER_OPEN_FAILED", "the Windows default browser can only be opened on Windows", diag.ExitOK)
}
