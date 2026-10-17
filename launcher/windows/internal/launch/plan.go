package launch

import (
	"encoding/json"
	"errors"
	"strings"

	"speechcraft/windows/internal/config"
	"speechcraft/windows/internal/diag"
)

const readyPrefix = "SPEECHCRAFT_READY "

type ReadyEvent struct {
	Event    string `json:"event"`
	Protocol int    `json:"protocol"`
	URL      string `json:"url"`
	Route    string `json:"route"`
	Ready    bool   `json:"ready"`
	Owned    bool   `json:"owned"`
}

func ParseReady(line string) (ReadyEvent, bool) {
	if !strings.HasPrefix(line, readyPrefix) {
		return ReadyEvent{}, false
	}
	decoder := json.NewDecoder(strings.NewReader(strings.TrimPrefix(line, readyPrefix)))
	decoder.DisallowUnknownFields()
	var event ReadyEvent
	if err := decoder.Decode(&event); err != nil {
		return ReadyEvent{}, false
	}
	if decoder.More() {
		return ReadyEvent{}, false
	}
	if event.Event != "frontend_ready" || event.Protocol != 1 || !event.Ready {
		return ReadyEvent{}, false
	}
	if err := ValidateFrontendURL(event.URL, event.Route); err != nil {
		return ReadyEvent{}, false
	}
	return event, true
}

func ValidateFrontendURL(rawURL, route string) error {
	switch route {
	case "/setup", "/login":
	default:
		return errors.New("untrusted frontend route")
	}
	expected := "http://127.0.0.1:3002" + route
	if rawURL != expected {
		return diag.New("URL_REJECTED", "refusing to open a non-SpeechCraft loopback URL", diag.ExitArgument)
	}
	if strings.Contains(rawURL, "@") || strings.Contains(rawURL, "#") || strings.Contains(rawURL, "?") {
		return diag.New("URL_REJECTED", "frontend URL must not contain credentials, a query, or a fragment", diag.ExitArgument)
	}
	return nil
}

func SawGuest(line string) bool {
	return strings.HasPrefix(line, readyPrefix) ||
		strings.HasPrefix(line, "SpeechCraft") ||
		strings.HasPrefix(line, "speechcraft:") ||
		strings.HasPrefix(line, "Usage:") ||
		strings.HasPrefix(line, "Shutdown status:")
}

func Plan(args []string, cfg config.Installation) (PlanResult, error) {
	if err := config.Validate(cfg); err != nil {
		return PlanResult{}, diag.New("CONFIG_INVALID", err.Error(), diag.ExitIntegration)
	}
	command, forward, openBrowser, err := classify(args)
	if err != nil {
		return PlanResult{}, err
	}
	wslArgs := []string{
		"--distribution", cfg.Distribution,
		"--user", cfg.LinuxUser,
		"--cd", cfg.LinuxHome,
		"--exec", cfg.LinuxCommand,
	}
	wslArgs = append(wslArgs, forward...)
	return PlanResult{Command: command, WSLArgs: wslArgs, OpenBrowser: openBrowser}, nil
}

type PlanResult struct {
	Command     string
	WSLArgs     []string
	OpenBrowser bool
}

func classify(args []string) (string, []string, bool, error) {
	if len(args) == 0 {
		return "start", []string{"--no-browser"}, true, nil
	}
	command := "start"
	index := 0
	switch args[0] {
	case "status", "stop":
		command = args[0]
		index = 1
	case "--help", "-h":
		if len(args) != 1 {
			return "", nil, false, diag.New("ARGUMENT_REJECTED", "help does not take other arguments", diag.ExitArgument)
		}
		return "help", []string{"--help"}, false, nil
	}
	openBrowser := command == "start"
	forward := []string{}
	if command == "status" || command == "stop" {
		forward = append(forward, command)
	}
	jsonSeen := false
	noBrowser := false
	dataDir := ""
	for ; index < len(args); index++ {
		arg := args[index]
		if strings.ContainsAny(arg, "\x00\r\n") {
			return "", nil, false, diag.New("ARGUMENT_REJECTED", "arguments must not contain control characters", diag.ExitArgument)
		}
		switch arg {
		case "--no-browser":
			if command != "start" || noBrowser {
				return "", nil, false, diag.New("ARGUMENT_REJECTED", "--no-browser is only valid for launch", diag.ExitArgument)
			}
			noBrowser = true
			openBrowser = false
		case "--json":
			if command != "status" || jsonSeen {
				return "", nil, false, diag.New("ARGUMENT_REJECTED", "--json is only valid for status", diag.ExitArgument)
			}
			jsonSeen = true
			forward = append(forward, "--json")
		case "--data-dir":
			if dataDir != "" || index+1 >= len(args) {
				return "", nil, false, diag.New("ARGUMENT_REJECTED", "--data-dir requires one Linux path", diag.ExitArgument)
			}
			index++
			value := args[index]
			if err := validateDataDir(value); err != nil {
				return "", nil, false, err
			}
			dataDir = value
			forward = append(forward, "--data-dir", value)
		case "--help", "-h", "status", "stop":
			return "", nil, false, diag.New("ARGUMENT_REJECTED", "unexpected command placement", diag.ExitArgument)
		default:
			return "", nil, false, diag.New("ARGUMENT_REJECTED", "unknown or unsafe argument", diag.ExitArgument)
		}
	}
	if command == "start" {
		forward = append([]string{"--no-browser"}, forward...)
	}
	return command, forward, openBrowser, nil
}

func validateDataDir(value string) error {
	if value == "" || strings.ContainsAny(value, "\x00\r\n") || strings.Contains(value, "\\") || strings.Contains(value, "..") {
		return diag.New("ARGUMENT_REJECTED", "data directory must be an absolute Linux path", diag.ExitArgument)
	}
	if !strings.HasPrefix(value, "/") || strings.HasPrefix(value, "/mnt/") || value == "/mnt" {
		return diag.New("ARGUMENT_REJECTED", "data directory must stay on the Linux filesystem", diag.ExitArgument)
	}
	cleaned := cleanLinuxPath(value)
	if cleaned != value {
		return diag.New("ARGUMENT_REJECTED", "data directory must be a canonical Linux path", diag.ExitArgument)
	}
	return nil
}

func cleanLinuxPath(value string) string {
	if value == "/" || !strings.HasPrefix(value, "/") {
		return ""
	}
	parts := strings.Split(strings.TrimPrefix(value, "/"), "/")
	kept := make([]string, 0, len(parts))
	for _, part := range parts {
		if part == "" || part == "." || part == ".." {
			return ""
		}
		kept = append(kept, part)
	}
	return "/" + strings.Join(kept, "/")
}
