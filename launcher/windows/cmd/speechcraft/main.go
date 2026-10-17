package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/signal"

	"speechcraft/windows/internal/config"
	"speechcraft/windows/internal/diag"
	"speechcraft/windows/internal/install"
	"speechcraft/windows/internal/launch"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	code := run(ctx, os.Args[1:], os.Stdout, os.Stderr, os.Getenv)
	os.Exit(code)
}

func run(ctx context.Context, args []string, stdout, stderr io.Writer, getenv func(string) string) int {
	if len(args) > 0 && (args[0] == "--windows-install" || args[0] == "--windows-uninstall") {
		return install.Run(args, stdout, stderr, getenv)
	}
	local := getenv("LOCALAPPDATA")
	filename, err := config.Path(local)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", diag.New("CONFIG_INVALID", "LOCALAPPDATA is required to find the per-user SpeechCraft installation.", diag.ExitIntegration))
		return diag.ExitIntegration
	}
	cfg, err := config.Load(filename)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\nInstall SpeechCraft with install-windows.ps1 before running the command.\n", err)
		return diag.ExitIntegration
	}
	if !cfg.LauncherInstalled {
		fmt.Fprintln(stderr, "speechcraft: the Windows launcher is not installed. Run install-windows.ps1.")
		return diag.ExitIntegration
	}
	plan, err := launch.Plan(args, cfg)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", err)
		var diagnostic *diag.Error
		if errorsAs(err, &diagnostic) {
			return diagnostic.Exit
		}
		return diag.ExitArgument
	}
	wsl, err := launch.FindWSL(getenv)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", err)
		return diag.ExitIntegration
	}
	code, err := launch.Run(ctx, plan, launch.Deps{
		Start: func(argv []string) (launch.Process, error) {
			return launch.StartWSL(wsl, argv)
		},
		Probe: launch.ProbeFrontend,
		Open:  launch.OpenDefaultBrowser,
		Out:   stdout,
		Err:   stderr,
	})
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", err)
	}
	return code
}

func errorsAs(err error, target **diag.Error) bool {
	for err != nil {
		if typed, ok := err.(*diag.Error); ok {
			*target = typed
			return true
		}
		unwrap, ok := err.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		err = unwrap.Unwrap()
	}
	return false
}
