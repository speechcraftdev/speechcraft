"""Trusted public Hugging Face snapshot acquisition (no user URLs or tokens)."""
from __future__ import annotations

import re
import io
from pathlib import Path

# This is an explicit filename allowlist in addition to the packaged catalog.
# Extend only alongside reviewed catalog records.
PUBLIC_REPOSITORIES = {
    "csukuangfj/speaker-embedding-models": frozenset({
        "nemo_en_titanet_large.onnx",
    }),
    "Systran/faster-whisper-large-v3": frozenset({
        "config.json", "model.bin", "preprocessor_config.json",
        "tokenizer.json", "vocabulary.json",
    }),
    "mlx-community/whisper-large-v3-mlx": frozenset({
        "config.json", "weights.npz",
    }),
}


class HuggingFaceAcquisitionError(RuntimeError):
    """Safe diagnostic without signed URLs or request credentials."""
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def validate_request(repo_id: str, revision: str, filenames: list[str]) -> None:
    approved = PUBLIC_REPOSITORIES.get(repo_id)
    if approved is None or not filenames or not set(filenames) <= approved:
        raise HuggingFaceAcquisitionError("Hugging Face repository or file is not allowlisted")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise HuggingFaceAcquisitionError("Hugging Face revision must be a full immutable commit hash")
    if len(set(filenames)) != len(filenames):
        raise HuggingFaceAcquisitionError("duplicate Hugging Face file path")


def acquire_public_snapshot(repo_id: str, revision: str, filenames: list[str],
                            local_dir: Path, progress, *, total_bytes: int,
                            test_endpoint: str | None = None) -> None:
    """Download only named files using the supported Hub snapshot API.

    The packaged caller supplies the repository and filenames from the trusted
    catalog. ``test_endpoint`` exists only for private loopback integration tests.
    """
    validate_request(repo_id, revision, filenames)
    endpoint = "https://huggingface.co"
    if test_endpoint is not None:
        if not (test_endpoint.startswith("http://127.0.0.1:")
                or test_endpoint.startswith("http://localhost:")):
            raise HuggingFaceAcquisitionError("test Hub endpoint must be loopback-only")
        endpoint = test_endpoint
    try:
        from huggingface_hub import snapshot_download
        from tqdm.auto import tqdm
    except ImportError as exc:
        raise HuggingFaceAcquisitionError(
            "Hugging Face support is unavailable; install the lightweight package dependencies"
        ) from exc

    class _FileProgress(tqdm):
        """Hub snapshot progress counts completed files, not bytes."""
        def __init__(self, *args, **kwargs):
            kwargs["disable"] = False
            kwargs["file"] = io.StringIO()
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            result = super().update(n)
            progress({"event": "source_progress", "source": "huggingface",
                      "state": "downloading", "current_file": None,
                      "transferred_bytes": None, "total_bytes": total_bytes,
                      "transfer_indeterminate": True,
                      "files_completed": int(self.n),
                      "files_total": int(self.total) if self.total is not None else None})
            return result

    kwargs = {
        "repo_id": repo_id,
        "repo_type": "model",
        "revision": revision,
        "local_dir": str(local_dir),
        "allow_patterns": filenames,
        "max_workers": 1,
        "token": False,
        "tqdm_class": _FileProgress,
    }
    if test_endpoint is not None:
        kwargs["endpoint"] = endpoint
    try:
        snapshot_download(**kwargs)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        transient = status in (408, 429, 500, 502, 503, 504)
        if isinstance(status, int):
            detail = f"HTTP {status}"
        elif type(exc).__name__ in {"ChunkedEncodingError", "Timeout", "ConnectTimeout", "ReadTimeout"}:
            detail = type(exc).__name__
            transient = True
        elif isinstance(exc, OSError):
            detail = f"local staging I/O error ({exc.strerror or type(exc).__name__})"
            transient = exc.errno in (11, 110)
        else:
            # Hub exceptions can include signed CDN URLs or headers. Do not
            # relay their text into progress events, CLI output, or setup logs.
            detail = type(exc).__name__
            transient = type(exc).__name__ in {
                "ConnectionError", "ConnectError", "ReadError", "Timeout",
                "TimeoutException", "RemoteProtocolError",
            }
        raise HuggingFaceAcquisitionError(
            f"Hugging Face acquisition failed: {detail}", transient=transient
        ) from None
