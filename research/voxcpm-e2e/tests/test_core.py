"""Deterministic unit tests. JiWER alignment objects are mocked where a wheel is absent."""
from pathlib import Path
import json, unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
import yaml
from speechcraft_e2e_bench.manifest import load_dataset, validate_dataset, ManifestError
from speechcraft_e2e_bench.textmetrics import normalize_text, score_pair, aggregate
from speechcraft_e2e_bench.aggregates import pairwise_cosine, speaker_similarity
from speechcraft_e2e_bench.failures import classify, failure_rate, apply_override
from speechcraft_e2e_bench.audio import cache_key
from speechcraft_e2e_bench.evaluate import evaluate_dataset

def make_wav(path: Path, freq=220.0, seconds=0.5):
    t=np.arange(int(16000*seconds))/16000
    sf.write(path,(0.2*np.sin(2*np.pi*freq*t)).astype("float32"),16000)
    return path

def make_manifest(tmp_path, generated=1):
    make_wav(tmp_path/"real.wav",220)
    clips=[]
    for i in range(generated):
        name=f"gen{i}.wav"; make_wav(tmp_path/name,220+(i+1)*20)
        clips.append({"id":f"g{i}","prompt_id":"p1","path":name})
    data={"version":1,"speaker_id":"s1","real_recordings":[{"id":"r1","path":"real.wav","transcript":"Hello, world!"}],"prompts":[{"id":"p1","text":"Hello, world!"}],"conditions":{"arbitrary":{"clips":clips}}}
    p=tmp_path/"data.yaml"; p.write_text(yaml.safe_dump(data)); return p

class CoreTests(unittest.TestCase):
    def test_manifest_parsing_and_audio_validation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p=make_manifest(Path(td)); ds=load_dataset(p)
            self.assertEqual(ds.speaker_id,"s1"); self.assertEqual(list(ds.conditions),["arbitrary"])
            self.assertEqual(validate_dataset(ds),[])

    def test_duplicate_and_missing_audio_handling(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p=make_manifest(Path(td)); d=yaml.safe_load(p.read_text()); d["conditions"]["arbitrary"]["clips"][0]["id"]="r1"; p.write_text(yaml.safe_dump(d))
            with self.assertRaisesRegex(ManifestError,"Duplicate audio id"): load_dataset(p)
            d["conditions"]["arbitrary"]["clips"][0].update(id="g0",path="missing.wav"); p.write_text(yaml.safe_dump(d))
            with self.assertRaisesRegex(ManifestError,"Missing audio file"): load_dataset(p)
            d["conditions"]["arbitrary"]["clips"][0]["path"]="real.wav"; p.write_text(yaml.safe_dump(d))
            with self.assertRaisesRegex(ManifestError,"Duplicate recording path"): load_dataset(p)

    def test_duplicate_audio_content_detection(self):
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as td:
            p=make_manifest(Path(td)); shutil.copyfile(Path(td)/"gen0.wav",Path(td)/"duplicate.wav")
            d=yaml.safe_load(p.read_text()); d["conditions"]["arbitrary"]["clips"].append({"id":"g-duplicate","prompt_id":"p1","path":"duplicate.wav"}); p.write_text(yaml.safe_dump(d))
            with self.assertRaisesRegex(ManifestError,"Duplicate recording content"): load_dataset(p)

    def test_text_normalization_and_jiwer_scores(self):
        self.assertEqual(normalize_text(" HéLLo,   WORLD! "),"héllo world")
        a=score_pair("The cat sat.","The dog sat.")
        b=score_pair("hello world","hello")
        self.assertAlmostEqual(a["wer"],1/3); self.assertEqual(a["substitutions"],1)
        self.assertEqual(b["deletions"],1)
        agg=aggregate([a,b]); self.assertAlmostEqual(agg["wer"],2/5)
        self.assertAlmostEqual(agg["cer"],(a["char_edits"]+b["char_edits"])/(a["reference_chars"]+b["reference_chars"]))

    def test_optional_real_speech_asr_baseline(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p=make_manifest(Path(td),1); ds=load_dataset(p)
            mocked={"transcripts":{"g0":{"text":"hello world"},"r1":{"text":"hello world"}},"versions":{"faster-whisper":"mock"}}
            with patch("speechcraft_e2e_bench.evaluate.run_worker",return_value=mocked):
                run=evaluate_dataset(ds,metrics={"wer"},output=Path(td)/"runs",config={"transcribe_real":True})
            result=json.loads((run/"results.json").read_text())
            self.assertEqual(result["real_asr_baseline"]["status"],"available")
            self.assertEqual(result["real_asr_baseline"]["summary"]["wer"],0)

    def test_cosine_and_consistency_aggregation(self):
        vec={"a":[1,0],"b":[.8,.6],"c":[0,1]}
        d=pairwise_cosine(vec); self.assertEqual(d["pair_count"],3); self.assertAlmostEqual(d["mean"],(.8+0+.6)/3)
        self.assertAlmostEqual(speaker_similarity({"r":[1,0]},{"g":[1,0]})["g"],1)
        self.assertEqual(pairwise_cosine({"one":[1,0]})["pair_count"],0)

    def test_empty_and_insufficient_collection_behavior(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p=make_manifest(Path(td),0); ds=load_dataset(p)
            run=evaluate_dataset(ds,metrics={"secs","consistency","wer","failure","ttsds2"},output=Path(td)/"runs")
            c=json.loads((run/"results.json").read_text())["conditions"]["arbitrary"]
            self.assertEqual(c["metrics"]["secs"]["status"],"unavailable")
            self.assertEqual(c["metrics"]["consistency"]["status"],"unavailable")
            self.assertEqual(c["metrics"]["ttsds2"]["status"],"unavailable")
            self.assertEqual(c["metrics"]["failure_rate"]["status"],"unavailable")

    def test_speaker_backend_selection_and_license_provenance(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ds=load_dataset(make_manifest(Path(td),1))
            run=evaluate_dataset(ds,metrics={"secs"},output=Path(td)/"runs",config={"speaker":{"backend":"ecapa2"}})
            result=json.loads((run/"results.json").read_text())
            self.assertEqual(result["provenance"]["speaker"]["model"],"Jenthe/ECAPA2")
            self.assertEqual(result["provenance"]["speaker"]["license"],"CC-BY-NC-4.0")

    def test_pass_review_fail_and_human_override(self):
        self.assertEqual(classify(artifact_exists=True,readable=True,duration=2)["status"],"PASS")
        self.assertEqual(classify(artifact_exists=True,readable=True,duration=2,wer=.8)["status"],"REVIEW")
        self.assertEqual(classify(artifact_exists=True,readable=True,duration=2,transcript="half words",reference_text="one two three four five six seven eight")["status"],"REVIEW")
        self.assertEqual(classify(artifact_exists=True,readable=True,duration=.05)["status"],"FAIL")
        self.assertEqual(classify(artifact_exists=False,readable=False)["status"],"FAIL")
        self.assertEqual(classify(artifact_exists=True,readable=True,duration=2,vad={"speech_segments":0})["status"],"FAIL")
        self.assertEqual(classify(artifact_exists=True,readable=True,duration=.1,thresholds={"min_duration_seconds":.05})["status"],"REVIEW")
        overridden=apply_override({"status":"FAIL","reasons":["no speech"],"review_override":None},{"status":"PASS","reviewer":"human","reason":"confirmed speech","timestamp":"2026-10-25T00:00:00Z"})
        self.assertEqual(overridden["review_override"]["reason"],"confirmed speech")

    def test_attempt_based_and_observable_failure_denominators(self):
        d=failure_rate([{"outcome":"success"},{"outcome":"error"}],1,1,complete=True)
        self.assertEqual(d["denominator"],2); self.assertEqual(d["rate"],.5); self.assertIn("attempt-based",d["label"])
        o=failure_rate([{"outcome":"error"}],1,3,complete=False); self.assertAlmostEqual(o["rate"],1/3); self.assertIn("observable",o["label"])

    def test_manifest_attempt_without_artifact_is_counted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p=make_manifest(Path(td),1); d=yaml.safe_load(p.read_text())
            d["attempts_complete"]=True
            d["attempts"]=[{"id":"attempt-ok","condition":"arbitrary","prompt_id":"p1","outcome":"success","clip_id":"g0"},
                            {"id":"attempt-missing","condition":"arbitrary","prompt_id":"p1","outcome":"success"}]
            d["conditions"]["arbitrary"]["clips"][0]["generation_attempt_id"]="attempt-ok"
            p.write_text(yaml.safe_dump(d))
            with patch("speechcraft_e2e_bench.evaluate.run_worker",return_value={"vad":{"g0":{"speech_segments":1,"silence_fraction":0.0}},"versions":{"silero-vad":"mock"}}):
                run=evaluate_dataset(load_dataset(p),metrics={"failure"},output=Path(td)/"runs")
            result=json.loads((run/"results.json").read_text()); rate=result["conditions"]["arbitrary"]["metrics"]["failure_rate"]["result"]
            self.assertEqual(rate["denominator"],2); self.assertEqual(rate["failed"],1); self.assertEqual(rate["rate"],.5)
            self.assertTrue(any(x.get("attempt_id")=="attempt-missing" for x in result["clips"]))

    def test_complete_attempt_manifest_must_link_supplied_clips(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p=make_manifest(Path(td),1); d=yaml.safe_load(p.read_text()); d["attempts_complete"]=True; p.write_text(yaml.safe_dump(d))
            with self.assertRaisesRegex(ManifestError,"not linked to an attempt"): load_dataset(p)

    def test_cache_content_and_settings_invalidation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            a=make_wav(Path(td)/"x.wav"); k=cache_key(a,"e","v1",{"x":1})
            self.assertEqual(k,cache_key(a,"e","v1",{"x":1})); self.assertNotEqual(k,cache_key(a,"e","v2",{"x":1}))
            make_wav(Path(td)/"y.wav",440); self.assertNotEqual(k,cache_key(Path(td)/"y.wav","e","v1",{"x":1}))

    def test_transcription_cache_reuse_and_version_invalidation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p=make_manifest(Path(td)); ds=load_dataset(p)
            mock={"transcripts":{"g0":{"text":"hello world","language":"en","duration":.5}},"versions":{"faster-whisper":"mock"}}
            with patch("speechcraft_e2e_bench.evaluate.run_worker",return_value=mock) as worker:
                evaluate_dataset(ds,metrics={"wer"},output=Path(td)/"runs",config={"asr":{"version":"mock-v1"}})
                self.assertEqual(worker.call_count,1)
            with patch("speechcraft_e2e_bench.evaluate.run_worker") as worker:
                second=evaluate_dataset(ds,metrics={"wer"},output=Path(td)/"runs",config={"asr":{"version":"mock-v1"}})
                self.assertEqual(worker.call_count,0)
            r=json.loads((second/"results.json").read_text())
            self.assertEqual(r["conditions"]["arbitrary"]["metrics"]["wer"]["summary"]["wer"],0)
            self.assertEqual(r["provenance"]["asr"]["status"],"cache_hit")
            with patch("speechcraft_e2e_bench.evaluate.run_worker",return_value=mock) as worker:
                evaluate_dataset(ds,metrics={"wer"},output=Path(td)/"runs",config={"asr":{"version":"mock-v2"}})
                self.assertEqual(worker.call_count,1)

    def test_error_isolation_and_report_schema(self):
        import tempfile, sys
        with tempfile.TemporaryDirectory() as td:
            p=make_manifest(Path(td)); ds=load_dataset(p)
            def worker(python,script,payload):
                if str(script).endswith("speaker_worker.py"): return {"error":"mock speaker failure"}
                return {"transcripts":{"g0":{"text":"hello world","language":"en","duration":.5}},"versions":{"faster-whisper":"mock"}}
            with patch.dict("os.environ",{"SPEECHCRAFT_SPEAKER_PYTHON":sys.executable}):
                with patch("speechcraft_e2e_bench.evaluate.run_worker",side_effect=worker):
                    run=evaluate_dataset(ds,metrics={"secs","wer"},output=Path(td)/"runs")
            self.assertTrue(all((run/name).exists() for name in ("report.md","clips.csv","run_config.yaml","results.json","run.log")))
            r=json.loads((run/"results.json").read_text()); c=r["conditions"]["arbitrary"]
            self.assertEqual(c["metrics"]["secs"]["status"],"error")
            self.assertEqual(c["metrics"]["wer"]["status"],"available")

    def test_ttsds_incomplete_upstream_results_are_not_reported_as_complete(self):
        import tempfile, sys
        with tempfile.TemporaryDirectory() as td:
            p=make_manifest(Path(td)); ds=load_dataset(p)
            upstream={"version":"2.1.3","complete":False,"overall_score":None,
                      "category_scores":{"Speaker":.2,"Prosody":None,"Intelligibility":.4,"Generic":.3},
                      "raw":[{"benchmark_name":"pitch","score":None}],
                      "individual_errors":[{"benchmark_name":"pitch","score":None}]}
            with patch("speechcraft_e2e_bench.evaluate.run_worker",return_value=upstream):
                run=evaluate_dataset(ds,metrics={"ttsds2"},output=Path(td)/"runs",
                    config={"ttsds2":{"python":sys.executable,"minimum_samples":1}})
            result=json.loads((run/"results.json").read_text())
            metric=result["conditions"]["arbitrary"]["metrics"]["ttsds2"]
            self.assertEqual(metric["status"],"partial")
            self.assertIsNone(metric["result"]["overall_score"])
            self.assertEqual(metric["result"]["individual_errors"][0]["benchmark_name"],"pitch")

    def test_ttsds_worker_errors_keep_full_diagnostic(self):
        import tempfile, sys
        with tempfile.TemporaryDirectory() as td:
            p=make_manifest(Path(td)); ds=load_dataset(p)
            upstream={"error":"ModuleNotFoundError: unavailable dependency","traceback":"full traceback","upstream_log":"upstream logs"}
            with patch("speechcraft_e2e_bench.evaluate.run_worker",return_value=upstream):
                run=evaluate_dataset(ds,metrics={"ttsds2"},output=Path(td)/"runs",
                    config={"ttsds2":{"python":sys.executable,"minimum_samples":1}})
            result=json.loads((run/"results.json").read_text())
            metric=result["conditions"]["arbitrary"]["metrics"]["ttsds2"]
            self.assertEqual(metric["status"],"error")
            self.assertEqual(metric["diagnostic"]["traceback"],"full traceback")
            self.assertEqual(metric["diagnostic"]["upstream_log"],"upstream logs")

if __name__=="__main__": unittest.main(verbosity=2)
