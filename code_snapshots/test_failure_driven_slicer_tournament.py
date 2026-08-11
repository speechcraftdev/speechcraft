from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_failure_driven_slicer_tournament.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_failure_driven_slicer_tournament", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tournament_manifest_has_exact_24_unique_configs():
    module = load_module()

    configs = module.TOURNAMENT_CONFIGS

    assert len(configs) == 24
    assert sorted(config.config_id for config in configs) == list(range(1, 25))
    assert len({config.name for config in configs}) == 24


def test_manifest_separates_runnable_from_not_implemented():
    module = load_module()

    rows = module.config_rows()
    runnable = [row for row in rows if row["runnable"]]
    pending = [row for row in rows if not row["runnable"]]

    assert len(runnable) == 16
    assert len(pending) == 8
    assert any(row["name"] == "current_A_baseline" for row in runnable)
    assert any(row["name"] == "proper_D_baseline" for row in runnable)
    assert any(row["name"] == "large_boundary_risk_penalty" for row in runnable)
    assert {
        "openvpi_native",
        "openvpi_candidates_common_packer",
        "A_hard_D_veto",
        "A_soft_D_penalty",
        "A_only_stronger_RMS",
        "D_primary_A_fallback",
        "consensus_first_union",
        "D_regions_RMS_exact",
    } == {row["name"] for row in pending}
    assert all(row["implementation_status"] == "implemented" for row in runnable)


def test_vad_geometry_env_is_explicit_and_does_not_omit_offsets():
    module = load_module()

    config_by_name = {config.name: config for config in module.TOURNAMENT_CONFIGS}
    current = config_by_name["current_A_baseline"]
    proper_d = config_by_name["proper_D_baseline"]

    assert current.env()["SPEAKER_TS_EVAL_VAD_WINDOW_SAMPLES"] == "512"
    assert current.env()["SPEAKER_TS_EVAL_VAD_HOP_SAMPLES"] == "256"
    assert current.env()["SPEAKER_TS_EVAL_VAD_OFFSETS"] == "0,128"
    assert proper_d.env()["SPEAKER_TS_EVAL_VAD_HOP_SAMPLES"] == "512"
    assert proper_d.env()["SPEAKER_TS_EVAL_VAD_OFFSETS"] == "0,128,256,384"


def test_not_implemented_report_is_fail_closed(tmp_path):
    module = load_module()

    module.write_not_implemented_report(tmp_path)
    report = (tmp_path / "tables" / "not_implemented_configs.csv").read_text(encoding="utf-8")

    assert "Requires new detector/packer implementation" in report
    assert "openvpi_native" in report
    assert "openvpi_candidates_common_packer" in report
    assert "two_stage_high_conf_then_fallback" not in report
    assert "current_A_baseline" not in report


def test_all_runnable_configs_register_as_benchmark_detectors():
    module = load_module()

    for config in module.TOURNAMENT_CONFIGS:
        if not config.runnable:
            continue
        with module.patched_tournament_execution(config):
            supported = module.rb.supported_repaired_detectors()
            assert config.name in supported
            assert supported[config.name].name == config.name


def test_bundle_packaging_excludes_audio_and_cache_paths(tmp_path):
    module = load_module()
    out_root = tmp_path / "out"
    tables = out_root / "tables"
    tables.mkdir(parents=True)
    (tables / "tournament_config_manifest.csv").write_text("config_id,name\n1,current_A_baseline\n", encoding="utf-8")
    (out_root / "README.md").write_text("readme\n", encoding="utf-8")
    (out_root / "provenance.json").write_text("{}\n", encoding="utf-8")
    test_log = out_root / "test_log.txt"
    test_log.write_text("ok\n", encoding="utf-8")
    bundle = tmp_path / "bundle.zip"

    module.package_bundle(out_root, bundle, test_log)

    import zipfile

    with zipfile.ZipFile(bundle) as zf:
        names = zf.namelist()

    assert "tables/tournament_config_manifest.csv" in names
    assert not any(name.endswith(".wav") for name in names)
    assert not any("/cache/" in name or name.startswith("cache/") for name in names)
