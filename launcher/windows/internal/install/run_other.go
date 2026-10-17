//go:build !windows

package install

import (
	"fmt"
	"io"
)

func Run(args []string, stdout, stderr io.Writer, _ func(string) string) int {
	command, _, err := ParseArgs(args)
	if err != nil {
		fmt.Fprintf(stderr, "speechcraft: %s\n", err)
		return 2
	}
	fmt.Fprintf(stderr, "speechcraft: %s must be run on Windows. This build can prepare the package, but it cannot talk to wsl.exe.\n", command)
	fmt.Fprintln(stdout, Usage())
	return 3
}
