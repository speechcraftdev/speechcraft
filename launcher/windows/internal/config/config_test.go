package config

import (
	"strings"
	"testing"
)

func TestValidateRejectsRootAndWindowsPaths(t *testing.T) {
	good := Installation{
		SchemaVersion: 1, Distribution: "Ubuntu-24.04", GuestID: "ubuntu", WSLVersion: 2, LinuxUser: "ada",
		LinuxHome: "/home/ada", LinuxCommand: "/home/ada/.local/bin/speechcraft",
		LauncherInstalled: true,
	}
	if err := Validate(good); err != nil {
		t.Fatal(err)
	}
	root := good
	root.LinuxUser = "root"
	root.LinuxHome = "/root"
	root.LinuxCommand = "/root/.local/bin/speechcraft"
	if err := Validate(root); err == nil {
		t.Fatal("root was accepted")
	}
	mounted := good
	mounted.LinuxCommand = "/mnt/c/speechcraft"
	if err := Validate(mounted); err == nil {
		t.Fatal("windows mount command was accepted")
	}
	decoded, err := Decode(strings.NewReader(`{"schema_version":1,"distribution":"Ubuntu-24.04","guest_id":"ubuntu","wsl_version":2,"linux_user":"ada","linux_home":"/home/ada","linux_command":"/home/ada/.local/bin/speechcraft","launcher_installed":true}`))
	if err != nil {
		t.Fatal(err)
	}
	if decoded.Distribution != "Ubuntu-24.04" {
		t.Fatal(decoded.Distribution)
	}
	if _, err := Decode(strings.NewReader(`{"schema_version":1,"distribution":"Ubuntu-24.04","guest_id":"ubuntu","wsl_version":2,"linux_user":"ada","linux_home":"/home/ada","linux_command":"/home/ada/.local/bin/speechcraft","launcher_installed":true,"token":"secret"}`)); err == nil {
		t.Fatal("unknown field was accepted")
	}
}
