package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

var (
	distroPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
	userPattern   = regexp.MustCompile(`^[a-z_][a-z0-9_-]{0,31}$`)
)

type Installation struct {
	SchemaVersion      int    `json:"schema_version"`
	Distribution       string `json:"distribution"`
	GuestID            string `json:"guest_id"`
	WSLVersion         int    `json:"wsl_version"`
	LinuxUser          string `json:"linux_user"`
	LinuxHome          string `json:"linux_home"`
	LinuxCommand       string `json:"linux_command"`
	ApplicationVersion string `json:"application_version,omitempty"`
	ArchiveSHA256      string `json:"archive_sha256,omitempty"`
	SourceRevision     string `json:"source_revision,omitempty"`
	LauncherInstalled  bool   `json:"launcher_installed"`
}

func Path(localAppData string) (string, error) {
	if strings.TrimSpace(localAppData) == "" {
		return "", errors.New("LOCALAPPDATA is not set")
	}
	return filepath.Join(localAppData, "SpeechCraft", "config", "installation.json"), nil
}

func Load(filename string) (Installation, error) {
	file, err := os.Open(filename)
	if err != nil {
		return Installation{}, fmt.Errorf("read installation config: %w", err)
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return Installation{}, err
	}
	if !info.Mode().IsRegular() || info.Size() > 64*1024 {
		return Installation{}, errors.New("installation config is not a small regular file")
	}
	return Decode(file)
}

func Decode(r io.Reader) (Installation, error) {
	var cfg Installation
	decoder := json.NewDecoder(r)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&cfg); err != nil {
		return Installation{}, fmt.Errorf("installation config is invalid: %w", err)
	}
	if err := Validate(cfg); err != nil {
		return Installation{}, err
	}
	return cfg, nil
}

func Validate(cfg Installation) error {
	if cfg.SchemaVersion != 1 {
		return errors.New("installation config schema is unsupported")
	}
	if !distroPattern.MatchString(cfg.Distribution) {
		return errors.New("configured WSL distribution name is not a safe Ubuntu distro name")
	}
	if cfg.GuestID != "ubuntu" || cfg.WSLVersion != 2 || !strings.Contains(strings.ToLower(cfg.Distribution), "ubuntu") {
		return errors.New("configured guest must be Ubuntu on WSL2")
	}
	if cfg.LinuxUser == "root" || !userPattern.MatchString(cfg.LinuxUser) {
		return errors.New("configured Linux user must be an ordinary non-root account")
	}
	expectedHome := "/home/" + cfg.LinuxUser
	if cfg.LinuxHome != expectedHome {
		return fmt.Errorf("configured Linux home must be %s", expectedHome)
	}
	expectedCommand := expectedHome + "/.local/bin/speechcraft"
	if cfg.LinuxCommand != expectedCommand {
		return errors.New("configured Linux command must be the per-user SpeechCraft launcher")
	}
	if strings.Contains(cfg.LinuxCommand, "..") || strings.HasPrefix(cfg.LinuxCommand, "/mnt/") {
		return errors.New("configured Linux command is not on the Linux filesystem")
	}
	return nil
}

func Write(filename string, cfg Installation) error {
	if err := Validate(cfg); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(filename), 0o700); err != nil {
		return err
	}
	body, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	body = append(body, '\n')
	temporary := filename + ".tmp"
	if err := os.WriteFile(temporary, body, 0o600); err != nil {
		return err
	}
	return os.Rename(temporary, filename)
}
