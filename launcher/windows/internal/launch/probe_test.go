package launch

import (
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestAuthenticateFrontendRejectsUnrelatedServers(t *testing.T) {
	challenge := "challenge-value-0123456789"
	proof := "expected-proof-value"
	do := func(request *http.Request) (*http.Response, error) {
		if request.URL.Path != "/api/speechcraft-launcher/verify" {
			t.Fatal(request.URL.Path)
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"proof":"other"}`)), Header: make(http.Header)}, nil
	}
	if err := AuthenticateFrontend("http://127.0.0.1:3002/login", challenge, proof, do); err == nil {
		t.Fatal("forged proof was accepted")
	}
	ok := func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"proof":"` + proof + `"}`)), Header: make(http.Header)}, nil
	}
	if err := AuthenticateFrontend("http://127.0.0.1:3002/login", challenge, proof, ok); err != nil {
		t.Fatal(err)
	}
	redirect := func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 302, Body: io.NopCloser(strings.NewReader("")), Header: make(http.Header)}, nil
	}
	if err := AuthenticateFrontend("http://127.0.0.1:3002/login", challenge, proof, redirect); err == nil {
		t.Fatal("redirect was accepted")
	}
	if err := AuthenticateFrontend("http://127.0.0.1:3002/login", "short", proof, ok); err == nil {
		t.Fatal("stale or short challenge was accepted")
	}
}
