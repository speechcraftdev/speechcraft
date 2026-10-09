"""Reproduce scorer blind spots and audit production VR on a frozen Buckeye recording.

No labels enter the slicer. This is a diagnostic, not an ASR+MFA comparison.
Run with workers/dataset/.venv/bin/python scripts/audit_slicer_referee.py.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT.parent / "buckeye-slicer-lab"), str(ROOT / "workers/dataset")]

from referee.evaluate import evaluate
from referee.types import BufferScope, Clip, Cutpoint, PhoneInterval, RecordingReference, SlicerResult


def counterexamples():
    reference = RecordingReference("r", (BufferScope("b", 0, 20),), (
        PhoneInterval("r", "b", 3.9, 4.0, "first_half_of_word"),
        PhoneInterval("r", "b", 4.0, 4.1, "second_half_of_word"),
    ), ())
    # Adjacent legal clips split one word exactly between its phones.
    split = SlicerResult(tuple(Cutpoint("r", "b", t) for t in (1, 4, 7)),
                         (Clip("r", "b", 1, 4), Clip("r", "b", 4, 7)))
    scored = evaluate(reference, split)
    assert scored.speech_coverage == 1.0
    assert scored.final_edge_safety.inside_phone_count == 0
    # Same known bad edge, extra clips outside annotated speech dilute the rate.
    bad = SlicerResult((Cutpoint("r", "b", 3.95), Cutpoint("r", "b", 7)),
                       (Clip("r", "b", 3.95, 7),))
    diluted = SlicerResult(bad.cutpoints + (Cutpoint("r", "b", 10), Cutpoint("r", "b", 13)),
                          bad.clips + (Clip("r", "b", 10, 13),))
    before, after = evaluate(reference, bad), evaluate(reference, diluted)
    assert before.unique_cut_safety.inside_phone_count == after.unique_cut_safety.inside_phone_count == 1
    assert before.unique_cut_safety.inside_phone_rate == 0.5
    assert after.unique_cut_safety.inside_phone_rate == 0.25
    return {"word_split": asdict(scored), "denominator_before": asdict(before), "denominator_after": asdict(after)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speaker", default="s01")
    parser.add_argument("--recording", default="s0101b")
    parser.add_argument("--synthetic-only", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "eval_runs/slicer_audit_2026-10-07")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"counterexamples": counterexamples(), "asr_mfa_comparison_run": False}
    if not args.synthetic_only:
        from adapter.buckeye_loader import load_recording, BuckeyePaths
        from speechcraft_dataset import vr_slicer as vr
        loaded = load_recording(args.speaker, args.recording)
        result = vr.slice_wav(loaded.audio_path, recording_id=args.recording,
                              sample_rate_hz=loaded.sample_rate_hz,
                              buffers=[vr.BufferScope(b.buffer_id, b.start_sec, b.end_sec) for b in loaded.buffers])
        neutral = SlicerResult(
            tuple(Cutpoint(c.recording_id, c.buffer_id, c.time_sec) for c in result.selected_cutpoints),
            tuple(Clip(c.recording_id, c.buffer_id, c.start_sec, c.end_sec) for c in result.clips))
        with (BuckeyePaths.canonical().normalized_root / args.speaker / "reference_words.csv").open() as f:
            words = [w for w in csv.DictReader(f) if w["recording_id"] == args.recording and w["is_lexical_word"].lower() == "true"]
        rows = []
        for cut in neutral.cutpoints:
            phones = [p for p in loaded.reference.phones if p.buffer_id == cut.buffer_id]
            hit = next((p for p in phones if p.start_sec < cut.time_sec < p.end_sec), None)
            word = next((w for w in words if float(w["start_sec"]) < cut.time_sec < float(w["end_sec"])), None)
            masked = any(m.buffer_id == cut.buffer_id and m.start_sec <= cut.time_sec <= m.end_sec for m in loaded.reference.uncertainty_intervals)
            outside_span = not phones or cut.time_sec < min(p.start_sec for p in phones) or cut.time_sec > max(p.end_sec for p in phones)
            rows.append({**asdict(cut), "inside_phone": hit is not None,
                         "phone_depth_ms": min(cut.time_sec-hit.start_sec, hit.end_sec-cut.time_sec)*1000 if hit else None,
                         "inside_lexical_word": word is not None, "word": word["normalized_label"] if word else None,
                         "uncertainty": masked, "outside_first_last_phone_span": outside_span})
        report.update({"recording": args.recording, "geometry_fingerprint": result.geometry_fingerprint,
                       "metrics": asdict(evaluate(loaded.reference, neutral)),
                       "selected_cuts": len(rows),
                       "inside_lexical_word": sum(r["inside_lexical_word"] for r in rows),
                       "inside_word_but_not_phone": sum(r["inside_lexical_word"] and not r["inside_phone"] for r in rows),
                       "outside_first_last_phone_span": sum(r["outside_first_last_phone_span"] for r in rows),
                       "uncertainty": sum(r["uncertainty"] for r in rows)})
        (args.output / "vr_result.json").write_text(json.dumps(asdict(result), indent=2) + "\n")
        with (args.output / "selected_cut_audit.csv").open("w") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["time_sec"])
            writer.writeheader()
            writer.writerows(rows)
    (args.output / "audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
