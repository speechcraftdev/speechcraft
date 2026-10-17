package install

import "testing"

func TestDecodeWSLListWithoutAssumingEnglishHeaders(t *testing.T) {
	raw := []byte{0xFF, 0xFE}
	for _, r := range "Ubuntu-24.04\nUbuntu-22.04\n" {
		raw = append(raw, byte(r), 0)
	}
	names := SanitizeDistroNames(DecodeCommandOutput(raw))
	if len(names) != 2 || names[0] != "Ubuntu-24.04" || names[1] != "Ubuntu-22.04" {
		t.Fatal(names)
	}
	unsafe := SanitizeDistroNames("Ubuntu-24.04\nNot Ubuntu\n../escape\n")
	if len(unsafe) != 1 || unsafe[0] != "Ubuntu-24.04" {
		t.Fatal(unsafe)
	}
}
