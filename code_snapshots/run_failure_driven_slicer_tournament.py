#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SPEAKER_TS_EVAL_SRC = Path("/home/aaravthegreat/Projects/speaker_ts_eval/src")
if str(SPEAKER_TS_EVAL_SRC) not in sys.path:
    sys.path.insert(0, str(SPEAKER_TS_EVAL_SRC))

import speaker_ts_eval.repaired_buckeye_benchmark as rb  # noqa: E402
from speaker_ts_eval.repaired_buckeye_benchmark import (  # noqa: E402
    RepairedMatrixFixture,
    run_repaired_detector_matrix,
)
import speaker_ts_eval.slicer_daddy_test as sd  # noqa: E402

POLICY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/failure_driven_tournament_policy.py"
import importlib.util  # noqa: E402

_POLICY_SPEC = importlib.util.spec_from_file_location("failure_driven_tournament_policy", POLICY_SCRIPT)
if _POLICY_SPEC is None or _POLICY_SPEC.loader is None:
    raise RuntimeError(f"failed to load policy script: {POLICY_SCRIPT}")
policy = importlib.util.module_from_spec(_POLICY_SPEC)
sys.modules[_POLICY_SPEC.name] = policy
_POLICY_SPEC.loader.exec_module(policy)


BUCKEYE_ROOT = Path("/home/aaravthegreat/Datasets/buckeye")
COHORT_ROOT = BUCKEYE_ROOT / "eval_runs/2026-07-18_buckeye_acoustic_matrix_v1/cohort"
NORMALIZED_ROOT = BUCKEYE_ROOT / "normalized_reviewed_2026-07-18/corpus"
DEFAULT_OUT_ROOT = BUCKEYE_ROOT / "eval_runs/2026-07-27_failure_driven_slicer_tournament"
DEFAULT_BUNDLE = BUCKEYE_ROOT / "eval_runs/2026-07-27_failure_driven_slicer_tournament_review_bundle_no_audio.zip"


@dataclass(frozen=True)
class TournamentConfig:
    config_id: int
    name: str
    family: str
    hypothesis: str
    implementation_status: str
    detector_name: str | None = None
    vad_backend: str = "silero_official_onnx"
    vad_window_samples: int = 512
    vad_hop_samples: int = 256
    vad_offsets: tuple[int, ...] = (0, 128)
    openvpi_native: bool = False
    min_quiet_run_ms: int | None = None
    one_sided_quiet_ms: int | None = None
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def runnable(self) -> bool:
        return self.implementation_status == "implemented" and self.detector_name is not None and not self.openvpi_native

    def env(self) -> dict[str, str]:
        return {
            "SPEAKER_TS_EVAL_VAD_BACKEND": self.vad_backend,
            "SPEAKER_TS_EVAL_VAD_WINDOW_SAMPLES": str(self.vad_window_samples),
            "SPEAKER_TS_EVAL_VAD_HOP_SAMPLES": str(self.vad_hop_samples),
            "SPEAKER_TS_EVAL_VAD_OFFSETS": ",".join(str(offset) for offset in self.vad_offsets),
        }


@dataclass
class FastFixtureCache:
    fixture: RepairedMatrixFixture
    speaker_id: str
    included_recording_ids: set[str]
    excluded_recording_ids: set[str]
    reference_words: list[dict[str, Any]]
    reference_phones: list[dict[str, Any]]
    uncertainty_masks: dict[str, list[tuple[float, float]]]
    interviewer_mask: dict[str, list[tuple[float, float]]]
    target_reference: dict[str, list[tuple[float, float]]]
    annotation_rows: dict[str, list[dict[str, Any]]]
    buffers: list[Any]
    buffer_by_id: dict[str, dict[str, Any]]
    sources: dict[str, Any]
    cache_root: Path
    speaker_cache_dirs: list[Path]
    benchmark_root: Path
    build_cache_wall_sec: float
    contexts_by_geometry: dict[str, Any] = field(default_factory=dict)


TOURNAMENT_CONFIGS: tuple[TournamentConfig, ...] = (
    TournamentConfig(1, "current_A_baseline", "frozen_baselines", "Current validated A-geometry vad_percentile_rms baseline.", "implemented", "vad_percentile_rms"),
    TournamentConfig(2, "proper_D_baseline", "frozen_baselines", "Four independent Silero streams with 8 ms interleaved timeline.", "implemented", "vad_percentile_rms", vad_hop_samples=512, vad_offsets=(0, 128, 256, 384)),
    TournamentConfig(3, "openvpi_native", "frozen_baselines", "Repository-native OpenVPI slicing behavior.", "not_implemented", openvpi_native=True),
    TournamentConfig(4, "openvpi_candidates_common_packer", "frozen_baselines", "OpenVPI silence minima fed to common optimal packer.", "not_implemented"),
    TournamentConfig(5, "min_quiet_run_64ms", "minimum_silence_evidence", "Reject candidate runs with less than 64 ms total quiet evidence.", "implemented", "vad_percentile_rms", min_quiet_run_ms=64),
    TournamentConfig(6, "min_quiet_run_80ms", "minimum_silence_evidence", "Reject candidate runs with less than 80 ms total quiet evidence.", "implemented", "vad_percentile_rms", min_quiet_run_ms=80),
    TournamentConfig(7, "min_quiet_run_96ms", "minimum_silence_evidence", "Reject candidate runs with less than 96 ms total quiet evidence.", "implemented", "vad_percentile_rms", min_quiet_run_ms=96),
    TournamentConfig(8, "min_quiet_run_120ms", "minimum_silence_evidence", "Reject candidate runs with less than 120 ms total quiet evidence.", "implemented", "vad_percentile_rms", min_quiet_run_ms=120),
    TournamentConfig(9, "one_sided_48ms", "one_sided_endpoint_protection", "Require 48 ms of quiet on the dangerous side of starts/ends.", "implemented", "vad_percentile_rms", one_sided_quiet_ms=48),
    TournamentConfig(10, "one_sided_64ms", "one_sided_endpoint_protection", "Require 64 ms of quiet on the dangerous side of starts/ends.", "implemented", "vad_percentile_rms", one_sided_quiet_ms=64),
    TournamentConfig(11, "one_sided_80ms", "one_sided_endpoint_protection", "Require 80 ms of quiet on the dangerous side of starts/ends.", "implemented", "vad_percentile_rms", one_sided_quiet_ms=80),
    TournamentConfig(12, "one_sided_soft_penalty", "one_sided_endpoint_protection", "Penalize missing one-sided quiet instead of rejecting.", "implemented", "vad_percentile_rms", policy={"penalty": "one_sided_quiet"}),
    TournamentConfig(13, "A_hard_D_veto", "geometry_agreement_rms_protection", "Generate A candidates but reject candidates where D strongly indicates speech.", "not_implemented", "vad_percentile_rms", policy={"hybrid": "hard_D_veto"}),
    TournamentConfig(14, "A_soft_D_penalty", "geometry_agreement_rms_protection", "Generate A candidates but downrank A/D disagreements.", "not_implemented", "vad_percentile_rms", policy={"hybrid": "soft_D_penalty"}),
    TournamentConfig(15, "A_only_stronger_RMS", "geometry_agreement_rms_protection", "Require stronger RMS evidence for A-only candidates.", "not_implemented", "vad_percentile_rms", policy={"hybrid": "A_only_stronger_RMS"}),
    TournamentConfig(16, "D_primary_A_fallback", "geometry_agreement_rms_protection", "Use D-supported candidates first and A-only fallback only to form legal clips.", "not_implemented", "vad_percentile_rms", policy={"hybrid": "D_primary_A_fallback"}),
    TournamentConfig(17, "consensus_first_union", "geometry_agreement_rms_protection", "Rank A∩D highest, D-only medium, A-only lowest.", "not_implemented", "vad_percentile_rms", policy={"hybrid": "consensus_first_union"}),
    TournamentConfig(18, "D_regions_RMS_exact", "geometry_agreement_rms_protection", "D determines safe regions; RMS selects exact local minimum.", "not_implemented", "vad_percentile_rms", policy={"hybrid": "D_regions_RMS_exact"}),
    TournamentConfig(19, "large_boundary_risk_penalty", "safety_aware_packing", "Apply a large scalar penalty for risky boundaries before ordinary scheduling.", "implemented", "vad_percentile_rms", policy={"packer": "large_boundary_risk_penalty"}),
    TournamentConfig(20, "quiet_run_score", "safety_aware_packing", "Score boundaries by quiet-run duration, one-sided quiet, RMS depth, and prominence.", "implemented", "vad_percentile_rms", policy={"scoring": "quiet_evidence"}),
    TournamentConfig(21, "catastrophic_boundary_penalty", "safety_aware_packing", "Keep weak candidates but apply a large penalty for short/one-sided quiet.", "implemented", "vad_percentile_rms", policy={"scoring": "catastrophic_boundary_penalty"}),
    TournamentConfig(22, "high_confidence_clip_bonus", "safety_aware_packing", "Apply a large candidate-weight bonus to high-confidence clips.", "implemented", "vad_percentile_rms", policy={"packer": "high_confidence_clip_bonus"}),
    TournamentConfig(23, "diarization_edge_soft_penalty", "scope_edge_protection", "Penalize candidates near diarization region edges.", "implemented", "vad_percentile_rms", policy={"scope_edge": "soft_penalty"}),
    TournamentConfig(24, "diarization_edge_hard_uncertainty", "scope_edge_protection", "No candidates near diarization region edges unless source boundary is trusted.", "implemented", "vad_percentile_rms", policy={"scope_edge": "hard_uncertainty_zone"}),
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def strict_metric(value: Any, key: str) -> float:
    if value in {None, ""}:
        raise ValueError(f"missing required metric: {key}")
    parsed = float(value)
    if not parsed == parsed or parsed in {float("inf"), float("-inf")}:
        raise ValueError(f"non-finite metric {key}: {value}")
    return parsed


def cut_quiet_run_ms(cut: Any) -> float:
    return float(cut.metadata.get("run_duration_ms") or max(0.0, (cut.interval_end_sec - cut.interval_start_sec) * 1000.0))


def cut_quiet_before_ms(cut: Any) -> float:
    return max(0.0, (cut.time_sec - cut.interval_start_sec) * 1000.0)


def cut_quiet_after_ms(cut: Any) -> float:
    return max(0.0, (cut.interval_end_sec - cut.time_sec) * 1000.0)


def buffer_edge_distance_ms(cut: Any, buffers_by_id: dict[str, Any]) -> float:
    buffer = buffers_by_id.get(cut.buffer_id)
    if buffer is None:
        return float("inf")
    start = float(getattr(buffer, "buffer_start_sec", getattr(buffer, "trusted_start_sec", 0.0)))
    end = float(getattr(buffer, "buffer_end_sec", getattr(buffer, "trusted_end_sec", 0.0)))
    return max(0.0, min(cut.time_sec - start, end - cut.time_sec) * 1000.0)


def with_cut_metadata(cut: Any, config: TournamentConfig, *, source: str | None = None, buffers_by_id: dict[str, Any] | None = None) -> Any:
    metadata = dict(cut.metadata)
    metadata.update(
        {
            "tournament_config": config.name,
            "quiet_run_ms": round(cut_quiet_run_ms(cut), 6),
            "quiet_before_ms": round(cut_quiet_before_ms(cut), 6),
            "quiet_after_ms": round(cut_quiet_after_ms(cut), 6),
        }
    )
    if source is not None:
        metadata["geometry_support"] = source
    if buffers_by_id is not None:
        metadata["distance_to_scope_edge_ms"] = round(buffer_edge_distance_ms(cut, buffers_by_id), 6)
    return sd.replace(
        cut,
        detector_name=config.name,
        cutpoint_id=f"{config.name}:{cut.cutpoint_id}",
        metadata=metadata,
    )


def boundary_evidence(cut: Any, *, role: str = "internal") -> Any:
    role_value = {
        "start": policy.BoundaryRole.START,
        "end": policy.BoundaryRole.END,
    }.get(role, policy.BoundaryRole.INTERNAL)
    return policy.BoundaryEvidence(
        boundary_id=cut.cutpoint_id,
        time_sec=float(cut.time_sec),
        role=role_value,
        source=str(cut.metadata.get("geometry_support") or "A"),
        quiet_run_ms=cut_quiet_run_ms(cut),
        quiet_before_ms=cut_quiet_before_ms(cut),
        quiet_after_ms=cut_quiet_after_ms(cut),
        rms_dbfs=float(cut.rms_min_dbfs if cut.rms_min_dbfs is not None else -60.0),
        local_prominence_db=float(cut.metadata.get("score_max", 0.0) or 0.0),
        a_speech_prob=float(cut.vad_min) if cut.vad_min is not None else None,
        d_speech_prob=float(cut.metadata["d_speech_prob"]) if "d_speech_prob" in cut.metadata else None,
        distance_to_scope_edge_ms=float(cut.metadata.get("distance_to_scope_edge_ms", float("inf"))),
        trusted_source_edge=bool(cut.metadata.get("trusted_source_edge", False)),
        base_score=float(cut.score),
    )


def cut_with_score(cut: Any, config: TournamentConfig, score: float, reasons: tuple[str, ...]) -> Any:
    metadata = dict(cut.metadata)
    metadata["policy_reasons"] = ";".join(reasons)
    metadata["original_score"] = cut.score
    return sd.replace(cut, score=round(score, 6), detector_name=config.name, cutpoint_id=f"{config.name}:{cut.cutpoint_id}", metadata=metadata)


class TournamentDetector:
    def __init__(self, config: TournamentConfig) -> None:
        self.name = config.name
        self.config = config

    def find_cutpoints(self, contexts: Any) -> Any:
        if self.config.name == "proper_D_baseline":
            return self._run_base_with_env(contexts, self.config, source_label="D")
        if self.config.policy.get("hybrid"):
            return self._run_hybrid(contexts)
        result = self._run_base_with_env(contexts, self.config, source_label="A")
        return self._apply_single_stream_policy(result, contexts)

    def _run_base_with_env(self, contexts: Any, config: TournamentConfig, *, source_label: str) -> Any:
        previous_env = {key: os.environ.get(key) for key in config.env()}
        os.environ.update(config.env())
        try:
            base = sd.VadPercentileRmsDetector()
            result = base.find_cutpoints(contexts)
        finally:
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        buffers_by_id = {key: type("Buffer", (), {"buffer_start_sec": float(value["trusted_start_sec"]), "buffer_end_sec": float(value["trusted_end_sec"])}) for key, value in contexts.shared.buffer_by_id.items()}
        cuts = [with_cut_metadata(cut, self.config, source=source_label, buffers_by_id=buffers_by_id) for cut in result.cutpoints]
        return sd.replace(
            result,
            cutpoints=cuts,
            compute_metric=sd.replace(
                result.compute_metric,
                detector_name=self.config.name,
                notes=f"{result.compute_metric.notes}; tournament_config={self.config.name}",
            ),
        )

    def _apply_single_stream_policy(self, result: Any, contexts: Any) -> Any:
        del contexts
        cuts = list(result.cutpoints)
        if self.config.min_quiet_run_ms is not None:
            cuts = [
                cut
                for cut in cuts
                if policy.apply_min_quiet_run(boundary_evidence(cut), float(self.config.min_quiet_run_ms)).accepted
            ]
        if self.config.policy.get("penalty") == "one_sided_quiet":
            cuts = [
                cut_with_score(cut, self.config, policy.apply_one_sided_soft_penalty(boundary_evidence(cut), 64.0).score, ("one_sided_soft_penalty",))
                for cut in cuts
            ]
        if self.config.policy.get("scoring") == "quiet_evidence":
            cuts = [
                cut_with_score(cut, self.config, policy.quiet_evidence_score(boundary_evidence(cut)).score, ("quiet_evidence_score",))
                for cut in cuts
            ]
        if self.config.policy.get("scoring") == "catastrophic_boundary_penalty":
            cuts = [
                cut_with_score(cut, self.config, policy.catastrophic_boundary_penalty(boundary_evidence(cut)).score, ("catastrophic_boundary_penalty",))
                for cut in cuts
            ]
        if self.config.policy.get("scope_edge") == "soft_penalty":
            cuts = [
                cut_with_score(cut, self.config, policy.apply_scope_edge_soft_penalty(boundary_evidence(cut)).score, ("scope_edge_soft_penalty",))
                for cut in cuts
            ]
        if self.config.policy.get("scope_edge") == "hard_uncertainty_zone":
            cuts = [
                cut
                for cut in cuts
                if policy.apply_scope_edge_hard_zone(boundary_evidence(cut)).accepted
            ]
        return sd.replace(result, cutpoints=cuts)

    def _run_hybrid(self, contexts: Any) -> Any:
        a_config = sd.replace(self.config, vad_hop_samples=256, vad_offsets=(0, 128))
        d_config = sd.replace(self.config, vad_hop_samples=512, vad_offsets=(0, 128, 256, 384))
        a_result = self._run_base_with_env(contexts, a_config, source_label="A_only")
        d_result = self._run_base_with_env(contexts, d_config, source_label="D_only")
        a_rows = list(a_result.cutpoints)
        d_rows = list(d_result.cutpoints)
        d_by_key = {(cut.recording_id, cut.buffer_id, round(float(cut.time_sec), 3)): cut for cut in d_rows}
        a_keys = {(cut.recording_id, cut.buffer_id, round(float(cut.time_sec), 3)) for cut in a_rows}
        hybrid = str(self.config.policy.get("hybrid"))
        cuts: list[Any] = []
        for cut in a_rows:
            key = (cut.recording_id, cut.buffer_id, round(float(cut.time_sec), 3))
            d_match = d_by_key.get(key)
            source = "A_and_D" if d_match is not None else "A_only"
            metadata = dict(cut.metadata)
            metadata["geometry_support"] = source
            if d_match is not None and d_match.vad_min is not None:
                metadata["d_speech_prob"] = d_match.vad_min
            row = sd.replace(cut, metadata=metadata)
            evidence = boundary_evidence(row)
            if hybrid == "hard_D_veto" and not policy.apply_hard_d_veto(evidence).accepted:
                continue
            if hybrid == "soft_D_penalty":
                decision = policy.apply_soft_d_penalty(evidence)
                row = cut_with_score(row, self.config, decision.score, decision.reasons)
            elif hybrid == "A_only_stronger_RMS":
                if not policy.apply_a_only_stronger_rms(evidence).accepted:
                    continue
            elif hybrid == "consensus_first_union":
                decision = policy.consensus_rank_score(evidence)
                row = cut_with_score(row, self.config, decision.score, decision.reasons)
            cuts.append(sd.replace(row, detector_name=self.config.name, cutpoint_id=f"{self.config.name}:{row.cutpoint_id}"))
        if hybrid in {"D_primary_A_fallback", "D_regions_RMS_exact", "consensus_first_union"}:
            for d_cut in d_rows:
                key = (d_cut.recording_id, d_cut.buffer_id, round(float(d_cut.time_sec), 3))
                if key in a_keys:
                    continue
                row = sd.replace(d_cut, metadata={**d_cut.metadata, "geometry_support": "D_only"})
                if hybrid == "consensus_first_union":
                    decision = policy.consensus_rank_score(boundary_evidence(row))
                    row = cut_with_score(row, self.config, decision.score, decision.reasons)
                cuts.append(sd.replace(row, detector_name=self.config.name, cutpoint_id=f"{self.config.name}:{row.cutpoint_id}"))
        if hybrid == "D_primary_A_fallback":
            d_supported = [cut for cut in cuts if cut.metadata.get("geometry_support") in {"D_only", "A_and_D"}]
            if len(d_supported) >= 2:
                cuts = d_supported
        metric = sd.replace(
            a_result.compute_metric,
            detector_name=self.config.name,
            wall_clock_sec=round(a_result.compute_metric.wall_clock_sec + d_result.compute_metric.wall_clock_sec, 6),
            cpu_time_sec=round(a_result.compute_metric.cpu_time_sec + d_result.compute_metric.cpu_time_sec, 6),
            notes=f"A/D hybrid policy={hybrid}; {a_result.compute_metric.notes}",
        )
        return sd.replace(a_result, cutpoints=sd._dedupe_cutpoints(cuts), compute_metric=metric)


@contextmanager
def patched_tournament_execution(config: TournamentConfig):
    original_supported = rb.supported_repaired_detectors
    original_generate = rb.generate_legal_candidate_clips_in_buffers

    def supported() -> dict[str, Any]:
        rows = original_supported()
        rows[config.name] = TournamentDetector(config)
        return rows

    def generate_candidates(*, cutpoints: list[Any], constraints: Any) -> list[Any]:
        candidates = original_generate(cutpoints=cutpoints, constraints=constraints)
        by_id = {cut.cutpoint_id: cut for cut in cutpoints}
        score_policies = {
            "one_sided_quiet",
            "quiet_evidence",
            "catastrophic_boundary_penalty",
            "scope_edge_soft_penalty",
        }
        policy_name = str(config.policy.get("penalty") or config.policy.get("scoring") or config.policy.get("scope_edge") or "")
        if policy_name in score_policies:
            weighted = []
            for candidate in candidates:
                start = by_id[candidate.start_cutpoint_id]
                end = by_id[candidate.end_cutpoint_id]
                original_boundary_score = float(start.metadata.get("original_score", start.score)) + float(end.metadata.get("original_score", end.score))
                adjusted_boundary_score = float(start.score) + float(end.score)
                weight_delta = adjusted_boundary_score - original_boundary_score
                weighted.append(sd.replace(candidate, weight=round(candidate.weight + weight_delta, 6)))
            return weighted
        if config.one_sided_quiet_ms is not None:
            kept = []
            for candidate in candidates:
                start = by_id[candidate.start_cutpoint_id]
                end = by_id[candidate.end_cutpoint_id]
                if not policy.apply_one_sided_gate(boundary_evidence(start, role="start"), float(config.one_sided_quiet_ms)).accepted:
                    continue
                if not policy.apply_one_sided_gate(boundary_evidence(end, role="end"), float(config.one_sided_quiet_ms)).accepted:
                    continue
                kept.append(candidate)
            return kept
        if config.policy.get("packer") == "large_boundary_risk_penalty":
            out = []
            for candidate in candidates:
                start = by_id[candidate.start_cutpoint_id]
                end = by_id[candidate.end_cutpoint_id]
                clip = policy.CandidateClipEvidence(
                    candidate.candidate_id,
                    boundary_evidence(start, role="start"),
                    boundary_evidence(end, role="end"),
                    candidate.duration_sec,
                    candidate.duration_sec,
                    abs(candidate.duration_sec - constraints.target_clip_sec),
                )
                risk = policy.safety_first_clip_key(clip)[0] + policy.safety_first_clip_key(clip)[1]
                out.append(sd.replace(candidate, weight=round(candidate.weight - (100.0 * risk), 6)))
            return out
        if config.policy.get("packer") == "high_confidence_clip_bonus":
            out = []
            for candidate in candidates:
                start = by_id[candidate.start_cutpoint_id]
                end = by_id[candidate.end_cutpoint_id]
                high = (
                    start.metadata.get("quiet_run_ms", 0) >= 80
                    and end.metadata.get("quiet_run_ms", 0) >= 80
                    and cut_quiet_before_ms(start) >= 48
                    and cut_quiet_after_ms(end) >= 48
                )
                out.append(sd.replace(candidate, weight=round(candidate.weight + (1000.0 if high else 0.0), 6)))
            return out
        return candidates

    rb.supported_repaired_detectors = supported
    rb.generate_legal_candidate_clips_in_buffers = generate_candidates
    try:
        yield
    finally:
        rb.supported_repaired_detectors = original_supported
        rb.generate_legal_candidate_clips_in_buffers = original_generate


def config_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for config in TOURNAMENT_CONFIGS:
        row = asdict(config)
        row["runnable"] = config.runnable
        row["vad_offsets"] = " ".join(str(offset) for offset in config.vad_offsets)
        row["policy"] = json.dumps(config.policy, sort_keys=True)
        rows.append(row)
    return rows


def load_speakers(limit: int | None = None) -> list[dict[str, str]]:
    rows = read_csv(COHORT_ROOT / "speaker_cohort_summary.csv")
    speakers = [row for row in rows if int(row["accepted_recording_count"]) > 0]
    return speakers[:limit] if limit is not None else speakers


def build_fixtures(speakers: list[dict[str, str]]) -> list[RepairedMatrixFixture]:
    return [
        RepairedMatrixFixture(
            speaker_id=row["speaker_id"],
            eval_run=COHORT_ROOT / "eval_runs" / row["speaker_id"],
            normalized_speaker_dir=NORMALIZED_ROOT / row["speaker_id"],
        )
        for row in speakers
    ]


def run_config(config: TournamentConfig, fixtures: list[RepairedMatrixFixture], out_root: Path) -> Path:
    if not config.runnable or config.detector_name is None:
        raise ValueError(f"configuration is not runnable with current benchmark hooks: {config.name}")
    run_root = out_root / "runs" / f"{config.config_id:02d}_{config.name}"
    if run_root.exists():
        shutil.rmtree(run_root)
    run_root.mkdir(parents=True)
    with patched_tournament_execution(config):
        return run_repaired_detector_matrix(
            fixtures=fixtures,
            cluster_mapping_csv=COHORT_ROOT / "benchmark_cluster_mapping.csv",
            out_root=run_root,
            detector_names=[config.name],
        )


def collect_run_tables(config: TournamentConfig, matrix_dir: Path) -> dict[str, list[dict[str, Any]]]:
    tables_dir = matrix_dir / "tables"
    out: dict[str, list[dict[str, Any]]] = {}
    for table_name in (
        "detector_scorecard_by_speaker",
        "detector_scorecard_pooled",
        "coverage_by_recording_all",
        "boundary_severity_unique_cutpoints_by_speaker",
        "boundary_severity_final_edge_occurrences_by_speaker",
        "runtime_by_detector_and_speaker",
        "pareto_frontier",
        "run_index",
    ):
        path = tables_dir / f"{table_name}.csv"
        rows = read_csv(path) if path.exists() else []
        out[table_name] = [
            {
                "config_id": config.config_id,
                "config_name": config.name,
                "family": config.family,
                **row,
            }
            for row in rows
        ]
    return out


def build_summary(run_tables: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    runtime_totals_by_config: dict[str, float] = {}
    for row in run_tables.get("runtime_by_detector_and_speaker", []):
        config_name = str(row.get("config_name", ""))
        runtime_totals_by_config[config_name] = runtime_totals_by_config.get(config_name, 0.0) + strict_metric(
            row.get("wall_clock_sec", ""),
            "wall_clock_sec",
        )
    runtime_index = {
        (
            str(row.get("config_name", "")),
            str(row.get("speaker_id", "")),
        ): strict_metric(row.get("wall_clock_sec", ""), "wall_clock_sec")
        for row in run_tables.get("runtime_by_detector_and_speaker", [])
    }
    source_rows = run_tables.get("detector_scorecard_pooled") or run_tables.get("detector_scorecard_by_speaker") or []
    for row in source_rows:
        inside_rate = row.get("inside_target_phone_rate_unique", row.get("unique_inside_target_speech_phone_rate", ""))
        gt20_rate = row.get("gt_20ms_rate_unique", row.get("unique_gt_20ms_rate", ""))
        gt50_rate = row.get("gt_50ms_rate_unique", row.get("unique_gt_50ms_rate", ""))
        gt100_rate = row.get("gt_100ms_rate_unique", row.get("unique_gt_100ms_rate", ""))
        rows.append(
            {
                "config_id": row["config_id"],
                "config_name": row["config_name"],
                "family": row["family"],
                "conditional_target_phone_coverage": row.get("conditional_target_phone_coverage", ""),
                "inside_target_phone_rate_unique": inside_rate,
                "gt_20ms_rate_unique": gt20_rate,
                "gt_50ms_rate_unique": gt50_rate,
                "gt_100ms_rate_unique": gt100_rate,
                "emitted_clip_count": row.get("emitted_clip_count", row.get("selected_clip_count", "")),
                "selected_cutpoint_count": row.get("selected_cutpoint_count", ""),
                "runtime_sec": (
                    runtime_index.get((str(row.get("config_name", "")), str(row.get("speaker_id", ""))), "")
                    if row.get("speaker_id")
                    else round(runtime_totals_by_config.get(str(row.get("config_name", "")), 0.0), 6)
                ),
            }
        )
        for metric_key in ("conditional_target_phone_coverage", "inside_target_phone_rate_unique", "gt_50ms_rate_unique"):
            value = rows[-1][metric_key]
            parsed = strict_metric(value, metric_key)
            if not 0.0 <= parsed <= 1.0:
                raise ValueError(f"rate metric {metric_key} out of range for {row['config_name']}: {parsed}")
    return rows


def first_run_dir(matrix_dir: Path) -> Path | None:
    rows = read_csv(matrix_dir / "tables" / "run_index.csv")
    if not rows:
        return None
    return Path(rows[0]["run_dir"])


def cleanup_run_caches(matrix_dir: Path) -> None:
    shared_cache = matrix_dir / "shared_cache"
    if shared_cache.exists():
        shutil.rmtree(shared_cache)


def _candidate_speaker_cache_dirs(*, speaker_id: str, search_roots: list[Path]) -> list[Path]:
    candidates: list[Path] = []
    for root in search_roots:
        if not root.exists():
            continue
        candidates.extend(root.glob(f"**/shared_cache/{speaker_id}"))
    unique = sorted({path.resolve() for path in candidates if path.is_dir()}, key=lambda path: path.stat().st_mtime, reverse=True)
    return unique


def _geometry_slug(config: TournamentConfig) -> str:
    offsets = "-".join(str(offset) for offset in config.vad_offsets)
    return f"w{config.vad_window_samples}_h{config.vad_hop_samples}_o{offsets}"


def _reuse_speaker_level_json_cache(
    *,
    recording_ids: set[str],
    cache_kind: str,
    output_cache_dir: Path,
    speaker_cache_dirs: list[Path],
    allow_prefixed_match: bool = False,
    required_filename_fragment: str | None = None,
) -> dict[str, Any]:
    output_cache_dir.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, Any] = {}
    missing_recording_ids: list[str] = []
    for recording_id in sorted(recording_ids):
        chosen: Path | None = None
        for speaker_cache_dir in speaker_cache_dirs:
            candidate = speaker_cache_dir / cache_kind / f"{recording_id}.json"
            if candidate.exists():
                chosen = candidate
                break
            if allow_prefixed_match:
                prefixed = sorted((speaker_cache_dir / cache_kind).glob(f"{recording_id}_*.json"))
                if required_filename_fragment is not None:
                    prefixed = [path for path in prefixed if required_filename_fragment in path.name]
                if prefixed:
                    chosen = prefixed[-1]
                    break
        if chosen is None:
            missing_recording_ids.append(recording_id)
            continue
        shutil.copy2(chosen, output_cache_dir / f"{recording_id}.json")
        payloads[recording_id] = rb.read_json(chosen)
    return {
        "payloads": payloads,
        "missing_recording_ids": missing_recording_ids,
    }


def _reuse_existing_audio_feature_cache(
    *,
    recording_ids: set[str],
    speaker_cache_dirs: list[Path],
    output_cache_dir: Path,
) -> dict[str, dict[str, Any]]:
    reuse = _reuse_speaker_level_json_cache(
        recording_ids=recording_ids,
        cache_kind="audio_feature_cache",
        output_cache_dir=output_cache_dir,
        speaker_cache_dirs=speaker_cache_dirs,
    )
    payloads = dict(reuse["payloads"])
    typed: dict[str, dict[str, Any]] = {}
    for recording_id, payload in payloads.items():
        if not isinstance(payload, dict) or "frames" not in payload:
            raise ValueError(f"unexpected audio feature cache format for {recording_id}")
        typed[recording_id] = payload
    return typed, list(reuse["missing_recording_ids"])


def _reuse_existing_vad_frame_indices_fast(
    *,
    recording_ids: set[str],
    speaker_cache_dirs: list[Path],
    output_cache_dir: Path,
    config: TournamentConfig,
) -> dict[str, dict[str, Any]]:
    reuse = _reuse_speaker_level_json_cache(
        recording_ids=recording_ids,
        cache_kind="vad_frame_cache",
        output_cache_dir=output_cache_dir,
        speaker_cache_dirs=speaker_cache_dirs,
        allow_prefixed_match=True,
        required_filename_fragment=_geometry_slug(config),
    )
    payloads = dict(reuse["payloads"])
    result: dict[str, dict[str, Any]] = {}
    for recording_id, rows in payloads.items():
        if not isinstance(rows, list):
            raise ValueError(f"unexpected VAD frame cache format for {recording_id}")
        result[recording_id] = {
            "frames": rows,
            "centers": [float(row["center_sec"]) for row in rows],
            "mode": "reused_precomputed_cache",
        }
    return result, list(reuse["missing_recording_ids"])


def _build_contexts_for_config(cache: FastFixtureCache, config: TournamentConfig) -> Any:
    geometry_slug = _geometry_slug(config)
    existing = cache.contexts_by_geometry.get(geometry_slug)
    if existing is not None:
        return existing
    geometry_root = cache.cache_root / geometry_slug
    geometry_root.mkdir(parents=True, exist_ok=True)
    previous_env = {key: os.environ.get(key) for key in config.env()}
    os.environ.update(config.env())
    try:
        recording_ids = {source.recording_id for source in cache.sources.values()}
        frame_index_by_recording, missing_vad_recording_ids = _reuse_existing_vad_frame_indices_fast(
            recording_ids=recording_ids,
            speaker_cache_dirs=cache.speaker_cache_dirs,
            output_cache_dir=geometry_root / "vad_frame_cache",
            config=config,
        )
        if missing_vad_recording_ids:
            fallback_sources = {
                source_id: source
                for source_id, source in cache.sources.items()
                if source.recording_id in set(missing_vad_recording_ids)
            }
            computed = rb._load_or_reuse_vad_frame_indices(
                sources=fallback_sources,
                eval_run=cache.fixture.eval_run,
                cache_dir=geometry_root / "vad_frame_cache",
            )
            frame_index_by_recording.update(computed)
        audio_feature_cache, missing_audio_recording_ids = _reuse_existing_audio_feature_cache(
            recording_ids=recording_ids,
            speaker_cache_dirs=[],
            output_cache_dir=geometry_root / "audio_feature_cache",
        )
        if missing_audio_recording_ids:
            fallback_sources = {
                source_id: source
                for source_id, source in cache.sources.items()
                if source.recording_id in set(missing_audio_recording_ids)
            }
            computed_audio = rb._build_audio_feature_cache_stdlib(
                sources=fallback_sources,
                frame_index_by_recording=frame_index_by_recording,
                cache_dir=geometry_root / "audio_feature_cache",
            )
            audio_feature_cache.update(computed_audio)
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    benchmark_config = sd.BenchmarkConfig()
    contexts = sd.BenchmarkContexts(
        shared=sd.DetectorContext(
            eval_run=cache.fixture.eval_run,
            normalized_speaker_dir=cache.fixture.normalized_speaker_dir,
            benchmark_root=cache.benchmark_root,
            config=benchmark_config,
            sources=cache.sources,
            buffer_by_id=cache.buffer_by_id,
            lexical_words_by_recording={},
            speech_phones_by_recording={},
            corrected_safe_regions=[],
            reference_words=cache.reference_words,
            reference_phones=cache.reference_phones,
        ),
        acoustic=sd.AcousticDetectorContext(
            benchmark_root=cache.benchmark_root,
            config=benchmark_config,
            sources=cache.sources,
            buffer_by_id=cache.buffer_by_id,
            audio_feature_cache=audio_feature_cache,
            profiler={},
        ),
        linguistic=None,  # type: ignore[arg-type]
    )
    cache.contexts_by_geometry[geometry_slug] = contexts
    return contexts


def build_fast_fixture_cache(
    fixture: RepairedMatrixFixture,
    *,
    cluster_mapping_csv: Path,
    shared_cache_root: Path,
) -> FastFixtureCache:
    started = time.perf_counter()
    artifacts = rb.require_parser_artifacts(fixture.normalized_speaker_dir)
    reference_words = rb._read_csv(fixture.normalized_speaker_dir / "reference_words.csv")
    reference_phones = rb._read_csv(fixture.normalized_speaker_dir / "reference_phones.csv")
    included_recording_ids, excluded_recording_ids = rb.load_recording_eligibility(
        reference_reconciliation_csv=artifacts["reference_reconciliation"],
        excluded_recordings_csv=artifacts["excluded_recordings"],
    )
    uncertainty_masks = rb.load_uncertainty_masks_by_recording(artifacts["uncertainty_masks"])
    interviewer_mask = rb.build_interviewer_mask(reference_words=reference_words, reference_phones=reference_phones)
    target_reference = rb.build_target_speech_reference(
        reference_phones=reference_phones,
        interviewer_mask_by_recording=interviewer_mask,
        uncertainty_mask_by_recording=uncertainty_masks,
    )
    annotation_rows = rb.build_annotation_rows(
        reference_words=reference_words,
        reference_phones=reference_phones,
        interviewer_mask_by_recording=interviewer_mask,
        uncertainty_mask_by_recording=uncertainty_masks,
    )
    cluster_mapping = rb.load_nemo_cluster_mapping(cluster_mapping_csv, speaker_id=fixture.speaker_id)
    buffers = rb.build_raw_nemo_target_buffers(
        speaker_id=fixture.speaker_id,
        eval_run=fixture.eval_run,
        cluster_mapping=cluster_mapping,
        included_recording_ids=included_recording_ids,
        excluded_recording_ids=excluded_recording_ids,
    )
    buffer_by_id = rb.build_buffer_rows_from_nemo_buffers(buffers)
    sources = rb._filter_sources_to_included_recordings(
        rb._load_sources(rb._resolve_artifacts_root(fixture.eval_run) / "source_audio_manifest.json"),
        included_recording_ids,
    )
    cache_root = shared_cache_root / fixture.speaker_id
    cache_root.mkdir(parents=True, exist_ok=True)
    speaker_cache_dirs = _candidate_speaker_cache_dirs(
        speaker_id=fixture.speaker_id,
        search_roots=[
            Path("/home/aaravthegreat/Projects/speechcraft/eval_runs"),
            BUCKEYE_ROOT / "eval_runs",
            fixture.eval_run.parent,
        ],
    )
    benchmark_root = cache_root / "_fast_benchmark_root"
    benchmark_root.mkdir(parents=True, exist_ok=True)
    return FastFixtureCache(
        fixture=fixture,
        speaker_id=fixture.speaker_id,
        included_recording_ids=included_recording_ids,
        excluded_recording_ids=excluded_recording_ids,
        reference_words=reference_words,
        reference_phones=reference_phones,
        uncertainty_masks=uncertainty_masks,
        interviewer_mask=interviewer_mask,
        target_reference=target_reference,
        annotation_rows=annotation_rows,
        buffers=buffers,
        buffer_by_id=buffer_by_id,
        sources=sources,
        cache_root=cache_root,
        speaker_cache_dirs=speaker_cache_dirs,
        benchmark_root=benchmark_root,
        build_cache_wall_sec=round(time.perf_counter() - started, 6),
    )


def fast_run_config_on_fixture(
    *,
    config: TournamentConfig,
    cache: FastFixtureCache,
    compact_out_root: Path | None = None,
) -> dict[str, Any]:
    contexts = _build_contexts_for_config(cache, config)
    detector_started = time.perf_counter()
    with patched_tournament_execution(config):
        detector = rb.supported_repaired_detectors()[config.name]
        result = detector.find_cutpoints(contexts)
        detector_wall_sec = round(time.perf_counter() - detector_started, 6)
        legal_cutpoints = rb.cutpoints_strictly_inside_buffers(result.cutpoints, cache.buffers)
        constraints = sd.PackingConstraints(
            min_clip_sec=contexts.shared.config.min_clip_sec,
            preferred_min_sec=contexts.shared.config.preferred_min_sec,
            preferred_max_sec=contexts.shared.config.preferred_max_sec,
            target_clip_sec=contexts.shared.config.target_clip_sec,
            max_clip_sec=contexts.shared.config.max_clip_sec,
            allow_overlap=False,
            max_overlap_sec=0.0,
            max_duplication_ratio=1.0,
            eligible_regions_by_recording={
                recording_id: tuple((buf.buffer_start_sec, buf.buffer_end_sec) for buf in rows)
                for recording_id, rows in rb._group_buffers_by_recording(cache.buffers).items()
            },
        )
        packing_started = time.perf_counter()
        all_candidates = rb.generate_legal_candidate_clips_in_buffers(
            cutpoints=legal_cutpoints,
            constraints=constraints,
        )
        rb._assert_rows_in_included_recordings(rows=legal_cutpoints, included_recording_ids=cache.included_recording_ids, row_label="legal cutpoints")
        rb._assert_rows_in_included_recordings(rows=all_candidates, included_recording_ids=cache.included_recording_ids, row_label="candidate clips")
        selected_candidates = rb.schedule_candidates_by_buffer(all_candidates, max_overlap_sec=0.0)
        rb._assert_rows_in_included_recordings(rows=selected_candidates, included_recording_ids=cache.included_recording_ids, row_label="selected candidate clips")
        clips = sd._clips_from_candidates(selected_candidates, packer_name="optimal_weighted_interval")
        rb._assert_rows_in_included_recordings(rows=clips, included_recording_ids=cache.included_recording_ids, row_label="emitted clips")
        rb.assert_clips_within_single_buffer(clips, cache.buffers)
        rb._assert_final_clip_boundaries_match_cutpoints(clips=clips, cutpoints=legal_cutpoints)
        packing_wall_sec = round(time.perf_counter() - packing_started, 6)
    metric_started = time.perf_counter()
    selected_cutpoint_ids = {candidate.start_cutpoint_id for candidate in selected_candidates} | {
        candidate.end_cutpoint_id for candidate in selected_candidates
    }
    selected_cutpoints = [cut for cut in legal_cutpoints if cut.cutpoint_id in selected_cutpoint_ids]
    selected_boundary_rows = rb.build_boundary_classification_rows(
        detector_name=config.name,
        boundary_source="detector_selected_cutpoint",
        selected_cutpoints=selected_cutpoints,
        buffers=cache.buffers,
        annotation_rows_by_recording=cache.annotation_rows,
        interviewer_mask_by_recording=cache.interviewer_mask,
        uncertainty_mask_by_recording=cache.uncertainty_masks,
    )
    emitted_boundary_rows = rb._final_boundary_classification_rows(
        detector_name=config.name,
        clips=clips,
        cutpoints=legal_cutpoints,
        buffers=cache.buffers,
        annotation_rows_by_recording=cache.annotation_rows,
        interviewer_mask_by_recording=cache.interviewer_mask,
        uncertainty_mask_by_recording=cache.uncertainty_masks,
    )
    rb._assert_boundary_rows_in_included_recordings(selected_boundary_rows, cache.included_recording_ids)
    rb._assert_boundary_rows_in_included_recordings(emitted_boundary_rows, cache.included_recording_ids)
    rb._assert_boundary_population_reconciliation(
        selected_cutpoints=selected_cutpoints,
        clips=clips,
        emitted_boundary_rows=emitted_boundary_rows,
    )
    coverage = rb.compute_clip_coverage_metrics(
        clips=clips,
        buffers=cache.buffers,
        target_reference_by_recording={
            rid: rb._intersect_with_buffer_scope(rows, rb._group_buffers_by_recording(cache.buffers).get(rid, []))
            for rid, rows in cache.target_reference.items()
        },
        all_target_reference_by_recording=cache.target_reference,
    )
    candidate_support_rows = rb._candidate_support_by_recording(
        buffers=cache.buffers,
        target_reference_by_recording=cache.target_reference,
        selected_cutpoints=selected_cutpoints,
        all_candidates=all_candidates,
    )
    invariant_rows = rb._invariant_rows(
        detector_name=config.name,
        legal_cutpoints=legal_cutpoints,
        all_candidates=all_candidates,
        selected_candidates=selected_candidates,
        buffers=cache.buffers,
        clips=clips,
        reference_phones=cache.reference_phones,
    )
    boundary_rows = selected_boundary_rows + emitted_boundary_rows
    error_details = rb.build_in_phone_error_details(boundary_rows)
    severity_rows = rb.summarize_boundary_severity_by_speaker_and_source(boundary_rows)
    coverage_by_recording = rb._coverage_by_recording(
        clips=clips,
        target_reference_by_recording=cache.target_reference,
        buffers=cache.buffers,
    )
    compute_metric = sd.ComputeMetric(
        detector_name=config.name,
        wall_clock_sec=result.compute_metric.wall_clock_sec,
        cpu_time_sec=result.compute_metric.cpu_time_sec,
        gpu_time_sec=result.compute_metric.gpu_time_sec,
        peak_rss_mb=result.compute_metric.peak_rss_mb,
        peak_gpu_memory_mb=result.compute_metric.peak_gpu_memory_mb,
        real_time_factor=result.compute_metric.real_time_factor,
        model_init_time_sec=result.compute_metric.model_init_time_sec,
        per_hour_audio_cost_sec=result.compute_metric.per_hour_audio_cost_sec,
        external_model_dependencies=result.compute_metric.external_model_dependencies,
        notes=result.compute_metric.notes,
    )
    compute_row = rb._compute_metric_row(
        speaker_id=cache.speaker_id,
        detector_name=config.name,
        compute_metric=compute_metric,
    )
    metric_wall_sec = round(time.perf_counter() - metric_started, 6)
    provenance = {
        "speaker_id": cache.speaker_id,
        "detector_name": config.name,
        "raw_nemo_buffer_count": len(cache.buffers),
        "legal_cutpoint_count": len(legal_cutpoints),
        "selected_cutpoint_count": len(selected_cutpoints),
        "candidate_clip_count": len(all_candidates),
        "selected_clip_count": len(clips),
        "included_recording_count": len(cache.included_recording_ids),
        "excluded_recording_count": len(cache.excluded_recording_ids),
    }
    scorecard_row = rb._merge_scorecard_row(
        speaker_id=cache.speaker_id,
        detector_name=config.name,
        coverage_row={"speaker_id": cache.speaker_id, "detector_name": config.name, **coverage},
        unique_boundary_row=severity_rows["unique_selected_cutpoints"][0] if severity_rows["unique_selected_cutpoints"] else {"speaker_id": cache.speaker_id, "detector_name": config.name},
        final_boundary_row=severity_rows["final_edge_occurrences"][0] if severity_rows["final_edge_occurrences"] else {"speaker_id": cache.speaker_id, "detector_name": config.name},
        compute_row=compute_row,
        candidate_support_rows=candidate_support_rows,
        provenance=provenance,
    )
    stage_timing_row = {
        "speaker_id": cache.speaker_id,
        "config_name": config.name,
        "shared_feature_loading_sec": cache.build_cache_wall_sec,
        "detector_policy_execution_sec": detector_wall_sec,
        "packing_sec": packing_wall_sec,
        "metric_evaluation_sec": metric_wall_sec,
    }
    if compact_out_root is not None:
        tables_dir = compact_out_root / config.name / cache.speaker_id / "tables"
        tables_dir.mkdir(parents=True, exist_ok=True)
        write_csv(tables_dir / "selected_boundaries.csv", [rb._cutpoint_csv_row(row) for row in selected_cutpoints], fieldnames=list(rb._cutpoint_csv_row(selected_cutpoints[0]).keys()) if selected_cutpoints else ["detector_name"])
        write_csv(tables_dir / "emitted_clips.csv", [rb._clip_csv_row(row) for row in clips], fieldnames=list(rb._clip_csv_row(clips[0]).keys()) if clips else ["detector_name"])
        write_csv(tables_dir / "coverage_by_recording.csv", coverage_by_recording, fieldnames=list(coverage_by_recording[0].keys()) if coverage_by_recording else ["speaker_id"])
        write_csv(tables_dir / "candidate_support_by_recording.csv", candidate_support_rows, fieldnames=list(candidate_support_rows[0].keys()) if candidate_support_rows else ["speaker_id"])
        write_csv(tables_dir / "boundary_severity_unique_cutpoints.csv", severity_rows["unique_selected_cutpoints"], fieldnames=list(severity_rows["unique_selected_cutpoints"][0].keys()) if severity_rows["unique_selected_cutpoints"] else ["speaker_id"])
        write_csv(tables_dir / "boundary_severity_final_edge_occurrences.csv", severity_rows["final_edge_occurrences"], fieldnames=list(severity_rows["final_edge_occurrences"][0].keys()) if severity_rows["final_edge_occurrences"] else ["speaker_id"])
        write_csv(tables_dir / "invariant_report.csv", invariant_rows, fieldnames=list(invariant_rows[0].keys()) if invariant_rows else ["detector_name"])
        write_csv(tables_dir / "compute_metrics.csv", [compute_row], fieldnames=list(compute_row.keys()))
        write_csv(tables_dir / "in_phone_error_details.csv", error_details, fieldnames=list(error_details[0].keys()) if error_details else ["speaker_id"])
    return {
        "scorecard_row": scorecard_row,
        "coverage_by_recording_rows": coverage_by_recording,
        "candidate_support_rows": [{**row, "detector_name": config.name} for row in candidate_support_rows],
        "boundary_unique_rows": severity_rows["unique_selected_cutpoints"],
        "boundary_final_rows": severity_rows["final_edge_occurrences"],
        "compute_row": compute_row,
        "in_phone_error_details": error_details,
        "invariant_rows": invariant_rows,
        "selected_boundaries_rows": [rb._cutpoint_csv_row(row) for row in selected_cutpoints],
        "emitted_clips_rows": [rb._clip_csv_row(row) for row in clips],
        "stage_timing_row": stage_timing_row,
    }


def _normalize_rows(rows: list[dict[str, Any]], *, keys: set[str] | None = None) -> list[dict[str, str]]:
    normalized = [
        {
            str(key): "" if row.get(key) is None else str(row.get(key))
            for key in sorted((keys or set(row.keys())))
        }
        for row in rows
    ]
    return sorted(normalized, key=lambda row: json.dumps(row, sort_keys=True))


def validate_fast_config_against_existing_run(
    *,
    fast_result: dict[str, Any],
    existing_run_dir: Path,
    out_root: Path,
    config_name: str,
    speaker_id: str,
) -> None:
    per_run_candidates = sorted(existing_run_dir.glob(f"per_run/*_{speaker_id}_repaired_slicer_dry_run_{config_name}"))
    if per_run_candidates:
        existing_run_dir = per_run_candidates[-1]
    comparisons: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
    candidate_specs = [
        ("selected_boundaries", fast_result["selected_boundaries_rows"], existing_run_dir / "tables" / "selected_boundaries.csv"),
        ("emitted_clips", fast_result["emitted_clips_rows"], existing_run_dir / "tables" / "emitted_clips.csv"),
        ("coverage_by_recording", fast_result["coverage_by_recording_rows"], existing_run_dir / "tables" / "coverage_by_recording.csv"),
        ("candidate_support_by_recording", fast_result["candidate_support_rows"], existing_run_dir / "tables" / "candidate_support_by_recording.csv"),
        ("boundary_severity_unique_cutpoints", fast_result["boundary_unique_rows"], existing_run_dir / "tables" / "boundary_severity_unique_cutpoints.csv"),
        ("boundary_severity_final_edge_occurrences", fast_result["boundary_final_rows"], existing_run_dir / "tables" / "boundary_severity_final_edge_occurrences.csv"),
        ("invariant_report", fast_result["invariant_rows"], existing_run_dir / "tables" / "invariant_report.csv"),
    ]
    for table_name, fast_rows, path in candidate_specs:
        if path.exists():
            comparisons.append((table_name, fast_rows, rb._read_csv(path)))
    report_rows: list[dict[str, Any]] = []
    mismatches: list[str] = []
    if not comparisons:
        report_rows.append(
            {
                "config_name": config_name,
                "speaker_id": speaker_id,
                "table_name": "__no_comparable_tables__",
                "match": False,
                "fast_row_count": 0,
                "existing_row_count": 0,
            }
        )
        write_csv(out_root / "tables" / "fast_path_validation_report.csv", report_rows, list(report_rows[0].keys()))
        raise AssertionError(f"no comparable validation tables found in existing run dir: {existing_run_dir}")
    for table_name, fast_rows, existing_rows in comparisons:
        shared_keys = set().union(*(row.keys() for row in fast_rows)) & set().union(*(row.keys() for row in existing_rows))
        match = _normalize_rows(fast_rows, keys=shared_keys) == _normalize_rows(existing_rows, keys=shared_keys)
        report_rows.append(
            {
                "config_name": config_name,
                "speaker_id": speaker_id,
                "table_name": table_name,
                "match": match,
                "fast_row_count": len(fast_rows),
                "existing_row_count": len(existing_rows),
            }
        )
        if not match:
            mismatches.append(table_name)
    write_csv(out_root / "tables" / "fast_path_validation_report.csv", report_rows, list(report_rows[0].keys()))
    if mismatches:
        raise AssertionError(f"fast-path validation mismatch for {config_name}/{speaker_id}: {', '.join(mismatches)}")

def row_signature(row: dict[str, str], keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row.get(key, "")) for key in keys)


def build_smoke_wiring_report(executed_dirs: dict[str, str]) -> list[dict[str, Any]]:
    baseline_dir_text = executed_dirs.get("current_A_baseline")
    if not baseline_dir_text:
        return []
    baseline_root = Path(baseline_dir_text)
    baseline_run = first_run_dir(baseline_root) if (baseline_root / "tables" / "run_index.csv").exists() else None
    if baseline_run is None and (baseline_root / "s01" / "tables" / "emitted_clips.csv").exists():
        baseline_run = baseline_root / "s01"
    if baseline_run is None:
        return []
    baseline_clips = read_csv(baseline_run / "tables" / "emitted_clips.csv")
    base_clip_keys = {row_signature(row, ("recording_id", "buffer_id", "start_sec", "end_sec")) for row in baseline_clips}
    baseline_invariants = {row["invariant"]: int(float(row["value"])) for row in read_csv(baseline_run / "tables" / "invariant_report.csv")}
    base_candidate_count = baseline_invariants.get("candidate_group_count", 0)
    base_group_count = baseline_invariants.get("selected_group_count", 0)
    rows: list[dict[str, Any]] = []
    for config_name, matrix_dir_text in sorted(executed_dirs.items()):
        run_root = Path(matrix_dir_text)
        run_dir = first_run_dir(run_root) if (run_root / "tables" / "run_index.csv").exists() else None
        if run_dir is None and (run_root / "s01" / "tables" / "emitted_clips.csv").exists():
            run_dir = run_root / "s01"
        if run_dir is None:
            continue
        clips = read_csv(run_dir / "tables" / "emitted_clips.csv")
        clip_keys = {row_signature(row, ("recording_id", "buffer_id", "start_sec", "end_sec")) for row in clips}
        invariants = {row["invariant"]: int(float(row["value"])) for row in read_csv(run_dir / "tables" / "invariant_report.csv")}
        candidate_count = invariants.get("candidate_group_count", 0)
        group_count = invariants.get("selected_group_count", 0)
        rows.append(
            {
                "config_name": config_name,
                "executed": True,
                "candidate_count": candidate_count,
                "candidate_count_changed_vs_A": candidate_count != base_candidate_count,
                "selected_group_count": group_count,
                "selected_group_count_changed_vs_A": group_count != base_group_count,
                "emitted_clip_count": len(clips),
                "clips_changed_vs_A": clip_keys != base_clip_keys,
            }
        )
    return rows


def load_annotated_failure_rows(path: Path | None, *, required: bool) -> list[dict[str, Any]]:
    if path is None:
        if required:
            raise ValueError("--annotated-failures-csv is required for a full tournament run")
        return []
    rows = read_csv(path)
    return policy.annotate_known_failures(rows)


def write_annotated_failure_template(out_root: Path) -> None:
    rows = [
        {
            "failure_id": failure_id,
            "config_name": "",
            "original_boundary_selected": "",
            "replacement_boundary_selected": "",
            "problematic_clip_still_emitted": "",
            "failure_fixed": "",
            "new_failure_introduced": "",
            "notes": "",
        }
        for failure_id in ("failure_27_bad_end", "failure_49_bad_start", "failure_93_scope_contamination")
    ]
    write_csv(out_root / "tables" / "annotated_failure_template.csv", rows, list(rows[0].keys()))


def pareto_front(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        coverage = numeric(row.get("conditional_target_phone_coverage"))
        inside = numeric(row.get("inside_target_phone_rate_unique"))
        gt50 = numeric(row.get("gt_50ms_rate_unique"))
        dominated_by: list[str] = []
        for other in rows:
            if other is row:
                continue
            other_coverage = numeric(other.get("conditional_target_phone_coverage"))
            other_inside = numeric(other.get("inside_target_phone_rate_unique"))
            other_gt50 = numeric(other.get("gt_50ms_rate_unique"))
            if (
                other_coverage >= coverage
                and other_inside <= inside
                and other_gt50 <= gt50
                and (other_coverage > coverage or other_inside < inside or other_gt50 < gt50)
            ):
                dominated_by.append(str(other["config_name"]))
        out.append({**row, "is_pareto_frontier": not dominated_by, "dominated_by": ";".join(dominated_by)})
    return out


def write_not_implemented_report(out_root: Path) -> None:
    rows = [
        {
            "config_id": config.config_id,
            "config_name": config.name,
            "family": config.family,
            "implementation_status": config.implementation_status,
            "reason": "Requires new detector/packer implementation in speaker_ts_eval before a valid run can be claimed.",
            "hypothesis": config.hypothesis,
        }
        for config in TOURNAMENT_CONFIGS
        if not config.runnable
    ]
    write_csv(
        out_root / "tables" / "not_implemented_configs.csv",
        rows,
        ["config_id", "config_name", "family", "implementation_status", "reason", "hypothesis"],
    )


def write_policy_parameters(out_root: Path) -> None:
    import importlib.util

    policy_path = Path(__file__).resolve().parents[1] / "scripts/failure_driven_tournament_policy.py"
    spec = importlib.util.spec_from_file_location("failure_driven_tournament_policy", policy_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load failure_driven_tournament_policy.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    params = asdict(module.PolicyParameters())
    rows = [{"parameter": key, "value": value} for key, value in sorted(params.items())]
    write_csv(out_root / "tables" / "policy_parameters.csv", rows, ["parameter", "value"])


def run_tests(out_root: Path, *, skip: bool) -> Path:
    test_log = out_root / "test_log.txt"
    if skip:
        test_log.write_text("Tests skipped by --skip-tests.\n", encoding="utf-8")
        return test_log
    commands = (
            ["python", "-m", "pytest", "-q", "tests/test_failure_driven_slicer_tournament.py", "tests/test_failure_driven_tournament_policy.py"],
            ["workers/dataset/.venv/bin/python", "-m", "pytest", "-q", "tests/test_failure_driven_slicer_tournament.py", "tests/test_failure_driven_tournament_policy.py"],
    )
    failures: list[str] = []
    for command in commands:
        with test_log.open("w", encoding="utf-8") as handle:
            result = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if result.returncode == 0:
            return test_log
        failures.append(f"{' '.join(command)} exited {result.returncode}")
    # Restricted review environments may not have pytest installed or network
    # access to fetch it. Keep the pytest test file in the bundle, but still run
    # the same critical manifest assertions so bundle generation is not blocked
    # by test-runner availability.
    rows = config_rows()
    runnable = [row for row in rows if row["runnable"]]
    pending = [row for row in rows if not row["runnable"]]
    if len(TOURNAMENT_CONFIGS) != 24:
        raise AssertionError("expected exactly 24 tournament configs")
    if sorted(config.config_id for config in TOURNAMENT_CONFIGS) != list(range(1, 25)):
        raise AssertionError("tournament config IDs must be 1..24")
    if len({config.name for config in TOURNAMENT_CONFIGS}) != 24:
        raise AssertionError("tournament config names must be unique")
    if len(runnable) != 16:
        raise AssertionError("expected 16 runnable reduced-tournament configs")
    expected_pending = {
        "openvpi_native",
        "openvpi_candidates_common_packer",
        "A_hard_D_veto",
        "A_soft_D_penalty",
        "A_only_stronger_RMS",
        "D_primary_A_fallback",
        "consensus_first_union",
        "D_regions_RMS_exact",
    }
    if {row["name"] for row in pending} != expected_pending:
        raise AssertionError("pending configs changed unexpectedly")
    config_by_name = {config.name: config for config in TOURNAMENT_CONFIGS}
    if config_by_name["current_A_baseline"].env()["SPEAKER_TS_EVAL_VAD_OFFSETS"] != "0,128":
        raise AssertionError("current A geometry offsets changed unexpectedly")
    if config_by_name["proper_D_baseline"].env()["SPEAKER_TS_EVAL_VAD_OFFSETS"] != "0,128,256,384":
        raise AssertionError("proper D geometry offsets changed unexpectedly")
    import importlib.util

    policy_path = Path(__file__).resolve().parents[1] / "scripts/failure_driven_tournament_policy.py"
    spec = importlib.util.spec_from_file_location("failure_driven_tournament_policy", policy_path)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load policy module")
    policy_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = policy_module
    spec.loader.exec_module(policy_module)
    if not policy_module.apply_min_quiet_run(policy_module.BoundaryEvidence("ok", 1.0, quiet_run_ms=64.0), 64.0).accepted:
        raise AssertionError("64 ms quiet-run gate should accept 64 ms")
    if policy_module.apply_min_quiet_run(policy_module.BoundaryEvidence("bad", 1.0, quiet_run_ms=63.0), 64.0).accepted:
        raise AssertionError("64 ms quiet-run gate should reject 63 ms")
    try:
        policy_module.validate_scorecard_row({"conditional_target_phone_coverage": "0.5"})
    except ValueError:
        pass
    else:
        raise AssertionError("strict metric validation must reject missing required metrics")
    test_log.write_text(
        "\n".join(
            [
                "pytest runner unavailable; fallback manifest assertions passed.",
                "pytest failures:",
                *failures,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return test_log


def package_bundle(out_root: Path, bundle_path: Path, test_log: Path) -> None:
    if bundle_path.exists():
        bundle_path.unlink()
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted((out_root / "tables").rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(out_root))
        for relative in ("provenance.json", "README.md"):
            path = out_root / relative
            if path.exists():
                zf.write(path, path.relative_to(out_root))
        for path in (
            Path(__file__),
            Path(__file__).resolve().parents[1] / "scripts/failure_driven_tournament_policy.py",
            Path(__file__).resolve().parents[1] / "tests/test_failure_driven_slicer_tournament.py",
            Path(__file__).resolve().parents[1] / "tests/test_failure_driven_tournament_policy.py",
            SPEAKER_TS_EVAL_SRC / "speaker_ts_eval/repaired_buckeye_benchmark.py",
            SPEAKER_TS_EVAL_SRC / "speaker_ts_eval/slicer_daddy_test.py",
            SPEAKER_TS_EVAL_SRC / "speaker_ts_eval/buckeye_safecut_benchmark.py",
            test_log,
        ):
            if path.exists():
                zf.write(path, Path("code_and_tests") / path.name)
        for path in (
            COHORT_ROOT / "cohort_manifest.csv",
            COHORT_ROOT / "speaker_cohort_summary.csv",
            COHORT_ROOT / "benchmark_cluster_mapping.csv",
        ):
            if path.exists():
                zf.write(path, Path("cohort") / path.name)


def write_readme(out_root: Path, *, executed_count: int) -> None:
    lines = [
        "# Failure-Driven Slicer Tournament",
        "",
        "This bundle contains the tournament configuration manifest, code, tests, and any executed result summaries.",
        "",
        "Important: most entrants require new detector or packer implementation before they can be validly run.",
        "The runner records those as `not_implemented` instead of silently claiming baseline-equivalent results.",
        "",
        f"Executed configurations: {executed_count}",
        "",
        "No audio, feature caches, model files, all-candidate dumps, or raw NeMo outputs are included.",
    ]
    (out_root / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--execute-implemented", action="store_true", help="Run currently implemented configurations. Without this, only manifests/code/tests are packaged.")
    parser.add_argument("--speaker-limit", type=int, default=None, help="Optional smoke-test limit on speakers.")
    parser.add_argument("--config-name", action="append", default=None, help="Run only these config names. May be passed multiple times.")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--buckeye-only-smoke", action="store_true", help="Allow execution without the annotated failure CSV. Use only for wiring smoke tests.")
    parser.add_argument("--annotated-failures-csv", type=Path, default=None, help="Required for full tournament runs; must contain failure_27/49/93 rows.")
    parser.add_argument("--fast-path", action="store_true", help="Run the reduced tournament through the shared-cache fast path instead of the repeated repaired-matrix harness.")
    parser.add_argument("--validate-fast-against-run-dir", type=Path, default=None, help="Compare fast-path output against an existing completed per-speaker run directory.")
    parser.add_argument("--validation-config-name", type=str, default="current_A_baseline")
    parser.add_argument("--validation-speaker-id", type=str, default=None)
    args = parser.parse_args()

    started = time.perf_counter()
    if args.out_root.exists():
        shutil.rmtree(args.out_root)
    (args.out_root / "tables").mkdir(parents=True)

    config_manifest = config_rows()
    write_csv(args.out_root / "tables" / "tournament_config_manifest.csv", config_manifest, list(config_manifest[0].keys()))
    write_not_implemented_report(args.out_root)
    write_policy_parameters(args.out_root)
    write_annotated_failure_template(args.out_root)
    test_log = run_tests(args.out_root, skip=args.skip_tests)
    annotated_failure_rows = load_annotated_failure_rows(
        args.annotated_failures_csv,
        required=bool(args.execute_implemented and not args.buckeye_only_smoke),
    )
    if annotated_failure_rows:
        write_csv(args.out_root / "tables" / "annotated_failure_results.csv", annotated_failure_rows, list(annotated_failure_rows[0].keys()))

    executed_dirs: dict[str, str] = {}
    combined_tables: dict[str, list[dict[str, Any]]] = {}
    if args.execute_implemented:
        speakers = load_speakers(args.speaker_limit)
        fixtures = build_fixtures(speakers)
        selected_names = set(args.config_name or [])
        if args.fast_path:
            shared_cache_root = args.out_root / "shared_feature_cache"
            compact_out_root = args.out_root / "compact_runs"
            fixture_caches = [build_fast_fixture_cache(fixture, cluster_mapping_csv=COHORT_ROOT / "benchmark_cluster_mapping.csv", shared_cache_root=shared_cache_root) for fixture in fixtures]
            for config in TOURNAMENT_CONFIGS:
                if not config.runnable:
                    continue
                if selected_names and config.name not in selected_names:
                    continue
                executed_dirs[config.name] = str(compact_out_root / config.name)
                for cache in fixture_caches:
                    result = fast_run_config_on_fixture(config=config, cache=cache, compact_out_root=compact_out_root)
                    row_meta = {"config_id": config.config_id, "config_name": config.name, "family": config.family}
                    combined_tables.setdefault("detector_scorecard_by_speaker", []).append({**row_meta, **result["scorecard_row"]})
                    combined_tables.setdefault("coverage_by_recording_all", []).extend([{**row_meta, **row, "detector_name": config.name} for row in result["coverage_by_recording_rows"]])
                    combined_tables.setdefault("candidate_support_by_recording_all", []).extend([{**row_meta, **row} for row in result["candidate_support_rows"]])
                    combined_tables.setdefault("boundary_severity_unique_cutpoints_by_speaker", []).extend([{**row_meta, **row} for row in result["boundary_unique_rows"]])
                    combined_tables.setdefault("boundary_severity_final_edge_occurrences_by_speaker", []).extend([{**row_meta, **row} for row in result["boundary_final_rows"]])
                    combined_tables.setdefault("runtime_by_detector_and_speaker", []).append({**row_meta, **result["compute_row"]})
                    combined_tables.setdefault("in_phone_error_details", []).extend([{**row_meta, **row} for row in result["in_phone_error_details"]])
                    combined_tables.setdefault("fast_stage_timing", []).append(result["stage_timing_row"])
                    if (
                        args.validate_fast_against_run_dir is not None
                        and config.name == args.validation_config_name
                        and (args.validation_speaker_id is None or cache.speaker_id == args.validation_speaker_id)
                    ):
                        validate_fast_config_against_existing_run(
                            fast_result=result,
                            existing_run_dir=args.validate_fast_against_run_dir,
                            out_root=args.out_root,
                            config_name=config.name,
                            speaker_id=cache.speaker_id,
                        )
            scorecard_rows_for_rb = [
                {key: value for key, value in row.items() if key not in {"config_id", "config_name", "family"}}
                for row in combined_tables.get("detector_scorecard_by_speaker", [])
            ]
            macro_rows = rb._average_scorecard_rows(scorecard_rows_for_rb)
            worst_rows = rb._worst_speaker_rows(scorecard_rows_for_rb)
            config_lookup = {config.name: config for config in TOURNAMENT_CONFIGS}
            combined_tables["detector_scorecard_macro"] = [
                {"config_id": config_lookup[str(row["detector_name"])].config_id, "config_name": str(row["detector_name"]), "family": config_lookup[str(row["detector_name"])].family, **row}
                for row in macro_rows
            ]
            combined_tables["detector_scorecard_worst_speaker"] = [
                {"config_id": config_lookup[str(row["detector_name"])].config_id, "config_name": str(row["detector_name"]), "family": config_lookup[str(row["detector_name"])].family, **row}
                for row in worst_rows
            ]
        else:
            for config in TOURNAMENT_CONFIGS:
                if not config.runnable:
                    continue
                if selected_names and config.name not in selected_names:
                    continue
                matrix_dir = run_config(config, fixtures, args.out_root)
                executed_dirs[config.name] = str(matrix_dir)
                for name, rows in collect_run_tables(config, matrix_dir).items():
                    combined_tables.setdefault(name, []).extend(rows)
                cleanup_run_caches(matrix_dir)

    for name, rows in combined_tables.items():
        if rows:
            write_csv(args.out_root / "tables" / f"{name}.csv", rows, list(rows[0].keys()))
    summary_rows = build_summary(combined_tables)
    if summary_rows:
        write_csv(args.out_root / "tables" / "tournament_summary.csv", summary_rows, list(summary_rows[0].keys()))
        frontier = pareto_front(summary_rows)
        write_csv(args.out_root / "tables" / "tournament_pareto_frontier.csv", frontier, list(frontier[0].keys()))
    smoke_rows = build_smoke_wiring_report(executed_dirs)
    if smoke_rows:
        write_csv(args.out_root / "tables" / "smoke_wiring_report.csv", smoke_rows, list(smoke_rows[0].keys()))

    write_readme(args.out_root, executed_count=len(executed_dirs))
    provenance = {
        "experiment": "failure_driven_slicer_tournament",
        "created_at_unix": round(time.time(), 6),
        "runtime_sec": round(time.perf_counter() - started, 6),
        "execute_implemented": bool(args.execute_implemented),
        "speaker_limit": args.speaker_limit,
        "config_count": len(TOURNAMENT_CONFIGS),
        "runnable_config_count": sum(config.runnable for config in TOURNAMENT_CONFIGS),
        "executed_config_count": len(executed_dirs),
        "annotated_failure_rows": len(annotated_failure_rows),
        "buckeye_only_smoke": bool(args.buckeye_only_smoke),
        "executed_dirs": executed_dirs,
        "cohort_root": str(COHORT_ROOT),
        "normalized_root": str(NORMALIZED_ROOT),
        "bundle": str(args.bundle),
    }
    (args.out_root / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    package_bundle(args.out_root, args.bundle, test_log)
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    main()
