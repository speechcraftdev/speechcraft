package install

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"regexp"
)

var revisionPattern = regexp.MustCompile(`^[a-f0-9]{40}$`)
var shaPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

type ReleaseBinding struct {
	Version        string
	SourceRevision string
	LauncherSHA256 string
}

func sourceRevision(body []byte) string {
	binding, err := RequireWindowsRelease(body, "")
	if err != nil {
		return ""
	}
	return binding.SourceRevision
}

func RequireWindowsRelease(body []byte, exeSHA string) (ReleaseBinding, error) {
	if len(body) == 0 {
		return ReleaseBinding{}, errors.New("windows release metadata is missing")
	}
	var payload struct {
		SchemaVersion  int    `json:"schema_version"`
		Version        string `json:"version"`
		Platform       string `json:"platform"`
		SourceRevision string `json:"source_revision"`
		Launcher       struct {
			SHA256 string `json:"sha256"`
			Target string `json:"target"`
		} `json:"launcher"`
		LinuxPayload struct {
			Platform string `json:"platform"`
		} `json:"linux_payload"`
		Signing struct {
			Status string `json:"status"`
		} `json:"signing"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return ReleaseBinding{}, errors.New("windows release metadata is malformed")
	}
	if payload.SchemaVersion != 1 || payload.Platform != "windows-x64" || payload.Version == "" || !revisionPattern.MatchString(payload.SourceRevision) {
		return ReleaseBinding{}, errors.New("windows release metadata is incomplete")
	}
	if payload.Launcher.Target != "windows/amd64" || !shaPattern.MatchString(payload.Launcher.SHA256) {
		return ReleaseBinding{}, errors.New("windows launcher identity is missing")
	}
	if payload.LinuxPayload.Platform != "linux-x86_64" {
		return ReleaseBinding{}, errors.New("windows release is not bound to the Linux x86-64 payload")
	}
	if exeSHA != "" && payload.Launcher.SHA256 != exeSHA {
		return ReleaseBinding{}, errors.New("windows launcher binary does not match windows-release.json")
	}
	return ReleaseBinding{Version: payload.Version, SourceRevision: payload.SourceRevision, LauncherSHA256: payload.Launcher.SHA256}, nil
}

func fileSHA256(filename string) (string, error) {
	file, err := os.Open(filename)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}
