"""Browser uploads land in managed Linux storage.

The original client filename may look like a Windows path. That name is not a
storage location. These tests do not start WSL or process audio.
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.repository import SQLiteRepository


class ManagedUploadDestinationTest(unittest.TestCase):
    def test_source_recording_path_stays_under_the_linux_media_root(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            media = root / "media"
            windows_source = root / "mnt" / "c" / "Users" / "Ada" / "clip.wav"
            repository = SQLiteRepository(
                db_path=root / "db" / "speechcraft.db",
                legacy_seed_path=root / "missing-seed.json",
                media_root=media,
                exports_root=root / "exports",
            )
            try:
                destination = repository.managed_source_recording_path("source123")
                self.assertTrue(destination.is_relative_to(media.resolve()))
                self.assertEqual(destination.parent.name, "sources")
                self.assertNotIn("mnt", destination.parts)
                original_name = Path("C:\\Users\\Ada\\clip.wav").name
                destination.write_bytes(b"uploaded-bytes")
                self.assertEqual(destination.read_bytes(), b"uploaded-bytes")
                self.assertFalse(windows_source.exists())
                self.assertNotEqual(destination.name, original_name)
            finally:
                repository.close()

    def test_managed_media_identifier_cannot_escape_the_media_root(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SQLiteRepository(
                db_path=root / "db" / "speechcraft.db",
                legacy_seed_path=root / "missing-seed.json",
                media_root=root / "media",
                exports_root=root / "exports",
            )
            try:
                with self.assertRaises(ValueError):
                    repository.managed_source_recording_path("../outside")
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()
