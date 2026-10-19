# Model catalog and local storage

The setup controller must keep model assets outside application releases and
outside user project data. The intended root is an OS user data directory
(for example, `~/Library/Application Support/SpeechCraft/models` on macOS and
`$XDG_DATA_HOME/speechcraft/models` or `~/.local/share/speechcraft/models` on
Linux). Resolve the platform location in the installer; do not bake an absolute
path into the catalog.

Stage 2 implementation and commands are documented in
[the lightweight package](../../packages/model_manager/README.md). Its schema
and catalog are packaged resources; the JSON paths here are compatibility symlinks.

Store each installed artifact under `<root>/<model-id>/<artifact-key>/`, where
the key hashes the complete immutable artifact record, including its revision.
Keep downloader temporary files and completion metadata beside that revision,
and publish the revision directory only after every declared file passes its
integrity checks. Projects may refer to model IDs/revisions but should not copy
weights into project folders. Application upgrades must not replace this store.

`model-catalog.json` is the initial catalog; `model-manifest.schema.json` is
its authoritative structural schema. The standard-library validator evaluates
that schema's supported keywords directly and rejects unsupported keywords.
Additional semantic checks enforce unique roles/IDs/platforms/file paths,
canonical relative paths, consistent size bounds, and nonempty complete
inventories. They are documented in the schema's `$comment`. Schema validation
alone does not establish that catalog metadata is trusted or release-ready.

`inventory_complete` explicitly records whether the full required file set is
known. `availability: verified` describes resolved release metadata, not a local
installation. Unknown inventories, unresolved availability, explicit release
blockers, and missing SHA-256 values each prevent install readiness. The initial
catalog now permits the pinned Silero ONNX artifact; other roles remain blocked.

The verifier reports separate facts:

| Field | Meaning |
| --- | --- |
| `present` | A nonempty inventory was checked and every declared path was readable as a regular file, even if truncated or corrupt. |
| `complete` | The inventory is declared complete and every declared file is present without a detected truncation. This does not establish content integrity. |
| `integrity_verified` | The inventory is complete and every file matched its trusted SHA-256 (and any size constraints). |
| `install_ready` | Integrity is verified, availability is resolved, and there are no release blockers. |
| `runtime_ready` | Always `null` in this PR. No inference or runtime validation has occurred. |

Status precedence is `error` (unsafe path/read failure), `corrupt` (size excess
or hash mismatch), `missing` (all declared files absent), `partial` (some files
absent or truncated), then `verified` only when install-ready; otherwise
`unverified`. An empty inventory is always unverified. Size-only and minimum-size
metadata can detect truncation but cannot verify contents. A content-verified
artifact with unresolved release metadata remains unverified at the aggregate
level and is not install-ready. No files are downloaded or modified.

Only a trusted manifest can supply trusted hashes. The verifier does not
authenticate manifest publishers. It rejects all symlinks below the supplied
root and rejects a symlink as the root's final component, using descriptor-relative
opens with `O_NOFOLLOW` on Linux/macOS. It never opens a declared file through a
symlink, including a symlinked parent directory. HF cache snapshots using symlinks
must be deliberately materialized into regular files by a later installer.
Verification is a point-in-time check: the future installer must serialize writes
and publish immutable directories; a successful report cannot prevent later edits.

Platform selection accepts `linux-x86_64` and `macos-arm64`. An exact platform
entry wins, even if unresolved. Otherwise, an `all-supported` entry may be used
only if its explicit `supported_platforms` includes the requested platform.
Unsupported platforms and missing matches are errors. Shared Silero coverage
selects the same pinned bytes on both platforms; inference validation remains
deferred.

For a direct check from `workers/dataset`:

```bash
python -m speechcraft_dataset.model_manifest model-catalog.json diarization linux-x86_64 /path/to/model
python -m unittest tests.test_model_manifest -v
```

The CLI emits JSON and exits `0` only for install-ready assets, `1` for any failed
or unresolved file/release check, and `2` for malformed manifests, bad selection,
or command-line usage. Tests use only fabricated files and the standard library;
they require neither network access nor an ML environment.

## Next controller integration

- Resolve the explicit release blockers in the catalog before presenting those
  artifacts as installable, including immutable revisions, complete file
  inventories, licenses/access terms and hashes from authoritative sources.
- Use the Stage 2 lightweight downloader for staging, resumption, verification,
  locking and atomic publication; add controller cancellation and explicit cleanup.
- Expose progress and actionable access/network/disk errors to the future UI;
  never begin model inference until installation verification succeeds.
- Add runtime validation separately: this PR defines contracts only and does
  not load models or run inference.

## Repository implementation status (2026-10-14)

- Community-1: production worker accepts a local snapshot path and is offline;
  its current guard checks only `embedding/pytorch_model.bin` and
  `segmentation/pytorch_model.bin` minimum sizes. No immutable snapshot revision
  or full artifact inventory is recorded.
- Silero: worker depends on `silero-vad==6.2.1`; standard VAD loads the package
  model and the frozen VR slicer requests its ONNX variant. Standalone model
  bytes are now pinned and independently downloadable by Stage 2; production
  loading continues to use the existing package resource.
- Speaker Purity uses the manifest-pinned WeSpeaker ECAPA-TDNN512-LM ONNX artifact with the publisher-compatible Kaldi fbank frontend and offline ONNX Runtime inference. Raw cosine similarities are continuous, uncalibrated review measurements; they are not probabilities or automatic pass/fail decisions.
- ASR: faster-whisper is present (`1.2.1` worker lock; backend declares
  `>=1.1,<2`) and can use Hugging Face cache/downloads or a configured local
  path. There is no MLX ASR implementation or verified macOS artifact in this
  repository.
