"""Phase 9 external baseline identity. Parameters chosen before Buckeye scores.

Policy: documented / upstream defaults, not Buckeye-tuned values.

OpenVPI uses the *CLI* defaults from vendored slicer2.py (how users invoke the
tool). Those differ from the Slicer class constructor for hop_size and
max_sil_kept; this file records the CLI values actually used.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from adapter.config import O0_4, GeometryConfig


ADAPTER_VERSION = "phase9_external_v1"
OPENVPI_UPSTREAM_REPO = "https://github.com/openvpi/audio-slicer"
OPENVPI_UPSTREAM_FILE = "slicer2.py"
OPENVPI_REVISION = "9958eede8f38fb6ce26914b1673e202ecfce70f3"
OPENVPI_SLICER2_SHA256 = (
    "505a7fceda62275f391cea11f3bd5ad3d4b54b460ff4567a7ca8747daeaf1957"
)
FFMPEG_BINARY = "/usr/bin/ffmpeg"
TRUSTED_O0_4_GEOMETRY_FINGERPRINT = (
    "87130029b5443647ed1f1febd32ab768bf957a0a1698217eb3cf14b2d00c6ecf"
)

Mode = Literal["native", "common_packer"]
Family = Literal["openvpi", "librosa", "pydub", "ffmpeg"]

# Chosen from vendored slicer2.py CLI argparse defaults, not Buckeye labels.
# Class constructor uses hop_size=20 / max_sil_kept=5000; CLI uses 10 / 500.
OPENVPI_PARAMS: dict[str, Any] = {
    "threshold_db": -40.0,
    "min_length_ms": 5000,
    "min_interval_ms": 300,
    "hop_size_ms": 10,
    "max_sil_kept_ms": 500,
    "parameter_source": "vendored slicer2.py CLI argparse defaults",
}

# librosa.effects.split documented defaults (0.11.0).
LIBROSA_PARAMS: dict[str, Any] = {
    "top_db": 60.0,
    "frame_length": 2048,
    "hop_length": 512,
    "ref": "np.max",
    "aggregate": "np.max",
    "parameter_source": "librosa.effects.split documented defaults",
}

# pydub.silence.detect_silence / detect_nonsilent function defaults.
PYDUB_PARAMS: dict[str, Any] = {
    "min_silence_len_ms": 1000,
    "silence_thresh_dbfs": -16.0,
    "seek_step_ms": 1,
    "parameter_source": "pydub.silence.detect_silence documented defaults",
}

# FFmpeg silencedetect filter documented defaults (noise -60dB, duration 2s).
FFMPEG_PARAMS: dict[str, Any] = {
    "binary": FFMPEG_BINARY,
    "noise": "-60dB",
    "duration_sec": 2.0,
    "filter": "silencedetect=noise=-60dB:duration=2",
    "parameter_source": "FFmpeg silencedetect documented filter defaults",
}

# Adapter-only candidate rules. Not native tool behavior.
CANDIDATE_RULES: dict[str, str] = {
    "openvpi": "OpenVPI kept-chunk hop start/end times (silence-minima slice positions)",
    "librosa": "start and end of librosa.effects.split non-silent intervals",
    "pydub": "midpoint of pydub.silence.detect_silence intervals",
    "ffmpeg": "midpoint of FFmpeg silencedetect silence intervals",
}

PARAMETER_POLICY = (
    "documented defaults / upstream CLI defaults chosen before inspecting "
    "Buckeye phone labels or >50 ms rates"
)

RVC_DECISION = {
    "inspected": True,
    "classification": "RVC / slicer2 ecosystem implementation; derived from OpenVPI",
    "independent_competitor": False,
    "reason": "algorithmically equivalent; preprocess kwargs and 3.7s chopping are wrapper policy",
}


def _lab_root() -> Path:
    return Path(__file__).resolve().parents[1]


def openvpi_slicer2_path() -> Path:
    return _lab_root() / "vendor" / "openvpi_audio_slicer" / "slicer2.py"


def openvpi_license_path() -> Path:
    return _lab_root() / "vendor" / "openvpi_audio_slicer" / "LICENSE"


def hash_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_openvpi_vendor() -> dict[str, str]:
    source = openvpi_slicer2_path()
    license_path = openvpi_license_path()
    if not source.is_file():
        raise RuntimeError(f"OpenVPI vendored slicer missing: {source}")
    if not license_path.is_file():
        raise RuntimeError(f"OpenVPI license missing: {license_path}")
    digest = hash_file_sha256(source)
    if digest != OPENVPI_SLICER2_SHA256:
        raise RuntimeError(
            f"OpenVPI slicer2.py hash {digest} != recorded {OPENVPI_SLICER2_SHA256}"
        )
    return {
        "path": str(source),
        "sha256": digest,
        "revision": OPENVPI_REVISION,
        "repository": OPENVPI_UPSTREAM_REPO,
        "upstream_file": OPENVPI_UPSTREAM_FILE,
    }


@dataclass(frozen=True)
class ExternalBaselineSpec:
    """One named external row. Audio + rate + buffers + these params only."""

    name: str
    family: Family
    mode: Mode
    params: dict[str, Any]
    candidate_rule: str
    packing: str

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "adapter_version": ADAPTER_VERSION,
            "candidate_rule": self.candidate_rule,
            "family": self.family,
            "mode": self.mode,
            "name": self.name,
            "packing": self.packing,
            "params": self.params,
        }

    def fingerprint(self) -> str:
        blob = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _spec(name: str, family: Family, mode: Mode, params: dict[str, Any]) -> ExternalBaselineSpec:
    packing = (
        "none_native_legal_filter_only"
        if mode == "native"
        else "buckeye_common_optimal_3_15_packer"
    )
    return ExternalBaselineSpec(
        name=name,
        family=family,
        mode=mode,
        params=dict(params),
        candidate_rule=CANDIDATE_RULES[family],
        packing=packing,
    )


OPENVPI_NATIVE = _spec("OpenVPI_native", "openvpi", "native", OPENVPI_PARAMS)
OPENVPI_COMMON = _spec("OpenVPI_common_packer", "openvpi", "common_packer", OPENVPI_PARAMS)
LIBROSA_NATIVE = _spec("librosa_native", "librosa", "native", LIBROSA_PARAMS)
LIBROSA_COMMON = _spec("librosa_common_packer", "librosa", "common_packer", LIBROSA_PARAMS)
PYDUB_NATIVE = _spec("pydub_native", "pydub", "native", PYDUB_PARAMS)
PYDUB_COMMON = _spec("pydub_common_packer", "pydub", "common_packer", PYDUB_PARAMS)
FFMPEG_NATIVE = _spec("ffmpeg_native", "ffmpeg", "native", FFMPEG_PARAMS)
FFMPEG_COMMON = _spec("ffmpeg_common_packer", "ffmpeg", "common_packer", FFMPEG_PARAMS)

PHASE9_EXTERNAL_SPECS: tuple[ExternalBaselineSpec, ...] = (
    OPENVPI_NATIVE,
    OPENVPI_COMMON,
    LIBROSA_NATIVE,
    LIBROSA_COMMON,
    PYDUB_NATIVE,
    PYDUB_COMMON,
    FFMPEG_NATIVE,
    FFMPEG_COMMON,
)
PHASE9_SMOKE_SYSTEMS: tuple[str, ...] = ("O0_4",) + tuple(
    spec.name for spec in PHASE9_EXTERNAL_SPECS
)

# Compact blog table: native only where OpenVPI's own chunks can be scored.
BLOG_TABLE_ROWS: tuple[tuple[str, str], ...] = (
    ("O0_4", "frozen internal"),
    ("OpenVPI_native", "native"),
    ("OpenVPI_common_packer", "common packer"),
    ("librosa_common_packer", "common packer"),
    ("pydub_common_packer", "common packer"),
    ("ffmpeg_common_packer", "common packer"),
)


def require_frozen_o0_4(config: GeometryConfig | None = None) -> GeometryConfig:
    active = O0_4 if config is None else config
    from adapter.diagnostics import geometry_fingerprint

    observed = geometry_fingerprint(active)
    if active.name != "O0_4":
        raise RuntimeError(f"Phase 9 control must be O0_4, got {active.name}")
    if observed != TRUSTED_O0_4_GEOMETRY_FINGERPRINT:
        raise RuntimeError(
            f"O0_4 geometry fingerprint drifted: {observed} != {TRUSTED_O0_4_GEOMETRY_FINGERPRINT}"
        )
    if active.window_samples != 512 or active.hop_samples != 512:
        raise RuntimeError("O0_4 window/hop changed")
    if active.offsets != (0, 64, 128, 192, 256, 320, 384, 448):
        raise RuntimeError("O0_4 offsets changed")
    if active.sample_rate_hz != 16000:
        raise RuntimeError("O0_4 sample_rate changed")
    if active.rms_policy is not None or active.scoring is not None:
        raise RuntimeError("O0_4 policy knobs must remain unset")
    return active


def spec_by_name(name: str) -> ExternalBaselineSpec:
    for spec in PHASE9_EXTERNAL_SPECS:
        if spec.name == name:
            return spec
    raise RuntimeError(f"unknown external baseline {name!r}")
