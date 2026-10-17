//go:build windows

package launch

import (
	"io"
)

func ProductionDeps(wsl string, plan PlanResult, stdout, stderr io.Writer) Deps {
	exec := func(argv []string) (string, int, error) {
		proc, err := StartWSL(wsl, argv)
		if err != nil {
			return "", 1, err
		}
		stdout, readErr := io.ReadAll(proc.Stdout())
		_, _ = io.Copy(io.Discard, proc.Stderr())
		code := proc.Wait()
		return string(stdout), code, readErr
	}
	return BindProduction(plan, exec, func(argv []string) (Process, error) {
		return StartWSL(wsl, argv)
	}, OpenDefaultBrowser, stdout, stderr)
}
