"""Private Phase-3 execution diagnostics. Not part of SlicerResult / the evaluator.

Fingerprints cover only feature-producing geometry (rate, window, hop, offsets,
backend). Compact hashes replace full VAD/candidate arrays.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from adapter.config import GeometryConfig


SPEAKER_TS_EVAL_SRC_ENV = "SPEAKER_TS_EVAL_SRC"


def _lab_root() -> Path:
    return Path(__file__).resolve().parents[1]


def known_local_speaker_ts_eval_src() -> Path:
    """Sibling checkout default: <projects>/speaker_ts_eval/src."""
    return _lab_root().parent / "speaker_ts_eval" / "src"


def resolve_speaker_ts_eval_src() -> Path:
    """Resolve speaker_ts_eval src dir from env or a known local default.

    1. ``SPEAKER_TS_EVAL_SRC`` if set (must be an existing directory);
    2. otherwise ``<lab-parent>/speaker_ts_eval/src`` if that directory exists;
    3. otherwise raise with what to set.
    """
    raw = os.environ.get(SPEAKER_TS_EVAL_SRC_ENV)
    if raw is not None and raw.strip():
        path = Path(raw.strip()).expanduser()
        if not path.is_dir():
            raise RuntimeError(
                f"{SPEAKER_TS_EVAL_SRC_ENV}={raw!r} is not an existing directory. "
                "Set it to the speaker_ts_eval src directory (the parent of the "
                "speaker_ts_eval package)."
            )
        return path.resolve()

    default = known_local_speaker_ts_eval_src()
    if default.is_dir():
        return default.resolve()

    raise RuntimeError(
        "Could not locate speaker_ts_eval source. Set environment variable "
        f"{SPEAKER_TS_EVAL_SRC_ENV} to the src directory (the parent of the "
        "speaker_ts_eval package). Looked for a local default at "
        f"{default}."
    )


def geometry_canonical_payload(config: GeometryConfig) -> dict[str, Any]:
    """Feature-producing geometry only — no packer/scoring knobs."""
    return {
        "hop_samples": int(config.hop_samples),
        "offsets": [int(value) for value in config.offsets],
        "sample_rate_hz": int(config.sample_rate_hz),
        "vad_backend": str(config.vad_backend),
        "window_samples": int(config.window_samples),
    }


def geometry_canonical_json(config: GeometryConfig) -> str:
    return json.dumps(
        geometry_canonical_payload(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def geometry_fingerprint(config: GeometryConfig) -> str:
    """Deterministic SHA-256 of the canonical geometry JSON."""
    return hashlib.sha256(geometry_canonical_json(config).encode("utf-8")).hexdigest()


def policy_canonical_payload(config: GeometryConfig) -> dict[str, Any]:
    """Candidate/selection policy only — not window/hop/offsets."""
    payload: dict[str, Any] = {
        "min_quiet_run_ms": (
            None if config.min_quiet_run_ms is None else float(config.min_quiet_run_ms)
        ),
        "scoring": None if not config.scoring else str(config.scoring),
    }
    spec = getattr(config, "rms_policy", None)
    if spec is not None:
        payload["rms_policy"] = {
            "kind": str(spec.kind),
            "center_ms": float(spec.center_ms),
            "shoulder_ms": float(spec.shoulder_ms),
            "long_ms": float(spec.long_ms),
            "prominence_db": float(spec.prominence_db),
            "valley_width_ref_ms": float(spec.valley_width_ref_ms),
            "valley_margin_db": float(spec.valley_margin_db),
            "score_scale": float(spec.score_scale),
            "placement_window_ms": float(spec.placement_window_ms),
            "placement_hop_ms": float(spec.placement_hop_ms),
        }
    return payload


def policy_canonical_json(config: GeometryConfig) -> str:
    return json.dumps(
        policy_canonical_payload(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def policy_fingerprint(config: GeometryConfig) -> str:
    return hashlib.sha256(policy_canonical_json(config).encode("utf-8")).hexdigest()


def compact_sha256(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_vad_timestamps(frames: Iterable[dict[str, Any]]) -> str:
    payload = [
        [
            round(float(row["center_sec"]), 9),
            round(float(row["window_start_sec"]), 9),
            int(row["offset_samples"]),
        ]
        for row in frames
    ]
    return compact_sha256(payload)


def hash_vad_probabilities(frames: Iterable[dict[str, Any]]) -> str:
    payload = [round(float(row["speech_prob"]), 9) for row in frames]
    return compact_sha256(payload)


def hash_cutpoint_times(cuts: Iterable[Any]) -> str:
    payload = sorted(
        (
            str(cut.recording_id),
            str(cut.buffer_id),
            round(float(cut.time_sec), 9),
        )
        for cut in cuts
    )
    return compact_sha256(payload)


def hash_clip_intervals(clips: Iterable[Any]) -> str:
    payload = sorted(
        (
            str(clip.recording_id),
            str(clip.buffer_id),
            round(float(clip.start_sec), 9),
            round(float(clip.end_sec), 9),
        )
        for clip in clips
    )
    return compact_sha256(payload)


@dataclass(frozen=True)
class ExecutionDiagnostics:
    """Test-only compact signatures. Never merged into SlicerResult."""

    geometry_name: str
    geometry_canonical: str
    geometry_fingerprint: str
    instance_id: int
    workdir: str
    vad_cache_dir: str
    feature_cache_dir: str
    vad_cache_paths: tuple[str, ...]
    feature_cache_paths: tuple[str, ...]
    vad_observation_count: int
    vad_timestamp_sha256: str
    vad_probability_sha256: str
    candidate_cutpoint_sha256: str
    selected_clip_sha256: str
    policy_name: str = "baseline"
    policy_canonical: str = "{}"
    policy_fingerprint: str = ""
    selected_cutpoint_sha256: str = ""
    vad_compute_count: int = 1
    feature_bundle_id: str = ""
    vad_compute_sec: float | None = None
    policy_eval_sec: float | None = None

    def to_summary(self) -> dict[str, Any]:
        data = asdict(self)
        return data


def geometry_sensitive_signals_differ(
    left: ExecutionDiagnostics, right: ExecutionDiagnostics
) -> bool:
    """True if A/D did not collapse onto the same feature observations."""
    return (
        left.vad_observation_count != right.vad_observation_count
        or left.vad_timestamp_sha256 != right.vad_timestamp_sha256
        or left.vad_probability_sha256 != right.vad_probability_sha256
    )


def same_geometry_signatures(
    left: ExecutionDiagnostics, right: ExecutionDiagnostics
) -> bool:
    """Order-independence: same geometry must recompute the same signatures."""
    return (
        left.geometry_fingerprint == right.geometry_fingerprint
        and left.vad_observation_count == right.vad_observation_count
        and left.vad_timestamp_sha256 == right.vad_timestamp_sha256
        and left.vad_probability_sha256 == right.vad_probability_sha256
        and left.candidate_cutpoint_sha256 == right.candidate_cutpoint_sha256
        and left.selected_clip_sha256 == right.selected_clip_sha256
    )


def same_contender_signatures(
    left: ExecutionDiagnostics, right: ExecutionDiagnostics
) -> bool:
    """Order-independence for one contender: geometry, policy, and outputs match."""
    return (
        same_geometry_signatures(left, right)
        and left.policy_fingerprint == right.policy_fingerprint
        and left.selected_cutpoint_sha256 == right.selected_cutpoint_sha256
    )


def write_tiny_mono_wav(
    path: Path,
    *,
    duration_sec: float = 4.0,
    sample_rate_hz: int = 16000,
) -> Path:
    """Deterministic 16 kHz mono PCM WAV (tone bursts + silence). No downloads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(round(duration_sec * sample_rate_hz))
    burst_samples = int(round(0.4 * sample_rate_hz))
    amplitude = 0.28 * 32767.0
    two_pi_f = 2.0 * math.pi * 440.0
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate_hz)
        frames = bytearray()
        for index in range(n_samples):
            in_burst = (index // burst_samples) % 2 == 0
            if in_burst:
                sample = int(amplitude * math.sin(two_pi_f * (index / sample_rate_hz)))
            else:
                sample = 0
            frames += struct.pack("<h", sample)
        writer.writeframes(frames)
    return path
