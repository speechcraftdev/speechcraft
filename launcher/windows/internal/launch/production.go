package launch

import (
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"time"
)

type GuestExec func(argv []string) (stdout string, code int, err error)

func challengeToken() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(buf), nil
}

func proveArgs(plan PlanResult, challenge string) ([]string, error) {
	distro, user, home, command, err := splitWSLPlan(plan)
	if err != nil {
		return nil, err
	}
	return []string{"--distribution", distro, "--user", user, "--cd", home, "--exec", command, "--prove-frontend", challenge}, nil
}

func stopArgs(plan PlanResult) ([]string, error) {
	distro, user, home, command, err := splitWSLPlan(plan)
	if err != nil {
		return nil, err
	}
	return []string{"--distribution", distro, "--user", user, "--cd", home, "--exec", command, "stop"}, nil
}

func splitWSLPlan(plan PlanResult) (string, string, string, string, error) {
	args := plan.WSLArgs
	take := func(flag string) (string, error) {
		for i := 0; i < len(args)-1; i++ {
			if args[i] == flag {
				return args[i+1], nil
			}
		}
		return "", fmt.Errorf("missing %s", flag)
	}
	distro, err := take("--distribution")
	if err != nil {
		return "", "", "", "", err
	}
	user, err := take("--user")
	if err != nil {
		return "", "", "", "", err
	}
	home, err := take("--cd")
	if err != nil {
		return "", "", "", "", err
	}
	command, err := take("--exec")
	if err != nil {
		return "", "", "", "", err
	}
	return distro, user, home, command, nil
}

func AuthenticatedProbe(plan PlanResult, exec GuestExec) func(string) error {
	return func(rawURL string) error {
		challenge, err := challengeToken()
		if err != nil {
			return err
		}
		argv, err := proveArgs(plan, challenge)
		if err != nil {
			return err
		}
		stdout, code, err := exec(argv)
		if err != nil || code != 0 {
			return errors.New("the trusted WSL command did not produce a frontend identity proof")
		}
		proof := strings.TrimSpace(stdout)
		if proof == "" || strings.Contains(proof, "\n") {
			return errors.New("frontend identity proof from WSL was empty")
		}
		return AuthenticateFrontend(rawURL, challenge, proof, defaultRoundTrip)
	}
}

func OwnedGuestStop(plan PlanResult, exec GuestExec) func() error {
	return func() error {
		argv, err := stopArgs(plan)
		if err != nil {
			return err
		}
		done := make(chan error, 1)
		go func() {
			_, code, execErr := exec(argv)
			if execErr != nil {
				done <- execErr
				return
			}
			if code != 0 {
				done <- fmt.Errorf("guest stop exited %d", code)
				return
			}
			done <- nil
		}()
		select {
		case err := <-done:
			return err
		case <-time.After(8 * time.Second):
			return errors.New("guest shutdown timed out before owned services were confirmed stopped")
		}
	}
}
