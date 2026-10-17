//go:build windows

package install

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"unsafe"

	"speechcraft/windows/internal/config"
)

type windowsHost struct {
	getenv func(string) string
	wsl    string
}

func newWindowsHost(getenv func(string) string) (*windowsHost, error) {
	root := getenv("SystemRoot")
	if root == "" {
		root = `C:\Windows`
	}
	wsl := filepath.Join(root, "System32", "wsl.exe")
	info, err := os.Stat(wsl)
	if err != nil || info.IsDir() {
		return nil, errors.New("wsl.exe was not found in Windows System32. Install WSL2 before running SpeechCraft. SpeechCraft will not install a distribution for you.")
	}
	return &windowsHost{getenv: getenv, wsl: wsl}, nil
}

func (h *windowsHost) Architecture() (string, error) {
	if strings.EqualFold(h.getenv("PROCESSOR_ARCHITECTURE"), "AMD64") {
		return "amd64", nil
	}
	return h.getenv("PROCESSOR_ARCHITECTURE"), nil
}

func (h *windowsHost) WSLPath() (string, error) { return h.wsl, nil }

func (h *windowsHost) DistroNames() ([]string, error) {
	output, err := h.output("--list", "--quiet")
	if err != nil {
		return nil, err
	}
	return SanitizeDistroNames(DecodeCommandOutput(output)), nil
}

func (h *windowsHost) Inspect(distro string) (Guest, error) {
	if !distroNamePattern.MatchString(distro) {
		return Guest{}, errors.New("refusing to inspect an unsafe distribution name")
	}
	kernel, err := h.text(distro, "/usr/bin/cat", "/proc/sys/kernel/osrelease")
	if err != nil {
		return Guest{}, err
	}
	release, err := h.text(distro, "/usr/bin/cat", "/etc/os-release")
	if err != nil {
		return Guest{}, err
	}
	arch, err := h.text(distro, "/usr/bin/uname", "-m")
	if err != nil {
		return Guest{}, err
	}
	user, err := h.text(distro, "/usr/bin/id", "-un")
	if err != nil {
		return Guest{}, err
	}
	uidText, err := h.text(distro, "/usr/bin/id", "-u")
	if err != nil {
		return Guest{}, err
	}
	uid, err := strconv.Atoi(strings.TrimSpace(uidText))
	if err != nil {
		return Guest{}, err
	}
	home, err := h.text(distro, "/usr/bin/printenv", "HOME")
	if err != nil {
		return Guest{}, err
	}
	glibc, err := h.text(distro, "/usr/bin/getconf", "GNU_LIBC_VERSION")
	if err != nil {
		return Guest{}, err
	}
	freeText, err := h.text(distro, "/usr/bin/df", "-B1", "--output=avail", strings.TrimSpace(home))
	if err != nil {
		return Guest{}, err
	}
	free, err := lastInteger(freeText)
	if err != nil {
		return Guest{}, err
	}
	return Guest{
		Name: distro, Kernel: strings.TrimSpace(kernel), OSRelease: release, Arch: strings.TrimSpace(arch),
		User: strings.TrimSpace(user), UID: uid, Home: strings.TrimSpace(home), FreeBytes: free, Glibc: strings.TrimSpace(glibc),
	}, nil
}

func (h *windowsHost) ReadFile(name string) ([]byte, error) { return os.ReadFile(name) }
func (h *windowsHost) Size(name string) (int64, error) {
	info, err := os.Stat(name)
	if err != nil {
		return 0, err
	}
	return info.Size(), nil
}
func (h *windowsHost) SHA256(name string) (string, error) {
	file, err := os.Open(name)
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

func (h *windowsHost) Transfer(request TransferRequest) (Staging, error) {
	if !safeStagingID(request.StagingID) || !strings.HasPrefix(request.Home, "/home/") {
		return Staging{}, errors.New("refusing to stage outside the Linux install-staging directory")
	}
	dir := request.Home + "/.local/share/speechcraft/install-staging/" + request.StagingID
	if _, err := h.guest(request.Distro, request.User, request.Home, "/usr/bin/mkdir", "-p", "--", dir); err != nil {
		return Staging{LinuxDir: dir, Owned: true}, err
	}
	var archive string
	for base, windowsPath := range request.Files {
		if strings.Contains(base, "/") || strings.Contains(base, `\`) || base == "" || base == "." || base == ".." {
			return Staging{LinuxDir: dir, Owned: true}, errors.New("refusing to stage an unsafe file name")
		}
		linuxSource, err := h.text(request.Distro, "/usr/bin/wslpath", "-u", windowsPath)
		if err != nil {
			return Staging{LinuxDir: dir, Owned: true}, err
		}
		linuxSource = strings.TrimSpace(linuxSource)
		if !strings.HasPrefix(linuxSource, "/mnt/") {
			return Staging{LinuxDir: dir, Owned: true}, errors.New("the archive input was not on a Windows drive")
		}
		destination := dir + "/" + base
		if _, err := h.guest(request.Distro, request.User, request.Home, "/usr/bin/cp", "--", linuxSource, destination); err != nil {
			return Staging{LinuxDir: dir, Owned: true, Archive: destination}, err
		}
		if strings.HasSuffix(base, ".tar.gz") {
			archive = destination
		}
	}
	return Staging{LinuxDir: dir, Archive: archive, Owned: true}, nil
}

func (h *windowsHost) VerifyGuestFile(distro, linuxPath, expected string) error {
	output, err := h.text(distro, "/usr/bin/sha256sum", "--", linuxPath)
	if err != nil {
		return err
	}
	fields := strings.Fields(output)
	if len(fields) == 0 || !strings.EqualFold(fields[0], expected) {
		return errors.New("staged archive checksum did not match")
	}
	return nil
}

func (h *windowsHost) Run(distro, user, home string, argv []string) (CommandResult, error) {
	output, err := h.guest(distro, user, home, argv...)
	result := CommandResult{Stdout: string(output)}
	if err == nil {
		return result, nil
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		result.ExitCode = exit.ExitCode()
		result.Stderr = string(exit.Stderr)
		return result, nil
	}
	return result, err
}

func (h *windowsHost) RemoveStaging(distro string, staging Staging) error {
	if !staging.Owned || !strings.Contains(staging.LinuxDir, "/.local/share/speechcraft/install-staging/") || strings.Contains(staging.LinuxDir, "..") {
		return errors.New("refusing to delete a path outside install-staging")
	}
	_, err := h.output("--distribution", distro, "--exec", "/usr/bin/rm", "-rf", "--", staging.LinuxDir)
	return err
}

func (h *windowsHost) LoadConfig() (config.Installation, bool, error) {
	filename, err := h.configPath()
	if err != nil {
		return config.Installation{}, false, err
	}
	cfg, err := config.Load(filename)
	if errors.Is(err, os.ErrNotExist) {
		return config.Installation{}, false, nil
	}
	if err != nil {
		return config.Installation{}, false, err
	}
	return cfg, true, nil
}

func (h *windowsHost) SaveConfig(cfg config.Installation) error {
	filename, err := h.configPath()
	if err != nil {
		return err
	}
	return config.Write(filename, cfg)
}

func (h *windowsHost) LauncherDirectory() (string, error) {
	base := h.getenv("LOCALAPPDATA")
	if base == "" {
		return "", errors.New("LOCALAPPDATA is not set")
	}
	return filepath.Join(base, "SpeechCraft", "launcher"), nil
}

func (h *windowsHost) InstallExecutable(source, directory string) (string, error) {
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return "", err
	}
	target := filepath.Join(directory, "speechcraft.exe")
	if strings.EqualFold(filepath.Clean(source), filepath.Clean(target)) {
		return target, nil
	}
	input, err := os.Open(source)
	if err != nil {
		return "", err
	}
	defer input.Close()
	output, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o700)
	if err != nil {
		return "", err
	}
	defer output.Close()
	if _, err := io.Copy(output, input); err != nil {
		return "", err
	}
	return target, nil
}

func (h *windowsHost) RemoveLauncher() error {
	dir, err := h.LauncherDirectory()
	if err != nil {
		return err
	}
	err = os.Remove(filepath.Join(dir, "speechcraft.exe"))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

func (h *windowsHost) ReadPath() (string, error) {
	value, _, err := readUserPath()
	return value, err
}
func (h *windowsHost) WritePath(value string) error {
	if err := writeUserPath(value); err != nil {
		return err
	}
	broadcastEnvironment()
	return nil
}

func (h *windowsHost) Resolve(pathEnv, name string) (string, error) {
	for _, entry := range splitPath(pathEnv) {
		candidate := filepath.Join(entry, name)
		info, err := os.Stat(candidate)
		if err == nil && info.Mode().IsRegular() {
			return candidate, nil
		}
	}
	return "", nil
}

func (h *windowsHost) configPath() (string, error) {
	return config.Path(h.getenv("LOCALAPPDATA"))
}

func (h *windowsHost) text(distro string, argv ...string) (string, error) {
	args := append([]string{"--distribution", distro, "--exec"}, argv...)
	output, err := h.output(args...)
	return string(output), err
}

func (h *windowsHost) guest(distro, user, home string, argv ...string) ([]byte, error) {
	args := []string{"--distribution", distro, "--user", user, "--cd", home, "--exec"}
	args = append(args, argv...)
	return h.output(args...)
}

func (h *windowsHost) output(args ...string) ([]byte, error) {
	cmd := exec.Command(h.wsl, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	if err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			exit.Stderr = stderr.Bytes()
			return stdout.Bytes(), exit
		}
		return stdout.Bytes(), err
	}
	return stdout.Bytes(), nil
}

func lastInteger(text string) (int64, error) {
	var value int64
	found := false
	for _, field := range strings.Fields(text) {
		parsed, err := strconv.ParseInt(field, 10, 64)
		if err == nil {
			value = parsed
			found = true
		}
	}
	if !found {
		return 0, errors.New("could not read free space from df")
	}
	return value, nil
}

const (
	keyQueryValue = 0x0001
	keySetValue   = 0x0002
	regExpandSZ   = 2
)

var (
	advapi32          = syscall.NewLazyDLL("advapi32.dll")
	procRegOpenKeyExW = advapi32.NewProc("RegOpenKeyExW")
	procRegQueryValue = advapi32.NewProc("RegQueryValueExW")
	procRegSetValue   = advapi32.NewProc("RegSetValueExW")
	procRegCloseKey   = advapi32.NewProc("RegCloseKey")
	user32            = syscall.NewLazyDLL("user32.dll")
	procSendMessage   = user32.NewProc("SendMessageTimeoutW")
)

func openEnvKey(access uint32) (syscall.Handle, error) {
	var key syscall.Handle
	subkey, err := syscall.UTF16PtrFromString("Environment")
	if err != nil {
		return 0, err
	}
	r, _, callErr := procRegOpenKeyExW.Call(uintptr(syscall.HKEY_CURRENT_USER), uintptr(unsafe.Pointer(subkey)), 0, uintptr(access), uintptr(unsafe.Pointer(&key)))
	if r != 0 {
		return 0, callErr
	}
	return key, nil
}

func readUserPath() (string, uint32, error) {
	key, err := openEnvKey(keyQueryValue)
	if err != nil {
		return "", 0, err
	}
	defer procRegCloseKey.Call(uintptr(key))
	name, err := syscall.UTF16PtrFromString("Path")
	if err != nil {
		return "", 0, err
	}
	var kind, size uint32
	r, _, _ := procRegQueryValue.Call(uintptr(key), uintptr(unsafe.Pointer(name)), 0, uintptr(unsafe.Pointer(&kind)), 0, uintptr(unsafe.Pointer(&size)))
	if r != 0 || size == 0 {
		return "", 0, nil
	}
	buffer := make([]uint16, size/2+1)
	r, _, callErr := procRegQueryValue.Call(uintptr(key), uintptr(unsafe.Pointer(name)), 0, uintptr(unsafe.Pointer(&kind)), uintptr(unsafe.Pointer(&buffer[0])), uintptr(unsafe.Pointer(&size)))
	if r != 0 {
		return "", 0, callErr
	}
	return syscall.UTF16ToString(buffer), kind, nil
}

func writeUserPath(value string) error {
	key, err := openEnvKey(keyQueryValue | keySetValue)
	if err != nil {
		return err
	}
	defer procRegCloseKey.Call(uintptr(key))
	name, err := syscall.UTF16PtrFromString("Path")
	if err != nil {
		return err
	}
	data, err := syscall.UTF16FromString(value)
	if err != nil {
		return err
	}
	_, existingType, _ := readUserPath()
	r, _, callErr := procRegSetValue.Call(uintptr(key), uintptr(unsafe.Pointer(name)), 0, uintptr(PathRegistryType(existingType)), uintptr(unsafe.Pointer(&data[0])), uintptr(len(data)*2))
	if r != 0 {
		return callErr
	}
	return nil
}

func broadcastEnvironment() {
	param, err := syscall.UTF16PtrFromString("Environment")
	if err != nil {
		return
	}
	const hwndBroadcast = 0xffff
	const wmSettingChange = 0x001A
	procSendMessage.Call(hwndBroadcast, wmSettingChange, 0, uintptr(unsafe.Pointer(param)), 0x0002, 1000, 0)
}
