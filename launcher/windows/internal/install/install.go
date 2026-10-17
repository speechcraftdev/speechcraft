package install

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"path"
	"strconv"
	"strings"

	"speechcraft/windows/internal/config"
	"speechcraft/windows/internal/diag"
)

const (
	minimumGlibc = "2.28"
	stagingMark  = "/.local/share/speechcraft/install-staging/"
)

type Guest struct {
	Name      string
	Kernel    string
	OSRelease string
	Arch      string
	User      string
	UID       int
	Home      string
	FreeBytes int64
	Glibc     string
}

type Staging struct {
	LinuxDir string
	Archive  string
	Owned    bool
}

type CommandResult struct {
	ExitCode int
	Stdout   string
	Stderr   string
}

type TransferRequest struct {
	Distro    string
	User      string
	Home      string
	StagingID string
	Files     map[string]string
}

type Host interface {
	Architecture() (string, error)
	WSLPath() (string, error)
	DistroNames() ([]string, error)
	Inspect(distro string) (Guest, error)
	ReadFile(path string) ([]byte, error)
	Size(path string) (int64, error)
	SHA256(path string) (string, error)
	Transfer(request TransferRequest) (Staging, error)
	VerifyGuestFile(distro, linuxPath, sha string) error
	Run(distro, user, home string, argv []string) (CommandResult, error)
	RemoveStaging(distro string, staging Staging) error
	LoadConfig() (config.Installation, bool, error)
	SaveConfig(config.Installation) error
	LauncherDirectory() (string, error)
	InstallExecutable(source, directory string) (string, error)
	RemoveLauncher() error
	ReadPath() (string, error)
	WritePath(value string) error
	Resolve(pathEnv, name string) (string, error)
}

type Options struct {
	Distribution           string
	Archive                string
	InstallerScript        string
	LauncherExecutable     string
	ExpectedSourceRevision string
	StagingID              string
}

type ReleaseFile struct {
	SchemaVersion      int    `json:"schema_version"`
	Archive            string `json:"archive"`
	ArchiveSizeBytes   int64  `json:"archive_size_bytes"`
	ArchiveSHA256      string `json:"archive_sha256"`
	ApplicationVersion string `json:"application_version"`
	Platform           string `json:"platform"`
	SourceRevision     string `json:"source_revision"`
}

func Install(host Host, opts Options) (*config.Installation, error) {
	arch, err := host.Architecture()
	if err != nil {
		return nil, err
	}
	if !strings.EqualFold(arch, "amd64") {
		return nil, diag.New("WINDOWS_ARCH_UNSUPPORTED", "SpeechCraft for Windows requires an x64 Windows host. Windows ARM64 is not a supported host for this package.", diag.ExitIntegration)
	}
	if _, err := host.WSLPath(); err != nil {
		return nil, err
	}
	names, err := host.DistroNames()
	if err != nil {
		return nil, err
	}
	var guests []Guest
	for _, name := range names {
		guest, inspectErr := host.Inspect(name)
		if inspectErr != nil {
			return nil, inspectErr
		}
		guest.Name = name
		guests = append(guests, guest)
	}
	saved, hasSaved, err := host.LoadConfig()
	if err != nil {
		return nil, err
	}
	savedName := ""
	if hasSaved {
		savedName = saved.Distribution
	}
	selected, err := selectGuest(guests, opts.Distribution, savedName)
	if err != nil {
		return nil, err
	}
	release, archiveSHA, err := verifyWindowsArchive(host, opts)
	if err != nil {
		return nil, err
	}
	required := requiredFreeBytes(fileSize(host, opts.Archive))
	if selected.FreeBytes < required {
		return nil, diag.New("STORAGE_CAPACITY_INSUFFICIENT", fmt.Sprintf("Ubuntu %s has %d bytes free; installing this archive needs about %d bytes on the Linux filesystem.", selected.Name, selected.FreeBytes, required), diag.ExitIntegration)
	}
	launcherDir, err := host.LauncherDirectory()
	if err != nil {
		return nil, err
	}
	currentPath, err := host.ReadPath()
	if err != nil {
		return nil, err
	}
	if existing, resolveErr := host.Resolve(currentPath, "speechcraft.exe"); resolveErr == nil && existing != "" && !sameDir(dirOf(existing), launcherDir) {
		return nil, diag.New("COMMAND_CONFLICT", "Another program named speechcraft is already on the user PATH. SpeechCraft will not replace it.", diag.ExitIntegration)
	}
	stagingID := opts.StagingID
	if stagingID == "" {
		stagingID = archiveSHA[:32]
	}
	if !safeStagingID(stagingID) {
		return nil, diag.New("STAGING_UNSAFE", "The installer refused to use an unsafe staging directory.", diag.ExitIntegration)
	}
	request := TransferRequest{
		Distro: selected.Name, User: selected.User, Home: selected.Home, StagingID: stagingID,
		Files: map[string]string{
			archiveBase(opts.Archive):                   opts.Archive,
			archiveBase(opts.Archive) + ".sha256":       opts.Archive + ".sha256",
			archiveBase(opts.Archive) + ".release.json": opts.Archive + ".release.json",
			"install-linux.py":                          opts.InstallerScript,
		},
	}
	staging, transferErr := host.Transfer(request)
	if transferErr != nil {
		if staging.Owned {
			_ = host.RemoveStaging(selected.Name, staging)
		}
		return nil, diag.New("TRANSFER_FAILED", transferErr.Error(), diag.ExitIntegration)
	}
	if !stagingOwnedBy(selected.Home, staging) {
		return nil, diag.New("STAGING_UNSAFE", "The staged archive is not inside the Linux install-staging directory.", diag.ExitIntegration)
	}
	if err := host.VerifyGuestFile(selected.Name, staging.Archive, archiveSHA); err != nil {
		_ = host.RemoveStaging(selected.Name, staging)
		return nil, diag.New("TRANSFER_CORRUPT", "The archive copied into Ubuntu did not match its SHA-256. Installation did not run.", diag.ExitIntegration)
	}
	installerPath := path.Join(staging.LinuxDir, "install-linux.py")
	result, err := host.Run(selected.Name, selected.User, selected.Home, []string{"/usr/bin/python3", installerPath, staging.Archive})
	if err != nil || result.ExitCode != 0 {
		_ = host.RemoveStaging(selected.Name, staging)
		message := "The Linux installer failed."
		if err != nil {
			message = err.Error()
		} else if result.Stderr != "" {
			message = strings.TrimSpace(result.Stderr)
		}
		return nil, diag.New("LINUX_INSTALL_FAILED", message, diag.ExitIntegration)
	}
	command := selected.Home + "/.local/bin/speechcraft"
	helpResult, err := host.Run(selected.Name, selected.User, selected.Home, []string{command, "--help"})
	if err != nil || helpResult.ExitCode != 0 || !strings.Contains(helpResult.Stdout, "Usage: speechcraft") {
		_ = host.RemoveStaging(selected.Name, staging)
		return nil, diag.New("LINUX_COMMAND_MISSING", "The Linux speechcraft command was not available after installation.", diag.ExitIntegration)
	}
	installedExe, err := host.InstallExecutable(opts.LauncherExecutable, launcherDir)
	if err != nil {
		_ = host.RemoveStaging(selected.Name, staging)
		return nil, diag.New("LAUNCHER_INSTALL_FAILED", err.Error(), diag.ExitIntegration)
	}
	cfg := config.Installation{
		SchemaVersion: 1, Distribution: selected.Name, GuestID: "ubuntu", WSLVersion: 2,
		LinuxUser: selected.User, LinuxHome: selected.Home, LinuxCommand: command,
		ApplicationVersion: release.ApplicationVersion, ArchiveSHA256: archiveSHA,
		SourceRevision: release.SourceRevision, LauncherInstalled: true,
	}
	if err := host.SaveConfig(cfg); err != nil {
		_ = host.RemoveStaging(selected.Name, staging)
		return nil, err
	}
	updated := addPathEntry(currentPath, launcherDir)
	if err := host.WritePath(updated); err != nil {
		_ = host.RemoveStaging(selected.Name, staging)
		return nil, diag.New("PATH_UPDATE_FAILED", err.Error(), diag.ExitIntegration)
	}
	resolved, err := host.Resolve(updated, "speechcraft.exe")
	if err != nil || !sameDir(dirOf(resolved), launcherDir) || !strings.EqualFold(resolved, installedExe) {
		_ = host.WritePath(currentPath)
		_ = host.RemoveStaging(selected.Name, staging)
		return nil, diag.New("PATH_VERIFY_FAILED", "The user PATH did not resolve to the installed SpeechCraft launcher. The previous PATH was restored. Already-open terminals still need to be restarted after a later successful install.", diag.ExitIntegration)
	}
	_ = host.RemoveStaging(selected.Name, staging)
	return &cfg, nil
}

func Uninstall(host Host) error {
	cfg, ok, err := host.LoadConfig()
	if err != nil {
		return err
	}
	launcherDir, err := host.LauncherDirectory()
	if err != nil {
		return err
	}
	current, err := host.ReadPath()
	if err != nil {
		return err
	}
	if err := host.WritePath(removePathEntry(current, launcherDir)); err != nil {
		return err
	}
	if err := host.RemoveLauncher(); err != nil {
		return err
	}
	if ok {
		cfg.LauncherInstalled = false
		if err := host.SaveConfig(cfg); err != nil {
			return err
		}
	}
	return nil
}

func selectGuest(guests []Guest, requested, saved string) (Guest, error) {
	if requested != "" {
		guest, ok := findGuest(guests, requested)
		if !ok {
			return Guest{}, diag.New("DISTRO_NOT_INSTALLED", "The requested Ubuntu distribution is not installed. SpeechCraft will not install one automatically.", diag.ExitIntegration)
		}
		if err := guestError(guest); err != nil {
			return Guest{}, err
		}
		return guest, nil
	}
	if saved != "" {
		guest, ok := findGuest(guests, saved)
		if !ok {
			return Guest{}, diag.New("DISTRO_MISSING", "The previously selected Ubuntu distribution is no longer installed. SpeechCraft will not switch to a different distribution.", diag.ExitIntegration)
		}
		if err := guestError(guest); err != nil {
			return Guest{}, err
		}
		return guest, nil
	}
	var suitable []Guest
	for _, guest := range guests {
		if guestError(guest) == nil {
			suitable = append(suitable, guest)
		}
	}
	switch len(suitable) {
	case 0:
		if len(guests) == 1 {
			return Guest{}, guestError(guests[0])
		}
		return Guest{}, diag.New("NO_SUPPORTED_DISTRO", "No Ubuntu 22.04 or 24.04 WSL2 distribution with a non-root user was found. SpeechCraft will not install a distribution or accept WSL1.", diag.ExitIntegration)
	case 1:
		return suitable[0], nil
	default:
		names := make([]string, 0, len(suitable))
		for _, guest := range suitable {
			names = append(names, guest.Name)
		}
		return Guest{}, diag.New("MULTIPLE_DISTROS", "More than one suitable Ubuntu distribution is installed ("+strings.Join(names, ", ")+"). Choose one explicitly. SpeechCraft will not guess.", diag.ExitIntegration)
	}
}

func findGuest(guests []Guest, name string) (Guest, bool) {
	for _, guest := range guests {
		if guest.Name == name {
			return guest, true
		}
	}
	return Guest{}, false
}

func guestError(guest Guest) error {
	kernel := strings.ToLower(guest.Kernel)
	if strings.Contains(kernel, "microsoft") && !strings.Contains(kernel, "microsoft-standard-wsl2") {
		return diag.New("WSL_VERSION_UNSUPPORTED", guest.Name+" is not WSL2. Upgrade that distribution to WSL2. WSL1 is not supported.", diag.ExitIntegration)
	}
	if !strings.Contains(kernel, "microsoft-standard-wsl2") {
		return diag.New("WSL_VERSION_UNSUPPORTED", guest.Name+" did not report a WSL2 kernel.", diag.ExitIntegration)
	}
	id, version := parseOSRelease(guest.OSRelease)
	if id != "ubuntu" || (version != "22.04" && version != "24.04") {
		return diag.New("DISTRO_UNSUPPORTED", guest.Name+" is not Ubuntu 22.04 or 24.04.", diag.ExitIntegration)
	}
	if guest.Arch != "x86_64" && guest.Arch != "amd64" {
		return diag.New("ARCHITECTURE_UNSUPPORTED", guest.Name+" is "+guest.Arch+". SpeechCraft requires Linux x86-64 inside WSL.", diag.ExitIntegration)
	}
	if guest.UID == 0 || guest.User == "root" || guest.User == "" {
		return diag.New("ROOT_USER_UNSUPPORTED", guest.Name+" does not have an ordinary non-root default user. Create one before installing SpeechCraft.", diag.ExitIntegration)
	}
	if guest.Home != "/home/"+guest.User {
		return diag.New("LINUX_HOME_UNSUPPORTED", "The Linux home directory must be /home/"+guest.User+".", diag.ExitIntegration)
	}
	if compareVersions(guest.Glibc, minimumGlibc) < 0 {
		return diag.New("GLIBC_INCOMPATIBLE", "glibc "+guest.Glibc+" is older than "+minimumGlibc+".", diag.ExitIntegration)
	}
	return nil
}

func verifyWindowsArchive(host Host, opts Options) (ReleaseFile, string, error) {
	if opts.Archive == "" || opts.InstallerScript == "" || opts.LauncherExecutable == "" {
		return ReleaseFile{}, "", diag.New("ARCHIVE_MISSING", "The Linux archive, its installer, and the Windows launcher executable are required.", diag.ExitIntegration)
	}
	sumText, err := host.ReadFile(opts.Archive + ".sha256")
	if err != nil {
		return ReleaseFile{}, "", diag.New("ARCHIVE_MISSING", "The archive SHA-256 file is missing.", diag.ExitIntegration)
	}
	releaseText, err := host.ReadFile(opts.Archive + ".release.json")
	if err != nil {
		return ReleaseFile{}, "", diag.New("ARCHIVE_MISSING", "The archive release manifest is missing.", diag.ExitIntegration)
	}
	expected := strings.Fields(string(sumText))
	if len(expected) == 0 || len(expected[0]) != 64 {
		return ReleaseFile{}, "", diag.New("ARCHIVE_CHECKSUM_INVALID", "The checksum file does not contain a SHA-256.", diag.ExitIntegration)
	}
	actual, err := host.SHA256(opts.Archive)
	if err != nil {
		return ReleaseFile{}, "", diag.New("ARCHIVE_MISSING", err.Error(), diag.ExitIntegration)
	}
	if !strings.EqualFold(actual, expected[0]) {
		return ReleaseFile{}, "", diag.New("ARCHIVE_CHECKSUM_INVALID", "The Linux archive does not match its SHA-256.", diag.ExitIntegration)
	}
	var release ReleaseFile
	if err := json.Unmarshal(releaseText, &release); err != nil {
		return ReleaseFile{}, "", diag.New("ARCHIVE_MANIFEST_INVALID", "The Linux release manifest is not valid JSON.", diag.ExitIntegration)
	}
	base := archiveBase(opts.Archive)
	if release.SchemaVersion != 1 || release.Platform != "linux-x86_64" || release.Archive != base || !strings.EqualFold(release.ArchiveSHA256, actual) {
		return ReleaseFile{}, "", diag.New("ARCHIVE_MANIFEST_INVALID", "The Linux release manifest does not match this x86-64 archive.", diag.ExitIntegration)
	}
	if opts.ExpectedSourceRevision != "" && release.SourceRevision != opts.ExpectedSourceRevision {
		return ReleaseFile{}, "", diag.New("RELEASE_IDENTITY_MISMATCH", "The Linux archive was built from a different source revision than this Windows installer.", diag.ExitIntegration)
	}
	if release.ArchiveSizeBytes != 0 && release.ArchiveSizeBytes != fileSize(host, opts.Archive) {
		return ReleaseFile{}, "", diag.New("ARCHIVE_MANIFEST_INVALID", "The Linux archive size does not match its release manifest.", diag.ExitIntegration)
	}
	return release, strings.ToLower(actual), nil
}

func fileSize(host Host, filename string) int64 {
	size, err := host.Size(filename)
	if err != nil {
		return 0
	}
	return size
}

func requiredFreeBytes(size int64) int64 {
	need := size*3 + 512*1024*1024
	if need < 2*1024*1024*1024 {
		return 2 * 1024 * 1024 * 1024
	}
	return need
}

func parseOSRelease(text string) (string, string) {
	values := map[string]string{}
	for _, line := range strings.Split(text, "\n") {
		key, value, ok := strings.Cut(strings.TrimSpace(line), "=")
		if !ok {
			continue
		}
		value = strings.Trim(value, `"'`)
		values[key] = value
	}
	return values["ID"], values["VERSION_ID"]
}

func compareVersions(left, right string) int {
	left = strings.TrimPrefix(strings.ToLower(strings.TrimSpace(left)), "glibc ")
	right = strings.TrimPrefix(strings.ToLower(strings.TrimSpace(right)), "glibc ")
	a := strings.Split(left, ".")
	b := strings.Split(right, ".")
	n := len(a)
	if len(b) > n {
		n = len(b)
	}
	for i := 0; i < n; i++ {
		av, bv := 0, 0
		if i < len(a) {
			av, _ = strconv.Atoi(a[i])
		}
		if i < len(b) {
			bv, _ = strconv.Atoi(b[i])
		}
		if av != bv {
			if av < bv {
				return -1
			}
			return 1
		}
	}
	return 0
}

func addPathEntry(current, dir string) string {
	for _, entry := range splitPath(current) {
		if sameDir(entry, dir) {
			return current
		}
	}
	if strings.Trim(current, ";") == "" {
		return dir
	}
	return strings.TrimRight(current, ";") + ";" + dir
}

func removePathEntry(current, dir string) string {
	var kept []string
	for _, entry := range splitPath(current) {
		if sameDir(entry, dir) {
			continue
		}
		kept = append(kept, entry)
	}
	return strings.Join(kept, ";")
}

func splitPath(value string) []string {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	parts := strings.Split(value, ";")
	var kept []string
	for _, part := range parts {
		if strings.TrimSpace(part) != "" {
			kept = append(kept, part)
		}
	}
	return kept
}

func sameDir(left, right string) bool {
	return strings.EqualFold(strings.TrimRight(left, `\/`), strings.TrimRight(right, `\/`))
}

func dirOf(filename string) string {
	slash := strings.ReplaceAll(filename, "/", `\`)
	index := strings.LastIndex(slash, `\`)
	if index < 0 {
		return ""
	}
	return slash[:index]
}

func archiveBase(filename string) string {
	slash := strings.ReplaceAll(filename, `\`, "/")
	return path.Base(slash)
}

func safeStagingID(value string) bool {
	if len(value) < 16 || len(value) > 64 {
		return false
	}
	for _, r := range value {
		if (r < 'a' || r > 'z') && (r < '0' || r > '9') {
			return false
		}
	}
	return true
}

func stagingOwnedBy(home string, staging Staging) bool {
	if !staging.Owned || home == "" {
		return false
	}
	prefix := strings.TrimRight(home, "/") + stagingMark
	return strings.HasPrefix(staging.LinuxDir, prefix) && staging.LinuxDir != prefix && strings.HasPrefix(staging.Archive, staging.LinuxDir+"/")
}

func HashBytes(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}
