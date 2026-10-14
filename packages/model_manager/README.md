# SpeechCraft model preparation — Stage 2

`speechcraft_models` is an internal Python 3.11+ helper, distributed as
`speechcraft-model-manager`. Its only runtime dependencies are pinned
`huggingface-hub==0.36.2` and `hf-xet==1.5.0`; it does not install ML runtimes.
It packages the catalog and JSON Schema, installs verified Silero ONNX, and
contains a public Hugging Face adapter for pinned catalog entries. There is no
`speechcraft-models` executable; the eventual `speechcraft` app launcher remains
reserved for the application.

## Build, install, download

From the repository root (requires `uv` for these developer build commands):

```bash
uv build --wheel --out-dir /tmp/speechcraft-stage2-dist packages/model_manager
uv venv --python 3.12 /tmp/speechcraft-stage2-env
uv pip install --python /tmp/speechcraft-stage2-env/bin/python /tmp/speechcraft-stage2-dist/speechcraft_model_manager-0.1.0-py3-none-any.whl
```

For developer testing only, invoke the internal CLI as a Python module. After
installing the wheel, this command downloads, verifies, and publishes Silero and
its license into a clean directory. Run it again to verify and reuse:

```bash
/tmp/speechcraft-stage2-env/bin/python -m speechcraft_models install speech_detection --platform linux-x86_64 --root /tmp/speechcraft-models-demo
```

Use `--platform macos-arm64` on Apple Silicon. `status` checks a managed store
without downloading. `verify` checks an arbitrary artifact directory, supplied
as `--root`. JSON results go to stdout; progress JSON lines go to stderr. Exit
codes are `0` for file-verified success, `1` for failed/blocked checks or installs,
and `2` for invalid CLI arguments. The legacy manifest CLI retains its original
exit-code contract. There is no URL or custom-catalog option on the install API.
The future setup controller calls the library directly; developers may use
`python -m speechcraft_models` for local diagnostics and testing.

## Library and progress contract

```python
from speechcraft_models import install_model, check_installation, verify_artifact

result = install_model(root, "speech_detection", "macos-arm64", progress=on_event)
status = check_installation(root, "speech_detection", "macos-arm64")
# Offline check against an already trusted artifact record:
verification = verify_artifact(directory, artifact)
```

The callback receives JSON-serializable dicts synchronously. It should return
quickly; callback exceptions abort the call and leave recoverable staging data.
Events are `resolving`, `waiting_for_lock`, `capacity`, `downloading`, `progress`,
`retrying`, `file_verified`, `verifying`, `publishing`, `installed`, `reused`,
and `failed`, plus Hugging Face `source_acquiring`, `source_progress`, and
`source_file_acquired`. GitHub transfer events report byte counts; the pinned
Hub API exposes trustworthy selected-file completion counts but no byte callback
through `snapshot_download`, so Hub progress reports files completed/total and
does not invent a percentage. Events never contain download URLs or request
headers. Byte counts can reset on restart. `capacity` reports required and
available bytes. `failed`/`retrying` include sanitized errors; completion includes `root`.
`InstallError` means no success is claimed. Status may race with a concurrent
installer and should be refreshed after completion.

The existing verifier's `present`, `complete`, `integrity_verified`, and
`install_ready` fields are preserved. `runtime_ready` is always `null`.
Installation results add `reused`. File readiness does not establish inference
readiness. Catalog licensing/access status, inventory completeness, immutable
revision, exact positive sizes, hashes, approved HTTPS sources, and release
blockers are checked before transfer. Hugging Face acquisitions must match the
catalog repository, filename allowlist, and a full 40-character commit. No Hub
token is read or persisted. TitaNet can only be fetched with the explicit
Python `experimental=True` option while its provenance/licensing blockers remain;
its report has `integrity_verified: true`, `install_ready: false`, and
`runtime_ready: null`. Normal installation refuses it.

## Storage and recovery

Choose a user data directory outside releases and projects: for example
`~/.local/share/speechcraft/models` or
`~/Library/Application Support/SpeechCraft/models`. No default path is implicitly
written. Tests use disposable temporary directories and never use model caches.

```text
<root>/<model-id>/
  <artifact-key>/                    # verified, atomically published directory
  .lock-<artifact-key>               # persistent inode; OS releases flock on exit
  .stage-<artifact-key>/            # one predictable owner per artifact/lock
    0.part, 0.resume.json, ...        # checkpointed sequential transfers
    payload-<random>/                # fresh publication copy per attempt
    .hf-source/                       # pinned Hub local_dir retained across retries
  .quarantine-<artifact-key>-<uuid>/  # retained invalid same-version installation
```

The artifact key is SHA-256 of canonical sorted compact JSON for the full artifact
record, including its immutable upstream revision. Changed versions/inventories
get distinct directories; prior valid versions are never replaced or deleted.
There is no mutable `current` link. Callers obtain the selected path from results.

A per-artifact `flock` serializes processes and waits at most 60 seconds. Lock
files are never unlinked. Staging cleanup runs while holding the current
artifact lock and obtains each other stage lock nonblocking before touching it.
It removes abandoned `payload-*` directories and checkpoint temp files
immediately, preserves resumable partials for seven days, then expires the stage.
Cleanup only walks private, current-user-owned `.stage-<64 hex>` directories
under the model directory. Partial files are retained as resume candidates and
validated before use; stale attempts expire after seven days. Removal uses
directory descriptors and unlinks symlinks without following them. Published
model versions are never cleanup targets. Tiny lock files remain because
unlinking a lock inode can split concurrent locks. The root and all managed children reject symlinks;
transfer files reject hardlinks before truncation. Writes and directory traversal
use descriptors with `O_NOFOLLOW`. Parent aliases of the explicitly chosen root
(e.g. `/tmp` on macOS) are allowed. The store assumes a local filesystem and one
user; advisory locking is not a security boundary against malicious same-user
processes that ignore locks. NFS and native Windows are outside this milestone.
On Windows 11 use Ubuntu WSL2 and keep the model store on the WSL Linux
filesystem, rather than a Windows mounted drive. macOS uses the same POSIX
implementation; native Apple Silicon acquisition testing is still pending.

GitHub artifacts reserve two full copies plus 1 MiB, without crediting partial
data. Hugging Face artifacts use `snapshot_download(local_dir=...)` into the
stable `.hf-source` staging directory. The Hub library's `.cache/huggingface`
metadata stays there during retries, then is removed after verification. The
verified files are renamed into the published directory on the same filesystem;
SpeechCraft does not make a second full model copy or request other repository
files. Hugging Face's Xet chunk cache stays at the path configured by
`HF_XET_CACHE` (normally `~/.cache/huggingface/xet`). Hugging Face and the user
own that shared cache; SpeechCraft never deletes it. Capacity checks reserve
temporary output plus 1 MiB. When the Xet chunk cache is explicitly enabled, the
check also reserves possible configured cache growth, as detailed in the
Whisper section below. When both paths share a filesystem, requirements are
combined; otherwise each volume is checked separately. Existing use is reflected
in free-space readings; cached chunk bytes are credited against configured cache
growth. ENOSPC/write/fsync failures propagate without a success report. GitHub
transfers use at most three attempts and bounded exponential retry delays; the
Hugging Face library owns transfer retry/resumption for Hub/Xet.

In-memory SHA-256 is updated from each received chunk; an uninterrupted transfer
never re-reads its growing file. Checkpoints are due after 4 MiB or five seconds,
with at most one chunk of overshoot. Each checkpoint fsyncs partial data, then
atomically saves the URL, strong ETag, durable prefix length and digest, followed
by a directory fsync. A crash between data flush and metadata replacement leaves
a length mismatch and safely restarts the file. An interrupted in-process attempt
truncates to its last durable offset. At completion the incremental digest, which
includes a prefix rehashed once on resume, is compared with the trusted catalog
hash after flushing. The copied publication payload is independently checked by
the offline verifier. Range requests use `If-Range`, require the expected
start/end/total and matching ETag,
and check identity encoding/length. A 200 response restarts from zero; invalid
ranges/416 restart on the next bounded attempt. Missing validators or mismatched
checkpoints also restart. A full-sized partial needs a full hash, regardless of
its checkpoint. Whole-file integrity failures clear the affected transfer.

Only catalog-listed files are requested from the Hub or copied into a GitHub
payload. The complete payload is verified and fsynced before its directory is
renamed within the same filesystem.
A crash before publication leaves no final installation; a crash after publication
leaves bytes that the next call verifies before reuse. Abandoned payloads are
removed on the next install under the stage lock; recoverable partials remain
within the seven-day retention window. Invalid same-version trees
are retained in quarantine before replacement for diagnosis. Current-user-owned
quarantine directories older than 30 days are removed under their artifact lock;
recent quarantines, unsafe entries, and quarantines whose lock is active are
left untouched. This cleanup only walks exact manager-generated quarantine names
under the model directory, and it never considers published version directories.
The capacity check accounts for required new bytes, so repeated failed attempts
can eventually report insufficient space rather than deleting data. Do not edit
installed trees in place.

## Tests

Default tests are offline; GitHub and Hugging Face integration use tiny loopback
fixtures and never fetch model weights:

```bash
PYTHONPATH="$PWD/packages/model_manager" python -m unittest discover -s packages/model_manager/tests -v
packages/model_manager/scripts/test_installed_package.sh
PYTHONPATH="$PWD/workers/dataset" /tmp/speechcraft-stage2-env/bin/python -m unittest discover -s workers/dataset/tests -p test_model_manifest.py -v
uv lock --check --project workers/dataset
```

The package smoke script builds a wheel, installs it into a clean Python 3.12
environment with only the two pinned Hub dependencies, copies tests outside the
checkout, removes `PYTHONPATH`, and runs offline verification, CLI checks and
the full fake-server suite there. Dependency installation may require network
access; the tests themselves do not download real models.

Real download smoke is explicitly opt-in and uses a temporary model store. It
uses the internal module interface, never a separate installed executable:

```bash
/tmp/speechcraft-stage2-env/bin/python -I packages/model_manager/tests/smoke_real_silero.py --download-real-silero
```

TitaNet's real public Hub download is also opt-in. The script is developer-only:
it uses `experimental=True`, verifies the exact file, confirms repeat reuse, and
keeps the result's `install_ready` false because catalog release blockers remain.
Run from an installed package environment:

```bash
/tmp/speechcraft-stage2-env/bin/python -I packages/model_manager/tests/smoke_titanet.py --download-real-titanet --platform linux-x86_64
```

### macOS handoff for Kavin (Apple Silicon M4 Pro)

Run these checks on the M4 Pro; Linux results do not establish native macOS
support. They install the lightweight package only and put model files in a
temporary directory:

```bash
uname -m
uv venv --python 3.12 /tmp/speechcraft-model-manager-macos
uv pip install --python /tmp/speechcraft-model-manager-macos/bin/python packages/model_manager
/tmp/speechcraft-model-manager-macos/bin/python -I packages/model_manager/tests/smoke_titanet.py --download-real-titanet --platform macos-arm64
```

The smoke should report verified file integrity, `install_ready: false`,
`runtime_ready: null`, and reuse on the second call. Record the macOS version,
Python version, test result, and any filesystem or Xet-cache issue. Do not clear
the catalog's macOS release blocker until Kavin has run this on Apple Silicon
and the provenance/licensing blockers are resolved.

See [PROVENANCE.md](PROVENANCE.md) for measured identities and sources. Linux
packaging/download/recovery tests were executed; native macOS execution remains
pending Kavin's handoff. No inference readiness is claimed on either platform.

## Whisper Large-v3 variants

`asr` selects one catalog-pinned full Whisper Large-v3 artifact by platform:
Linux x86-64 and Windows WSL2 use `Systran/faster-whisper-large-v3` in
CTranslate2 format; macOS ARM64 uses `mlx-community/whisper-large-v3-mlx` in
MLX format. There is no size/model fallback. WSL2 callers may pass
`windows-wsl2`; storage under `/mnt/<drive>` is rejected when running inside
WSL. Keep both the model store and caches on the WSL Linux filesystem.

The catalog pins every required file by size and SHA-256. Installation reserves
space for bytes not already integrity-verified in resumable staging. The pinned
`hf-xet` chunk cache defaults to disabled, so it does not require a second
artifact-sized copy. If `HF_XET_CHUNK_CACHE_SIZE_BYTES` enables it, the capacity
check reserves possible cache growth up to the smaller of that limit and the
remaining model bytes, less already allocated chunk-cache data. When the model
store and Xet cache share a filesystem, required bytes are added. An incomplete
local file is retained only according to
Hugging Face's supported local-dir resume metadata; SpeechCraft never appends to
it itself. Verified model files are published by same-filesystem rename from
staging, with no second permanent snapshot copy. `HF_XET_CACHE` configures the
shared Xet cache path, while the explicit model-store root controls persistent
verified files and temporary local-dir state. The future setup controller can
set both before launching the helper. SpeechCraft does not delete shared Xet
cache content.

The pinned `huggingface_hub==0.36.2` API's documented `tqdm_class` callback
tracks selected-file completion only; it is not passed to the per-file byte
transfer. Therefore Hub events identify role, requested platform, file counts,
and known catalog byte total, while transferred bytes/current file remain null
and transfer progress is marked indeterminate. Verification and publication
are separate events. See [PROVENANCE.md](PROVENANCE.md) for immutable revisions,
full file inventories, measured hashes and source references.

Runtime validation remains deferred. Linux uses the worker's pinned
`faster-whisper==1.2.1`; the Mac candidate is `mlx-whisper==0.4.3` and requires
Kavin's local M4 Pro validation. MLX acquisition does not create a verified
CPU fallback. A separate CTranslate2 Mac artifact, if needed, must be decided
explicitly during runtime integration.

For Linux real-network installation, temporary-store measurement and repeat
reuse, install the wheel as above and run:

```bash
/tmp/speechcraft-stage2-env/bin/python -I packages/model_manager/tests/smoke_whisper.py --download-real-whisper --platform linux-x86_64
```

Do not run large-network smoke tests in ordinary CI. For native macOS steps,
see [MACOS_WHISPER_HANDOFF.md](MACOS_WHISPER_HANDOFF.md).

## Next integration milestone

The independent setup controller can install this wheel and call the library
before provisioning the ML worker. Bridge callback events into the controller's
progress transport and retain the returned artifact path. Add cancellation,
explicit garbage collection and runtime validation as later contracts. Wiring
the downloaded directory into production inference is intentionally deferred.
The worker imports the verifier through a compatibility shim and has a local
package dependency; its existing model loader and frozen VR slicing are unchanged.
The old schema/catalog paths are compatibility symlinks to packaged resources,
not duplicate authorities.
