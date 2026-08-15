"""Pool Phase-1 EvaluationResult values. No alternate metric definitions."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from referee.evaluate import _rate
from referee.types import EvaluationResult


def _require_finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise RuntimeError(f"invalid non-finite {name}: {value!r}")
    return value


def _require_finite_or_none(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    return _require_finite(value, name)


@dataclass(frozen=True)
class PooledMetrics:
    unique_cutpoint_count: int
    inside_phone_count: int
    inside_phone_rate: float | None
    depth_gt_20ms_count: int
    depth_gt_50ms_count: int
    depth_gt_100ms_count: int
    depth_gt_20ms_rate: float | None
    depth_gt_50ms_rate: float | None
    depth_gt_100ms_rate: float | None
    edge_occurrence_count: int
    edge_inside_phone_count: int
    edge_inside_phone_rate: float | None
    edge_depth_gt_20ms_count: int
    edge_depth_gt_50ms_count: int
    edge_depth_gt_100ms_count: int
    edge_depth_gt_20ms_rate: float | None
    edge_depth_gt_50ms_rate: float | None
    edge_depth_gt_100ms_rate: float | None
    eligible_target_speech_sec: float
    retained_target_speech_sec: float
    speech_coverage: float | None
    emitted_audio_sec: float
    clip_count: int

    def to_dict(self) -> dict[str, int | float | None]:
        return {
            "unique_cutpoint_count": self.unique_cutpoint_count,
            "inside_phone_count": self.inside_phone_count,
            "inside_phone_rate": self.inside_phone_rate,
            "depth_gt_20ms_count": self.depth_gt_20ms_count,
            "depth_gt_50ms_count": self.depth_gt_50ms_count,
            "depth_gt_100ms_count": self.depth_gt_100ms_count,
            "depth_gt_20ms_rate": self.depth_gt_20ms_rate,
            "depth_gt_50ms_rate": self.depth_gt_50ms_rate,
            "depth_gt_100ms_rate": self.depth_gt_100ms_rate,
            "edge_occurrence_count": self.edge_occurrence_count,
            "edge_inside_phone_count": self.edge_inside_phone_count,
            "edge_inside_phone_rate": self.edge_inside_phone_rate,
            "edge_depth_gt_20ms_count": self.edge_depth_gt_20ms_count,
            "edge_depth_gt_50ms_count": self.edge_depth_gt_50ms_count,
            "edge_depth_gt_100ms_count": self.edge_depth_gt_100ms_count,
            "edge_depth_gt_20ms_rate": self.edge_depth_gt_20ms_rate,
            "edge_depth_gt_50ms_rate": self.edge_depth_gt_50ms_rate,
            "edge_depth_gt_100ms_rate": self.edge_depth_gt_100ms_rate,
            "eligible_target_speech_sec": self.eligible_target_speech_sec,
            "retained_target_speech_sec": self.retained_target_speech_sec,
            "speech_coverage": self.speech_coverage,
            "emitted_audio_sec": self.emitted_audio_sec,
            "clip_count": self.clip_count,
            "mean_clip_duration_sec": self.mean_clip_duration_sec,
        }

    @property
    def mean_clip_duration_sec(self) -> float | None:
        if self.clip_count == 0:
            return None
        return self.emitted_audio_sec / float(self.clip_count)


def check_evaluation_finite(score: EvaluationResult) -> None:
    _require_finite(score.eligible_target_speech_sec, "eligible_target_speech_sec")
    _require_finite(score.retained_target_speech_sec, "retained_target_speech_sec")
    _require_finite(score.emitted_audio_sec, "emitted_audio_sec")
    _require_finite_or_none(score.speech_coverage, "speech_coverage")
    unique = score.unique_cut_safety
    edge = score.final_edge_safety
    for name, value in (
        ("unique_inside_phone_rate", unique.inside_phone_rate),
        ("unique_gt20_rate", unique.depth_gt_20ms_rate),
        ("unique_gt50_rate", unique.depth_gt_50ms_rate),
        ("unique_gt100_rate", unique.depth_gt_100ms_rate),
        ("edge_inside_phone_rate", edge.inside_phone_rate),
        ("edge_gt20_rate", edge.depth_gt_20ms_rate),
        ("edge_gt50_rate", edge.depth_gt_50ms_rate),
        ("edge_gt100_rate", edge.depth_gt_100ms_rate),
    ):
        _require_finite_or_none(value, name)


def metrics_from_evaluation(score: EvaluationResult) -> PooledMetrics:
    """Copy evaluator fields; rates stay the evaluator's own rates."""
    check_evaluation_finite(score)
    unique = score.unique_cut_safety
    edge = score.final_edge_safety
    return PooledMetrics(
        unique_cutpoint_count=unique.unique_cutpoint_count,
        inside_phone_count=unique.inside_phone_count,
        inside_phone_rate=unique.inside_phone_rate,
        depth_gt_20ms_count=unique.depth_gt_20ms_count,
        depth_gt_50ms_count=unique.depth_gt_50ms_count,
        depth_gt_100ms_count=unique.depth_gt_100ms_count,
        depth_gt_20ms_rate=unique.depth_gt_20ms_rate,
        depth_gt_50ms_rate=unique.depth_gt_50ms_rate,
        depth_gt_100ms_rate=unique.depth_gt_100ms_rate,
        edge_occurrence_count=edge.edge_occurrence_count,
        edge_inside_phone_count=edge.inside_phone_count,
        edge_inside_phone_rate=edge.inside_phone_rate,
        edge_depth_gt_20ms_count=edge.depth_gt_20ms_count,
        edge_depth_gt_50ms_count=edge.depth_gt_50ms_count,
        edge_depth_gt_100ms_count=edge.depth_gt_100ms_count,
        edge_depth_gt_20ms_rate=edge.depth_gt_20ms_rate,
        edge_depth_gt_50ms_rate=edge.depth_gt_50ms_rate,
        edge_depth_gt_100ms_rate=edge.depth_gt_100ms_rate,
        eligible_target_speech_sec=score.eligible_target_speech_sec,
        retained_target_speech_sec=score.retained_target_speech_sec,
        speech_coverage=score.speech_coverage,
        emitted_audio_sec=score.emitted_audio_sec,
        clip_count=score.clip_count,
    )


def _as_int(value: object, name: str) -> int:
    if value is None or value == "":
        raise RuntimeError(f"missing integer field {name}")
    return int(value)


def _as_float(value: object, name: str) -> float:
    if value is None or value == "":
        raise RuntimeError(f"missing float field {name}")
    return float(value)


def _as_optional_float(value: object, name: str) -> float | None:
    if value is None or value == "":
        return None
    return _require_finite(float(value), name)


def pool_metric_dicts(rows: list[dict[str, object]]) -> PooledMetrics:
    """Pool from compact per-recording rows using summed counts, not mean rates."""
    if not rows:
        raise RuntimeError("cannot pool zero evaluation results")
    unique_n = 0
    inside = 0
    gt20 = 0
    gt50 = 0
    gt100 = 0
    edge_n = 0
    edge_inside = 0
    edge_gt20 = 0
    edge_gt50 = 0
    edge_gt100 = 0
    eligible = 0.0
    retained = 0.0
    emitted = 0.0
    clips = 0
    for row in rows:
        unique_n += _as_int(row["unique_cutpoint_count"], "unique_cutpoint_count")
        inside += _as_int(row["inside_phone_count"], "inside_phone_count")
        gt20 += _as_int(row["depth_gt_20ms_count"], "depth_gt_20ms_count")
        gt50 += _as_int(row["depth_gt_50ms_count"], "depth_gt_50ms_count")
        gt100 += _as_int(row["depth_gt_100ms_count"], "depth_gt_100ms_count")
        edge_n += _as_int(row["edge_occurrence_count"], "edge_occurrence_count")
        edge_inside += _as_int(row["edge_inside_phone_count"], "edge_inside_phone_count")
        edge_gt20 += _as_int(row["edge_depth_gt_20ms_count"], "edge_depth_gt_20ms_count")
        edge_gt50 += _as_int(row["edge_depth_gt_50ms_count"], "edge_depth_gt_50ms_count")
        edge_gt100 += _as_int(row["edge_depth_gt_100ms_count"], "edge_depth_gt_100ms_count")
        eligible += _as_float(row["eligible_target_speech_sec"], "eligible_target_speech_sec")
        retained += _as_float(row["retained_target_speech_sec"], "retained_target_speech_sec")
        emitted += _as_float(row["emitted_audio_sec"], "emitted_audio_sec")
        clips += _as_int(row["clip_count"], "clip_count")
    coverage = None if eligible == 0.0 else retained / eligible
    _require_finite_or_none(coverage, "pooled_speech_coverage")
    return PooledMetrics(
        unique_cutpoint_count=unique_n,
        inside_phone_count=inside,
        inside_phone_rate=_rate(inside, unique_n),
        depth_gt_20ms_count=gt20,
        depth_gt_50ms_count=gt50,
        depth_gt_100ms_count=gt100,
        depth_gt_20ms_rate=_rate(gt20, unique_n),
        depth_gt_50ms_rate=_rate(gt50, unique_n),
        depth_gt_100ms_rate=_rate(gt100, unique_n),
        edge_occurrence_count=edge_n,
        edge_inside_phone_count=edge_inside,
        edge_inside_phone_rate=_rate(edge_inside, edge_n),
        edge_depth_gt_20ms_count=edge_gt20,
        edge_depth_gt_50ms_count=edge_gt50,
        edge_depth_gt_100ms_count=edge_gt100,
        edge_depth_gt_20ms_rate=_rate(edge_gt20, edge_n),
        edge_depth_gt_50ms_rate=_rate(edge_gt50, edge_n),
        edge_depth_gt_100ms_rate=_rate(edge_gt100, edge_n),
        eligible_target_speech_sec=eligible,
        retained_target_speech_sec=retained,
        speech_coverage=coverage,
        emitted_audio_sec=emitted,
        clip_count=clips,
    )


def pool_evaluations(scores: list[EvaluationResult]) -> PooledMetrics:
    return pool_metric_dicts([metrics_from_evaluation(score).to_dict() for score in scores])


def evaluation_signature(score: EvaluationResult) -> tuple[object, ...]:
    check_evaluation_finite(score)
    unique = score.unique_cut_safety
    edge = score.final_edge_safety
    return (
        unique.unique_cutpoint_count,
        unique.inside_phone_count,
        unique.depth_gt_20ms_count,
        unique.depth_gt_50ms_count,
        unique.depth_gt_100ms_count,
        edge.edge_occurrence_count,
        edge.inside_phone_count,
        edge.depth_gt_20ms_count,
        edge.depth_gt_50ms_count,
        edge.depth_gt_100ms_count,
        score.eligible_target_speech_sec,
        score.retained_target_speech_sec,
        score.speech_coverage,
        score.emitted_audio_sec,
        score.clip_count,
    )


def comparison_table(pooled_a: PooledMetrics, pooled_d: PooledMetrics) -> list[dict[str, object]]:
    def pct(rate: float | None) -> float | None:
        return None if rate is None else rate * 100.0

    rows = [
        ("speech coverage", pooled_a.speech_coverage, pooled_d.speech_coverage),
        ("retained speech sec", pooled_a.retained_target_speech_sec, pooled_d.retained_target_speech_sec),
        ("emitted sec", pooled_a.emitted_audio_sec, pooled_d.emitted_audio_sec),
        ("clips", pooled_a.clip_count, pooled_d.clip_count),
        ("selected cuts", pooled_a.unique_cutpoint_count, pooled_d.unique_cutpoint_count),
        ("inside-phone %", pct(pooled_a.inside_phone_rate), pct(pooled_d.inside_phone_rate)),
        (">20 ms %", pct(pooled_a.depth_gt_20ms_rate), pct(pooled_d.depth_gt_20ms_rate)),
        (">50 ms %", pct(pooled_a.depth_gt_50ms_rate), pct(pooled_d.depth_gt_50ms_rate)),
        (">100 ms %", pct(pooled_a.depth_gt_100ms_rate), pct(pooled_d.depth_gt_100ms_rate)),
        (
            "final-edge inside-phone %",
            pct(pooled_a.edge_inside_phone_rate),
            pct(pooled_d.edge_inside_phone_rate),
        ),
        (
            "final-edge >50 ms %",
            pct(pooled_a.edge_depth_gt_50ms_rate),
            pct(pooled_d.edge_depth_gt_50ms_rate),
        ),
    ]
    out: list[dict[str, object]] = []
    for name, a_val, d_val in rows:
        delta: float | None
        if a_val is None or d_val is None:
            delta = None
        else:
            delta = float(d_val) - float(a_val)
        out.append({"metric": name, "A": a_val, "D": d_val, "D - A": delta})
    return out


def format_comparison_table(rows: list[dict[str, object]]) -> str:
    def cell(value: object, width: int, numeric: bool) -> str:
        if value is None:
            text = "n/a"
        elif isinstance(value, float):
            text = f"{value:.6f}"
        else:
            text = str(value)
        return text.rjust(width) if numeric else text.ljust(width)

    lines = [
        f"| {'metric':<26} | {'A':>12} | {'D':>12} | {'D - A':>12} |",
        f"| {'-' * 26} | {'-' * 12} | {'-' * 12} | {'-' * 12} |",
    ]
    for row in rows:
        lines.append(
            f"| {cell(row['metric'], 26, False)} | "
            f"{cell(row['A'], 12, True)} | "
            f"{cell(row['D'], 12, True)} | "
            f"{cell(row['D - A'], 12, True)} |"
        )
    return "\n".join(lines)


def assess_qualitative_shape(pooled_a: PooledMetrics, pooled_d: PooledMetrics) -> dict[str, object]:
    """Historical full-corpus shape: A higher yield, D safer. Subset exceptions allowed."""
    identical = (
        pooled_a.speech_coverage == pooled_d.speech_coverage
        and pooled_a.emitted_audio_sec == pooled_d.emitted_audio_sec
        and pooled_a.clip_count == pooled_d.clip_count
        and pooled_a.unique_cutpoint_count == pooled_d.unique_cutpoint_count
        and pooled_a.inside_phone_rate == pooled_d.inside_phone_rate
        and pooled_a.depth_gt_50ms_rate == pooled_d.depth_gt_50ms_rate
    )
    if identical:
        raise RuntimeError(
            "A and D pooled metrics are identical; harness likely collapsed geometries"
        )

    a_higher_yield = (
        (pooled_a.speech_coverage is not None and pooled_d.speech_coverage is not None
         and pooled_a.speech_coverage >= pooled_d.speech_coverage)
        or pooled_a.emitted_audio_sec >= pooled_d.emitted_audio_sec
        or pooled_a.clip_count >= pooled_d.clip_count
    )
    d_safer = (
        (pooled_a.inside_phone_rate is not None and pooled_d.inside_phone_rate is not None
         and pooled_d.inside_phone_rate <= pooled_a.inside_phone_rate)
        or (pooled_a.depth_gt_50ms_rate is not None and pooled_d.depth_gt_50ms_rate is not None
            and pooled_d.depth_gt_50ms_rate <= pooled_a.depth_gt_50ms_rate)
    )
    fully_inverted = (
        pooled_a.speech_coverage is not None
        and pooled_d.speech_coverage is not None
        and pooled_d.speech_coverage > pooled_a.speech_coverage
        and pooled_d.emitted_audio_sec > pooled_a.emitted_audio_sec
        and pooled_d.clip_count > pooled_a.clip_count
        and pooled_a.inside_phone_rate is not None
        and pooled_d.inside_phone_rate is not None
        and pooled_d.inside_phone_rate > pooled_a.inside_phone_rate
        and pooled_a.depth_gt_50ms_rate is not None
        and pooled_d.depth_gt_50ms_rate is not None
        and pooled_d.depth_gt_50ms_rate > pooled_a.depth_gt_50ms_rate
    )
    if fully_inverted:
        raise RuntimeError(
            "A/D qualitative shape is fully inverted vs historical expectation "
            "(D higher yield and less safe on all axes); investigate the harness"
        )
    recovered = bool(a_higher_yield and d_safer)
    return {
        "a_higher_yield": bool(a_higher_yield),
        "d_safer": bool(d_safer),
        "historical_shape_recovered": recovered,
        "note": (
            "A generally higher yield and D generally safer"
            if recovered
            else "subset shape is mixed; historical percentages are not required"
        ),
    }


def paired_recording_comparison(
    rows: list[dict[str, object]],
    *,
    left: str = "current_A",
    right: str = "proper_D",
) -> dict[str, object]:
    """Paired right−left coverage from per-recording rows. Extra contenders are ignored."""
    by_rec: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    for row in rows:
        key = (str(row["speaker_id"]), str(row["recording_id"]))
        geometry = str(row["geometry"])
        by_rec.setdefault(key, {})
        if geometry in by_rec[key]:
            raise RuntimeError(f"duplicate {geometry} row for {key[0]}/{key[1]}")
        by_rec[key][geometry] = row
    diffs: list[float] = []
    right_greater = 0
    right_less = 0
    ties = 0
    right_safer_gt50 = 0
    left_safer_gt50 = 0
    gt50_ties = 0
    gt50_undefined = 0
    for key, geos in sorted(by_rec.items()):
        if left not in geos or right not in geos:
            raise RuntimeError(
                f"paired comparison missing {left}/{right} for {key[0]}/{key[1]}: {sorted(geos)}"
            )
        left_cov = _as_optional_float(geos[left]["speech_coverage"], "speech_coverage")
        right_cov = _as_optional_float(geos[right]["speech_coverage"], "speech_coverage")
        if left_cov is None and right_cov is None:
            ties += 1
            diffs.append(0.0)
        elif left_cov is None or right_cov is None:
            raise RuntimeError(
                f"speech_coverage missing for one geometry on {key[0]}/{key[1]}"
            )
        else:
            delta = right_cov - left_cov
            diffs.append(delta)
            if delta > 0.0:
                right_greater += 1
            elif delta < 0.0:
                right_less += 1
            else:
                ties += 1
        left_gt50 = _as_optional_float(geos[left]["depth_gt_50ms_rate"], "depth_gt_50ms_rate")
        right_gt50 = _as_optional_float(geos[right]["depth_gt_50ms_rate"], "depth_gt_50ms_rate")
        if left_gt50 is None and right_gt50 is None:
            gt50_ties += 1
        elif left_gt50 is None or right_gt50 is None:
            gt50_undefined += 1
        elif right_gt50 < left_gt50:
            right_safer_gt50 += 1
        elif right_gt50 > left_gt50:
            left_safer_gt50 += 1
        else:
            gt50_ties += 1
    if not diffs:
        raise RuntimeError("paired comparison has no recordings")
    mean_delta = statistics.fmean(diffs)
    median_delta = float(statistics.median(diffs))
    return {
        "left": left,
        "right": right,
        "recording_count": len(diffs),
        "d_coverage_greater": right_greater if right == "proper_D" and left == "current_A" else None,
        "d_coverage_less": right_less if right == "proper_D" and left == "current_A" else None,
        "coverage_greater": right_greater,
        "coverage_less": right_less,
        "coverage_ties": ties,
        "mean_d_minus_a_coverage": mean_delta if right == "proper_D" and left == "current_A" else None,
        "median_d_minus_a_coverage": median_delta if right == "proper_D" and left == "current_A" else None,
        "mean_delta_coverage": mean_delta,
        "median_delta_coverage": median_delta,
        "mean_delta_coverage_pp": mean_delta * 100.0,
        "median_delta_coverage_pp": median_delta * 100.0,
        "mean_d_minus_a_coverage_pp": mean_delta * 100.0 if right == "proper_D" and left == "current_A" else None,
        "median_d_minus_a_coverage_pp": median_delta * 100.0 if right == "proper_D" and left == "current_A" else None,
        "d_safer_gt50ms": right_safer_gt50 if right == "proper_D" and left == "current_A" else None,
        "a_safer_gt50ms": left_safer_gt50 if right == "proper_D" and left == "current_A" else None,
        "contender_safer_gt50ms": right_safer_gt50,
        "baseline_safer_gt50ms": left_safer_gt50,
        "gt50ms_ties": gt50_ties,
        "gt50ms_undefined": gt50_undefined,
    }


# Historical proper A/D experiment (trusted qualitative reference, not a pass/fail ruler).
# Mean conditional / end-to-end coverage used a different denominator mix than the
# rebuilt Phase-1 pooled speech_coverage; compare those historical coverage numbers
# cautiously. Counts and safety rates are the closer neighborhood check.
HISTORICAL_PROPER_AD = {
    "recording_count_paired_table": 153,
    "accepted_recording_count": 120,
    "A": {
        "mean_conditional_coverage": 0.6867,
        "mean_end_to_end_coverage": 0.5484,
        "emitted_audio_sec": 26551.0,
        "clip_count": 4983,
        "unique_cutpoint_count": 9310,
        "inside_phone_rate": 0.1260,
        "depth_gt_50ms_rate": 0.0272,
        "edge_inside_phone_rate": 0.1360,
        "edge_depth_gt_50ms_rate": 0.0315,
    },
    "D": {
        "mean_conditional_coverage": 0.6250,
        "mean_end_to_end_coverage": 0.5005,
        "emitted_audio_sec": 24342.0,
        "clip_count": 4518,
        "unique_cutpoint_count": 8557,
        "inside_phone_rate": 0.0965,
        "depth_gt_50ms_rate": 0.0218,
        "edge_inside_phone_rate": 0.0997,
        "edge_depth_gt_50ms_rate": 0.0232,
    },
    "paired": {
        "d_coverage_less": 111,
        "d_coverage_greater": 8,
        "coverage_ties": 34,
        "median_d_minus_a_coverage_pp": -3.61,
        "mean_d_minus_a_coverage_pp": -4.64,
    },
}


def compare_to_historical(
    *,
    recording_count: int,
    pooled_a: PooledMetrics,
    pooled_d: PooledMetrics,
    paired: dict[str, object],
    qualitative: dict[str, object],
) -> dict[str, object]:
    """Neighborhood check against the earlier proper A/D experiment. Does not require equality."""
    flags: list[str] = []
    if recording_count != HISTORICAL_PROPER_AD["accepted_recording_count"]:
        flags.append(
            f"cohort size {recording_count} != historical accepted "
            f"{HISTORICAL_PROPER_AD['accepted_recording_count']}"
        )
    a_higher_yield = bool(qualitative.get("a_higher_yield"))
    d_safer = bool(qualitative.get("d_safer"))
    if not a_higher_yield:
        flags.append("A is not higher yield than D on pooled coverage/emitted/clips")
    if not d_safer:
        flags.append("D is not safer than A on pooled inside-phone or >50 ms rate")
    d_less = int(paired["d_coverage_less"])
    d_greater = int(paired["d_coverage_greater"])
    if d_less <= d_greater:
        flags.append(
            f"paired coverage does not favor A (D lost {d_less}, won {d_greater})"
        )
    identical = (
        pooled_a.speech_coverage == pooled_d.speech_coverage
        and pooled_a.emitted_audio_sec == pooled_d.emitted_audio_sec
        and pooled_a.unique_cutpoint_count == pooled_d.unique_cutpoint_count
    )
    if identical:
        flags.append("suspicious A=D collapse on pooled yield metrics")
    material = bool(flags)
    return {
        "historical": HISTORICAL_PROPER_AD,
        "trusted_new_coverage_metric": "pooled Phase-1 speech_coverage (eligible phones minus uncertainty union)",
        "historical_coverage_note": (
            "Historical mean conditional (~68.67% / ~62.50%) and mean end-to-end "
            "(~54.84% / ~50.05%) coverage used different denominator semantics; "
            "do not treat pooled speech_coverage as the same number."
        ),
        "flags": flags,
        "material_discrepancy": material,
        "neighborhood": {
            "same_broad_cohort": recording_count == HISTORICAL_PROPER_AD["accepted_recording_count"],
            "a_higher_yield": a_higher_yield,
            "d_safer": d_safer,
            "paired_coverage_favors_a": d_less > d_greater,
            "no_ad_collapse": not identical,
        },
    }


FOUR_WAY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("current_A", "A"),
    ("proper_D", "D"),
    ("min_quiet_run_64ms", "min_quiet_64"),
    ("quiet_run_score", "quiet_run_score"),
)


def four_way_comparison_table(pooled: dict[str, PooledMetrics]) -> list[dict[str, object]]:
    def pct(rate: float | None) -> float | None:
        return None if rate is None else rate * 100.0

    metric_specs = [
        ("speech coverage", lambda m: m.speech_coverage),
        ("emitted sec", lambda m: m.emitted_audio_sec),
        ("clips", lambda m: m.clip_count),
        ("selected cuts", lambda m: m.unique_cutpoint_count),
        ("inside-phone %", lambda m: pct(m.inside_phone_rate)),
        (">20 ms %", lambda m: pct(m.depth_gt_20ms_rate)),
        (">50 ms %", lambda m: pct(m.depth_gt_50ms_rate)),
        (">100 ms %", lambda m: pct(m.depth_gt_100ms_rate)),
        ("final-edge inside-phone %", lambda m: pct(m.edge_inside_phone_rate)),
        ("final-edge >50 ms %", lambda m: pct(m.edge_depth_gt_50ms_rate)),
    ]
    rows: list[dict[str, object]] = []
    for label, getter in metric_specs:
        row: dict[str, object] = {"metric": label}
        for name, col in FOUR_WAY_COLUMNS:
            if name in pooled:
                row[col] = getter(pooled[name])
        rows.append(row)
    return rows


def format_four_way_table(rows: list[dict[str, object]]) -> str:
    def cell(value: object, width: int, numeric: bool) -> str:
        if value is None:
            text = "n/a"
        elif isinstance(value, float):
            text = f"{value:.6f}"
        else:
            text = str(value)
        return text.rjust(width) if numeric else text.ljust(width)

    header = (
        f"| {'metric':<26} | {'A':>12} | {'D':>12} | {'min_quiet_64':>12} | {'quiet_run_score':>15} |"
    )
    rule = (
        f"| {'-' * 26} | {'-' * 12} | {'-' * 12} | {'-' * 12} | {'-' * 15} |"
    )
    lines = [header, rule]
    for row in rows:
        lines.append(
            f"| {cell(row['metric'], 26, False)} | "
            f"{cell(row.get('A'), 12, True)} | "
            f"{cell(row.get('D'), 12, True)} | "
            f"{cell(row.get('min_quiet_64'), 12, True)} | "
            f"{cell(row.get('quiet_run_score'), 15, True)} |"
        )
    return "\n".join(lines)


def delta_vs_a_table(
    pooled: dict[str, PooledMetrics],
    *,
    baseline: str = "current_A",
    contender_names: list[str] | None = None,
) -> list[dict[str, object]]:
    if baseline not in pooled:
        raise RuntimeError(f"delta_vs_a_table requires {baseline}")
    baseline_metrics = pooled[baseline]

    def pct(rate: float | None) -> float | None:
        return None if rate is None else rate * 100.0

    specs = [
        ("speech coverage", lambda m: m.speech_coverage, False),
        ("emitted sec", lambda m: m.emitted_audio_sec, False),
        ("inside-phone %", lambda m: pct(m.inside_phone_rate), True),
        (">50 ms %", lambda m: pct(m.depth_gt_50ms_rate), True),
        (">100 ms %", lambda m: pct(m.depth_gt_100ms_rate), True),
    ]
    if contender_names is None:
        contenders = [
            name for name, _col in FOUR_WAY_COLUMNS if name != baseline and name in pooled
        ]
        if not contenders:
            contenders = [name for name in pooled if name != baseline]
    else:
        contenders = [name for name in contender_names if name != baseline and name in pooled]
    rows: list[dict[str, object]] = []
    for label, getter, _lower_is_safer in specs:
        row: dict[str, object] = {"metric": label}
        base_val = getter(baseline_metrics)
        for name in contenders:
            other = getter(pooled[name])
            if base_val is None or other is None:
                row[name] = None
            else:
                row[name] = float(other) - float(base_val)
        rows.append(row)
    return rows


def geometry_comparison_table(
    pooled: dict[str, PooledMetrics],
    names: Sequence[str],
) -> list[dict[str, object]]:
    def pct(rate: float | None) -> float | None:
        return None if rate is None else rate * 100.0

    metric_specs = [
        ("speech coverage", lambda m: m.speech_coverage),
        ("retained speech sec", lambda m: m.retained_target_speech_sec),
        ("emitted sec", lambda m: m.emitted_audio_sec),
        ("clips", lambda m: m.clip_count),
        ("selected cuts", lambda m: m.unique_cutpoint_count),
        ("inside-phone %", lambda m: pct(m.inside_phone_rate)),
        (">20 ms %", lambda m: pct(m.depth_gt_20ms_rate)),
        (">50 ms %", lambda m: pct(m.depth_gt_50ms_rate)),
        (">100 ms %", lambda m: pct(m.depth_gt_100ms_rate)),
        ("final-edge inside-phone %", lambda m: pct(m.edge_inside_phone_rate)),
        ("final-edge >20 ms %", lambda m: pct(m.edge_depth_gt_20ms_rate)),
        ("final-edge >50 ms %", lambda m: pct(m.edge_depth_gt_50ms_rate)),
        ("final-edge >100 ms %", lambda m: pct(m.edge_depth_gt_100ms_rate)),
    ]
    rows: list[dict[str, object]] = []
    for label, getter in metric_specs:
        row: dict[str, object] = {"metric": label}
        for name in names:
            if name in pooled:
                row[name] = getter(pooled[name])
        rows.append(row)
    return rows


def format_geometry_table(rows: list[dict[str, object]], names: Sequence[str]) -> str:
    def cell(value: object, width: int, numeric: bool) -> str:
        if value is None:
            text = "n/a"
        elif isinstance(value, float):
            text = f"{value:.6f}"
        else:
            text = str(value)
        return text.rjust(width) if numeric else text.ljust(width)

    width = 12
    header = f"| {'metric':<26} |" + "".join(f" {name:>{width}} |" for name in names)
    rule = f"| {'-' * 26} |" + "".join(f" {'-' * width} |" for _ in names)
    lines = [header, rule]
    for row in rows:
        line = f"| {cell(row['metric'], 26, False)} |"
        for name in names:
            line += f" {cell(row.get(name), width, True)} |"
        lines.append(line)
    return "\n".join(lines)


def interpret_pareto(
    *,
    name: str,
    contender: PooledMetrics,
    pooled_a: PooledMetrics,
    pooled_d: PooledMetrics,
) -> dict[str, object]:
    """Plain-language domination check. No composite score."""

    def cov(metrics: PooledMetrics) -> float | None:
        return metrics.speech_coverage

    def deep(metrics: PooledMetrics) -> tuple[float | None, float | None]:
        return metrics.depth_gt_50ms_rate, metrics.depth_gt_100ms_rate

    def worse_yield(left: PooledMetrics, right: PooledMetrics) -> bool:
        if cov(left) is None or cov(right) is None:
            return left.emitted_audio_sec < right.emitted_audio_sec
        return cov(left) < cov(right) and left.emitted_audio_sec <= right.emitted_audio_sec

    def less_safe_deep(left: PooledMetrics, right: PooledMetrics) -> bool:
        l50, l100 = deep(left)
        r50, r100 = deep(right)
        if l50 is None or r50 is None:
            return False
        worse_50 = l50 > r50
        worse_100 = l100 is not None and r100 is not None and l100 > r100
        return worse_50 or worse_100

    dominated_by_a = worse_yield(contender, pooled_a) and less_safe_deep(contender, pooled_a)
    dominated_by_d = worse_yield(contender, pooled_d) and less_safe_deep(contender, pooled_d)
    status = "plausibly Pareto-useful"
    if dominated_by_a and dominated_by_d:
        status = "dominated by A and D"
    elif dominated_by_a:
        status = "dominated by A"
    elif dominated_by_d:
        status = "dominated by D"
    return {
        "contender": name,
        "status": status,
        "dominated_by_A": dominated_by_a,
        "dominated_by_D": dominated_by_d,
        "coverage_minus_A": None
        if cov(contender) is None or cov(pooled_a) is None
        else cov(contender) - cov(pooled_a),
        "coverage_minus_D": None
        if cov(contender) is None or cov(pooled_d) is None
        else cov(contender) - cov(pooled_d),
        "gt50_minus_A": None
        if contender.depth_gt_50ms_rate is None or pooled_a.depth_gt_50ms_rate is None
        else contender.depth_gt_50ms_rate - pooled_a.depth_gt_50ms_rate,
        "gt100_minus_A": None
        if contender.depth_gt_100ms_rate is None or pooled_a.depth_gt_100ms_rate is None
        else contender.depth_gt_100ms_rate - pooled_a.depth_gt_100ms_rate,
        "note": (
            "Deep errors (>50 / >100 ms) matter more than shallow inside-phone overlap. "
            "No composite winner score."
        ),
    }
