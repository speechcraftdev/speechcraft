//go:build windows

package install

import (
	"fmt"
	"io"
	"os"
	"path/filepath"

	"speechcraft/windows/internal/diag"
)

func Run(args []string, stdout, stderr io.Writer, getenv func(string) string) int {
	command, opts, err := ParseArgs(args)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", err)
		return diag.ExitArgument
	}
	executable, err := os.Executable()
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", err)
		return diag.ExitIntegration
	}
	executable, err = filepath.EvalSymlinks(executable)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", err)
		return diag.ExitIntegration
	}
	host, err := newWindowsHost(getenv)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", err)
		return diag.ExitIntegration
	}
	if command == "--windows-uninstall" {
		if err := Uninstall(host); err != nil {
			fmt.Fprintf(stderr, "speechcraft: %s\n", err)
			return diag.ExitIntegration
		}
		fmt.Fprintln(stdout, "Removed the SpeechCraft Windows command. Ubuntu, models, projects, and application data were left in place. Open a new terminal so PATH changes are visible.")
		return 0
	}
	releasePath := filepath.Join(filepath.Dir(executable), "windows-release.json")
	releaseBody, err := os.ReadFile(releasePath)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: windows-release.json is required and was not found next to the launcher.\n")
		return diag.ExitIntegration
	}
	exeHash, err := fileSHA256(executable)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", err)
		return diag.ExitIntegration
	}
	binding, err := RequireWindowsRelease(releaseBody, exeHash)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", err)
		return diag.ExitIntegration
	}
	opts.ExpectedSourceRevision = binding.SourceRevision
	opts.InstallerScript = filepath.Join(filepath.Dir(executable), "install-linux.py")
	opts.LauncherExecutable = executable
	cfg, err := Install(host, opts)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", err)
		return diag.ExitIntegration
	}
	fmt.Fprintf(stdout, "Installed SpeechCraft %s into Ubuntu distribution %s.\nThe Windows command is speechcraft. Open a new Windows Terminal window before using it.\n", cfg.ApplicationVersion, cfg.Distribution)
	return 0
}
