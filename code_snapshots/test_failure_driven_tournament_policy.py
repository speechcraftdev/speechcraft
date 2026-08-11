from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "failure_driven_tournament_policy.py"


def load_module():
    spec = importlib.util.spec_from_file_location("failure_driven_tournament_policy", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_min_quiet_run_accepts_boundary_at_threshold_and_rejects_below():
    m = load_module()

    assert m.apply_min_quiet_run(m.BoundaryEvidence("ok", 1.0, quiet_run_ms=64.0), 64.0).accepted
    rejected = m.apply_min_quiet_run(m.BoundaryEvidence("bad", 1.0, quiet_run_ms=63.0), 64.0)

    assert not rejected.accepted
    assert rejected.reasons == ("quiet_run_lt_64ms",)


def test_one_sided_gate_uses_start_before_and_end_after():
    m = load_module()

    start = m.BoundaryEvidence("s", 1.0, role=m.BoundaryRole.START, quiet_before_ms=47.0, quiet_after_ms=200.0)
    end = m.BoundaryEvidence("e", 2.0, role=m.BoundaryRole.END, quiet_before_ms=200.0, quiet_after_ms=47.0)

    assert not m.apply_one_sided_gate(start, 48.0).accepted
    assert not m.apply_one_sided_gate(end, 48.0).accepted
    assert m.apply_one_sided_gate(m.replace(start, quiet_before_ms=48.0), 48.0).accepted
    assert m.apply_one_sided_gate(m.replace(end, quiet_after_ms=48.0), 48.0).accepted


def test_d_veto_and_soft_penalty_are_distinct():
    m = load_module()
    boundary = m.BoundaryEvidence("b", 1.0, d_speech_prob=0.80, base_score=1.0)

    hard = m.apply_hard_d_veto(boundary)
    soft = m.apply_soft_d_penalty(boundary)

    assert not hard.accepted
    assert soft.accepted
    assert soft.score < boundary.base_score


def test_a_only_stronger_rms_leaves_consensus_unchanged():
    m = load_module()
    params = m.PolicyParameters(stronger_rms_threshold_dbfs=-50.0)
    a_only = m.BoundaryEvidence("a", 1.0, source="A_only", rms_dbfs=-45.0)
    consensus = m.BoundaryEvidence("c", 1.0, source="A_and_D", rms_dbfs=-45.0)

    assert not m.apply_a_only_stronger_rms(a_only, params).accepted
    assert m.apply_a_only_stronger_rms(consensus, params).accepted


def test_consensus_rank_ordering():
    m = load_module()

    consensus = m.consensus_rank_score(m.BoundaryEvidence("c", 1.0, source="A_and_D"))
    d_only = m.consensus_rank_score(m.BoundaryEvidence("d", 1.0, source="D_only"))
    a_only = m.consensus_rank_score(m.BoundaryEvidence("a", 1.0, source="A_only"))

    assert consensus.score > d_only.score > a_only.score


def test_d_primary_fallback_uses_a_only_only_when_d_cannot_form_clip():
    m = load_module()
    rows = [
        m.BoundaryEvidence("a", 1.0, source="A_only"),
        m.BoundaryEvidence("d", 2.0, source="D_only"),
    ]

    assert [row.boundary_id for row in m.select_d_primary_with_a_fallback(rows, can_form_legal_clip=True)] == ["d"]
    assert [row.boundary_id for row in m.select_d_primary_with_a_fallback(rows, can_form_legal_clip=False)] == ["a", "d"]


def test_safety_first_packing_prefers_safer_endpoint_over_prettier_duration():
    m = load_module()
    risky_start = m.BoundaryEvidence("rs", 1.0, role=m.BoundaryRole.START, quiet_run_ms=30.0, quiet_before_ms=20.0, rms_dbfs=-35.0)
    safe_start = m.BoundaryEvidence("ss", 1.2, role=m.BoundaryRole.START, quiet_run_ms=120.0, quiet_before_ms=90.0, rms_dbfs=-65.0)
    safe_end = m.BoundaryEvidence("e", 8.0, role=m.BoundaryRole.END, quiet_run_ms=120.0, quiet_after_ms=90.0, rms_dbfs=-65.0)
    pretty_but_risky = m.CandidateClipEvidence("pretty", risky_start, safe_end, duration_sec=7.0, retained_speech_sec=7.0, preferred_duration_penalty=0.0)
    safer = m.CandidateClipEvidence("safe", safe_start, safe_end, duration_sec=6.8, retained_speech_sec=6.8, preferred_duration_penalty=0.5)

    assert min([pretty_but_risky, safer], key=m.duration_first_clip_key).clip_id == "pretty"
    assert min([pretty_but_risky, safer], key=m.safety_first_clip_key).clip_id == "safe"


def test_scope_edge_rule_distinguishes_trusted_source_edges():
    m = load_module()
    params = m.PolicyParameters(scope_edge_zone_ms=200.0)
    diar_edge = m.BoundaryEvidence("d", 1.0, distance_to_scope_edge_ms=100.0, trusted_source_edge=False)
    source_edge = m.replace(diar_edge, boundary_id="s", trusted_source_edge=True)

    assert not m.apply_scope_edge_hard_zone(diar_edge, params).accepted
    assert m.apply_scope_edge_hard_zone(source_edge, params).accepted


def test_strict_metric_validation_rejects_missing_zero_default_trap():
    m = load_module()

    m.validate_scorecard_row(
        {
            "conditional_target_phone_coverage": "0.5",
            "inside_target_phone_rate_unique": "0.2",
            "gt_50ms_rate_unique": "0.1",
        }
    )
    try:
        m.validate_scorecard_row({"conditional_target_phone_coverage": "0.5", "inside_target_phone_rate_unique": "0.2"})
    except ValueError as exc:
        assert "missing required metric" in str(exc)
    else:
        raise AssertionError("missing gt_50ms_rate_unique should fail")


def test_annotated_failures_are_required():
    m = load_module()

    rows = [
        {"failure_id": "failure_27_bad_end"},
        {"failure_id": "failure_49_bad_start"},
        {"failure_id": "failure_93_scope_contamination"},
    ]
    assert m.annotate_known_failures(rows) == rows
    try:
        m.annotate_known_failures(rows[:2])
    except ValueError as exc:
        assert "failure_93_scope_contamination" in str(exc)
    else:
        raise AssertionError("missing known failure should fail")
