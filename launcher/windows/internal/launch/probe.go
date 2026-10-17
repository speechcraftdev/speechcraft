package launch

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
)

func ProbeFrontend(rawURL string) error {
	return errors.New("refusing to open a frontend that has not proved its installation identity")
}

func AuthenticateFrontend(rawURL, challenge, expectedProof string, do func(*http.Request) (*http.Response, error)) error {
	route := strings.TrimPrefix(rawURL, "http://127.0.0.1:3002")
	if err := ValidateFrontendURL(rawURL, route); err != nil {
		return err
	}
	if len(challenge) < 16 || expectedProof == "" {
		return errors.New("frontend identity challenge is missing")
	}
	request, err := http.NewRequest(http.MethodPost, "http://127.0.0.1:3002/api/speechcraft-launcher/verify", strings.NewReader(`{"challenge":"`+challenge+`"}`))
	if err != nil {
		return err
	}
	request.Header.Set("content-type", "application/json")
	response, err := do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode >= 300 && response.StatusCode < 400 {
		return errors.New("frontend identity check refused a redirect")
	}
	var payload struct {
		Proof string `json:"proof"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 4096)).Decode(&payload); err != nil {
		return errors.New("frontend identity response was not the expected proof")
	}
	if subtle.ConstantTimeCompare([]byte(payload.Proof), []byte(expectedProof)) != 1 {
		return errors.New("frontend identity did not match the proof from the trusted WSL command")
	}
	return nil
}
