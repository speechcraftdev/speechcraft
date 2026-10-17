package diag

import "fmt"

const (
	ExitOK           = 0
	ExitArgument     = 2
	ExitIntegration  = 3
	ExitWSLStart     = 4
	ExitGuestUnreach = 0
)

type Error struct {
	Code string
	Msg  string
	Exit int
}

func (e *Error) Error() string {
	if e == nil {
		return ""
	}
	return e.Code + ": " + e.Msg
}

func New(code, message string, exit int) *Error {
	return &Error{Code: code, Msg: message, Exit: exit}
}

func Wrap(code string, exit int, err error) *Error {
	if err == nil {
		return New(code, "", exit)
	}
	return New(code, err.Error(), exit)
}

func Networking(url string) string {
	return fmt.Sprintf("SpeechCraft is running, but Windows could not reach %s. Check that WSL2 localhost forwarding is enabled for NAT mode, or that mirrored networking is in use. The application was left running. SpeechCraft will not change Windows firewall rules, publish a Windows port forward, or bind its services to a public address.", url)
}
