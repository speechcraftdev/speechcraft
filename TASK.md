# Speechcraft model download onboarding

## Goal
Implement the agreed UX in existing `/login3`, frontend-first, with minimal safe backend work.

## Requirements
- Inspect current HEAD and relevant UI/backend flow before edits.
- Preserve existing `(login-chain)` split-screen layout, left video and testimonials, typography, buttons, mobile behavior.
- Keep `/login2` import -> `/login3` model setup -> `/login4` speaker detection, existing model resolution (`large-v3` then `base`), run creation/start, demo and error recovery.
- If model download needed, show one `Download & Continue` button. On click, show a restrained progress screen: actual currently downloaded model/package name and type, thin progress bar, percent, transferred/total bytes, speed and ETA **only when genuinely measurable**. Distinguish downloading from loading/initializing. Automatically navigate after preparation and run startup. If model already available, bypass download prompt.
- Determine whether preflight itself downloads; avoid downloading before consent where feasible.
- Prevent duplicate runs from double-click, retry and refresh; preserve imported recordings.
- Backend changes only for smallest viable accurate telemetry or cache/readiness detection; never invent progress, model names, ETA, or speed. When telemetry unavailable, show honest indeterminate status.
- No unrelated refactors, migrations, new visual system or processing algorithm changes.

## Verification
Test cached, fresh, failed/interrupted, fallback, demo, and navigation paths where feasible. Report changes, tests, telemetry limitations, and risks. Avoid unrelated dirty files.
