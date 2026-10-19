#!/usr/bin/env python3
"""Write a normalized gzip tar archive for release packaging."""
import gzip
import os
import pathlib
import sys
import tarfile


def fail(message):
    raise SystemExit(f"package archive failed: {message}")


def main():
    if len(sys.argv) != 4:
        fail("usage: package-archive.py ROOT DIRECTORY_NAME OUTPUT.tar.gz")
    root = pathlib.Path(sys.argv[1]).resolve()
    directory = sys.argv[2]
    output = pathlib.Path(sys.argv[3])
    if not root.is_dir() or not directory or "/" in directory or directory in {".", ".."}:
        fail("invalid archive root or top-level directory")
    source = root / directory
    if not source.is_dir() or source.is_symlink():
        fail("top-level package directory is missing or is a symlink")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.GNU_FORMAT) as archive:
            paths = [source]
            for current, directories, files in os.walk(source, followlinks=False):
                directories.sort()
                files.sort()
                current_path = pathlib.Path(current)
                paths.extend(current_path / name for name in directories)
                paths.extend(current_path / name for name in files)
            for path in paths:
                relative = path.relative_to(root).as_posix()
                info = path.lstat()
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    fail(f"unsupported package entry: {relative}")
                item = tarfile.TarInfo(relative + ("/" if path.is_dir() else ""))
                item.uid = item.gid = 0
                item.uname = item.gname = "root"
                item.mtime = 0
                item.mode = 0o755 if path.is_dir() or info.st_mode & 0o111 else 0o644
                if path.is_dir():
                    item.type = tarfile.DIRTYPE
                    item.size = 0
                    archive.addfile(item)
                else:
                    item.type = tarfile.REGTYPE
                    item.size = info.st_size
                    with path.open("rb") as payload:
                        archive.addfile(item, payload)


if __name__ == "__main__":
    main()
