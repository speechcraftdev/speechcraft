package install

import "testing"

func TestRequireWindowsReleaseFailsClosed(t *testing.T) {
	if _, err := RequireWindowsRelease(nil, ""); err == nil {
		t.Fatal("missing metadata accepted")
	}
	if _, err := RequireWindowsRelease([]byte("{"), ""); err == nil {
		t.Fatal("malformed metadata accepted")
	}
	good := []byte(`{"schema_version":1,"version":"0.1.0","platform":"windows-x64","source_revision":"` + "a" + `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","launcher":{"target":"windows/amd64","sha256":"` + "b" + `bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"linux_payload":{"platform":"linux-x86_64"},"signing":{"status":"unsigned_development_build"}}`)
	// The concatenation above is hard to read; build it explicitly.
	body := []byte(`{"schema_version":1,"version":"0.1.0","platform":"windows-x64","source_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","launcher":{"target":"windows/amd64","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"linux_payload":{"platform":"linux-x86_64"},"signing":{"status":"unsigned_development_build"}}`)
	binding, err := RequireWindowsRelease(body, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
	if err != nil || binding.Version != "0.1.0" {
		t.Fatal(err, binding)
	}
	if _, err := RequireWindowsRelease(body, "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"); err == nil {
		t.Fatal("wrong binary identity accepted")
	}
	if _, err := RequireWindowsRelease([]byte(`{"schema_version":1,"version":"0.1.0","platform":"windows-x64","source_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","launcher":{"target":"windows/amd64","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"linux_payload":{"platform":"windows-x64"}}`), ""); err == nil {
		t.Fatal("non-linux payload accepted")
	}
	_ = good
}
