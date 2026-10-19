from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.reference_clip_candidates import (
    mark_dataset_clip_as_reference_candidate,
    reference_clip_candidates_root,
    resolve_unique_wav_path,
    transcript_filename_stem,
)


class ReferenceClipCandidateHelpersTests(unittest.TestCase):
    def test_transcript_filename_stem_uses_transcript_text(self) -> None:
        self.assertEqual(transcript_filename_stem("Hello world", "clip-1"), "Hello world")

    def test_transcript_filename_stem_strips_forbidden_chars_only(self) -> None:
        self.assertEqual(transcript_filename_stem('Say: "hello"', "clip-1"), "Say hello")

    def test_resolve_unique_wav_path_adds_suffix_for_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = resolve_unique_wav_path(root, "Hello world")
            first.write_bytes(b"wav")
            second = resolve_unique_wav_path(root, "Hello world")
            self.assertEqual(first.name, "Hello world.wav")
            self.assertEqual(second.name, "Hello world (2).wav")


class ReferenceClipCandidatePromotionTests(unittest.TestCase):
    @patch("app.reference_clip_candidates._get_dataset_run_for_project")
    @patch("app.clip_lab_state.get_dataset_clip_lab_audio_bytes")
    @patch("app.clip_lab_state.get_dataset_clip_lab")
    def test_mark_dataset_clip_copies_audio_into_project_folder(
        self,
        get_clip_lab,
        get_media_bytes,
        validate_run,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = Path(temp_dir)
            get_media_bytes.return_value = b"RIFF"
            get_clip_lab.return_value = SimpleNamespace(clips=[SimpleNamespace(
                clip_id="candidate_review_clip_000001", effective_audio_revision_key="revision-1")])

            class FakeRepository:
                def __init__(self) -> None:
                    self.media_root = media_root

                def get_project(self, project_id: str) -> None:
                    return None

            result = mark_dataset_clip_as_reference_candidate(
                FakeRepository(),
                project_id="project-1",
                dataset_run_id="dataset-run-1",
                clip_id="candidate_review_clip_000001",
                transcript_text="Listen carefully",
            )
            get_media_bytes.assert_called_once_with(
                unittest.mock.ANY, "dataset-run-1", "candidate_review_clip_000001", "revision-1")

            destination = reference_clip_candidates_root(media_root, "project-1") / "Listen carefully.wav"
            validate_run.assert_called_once()
            self.assertTrue(destination.exists())
            self.assertEqual(destination.read_bytes(), b"RIFF")
            self.assertEqual(result["filename"], "Listen carefully.wav")
            self.assertEqual(result["relative_path"], "reference-clip-candidates/project-1/Listen carefully.wav")


if __name__ == "__main__":
    unittest.main()
