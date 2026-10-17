package launch

import (
	"errors"
	"io"
	"net/http"
	"strings"
	"time"
)

func ProbeFrontend(rawURL string) error {
	route := strings.TrimPrefix(rawURL, "http://127.0.0.1:3002")
	if err := ValidateFrontendURL(rawURL, route); err != nil {
		return err
	}
	client := &http.Client{
		Timeout: 3 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	response, err := client.Get("http://127.0.0.1:3002/api/speechcraft-launcher/health")
	if err != nil {
		return err
	}
	defer response.Body.Close()
	io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
	if response.StatusCode < 200 || response.StatusCode >= 400 {
		return errors.New("frontend health check failed")
	}
	return nil
}
