"""Trusted public Hugging Face snapshot acquisition (no user URLs or tokens)."""
from __future__ import annotations

import re
import io
import shutil
import stat
import traceback
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

# Gated downloads are deliberately narrower: only this repository, immutable
# revision and complete catalog inventory can consume an explicit credential.
GATED_REPOSITORIES = {
    "pyannote/speaker-diarization-community-1": {
        "revision": "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee",
        "files": frozenset({
            "config.yaml", "embedding/pytorch_model.bin", "plda/plda.npz",
            "plda/xvec_transform.npz", "segmentation/pytorch_model.bin",
        }),
        "access_file": "config.yaml",
    },
}


class HuggingFaceAcquisitionError(RuntimeError):
    """Safe diagnostic without signed URLs or request credentials."""
    def __init__(self, message: str, *, transient: bool = False,
                 diagnostic: dict[str, str | int] | None = None):
        super().__init__(message)
        self.transient = transient
        self.diagnostic = diagnostic or {}


def _safe_exception_diagnostic(exc: Exception) -> dict[str, object]:
    """Return allowlisted structural details; never include upstream text or URLs."""
    name = type(exc).__name__
    diagnostic: dict[str, object] = {}
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,79}", name):
        diagnostic["exception_type"] = name
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(response, "status", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        diagnostic["http_status"] = status
    if isinstance(exc, AttributeError):
        missing = getattr(exc, "name", None)
        if isinstance(missing, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", missing):
            diagnostic["attribute"] = missing
        obj = getattr(exc, "obj", None)
        object_type = type(obj).__name__ if obj is not None else None
        if isinstance(object_type, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", object_type):
            diagnostic["object_type"] = object_type
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        module = frame.f_globals.get("__name__")
        function = frame.f_code.co_name
        if (isinstance(module, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,160}", module)
                and re.fullmatch(r"[A-Za-z0-9_]{1,100}", function)):
            frames.append({"module": module, "function": function})
        tb = tb.tb_next
    if frames:
        diagnostic["frames"] = frames[-4:]
    return diagnostic


def validate_request(repo_id: str, revision: str, filenames: list[str]) -> None:
    approved = PUBLIC_REPOSITORIES.get(repo_id)
    if approved is None or not filenames or not set(filenames) <= approved:
        raise HuggingFaceAcquisitionError("Hugging Face repository or file is not allowlisted")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise HuggingFaceAcquisitionError("Hugging Face revision must be a full immutable commit hash")
    if len(set(filenames)) != len(filenames):
        raise HuggingFaceAcquisitionError("duplicate Hugging Face file path")


def validate_gated_catalog_request(repo_id: str, revision: str, filenames: list[str]) -> None:
    approved = GATED_REPOSITORIES.get(repo_id)
    if approved is None or revision != approved["revision"] or set(filenames) != approved["files"]:
        raise HuggingFaceAcquisitionError("gated Hugging Face repository, revision, or inventory is not allowlisted")


def validate_gated_request(repo_id: str, revision: str, filenames: list[str], token: str) -> None:
    validate_gated_catalog_request(repo_id, revision, filenames)
    if not isinstance(token, str) or not re.fullmatch(r"hf_[A-Za-z0-9_-]{16,512}", token):
        raise HuggingFaceAcquisitionError("HF_TOKEN_INVALID: credential format is invalid")


def _safe_hf_code(exc: Exception) -> tuple[str, str, bool]:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__
    if status == 401 or name in {"InvalidTokenError", "LocalTokenNotFoundError"}:
        return "HF_TOKEN_INVALID", "Hugging Face rejected this token. Create a valid read token and try again.", False
    if status in (403, 404):
        return "HF_MODEL_ACCESS_REQUIRED", "Accept the Community-1 model conditions and ensure this token has read access, then retry.", False
    if status == 429:
        return "HF_RATE_LIMITED", "Hugging Face is rate limiting this request. Wait briefly and retry.", True
    if status in (408, 500, 502, 503, 504) or name in {
        "ConnectionError", "ConnectError", "ReadError", "Timeout", "TimeoutException",
        "RemoteProtocolError", "ConnectTimeout", "ReadTimeout",
    }:
        return "HF_NETWORK_ERROR", "Could not reach Hugging Face. Check the network connection and retry.", True
    return "HF_NETWORK_ERROR", "The Hugging Face authorization request failed safely. Check the connection and retry.", False


def _official_gated_client(endpoint: str | None = None):
    from huggingface_hub import HfApi, hf_hub_download, snapshot_download

    api = HfApi(endpoint=endpoint) if endpoint else HfApi()
    return api, hf_hub_download, snapshot_download


def acquire_gated_snapshot(repo_id: str, revision: str, filenames: list[str], token: str,
                           local_dir: Path, progress, *, total_bytes: int,
                           hub_client=None) -> None:
    """Validate identity and actual file read access, then fetch allowlisted files.

    ``hub_client`` is an internal deterministic fake-Hub seam used by offline
    integration tests. Production always uses the official Hub client above.
    The token is never placed in process environment, URLs, or progress events.
    """
    validate_gated_request(repo_id, revision, filenames, token)
    progress({"event": "authorization_status", "state": "checking_token", "source": "huggingface"})
    try:
        api, download_file, download_snapshot = hub_client or _official_gated_client()
        identity = api.whoami(token=token)
        if not isinstance(identity, dict) or not identity.get("name"):
            raise HuggingFaceAcquisitionError("HF_TOKEN_INVALID: Hugging Face did not validate this credential.")
    except HuggingFaceAcquisitionError:
        raise
    except Exception as exc:
        code, message, transient = _safe_hf_code(exc)
        raise HuggingFaceAcquisitionError(f"{code}: {message}", transient=transient,
                                          diagnostic=_safe_exception_diagnostic(exc)) from None

    progress({"event": "authorization_status", "state": "checking_model_access", "source": "huggingface"})
    try:
        info = api.model_info(repo_id, revision=revision, token=token, files_metadata=True)
        if getattr(info, "sha", revision) != revision:
            raise HuggingFaceAcquisitionError("HF_SNAPSHOT_INCOMPLETE: Hugging Face did not resolve the pinned revision.")
        siblings = {getattr(item, "rfilename", None) for item in getattr(info, "siblings", [])}
        if not set(filenames) <= siblings:
            raise HuggingFaceAcquisitionError("HF_SNAPSHOT_INCOMPLETE: The pinned repository is missing required model files.")
        # Metadata is not the access check: read the actual pinned small config
        # through the authenticated Hub endpoint before starting the snapshot.
        local_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        download_file(repo_id=repo_id, filename=GATED_REPOSITORIES[repo_id]["access_file"],
                      revision=revision, token=token, local_dir=str(local_dir), force_download=False)
    except HuggingFaceAcquisitionError:
        raise
    except Exception as exc:
        code, message, transient = _safe_hf_code(exc)
        raise HuggingFaceAcquisitionError(f"{code}: {message}", transient=transient,
                                          diagnostic=_safe_exception_diagnostic(exc)) from None

    # snapshot_download's supported tqdm hook reports file completion. Byte
    # values remain unknown rather than fabricating a percentage.
    from tqdm.auto import tqdm
    class _FileProgress(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            result = super().update(n)
            progress({"event": "source_progress", "source": "huggingface",
                      "state": "downloading", "current_file": None,
                      "downloaded_bytes": None, "transferred_bytes": None,
                      "total_bytes": total_bytes,
                      "transfer_indeterminate": True,
                      "files_completed": int(self.n),
                      "files_total": int(self.total) if self.total is not None else None})
            return result

    try:
        progress({"event": "authorization_status", "state": "downloading", "source": "huggingface"})
        download_snapshot(repo_id=repo_id, repo_type="model", revision=revision,
                          token=token, local_dir=str(local_dir), allow_patterns=filenames,
                          max_workers=1, tqdm_class=_FileProgress)
        # huggingface_hub's local_dir mode maintains resumability metadata under
        # .cache/huggingface. Keep it while transfers can be retried, then remove
        # only that generated directory before exact-tree verification/publication.
        root_info = local_dir.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise HuggingFaceAcquisitionError("HF_SNAPSHOT_INTEGRITY_FAILED: staging directory became unsafe.")
        metadata = local_dir / ".cache"
        try:
            metadata_info = metadata.lstat()
        except FileNotFoundError:
            metadata_info = None
        if metadata_info is not None:
            if stat.S_ISLNK(metadata_info.st_mode) or not stat.S_ISDIR(metadata_info.st_mode):
                raise HuggingFaceAcquisitionError("HF_SNAPSHOT_INTEGRITY_FAILED: Hugging Face staging metadata path is unsafe.")
            shutil.rmtree(metadata)
    except HuggingFaceAcquisitionError:
        raise
    except Exception as exc:
        code, message, transient = _safe_hf_code(exc)
        raise HuggingFaceAcquisitionError(f"{code}: {message}", transient=transient,
                                          diagnostic=_safe_exception_diagnostic(exc)) from None


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
                      "downloaded_bytes": None, "transferred_bytes": None,
                      "total_bytes": total_bytes,
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
