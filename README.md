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
```

## Phase 1 referee / Phase 2 adapters

Neutral evaluation of slicer cutpoints/clips against annotated references. Phase 2 adds A/D execution adapters over that contract (one path, explicit geometry configs, no shared acoustic cache). See `PHASE1_REFEREE.md`. Run tests:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
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

