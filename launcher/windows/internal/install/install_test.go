package install

import (
	"errors"
	"fmt"
	"strings"
	"testing"

	"speechcraft/windows/internal/config"
	"speechcraft/windows/internal/diag"
)

type fakeHost struct {
	arch      string
	wsl       bool
	names     []string
	guests    map[string]Guest
	files     map[string][]byte
	config    config.Installation
	hasConfig bool
	path      string
	resolved  string
	calls     []string
	transfer  error
	verify    error
	runExit   int
	runErr    error
	helpOut   string
	deleted   []string
	linuxData bool
	exeBackup []byte
	hadExe    bool
}

func (h *fakeHost) note(call string) { h.calls = append(h.calls, call) }

func (h *fakeHost) Architecture() (string, error) {
	h.note("arch")
	if h.arch == "" {
		return "amd64", nil
	}
	return h.arch, nil
}
func (h *fakeHost) WSLPath() (string, error) {
	h.note("wsl")
	if !h.wsl && h.arch != "skip-wsl" {
		if h.wsl {
			return `C:\Windows\System32\wsl.exe`, nil
		}
	}
	if h.arch == "skip-wsl" || !h.wsl {
		return "", diag.New("WSL_NOT_FOUND", "wsl.exe was not found", diag.ExitIntegration)
	}
	return `C:\Windows\System32\wsl.exe`, nil
}
func (h *fakeHost) DistroNames() ([]string, error) { h.note("list"); return h.names, nil }
func (h *fakeHost) Inspect(distro string) (Guest, error) {
	h.note("inspect:" + distro)
	return h.guests[distro], nil
}
func (h *fakeHost) ReadFile(name string) ([]byte, error) {
	body, ok := h.files[name]
	if !ok {
		return nil, errors.New("missing " + name)
	}
	return body, nil
}
func (h *fakeHost) Size(name string) (int64, error) {
	body, err := h.ReadFile(name)
	return int64(len(body)), err
}
func (h *fakeHost) SHA256(name string) (string, error) {
	body, err := h.ReadFile(name)
	if err != nil {
		return "", err
	}
	return HashBytes(body), nil
}
func (h *fakeHost) Transfer(request TransferRequest) (Staging, error) {
	h.note("transfer")
	staging := Staging{LinuxDir: request.Home + "/.local/share/speechcraft/install-staging/" + request.StagingID, Owned: true}
	staging.Archive = staging.LinuxDir + "/" + archiveBase(request.Files[archiveBase(firstArchive(request))])
	if h.transfer != nil {
		return staging, h.transfer
	}
	return staging, nil
}
func (h *fakeHost) VerifyGuestFile(string, string, string) error {
	h.note("verify-copy")
	return h.verify
}
func (h *fakeHost) Run(_, _, _ string, argv []string) (CommandResult, error) {
	h.note("run:" + strings.Join(argv, " "))
	if len(argv) > 0 && strings.HasSuffix(argv[0], "/speechcraft") {
		return CommandResult{ExitCode: 0, Stdout: h.helpOut}, h.runErr
	}
	return CommandResult{ExitCode: h.runExit, Stderr: "linux installer failed"}, h.runErr
}
func (h *fakeHost) RemoveStaging(string, Staging) error { h.note("remove-staging"); return nil }
func (h *fakeHost) LoadConfig() (config.Installation, bool, error) {
	return h.config, h.hasConfig, nil
}
func (h *fakeHost) SaveConfig(cfg config.Installation) error {
	h.note("save-config")
	h.config = cfg
	h.hasConfig = true
	return nil
}
func (h *fakeHost) LauncherDirectory() (string, error) {
	return `C:\Users\Ada\AppData\Local\SpeechCraft\launcher`, nil
}
func (h *fakeHost) InstallExecutable(source, directory string) (string, error) {
	h.note("install-exe")
	h.hadExe = true
	return directory + `\speechcraft.exe`, nil
}
func (h *fakeHost) BackupLauncher() ([]byte, bool, error) {
	h.note("backup-launcher")
	return h.exeBackup, h.hadExe, nil
}
func (h *fakeHost) RestoreLauncher(data []byte, existed bool) error {
	h.note("restore-launcher")
	h.exeBackup = data
	h.hadExe = existed
	return nil
}
func (h *fakeHost) RemoveConfig() error {
	h.note("remove-config")
	h.hasConfig = false
	h.config = config.Installation{}
	return nil
}
func (h *fakeHost) RemoveLauncher() error     { h.note("remove-launcher"); return nil }
func (h *fakeHost) ReadPath() (string, error) { return h.path, nil }
func (h *fakeHost) WritePath(value string) error {
	h.note("write-path")
	h.path = value
	return nil
}
func (h *fakeHost) Resolve(pathEnv, name string) (string, error) {
	h.note("resolve")
	if h.resolved != "" && strings.Contains(strings.ToLower(pathEnv), strings.ToLower(`SpeechCraft\launcher`)) {
		return h.resolved, nil
	}
	for _, entry := range splitPath(pathEnv) {
		if strings.EqualFold(entry, `C:\Other`) {
			return `C:\Other\` + name, nil
		}
		if sameDir(entry, `C:\Users\Ada\AppData\Local\SpeechCraft\launcher`) {
			return entry + `\` + name, nil
		}
	}
	return "", nil
}

func firstArchive(request TransferRequest) string {
	for _, value := range request.Files {
		if strings.HasSuffix(value, ".tar.gz") {
			return value
		}
	}
	return ""
}

func ubuntuGuest(name, version string) Guest {
	return Guest{
		Name: name, Kernel: "6.6.87.2-microsoft-standard-WSL2",
		OSRelease: "ID=ubuntu\nVERSION_ID=\"" + version + "\"\n", Arch: "x86_64",
		User: "ada", UID: 1000, Home: "/home/ada", FreeBytes: 4 << 30, Glibc: "2.35",
	}
}

func happyHost() *fakeHost {
	archive := []byte("linux-archive-bytes")
	sum := HashBytes(archive)
	release := fmt.Sprintf(`{"schema_version":1,"archive":"speechcraft-0.1.0-linux-x86_64.tar.gz","archive_size_bytes":%d,"archive_sha256":"%s","application_version":"0.1.0","platform":"linux-x86_64","source_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`, len(archive), sum)
	archiveName := `C:\Users\Ada\Downloads\speechcraft-0.1.0-linux-x86_64.tar.gz`
	return &fakeHost{
		wsl: true, names: []string{"Ubuntu-24.04"},
		guests: map[string]Guest{"Ubuntu-24.04": ubuntuGuest("Ubuntu-24.04", "24.04")},
		files: map[string][]byte{
			archiveName:                   archive,
			archiveName + ".sha256":       []byte(sum + "  speechcraft-0.1.0-linux-x86_64.tar.gz\n"),
			archiveName + ".release.json": []byte(release),
			`C:\pkg\install-linux.py`:     []byte("print('installer')\n"),
			`C:\pkg\speechcraft.exe`:      []byte("MZ"),
		},
		path:      `C:\Windows`,
		helpOut:   "Usage: speechcraft\n",
		linuxData: true,
	}
}

func second(_ *config.Installation, err error) error { return err }

func opts() Options {
	return Options{
		Archive:         `C:\Users\Ada\Downloads\speechcraft-0.1.0-linux-x86_64.tar.gz`,
		InstallerScript: `C:\pkg\install-linux.py`, LauncherExecutable: `C:\pkg\speechcraft.exe`,
		ExpectedSourceRevision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		StagingID:              "abcdef0123456789",
	}
}

func codeOf(t *testing.T, err error) string {
	t.Helper()
	var diagnostic *diag.Error
	if !errors.As(err, &diagnostic) {
		t.Fatalf("expected diagnostic, got %v", err)
	}
	return diagnostic.Code
}

func TestFreshInstallSelectsTheOnlyUbuntuAndRegistersPath(t *testing.T) {
	host := happyHost()
	cfg, err := Install(host, opts())
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Distribution != "Ubuntu-24.04" || !cfg.LauncherInstalled || cfg.LinuxUser != "ada" {
		t.Fatalf("%#v", cfg)
	}
	if !strings.Contains(host.path, `SpeechCraft\launcher`) {
		t.Fatal(host.path)
	}
	if strings.Count(host.path, `SpeechCraft\launcher`) != 1 {
		t.Fatal("duplicate path entry")
	}
	joined := strings.Join(host.calls, "\n")
	if strings.Contains(joined, "unregister") || strings.Contains(joined, "/home/ada/.local/share/speechcraft/data") {
		t.Fatal(joined)
	}
	if !host.linuxData {
		t.Fatal("user data flag changed")
	}
}

func TestMultipleDistrosRequireAnExplicitChoice(t *testing.T) {
	host := happyHost()
	host.names = []string{"Ubuntu-22.04", "Ubuntu-24.04"}
	host.guests["Ubuntu-22.04"] = ubuntuGuest("Ubuntu-22.04", "22.04")
	if codeOf(t, second(Install(host, opts()))) != "MULTIPLE_DISTROS" {
		t.Fatal(host.calls)
	}
	choice := opts()
	choice.Distribution = "Ubuntu-22.04"
	cfg, err := Install(host, choice)
	if err != nil || cfg.Distribution != "Ubuntu-22.04" {
		t.Fatal(err, cfg)
	}
}

func TestSavedDistributionIsNotSilentlyChanged(t *testing.T) {
	host := happyHost()
	host.names = []string{"Ubuntu-22.04", "Ubuntu-24.04"}
	host.guests["Ubuntu-22.04"] = ubuntuGuest("Ubuntu-22.04", "22.04")
	host.hasConfig = true
	host.config = config.Installation{SchemaVersion: 1, Distribution: "Ubuntu-22.04", GuestID: "ubuntu", WSLVersion: 2, LinuxUser: "ada", LinuxHome: "/home/ada", LinuxCommand: "/home/ada/.local/bin/speechcraft", LauncherInstalled: true}
	cfg, err := Install(host, opts())
	if err != nil || cfg.Distribution != "Ubuntu-22.04" {
		t.Fatal(err, cfg)
	}
	host.names = []string{"Ubuntu-24.04"}
	if codeOf(t, second(Install(host, opts()))) != "DISTRO_MISSING" {
		t.Fatal("switched away from the saved distribution")
	}
}

func TestRejectsMissingWSLWSL1RootAndBadArchives(t *testing.T) {
	missing := happyHost()
	missing.wsl = false
	if codeOf(t, second(Install(missing, opts()))) != "WSL_NOT_FOUND" {
		t.Fatal()
	}
	wsl1 := happyHost()
	guest := wsl1.guests["Ubuntu-24.04"]
	guest.Kernel = "4.4.0-19041-Microsoft"
	wsl1.guests["Ubuntu-24.04"] = guest
	if codeOf(t, second(Install(wsl1, opts()))) != "WSL_VERSION_UNSUPPORTED" {
		t.Fatal()
	}
	root := happyHost()
	guest = root.guests["Ubuntu-24.04"]
	guest.User, guest.UID, guest.Home = "root", 0, "/root"
	root.guests["Ubuntu-24.04"] = guest
	if codeOf(t, second(Install(root, opts()))) != "ROOT_USER_UNSUPPORTED" {
		t.Fatal()
	}
	arm := happyHost()
	arm.arch = "arm64"
	if codeOf(t, second(Install(arm, opts()))) != "WINDOWS_ARCH_UNSUPPORTED" {
		t.Fatal()
	}
	bad := happyHost()
	bad.files[opts().Archive] = []byte("corrupt")
	if codeOf(t, second(Install(bad, opts()))) != "ARCHIVE_CHECKSUM_INVALID" {
		t.Fatal()
	}
	gone := happyHost()
	delete(gone.files, opts().Archive)
	if codeOf(t, second(Install(gone, opts()))) != "ARCHIVE_MISSING" {
		t.Fatal()
	}
}

func TestTransferAndLinuxFailuresDoNotTouchPathOrUserData(t *testing.T) {
	interrupted := happyHost()
	interrupted.transfer = errors.New("copy interrupted")
	original := interrupted.path
	if codeOf(t, second(Install(interrupted, opts()))) != "TRANSFER_FAILED" {
		t.Fatal()
	}
	if interrupted.path != original || !containsCall(interrupted.calls, "remove-staging") || containsCall(interrupted.calls, "write-path") {
		t.Fatal(interrupted.calls, interrupted.path)
	}
	corrupt := happyHost()
	corrupt.verify = errors.New("hash mismatch")
	if codeOf(t, second(Install(corrupt, opts()))) != "TRANSFER_CORRUPT" {
		t.Fatal()
	}
	if containsCall(corrupt.calls, "run:") {
		t.Fatal("installer ran after a corrupt transfer")
	}
	failed := happyHost()
	failed.runExit = 1
	if codeOf(t, second(Install(failed, opts()))) != "LINUX_INSTALL_FAILED" {
		t.Fatal()
	}
	if containsCall(failed.calls, "write-path") || containsCall(failed.calls, "save-config") {
		t.Fatal(failed.calls)
	}
}

func TestCommandConflictAndPathRollback(t *testing.T) {
	conflict := happyHost()
	conflict.path = `C:\Other`
	if codeOf(t, second(Install(conflict, opts()))) != "COMMAND_CONFLICT" {
		t.Fatal()
	}
	if containsCall(conflict.calls, "transfer") {
		t.Fatal("transferred despite a command conflict")
	}
	rollback := happyHost()
	rollback.resolved = `C:\SomewhereElse\speechcraft.exe`
	if codeOf(t, second(Install(rollback, opts()))) != "PATH_VERIFY_FAILED" {
		t.Fatal()
	}
	if rollback.path != `C:\Windows` || rollback.hasConfig {
		t.Fatalf("windows launcher state was not restored: path %s config %v", rollback.path, rollback.hasConfig)
	}
	if !containsCall(rollback.calls, "restore-launcher") || containsCall(rollback.calls, "delete-data") {
		t.Fatal(rollback.calls)
	}
}

func TestReinstallAndUninstallPreserveLinuxData(t *testing.T) {
	host := happyHost()
	host.path = `C:\Windows;C:\Users\Ada\AppData\Local\SpeechCraft\launcher`
	if _, err := Install(host, opts()); err != nil {
		t.Fatal(err)
	}
	if strings.Count(host.path, `SpeechCraft\launcher`) != 1 {
		t.Fatal(host.path)
	}
	if err := Uninstall(host); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(host.path, "SpeechCraft") || host.config.LauncherInstalled {
		t.Fatal(host.path, host.config.LauncherInstalled)
	}
	joined := strings.Join(host.calls, "\n")
	if strings.Contains(joined, "unregister") || strings.Contains(joined, "delete-data") {
		t.Fatal(joined)
	}
}

func TestUpgradeKeepsTheSelectedDistribution(t *testing.T) {
	host := happyHost()
	host.hasConfig = true
	host.config = config.Installation{Distribution: "Ubuntu-24.04"}
	host.names = []string{"Ubuntu-24.04", "Ubuntu-22.04"}
	host.guests["Ubuntu-22.04"] = ubuntuGuest("Ubuntu-22.04", "22.04")
	cfg, err := Install(host, opts())
	if err != nil || cfg.Distribution != "Ubuntu-24.04" {
		t.Fatal(err, cfg)
	}
}

func TestPathRegistryTypePreservesTheExistingKind(t *testing.T) {
	if PathRegistryType(1) != 1 || PathRegistryType(2) != 2 || PathRegistryType(0) != 2 {
		t.Fatal(PathRegistryType(1), PathRegistryType(0))
	}
}

func containsCall(calls []string, prefix string) bool {
	for _, call := range calls {
		if strings.Contains(call, prefix) {
			return true
		}
	}
	return false
}
