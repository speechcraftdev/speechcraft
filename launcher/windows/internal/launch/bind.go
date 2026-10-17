package launch

import "io"

func BindProduction(plan PlanResult, exec GuestExec, start func([]string) (Process, error), open func(string) error, stdout, stderr io.Writer) Deps {
	return Deps{
		Start:     start,
		Probe:     AuthenticatedProbe(plan, exec),
		StopGuest: OwnedGuestStop(plan, exec),
		Open:      open,
		Out:       stdout,
		Err:       stderr,
	}
}
