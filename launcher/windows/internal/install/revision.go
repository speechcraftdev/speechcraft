package install

import "encoding/json"

func sourceRevision(body []byte) string {
	var payload struct {
		SourceRevision string `json:"source_revision"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return ""
	}
	return payload.SourceRevision
}
