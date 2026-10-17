package install

import (
	"fmt"

	"speechcraft/windows/internal/diag"
)

func ParseArgs(args []string) (string, Options, error) {
	if len(args) == 0 {
		return "", Options{}, diag.New("ARGUMENT_REJECTED", "missing installer command", diag.ExitArgument)
	}
	command := args[0]
	opts := Options{}
	switch command {
	case "--windows-uninstall":
		if len(args) != 1 {
			return "", Options{}, diag.New("ARGUMENT_REJECTED", "uninstall does not take other arguments", diag.ExitArgument)
		}
		return command, opts, nil
	case "--windows-install":
	default:
		return "", Options{}, diag.New("ARGUMENT_REJECTED", "unknown installer command", diag.ExitArgument)
	}
	for index := 1; index < len(args); index++ {
		switch args[index] {
		case "--linux-archive":
			if opts.Archive != "" || index+1 >= len(args) {
				return "", Options{}, diag.New("ARGUMENT_REJECTED", "--linux-archive requires one path", diag.ExitArgument)
			}
			index++
			opts.Archive = args[index]
		case "--distribution":
			if opts.Distribution != "" || index+1 >= len(args) {
				return "", Options{}, diag.New("ARGUMENT_REJECTED", "--distribution requires one name", diag.ExitArgument)
			}
			index++
			opts.Distribution = args[index]
		default:
			return "", Options{}, diag.New("ARGUMENT_REJECTED", "unknown installer argument", diag.ExitArgument)
		}
	}
	if opts.Archive == "" {
		return "", Options{}, diag.New("ARCHIVE_MISSING", "Pass the verified Linux x86-64 archive.", diag.ExitArgument)
	}
	return command, opts, nil
}

func Usage() string {
	return fmt.Sprintf("%s", "speechcraft --windows-install --linux-archive ARCHIVE [--distribution NAME]\nspeechcraft --windows-uninstall")
}
