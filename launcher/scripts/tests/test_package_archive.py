import hashlib
import pathlib
import subprocess
import tarfile
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "package-archive.py"


class PackageArchiveTests(unittest.TestCase):
    def test_repeated_archives_are_identical_and_preserve_entrypoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            package = root / "speechcraft"
            (package / "bin").mkdir(parents=True)
            (package / "bin/speechcraft").write_text("#!/bin/sh\nexit 0\n")
            (package / "bin/speechcraft").chmod(0o755)
            (package / "README").write_text("fixture\n")
            archives = [root / "one.tar.gz", root / "two.tar.gz"]
            for archive in archives:
                subprocess.run(["python3", str(SCRIPT), str(root), package.name, str(archive)], check=True)
            digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            self.assertEqual(digest(archives[0]), digest(archives[1]))
            with tarfile.open(archives[0], "r:gz") as bundle:
                members = {member.name: member for member in bundle.getmembers()}
            self.assertEqual(members["speechcraft/bin/speechcraft"].mode & 0o777, 0o755)
            self.assertEqual(members["speechcraft/README"].mode & 0o777, 0o644)
            self.assertTrue(all(member.uid == 0 and member.gid == 0 and member.mtime == 0 for member in members.values()))

    def test_symlink_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            package = root / "speechcraft"
            package.mkdir()
            (package / "escape").symlink_to(root)
            result = subprocess.run(["python3", str(SCRIPT), str(root), package.name, str(root / "out.tar.gz")],
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsupported package entry", result.stderr)


if __name__ == "__main__":
    unittest.main()
