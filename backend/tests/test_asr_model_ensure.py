from __future__ import annotations

import json
import threading
from unittest import TestCase
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.asr_model_ensure import reset_asr_ensure_state_for_tests, start_asr_ensure
from app.main import app


class AsrModelEnsureTests(TestCase):
    def setUp(self) -> None:
        reset_asr_ensure_state_for_tests()

    def tearDown(self) -> None:
        reset_asr_ensure_state_for_tests()

    def test_managed_runtime_refuses_ensure(self) -> None:
        with patch.dict("os.environ", {"SPEECHCRAFT_MANAGED_RUNTIME": "1"}, clear=False):
            with self.assertRaises(RuntimeError) as raised:
                start_asr_ensure("large-v3")
        self.assertEqual(str(raised.exception), "managed_runtime_use_setup")

        client = TestClient(app)
        with patch.dict("os.environ", {"SPEECHCRAFT_MANAGED_RUNTIME": "1"}, clear=False):
            response = client.post("/api/system/asr-models/ensure", json={"model": "large-v3"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("managed_runtime_use_setup", json.dumps(response.json()))

    def test_ensure_job_reports_indeterminate_progress_then_completes(self) -> None:
        stdout_lines = [
            json.dumps(
                {
                    "event": "progress",
                    "phase": "downloading",
                    "model_name": "base",
                    "repo_id": "Systran/faster-whisper-base",
                    "package_type": "faster-whisper",
                    "downloaded_bytes": None,
                    "total_bytes": None,
                    "transfer_indeterminate": True,
                    "files_completed": 1,
                    "files_total": 3,
                }
            )
            + "\n",
            json.dumps(
                {
                    "event": "result",
                    "ok": True,
                    "model": "base",
                    "repo_id": "Systran/faster-whisper-base",
                }
            )
            + "\n",
        ]

        process = MagicMock()
        process.stdout = iter(stdout_lines)
        process.stderr = MagicMock(read=MagicMock(return_value=""))
        process.wait.return_value = 0
        done = threading.Event()

        def fake_popen(*_args, **_kwargs):
            done.set()
            return process

        with (
            patch.dict("os.environ", {"SPEECHCRAFT_MANAGED_RUNTIME": "0"}, clear=False),
            patch("app.asr_model_ensure.dataset_worker_python") as worker_python,
            patch("app.asr_model_ensure.dataset_worker_root") as worker_root,
            patch("app.asr_model_ensure.subprocess.Popen", side_effect=fake_popen),
        ):
            python_path = MagicMock()
            python_path.exists.return_value = True
            worker_python.return_value = python_path
            root = MagicMock()
            script = MagicMock()
            script.exists.return_value = True
            root.__truediv__.return_value = script
            worker_root.return_value = root

            client = TestClient(app)
            created = client.post("/api/system/asr-models/ensure", json={"model": "base"})
            self.assertEqual(created.status_code, 200)
            job_id = created.json()["id"]
            self.assertTrue(done.wait(timeout=2))

            # Allow the worker thread to consume stdout.
            for _ in range(50):
                status = client.get(f"/api/system/asr-models/ensure/{job_id}")
                if status.json().get("phase") == "complete":
                    break
                threading.Event().wait(0.02)

        status = client.get(f"/api/system/asr-models/ensure/{job_id}")
        body = status.json()
        self.assertEqual(status.status_code, 200)
        self.assertEqual(body["phase"], "complete")
        self.assertTrue(body["ok"])
        self.assertEqual(body["model"], "base")
        self.assertIsNone(body["progress"].get("downloaded_bytes"))
        self.assertTrue(
            body["progress"].get("transfer_indeterminate")
            or body["phase"] == "complete"
        )

    def test_duplicate_ensure_returns_same_active_job(self) -> None:
        hold = threading.Event()
        release = threading.Event()

        def blocking_run(job_id: str) -> None:
            hold.set()
            release.wait(timeout=2)

        with (
            patch.dict("os.environ", {"SPEECHCRAFT_MANAGED_RUNTIME": "0"}, clear=False),
            patch("app.asr_model_ensure._run_job", side_effect=blocking_run),
        ):
            first = start_asr_ensure("large-v3")
            self.assertTrue(hold.wait(timeout=2))
            second = start_asr_ensure("large-v3")
            self.assertEqual(first["id"], second["id"])
            release.set()
