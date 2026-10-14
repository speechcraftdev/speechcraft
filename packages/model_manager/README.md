# SpeechCraft model preparation — Stage 2

`speechcraft_models` is an internal Python 3.11+ helper, distributed as
`speechcraft-model-manager` with **zero runtime dependencies**. It packages
the catalog and JSON Schema and installs the approved public Silero ONNX
artifact. Linux x86_64 and macOS arm64 share the same model bytes. No ML imports
or inference occur. There is no `speechcraft-models` executable; the eventual
`speechcraft` app launcher remains reserved for the application.

## Build, install, download

From the repository root (requires `uv` for these developer build commands):

```bash
uv build --wheel --out-dir /tmp/speechcraft-stage2-dist packages/model_manager
uv venv --python 3.12 /tmp/speechcraft-stage2-env
uv pip install --python /tmp/speechcraft-stage2-env/bin/python --no-deps --no-index /tmp/speechcraft-stage2-dist/speechcraft_model_manager-0.1.0-py3-none-any.whl
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
`retrying`, `file_verified`, `verifying`, `publishing`, `installed`, `reused`, and
`failed`. Byte events include `path`, `downloaded_bytes`, `total_bytes`, and
`attempt`; byte counts can reset on restart. `capacity` reports required and
available bytes. `failed`/`retrying` include `error`; completion includes `root`.
`InstallError` means no success is claimed. Status may race with a concurrent
installer and should be refreshed after completion.

The existing verifier's `present`, `complete`, `integrity_verified`, and
`install_ready` fields are preserved. `runtime_ready` is always `null`.
Installation results add `reused`. File readiness does not establish inference
readiness. Catalog licensing/access status, inventory completeness, immutable
revision, exact positive sizes, hashes, approved HTTPS sources, and release
blockers are checked before transfer. Other model roles remain blocked.

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
processes that ignore locks. NFS/Windows are outside this milestone.

Capacity checks conservatively reserve two full copies plus 1 MiB, without
crediting partial data. ENOSPC/write/fsync failures propagate without a success
report. Transfers use one stream, 15-second socket timeouts, at most three
attempts per file, and bounded exponential retry delays. Only transient HTTP
408/429/500/502/503/504 and transport failures retry; redirects are refused.

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

Only listed files are copied into a fresh payload. The complete payload is
verified and fsynced before its directory is renamed within the same filesystem.
A crash before publication leaves no final installation; a crash after publication
leaves bytes that the next call verifies before reuse. Abandoned payloads are
removed on the next install under the stage lock; recoverable partials remain
within the seven-day retention window. Invalid same-version trees
are retained in quarantine before replacement for diagnosis. Current-user-owned
quarantine directories older than 30 days are removed under their artifact lock;
recent quarantines, unsafe entries, and quarantines whose lock is active are
left untouched. This cleanup only walks exact manager-generated quarantine names
under the model directory, and it never considers published version directories.
The capacity check accounts for new copies, so repeated failed attempts can
eventually report insufficient space rather than deleting data. Do not edit
installed trees in place.

## Tests

Default tests are entirely offline; HTTP integration uses tiny loopback fixtures:

```bash
PYTHONPATH="$PWD/packages/model_manager" python -m unittest discover -s packages/model_manager/tests -v
packages/model_manager/scripts/test_installed_package.sh
PYTHONPATH="$PWD/workers/dataset" /tmp/speechcraft-stage2-env/bin/python -m unittest discover -s workers/dataset/tests -p test_model_manifest.py -v
uv lock --check --offline --project workers/dataset
```

The package smoke script builds a wheel, installs it alone into a clean Python
3.12 environment, copies the tests outside the checkout, removes `PYTHONPATH`,
and runs offline verification, CLI checks and the full fake-server suite there.
Build tools may require network access; test execution never downloads models.

Real download smoke is explicitly opt-in and uses a temporary model store. It
uses the internal module interface, never a separate installed executable:

```bash
/tmp/speechcraft-stage2-env/bin/python -I packages/model_manager/tests/smoke_real_silero.py --download-real-silero
```

See [PROVENANCE.md](PROVENANCE.md) for measured identities and sources. Linux
packaging/download/recovery tests were executed; native macOS execution remains
a future CI check. No inference readiness is claimed on either platform.

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
