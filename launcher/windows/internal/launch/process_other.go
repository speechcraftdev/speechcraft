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
func (unavailableProcess) Cancel()           {}

func OpenDefaultBrowser(rawURL string) error {
	route := "/setup"
	if len(rawURL) >= len("/login") && rawURL[len(rawURL)-len("/login"):] == "/login" {
		route = "/login"
	}
	if err := ValidateFrontendURL(rawURL, route); err != nil {
		return err
	}
	return diag.New("BROWSER_OPEN_FAILED", "the Windows default browser can only be opened on Windows", diag.ExitOK)
}
