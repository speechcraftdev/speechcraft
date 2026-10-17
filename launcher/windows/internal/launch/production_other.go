//go:build !windows

package launch

import (
	"errors"
	"io"
)

func ProductionDeps(wsl string, plan PlanResult, stdout, stderr io.Writer) Deps {
	return BindProduction(plan, func([]string) (string, int, error) {
		return "", 1, errors.New("WSL guest execution is only available on Windows")
	}, func(argv []string) (Process, error) {
		return StartWSL(wsl, argv)
	}, OpenDefaultBrowser, stdout, stderr)
}
