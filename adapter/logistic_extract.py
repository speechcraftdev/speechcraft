"""Extract the frozen 15-feature O0_4 pre-packer candidate table once."""

from __future__ import annotations

from typing import Any

from adapter.buckeye_loader import LoadedRecording
from adapter.canonical_executor import compute_detector_bundle
from adapter.config import FEATURE_NAMES, FEATURE_SCHEMA_ID, O0_4
from adapter.diagnostics import geometry_fingerprint
from adapter.logistic_features import features_from_cut
from adapter.logistic_labels import LABEL_SCHEMA_ID, label_candidate
from adapter.types import SlicerRequest

CANDIDATE_COLUMNS: tuple[str, ...] = (
    "schema_id",
    "label_schema_id",
    "speaker_id",
    "recording_id",
    "buffer_id",
    "cutpoint_id",
    "candidate_id",
    "time_sec",
    "original_score",
    "detector_name",
    "source_id",
    "interval_start_sec",
    "interval_end_sec",
    *FEATURE_NAMES,
    "y_inside",
    "y_gt20ms",
    "y_gt50ms",
    "state_inside",
    "state_gt20ms",
    "state_gt50ms",
    "geometry_fingerprint",
    "feature_bundle_id",
)


def _slicer_request(loaded: LoadedRecording) -> SlicerRequest:
    request = SlicerRequest(
        recording_id=loaded.recording_id,
        audio_path=loaded.audio_path,
        sample_rate_hz=loaded.sample_rate_hz,
        buffers=loaded.buffers,
        config=O0_4,
    )
    names = set(request.__dataclass_fields__)
    if names != {"recording_id", "audio_path", "sample_rate_hz", "buffers", "config"}:
        raise RuntimeError(f"slicer request leaked unexpected fields: {sorted(names)}")
    for forbidden in ("phones", "words", "uncertainty_intervals", "reference", "annotations"):
        if hasattr(request, forbidden):
            raise RuntimeError(f"slicer request carries annotation field {forbidden!r}")
    return request


def _cut_field(cut: Any, name: str, default: object) -> object:
    if hasattr(cut, name):
        return getattr(cut, name)
    return default


def extract_recording_candidates(loaded: LoadedRecording) -> list[dict[str, object]]:
    """Silero + O0_4 detector once. Labels are applied after feature extraction."""
    request = _slicer_request(loaded)
    expected_geo = geometry_fingerprint(O0_4)
    bundle, cuts, grid = compute_detector_bundle(request)
    if bundle.geometry_fingerprint != expected_geo:
        raise RuntimeError(
            f"{loaded.recording_id} detector fingerprint {bundle.geometry_fingerprint} "
            f"!= frozen O0_4 {expected_geo}"
        )
    rows: list[dict[str, object]] = []
    for cut in cuts:
        features = features_from_cut(cut, bundle, grid, FEATURE_NAMES)
        labels = label_candidate(
            float(cut.time_sec),
            recording_id=str(cut.recording_id),
            buffer_id=str(cut.buffer_id),
            reference=loaded.reference,
        )
        cutpoint_id = str(cut.cutpoint_id)
        row: dict[str, object] = {
            "schema_id": FEATURE_SCHEMA_ID,
            "label_schema_id": LABEL_SCHEMA_ID,
            "speaker_id": loaded.speaker_id,
            "recording_id": loaded.recording_id,
            "buffer_id": str(cut.buffer_id),
            "cutpoint_id": cutpoint_id,
            "candidate_id": f"{loaded.recording_id}:{cutpoint_id}",
            "time_sec": float(cut.time_sec),
            "original_score": float(cut.score),
            "detector_name": str(_cut_field(cut, "detector_name", "vad_percentile_rms")),
            "source_id": str(_cut_field(cut, "source_id", "")),
            "interval_start_sec": float(_cut_field(cut, "interval_start_sec", cut.time_sec)),
            "interval_end_sec": float(_cut_field(cut, "interval_end_sec", cut.time_sec)),
            "geometry_fingerprint": bundle.geometry_fingerprint,
            "feature_bundle_id": bundle.bundle_id,
        }
        for name, value in zip(FEATURE_NAMES, features):
            row[name] = float(value)
        row.update(labels)
        missing = [column for column in CANDIDATE_COLUMNS if column not in row]
        if missing:
            raise RuntimeError(f"candidate row missing columns: {missing}")
        rows.append({column: row[column] for column in CANDIDATE_COLUMNS})
    if not rows:
        raise RuntimeError(f"{loaded.recording_id} produced no O0_4 detector candidates")
    return rows
