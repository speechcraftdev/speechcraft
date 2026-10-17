package install

import (
	"strings"
	"unicode/utf16"
)

func DecodeCommandOutput(body []byte) string {
	if len(body) >= 2 && body[0] == 0xFF && body[1] == 0xFE {
		body = body[2:]
	}
	text := string(body)
	if len(body) >= 4 && body[1] == 0 && body[3] == 0 {
		units := make([]uint16, 0, len(body)/2)
		for index := 0; index+1 < len(body); index += 2 {
			units = append(units, uint16(body[index])|uint16(body[index+1])<<8)
		}
		text = string(utf16.Decode(units))
	}
	return strings.ReplaceAll(text, "\x00", "")
}

func SanitizeDistroNames(text string) []string {
	var names []string
	seen := map[string]bool{}
	for _, line := range strings.Split(text, "\n") {
		name := strings.TrimSpace(strings.Trim(line, "\r\x00"))
		if name == "" || seen[name] {
			continue
		}
		if !distroNamePattern.MatchString(name) {
			continue
		}
		seen[name] = true
		names = append(names, name)
	}
	return names
}
