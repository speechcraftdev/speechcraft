# Buckeye Slicer Lab

IMPORTANT:

`failure_driven_slicer_tournament_fast_full_16way_buckeye_2026-07-29` contains an INVALID A-vs-D geometry comparison because the fast runner reused A geometry for D.

The A-derived policy variants from that run remain valid.

The authoritative proper-D result is:

```text
/home/aaravthegreat/Projects/speechcraft/eval_runs/failure_driven_geometry_recheck_proper_D_full_2026-07-29
```

This folder is an index for the Buckeye speaker-purity, NeMo, normalization, VAD geometry, and slicer-tournament work. Large artifacts are symlinked. Small review bundles and code snapshots are copied here for review convenience. Production code remains in `speechcraft`.

## Canonical Inputs

```text
canonical/normalized
canonical/nemo
canonical/acoustic_matrix
canonical/vad_geometry
canonical/failure_tournament_A_policies
canonical/proper_D_recheck
```

## Product Validation

```text
product_validation/emma_watson/diarization
product_validation/emma_watson/speaker0_slicer_asr
```

## Important Files

```text
manifests/slicer_daddy_test_v2.yaml
AUDIT_2026-08-11.md
scratch_manifest.md
review_bundles/
code_snapshots/
PHASE1_REFEREE.md
referee/
adapter/
tests/test_referee.py
tests/test_adapter.py
scripts/run_canonical_ad_smoke.py
scripts/run_phase5_full_ad_validation.py
scripts/run_phase6_finalists.py
phase4_validation/
phase6_finalists/
```

## Phase 1 referee / Phase 2 adapters / Phase 3–4 A-D validation

Neutral evaluation of slicer cutpoints/clips against annotated references. Phase 2 adds A/D execution adapters over that contract (one path, explicit geometry configs, no shared acoustic cache). Phase 3 adds geometry fingerprints plus a real canonical A/D smoke so A and D cannot silently share feature computation. Phase 4 runs that path on a frozen 4-recording Buckeye subset. Phase 5 scales the same loop to the full accepted Buckeye cohort. Phase 6 adds two A-derived policies (`min_quiet_run_64ms`, `quiet_run_score`) on that same cohort. Phase 7 prep adds a geometry-only sweep harness; do not launch the expensive Silero geometry tournament yet. See `PHASE1_REFEREE.md`.

Fast tests:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

Real canonical A/D smoke (needs torch / silero-vad / soundfile; does not download Buckeye):

```bash
/home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \
    scripts/run_canonical_ad_smoke.py
```

Real Phase 4 Buckeye A/D subset:

```bash
/home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \
    scripts/run_phase4_buckeye_validation.py
```

Real Phase 5 full accepted Buckeye A/D:

```bash
/home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \
    scripts/run_phase5_full_ad_validation.py
```

Real Phase 6 four-contender comparison (A, D, min_quiet_run_64ms, quiet_run_score):

```bash
/home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \
    scripts/run_phase6_finalists.py
```

Phase 7 geometry smoke (10 recordings, original seven geometries) and full finalist run:

```bash
/home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \
    scripts/run_phase7_geometry_smoke.py --workers 2
/home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \
    scripts/run_phase7_geometry_full.py \
    --geometries A_O50_8,D_O0_8,O25_4,O0_4,O12.5_4,O0_2 --workers 2
```

Phase 8 RMS round 2 smoke (10 recordings; A / O25_4_CURRENT + three waveform veto/penalty variants / O0_4 / O0_2). Do not run the full 120-recording RMS tournament yet:

```bash
/home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \
    scripts/run_phase8_rms_smoke.py --workers 2
```

Phase 9 external slicer smoke (10 recordings; frozen `O0_4` vs OpenVPI / librosa / pydub / FFmpeg). Do not run the 120-recording external benchmark yet:

```bash
/home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \
    scripts/run_phase9_external_smoke.py --workers 2
```

## Current Interpretation

Use the completed fast 16-way run for A-derived policy variants. Do not use it to compare A against D.

Use `canonical/proper_D_recheck` for proper-D metrics and compare it against `canonical/failure_tournament_A_policies` only through the explicit comparison tables in the geometry blocker review bundle.

The next real testing task is still the annotated character-dataset replay for:

```text
failure_27_bad_end
failure_49_bad_start
failure_93_scope_contamination
```

