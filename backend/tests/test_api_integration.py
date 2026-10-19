from __future__ import annotations

import io
import hashlib
import wave
from pathlib import Path

from tests.http_support import LiveServerTestCase


def make_wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x00\x00" * 160)
    return buffer.getvalue()


def make_24bit_wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(3)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x00\x00\x00" * 160)
    return buffer.getvalue()


class ApiIntegrationTests(LiveServerTestCase):
    def upload_file(self, project_id: str, filename: str, content: bytes) -> dict[str, object]:
        session_response = self.client.post(
            f"/api/projects/{project_id}/source-recordings/upload-sessions",
            json={"filename": filename, "size": len(content)},
        )
        self.assertEqual(session_response.status_code, 200, session_response.text)
        session = session_response.json()
        for index, start in enumerate(range(0, len(content), session["chunk_size"])):
            chunk = content[start : start + session["chunk_size"]]
            response = self.client.put(
                f"/api/projects/{project_id}/source-recordings/upload-sessions/{session['upload_id']}/chunks/{index}",
                content=chunk,
                headers={"X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
            )
            self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(
            f"/api/projects/{project_id}/source-recordings/upload-sessions/{session['upload_id']}/finalize"
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_healthz_route_returns_ok_json(self) -> None:
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response.headers["content-type"], "application/json")

    def test_project_routes_return_seeded_demo_project(self) -> None:
        projects_response = self.client.get("/api/projects")
        self.assertEqual(projects_response.status_code, 200)
        projects = projects_response.json()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["id"], "phase1-demo")

        recordings_response = self.client.get("/api/projects/phase1-demo/recordings")
        self.assertEqual(recordings_response.status_code, 200)
        recordings = recordings_response.json()
        self.assertGreaterEqual(len(recordings), 1)
        self.assertEqual(recordings[0]["slice_count"], 0)

    def test_missing_project_route_returns_404(self) -> None:
        response = self.client.get("/api/projects/missing-project")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Project not found")

    def test_project_preparation_enqueues_job_without_mutating_raw_recordings(self) -> None:
        before_response = self.client.get("/api/projects/phase1-demo/source-recordings")
        self.assertEqual(before_response.status_code, 200)
        before_recordings = before_response.json()
        raw_recordings = [recording for recording in before_recordings if recording["parent_recording_id"] is None]
        self.assertGreaterEqual(len(raw_recordings), 1)

        response = self.client.post(
            "/api/projects/phase1-demo/preparation",
            json={"target_sample_rate": 16000, "channel_mode": "mono"},
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["job"]["kind"], "preprocess")
        self.assertEqual(payload["job"]["status"], "pending")
        self.assertEqual(payload["created_recordings"], [])

        after_response = self.client.get("/api/projects/phase1-demo/source-recordings")
        self.assertEqual(after_response.status_code, 200)
        after_recordings = after_response.json()
        after_raw_recordings = [
            recording for recording in after_recordings if recording["parent_recording_id"] is None
        ]
        self.assertEqual(
            [recording["id"] for recording in after_raw_recordings],
            [recording["id"] for recording in raw_recordings],
        )

    def test_project_upload_stream_creates_raw_source_recording(self) -> None:
        create_response = self.client.post(
            "/api/import-batches",
            json={"id": "upload-demo", "name": "Upload Demo"},
        )
        self.assertEqual(create_response.status_code, 200)

        recording = self.upload_file("upload-demo", "sample.wav", make_wav_bytes())
        self.assertEqual(recording["batch_id"], "upload-demo")
        self.assertIsNone(recording["parent_recording_id"])
        self.assertEqual(recording["sample_rate"], 16000)
        self.assertEqual(recording["num_channels"], 1)
        self.assertEqual(recording["num_samples"], 160)

        recordings_response = self.client.get("/api/projects/upload-demo/source-recordings")
        self.assertEqual(recordings_response.status_code, 200)
        listed_recordings = recordings_response.json()
        self.assertEqual([item["id"] for item in listed_recordings], [recording["id"]])
        self.assertEqual(listed_recordings[0]["display_name"], "sample.wav")

    def test_project_upload_rejects_unsupported_extension(self) -> None:
        create_response = self.client.post(
            "/api/import-batches",
            json={"id": "upload-invalid", "name": "Upload Invalid"},
        )
        self.assertEqual(create_response.status_code, 200)

        upload_response = self.client.post(
            "/api/projects/upload-invalid/source-recordings/upload-sessions",
            json={"filename": "sample.exe", "size": 9},
        )

        self.assertEqual(upload_response.status_code, 400)
        self.assertIn("Supported formats", upload_response.json()["detail"])

    def test_upload_session_creation_is_idempotent(self) -> None:
        self.client.post("/api/import-batches", json={"id": "upload-retry", "name": "Upload Retry"})
        payload = {
            "filename": "retry.wav", "size": len(make_wav_bytes()),
            "client_upload_id": "a" * 32,
        }
        first = self.client.post("/api/projects/upload-retry/source-recordings/upload-sessions", json=payload)
        second = self.client.post("/api/projects/upload-retry/source-recordings/upload-sessions", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["upload_id"], second.json()["upload_id"])

    def test_upload_resume_discards_corrupted_committed_chunks(self) -> None:
        self.client.post("/api/import-batches", json={"id": "upload-recheck", "name": "Upload Recheck"})
        session = self.client.post(
            "/api/projects/upload-recheck/source-recordings/upload-sessions",
            json={"filename": "resume.wav", "size": 4},
        ).json()
        chunk = b"test"
        upload_path = f"/api/projects/upload-recheck/source-recordings/upload-sessions/{session['upload_id']}"
        committed = self.client.put(
            f"{upload_path}/chunks/0", content=chunk,
            headers={"X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
        )
        self.assertEqual(committed.status_code, 200)
        chunk_path = Path(self._tempdir.name, "data", "media", ".uploads", session["upload_id"], "00000000.chunk")
        chunk_path.write_bytes(b"bad!")
        status = self.client.get(upload_path)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["completed_chunks"], [])
        self.assertEqual(status.json()["committed_bytes"], 0)

    def test_project_upload_validates_media_before_registering(self) -> None:
        self.client.post("/api/import-batches", json={"id": "upload-corrupt", "name": "Upload Corrupt"})
        session = self.client.post(
            "/api/projects/upload-corrupt/source-recordings/upload-sessions",
            json={"filename": "sample.mp3", "size": 10},
        ).json()
        chunk = b"not audio!"
        self.client.put(
            f"/api/projects/upload-corrupt/source-recordings/upload-sessions/{session['upload_id']}/chunks/0",
            content=chunk,
            headers={"X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
        )
        response = self.client.post(
            f"/api/projects/upload-corrupt/source-recordings/upload-sessions/{session['upload_id']}/finalize"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/projects/upload-corrupt/source-recordings").json(), [])

    def test_project_upload_normalizes_mp3_to_pipeline_wav(self) -> None:
        self.client.post("/api/import-batches", json={"id": "upload-mp3", "name": "Upload MP3"})
        fixture = Path(__file__).resolve().parents[2] / "workers/dataset/runtime-fixtures/speech-compressed.mp3"
        recording = self.upload_file("upload-mp3", "speech.mp3", fixture.read_bytes())
        self.assertEqual(recording["sample_rate"], 16000)
        self.assertEqual(recording["num_channels"], 1)
        self.assertGreater(recording["num_samples"], 0)
        source_files = Path(self._tempdir.name, "data", "media", "sources")
        original = next(source_files.glob("*.original.mp3"))
        self.assertEqual(original.read_bytes(), fixture.read_bytes())

    def test_project_upload_non_pcm16_wav_keeps_original_and_creates_working_wav(self) -> None:
        self.client.post("/api/import-batches", json={"id": "upload-24bit", "name": "Upload 24-bit"})
        original_bytes = make_24bit_wav_bytes()
        recording = self.upload_file("upload-24bit", "source.wav", original_bytes)
        self.assertEqual(recording["sample_rate"], 16000)
        source_files = Path(self._tempdir.name, "data", "media", "sources")
        original = next(source_files.glob("*.original.wav"))
        working = source_files / f"{recording['id']}.wav"
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertNotEqual(working.read_bytes(), original_bytes)

    def test_cors_preflight_uses_explicit_origin_for_allowed_frontend(self) -> None:
        response = self.client.options(
            "/api/projects",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://127.0.0.1:5173")
        self.assertEqual(response.headers.get("access-control-allow-credentials"), "true")
        self.assertNotEqual(response.headers.get("access-control-allow-origin"), "*")

    def test_cors_preflight_rejects_unknown_origin(self) -> None:
        response = self.client.options(
            "/api/projects",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Disallowed CORS origin", response.text)
        self.assertNotIn("access-control-allow-origin", response.headers)
