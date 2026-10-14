# Model catalog and local storage

The setup controller must keep model assets outside application releases and
outside user project data. The intended root is an OS user data directory
(for example, `~/Library/Application Support/SpeechCraft/models` on macOS and
`$XDG_DATA_HOME/speechcraft/models` or `~/.local/share/speechcraft/models` on
Linux). Resolve the platform location in the installer; do not bake an absolute
path into the catalog.

Store each installed artifact under `<root>/<model-id>/<immutable-revision>/`.
Keep downloader temporary files and completion metadata beside that revision,
and publish the revision directory only after every declared file passes its
integrity checks. Projects may refer to model IDs/revisions but should not copy
weights into project folders. Application upgrades must not replace this store.

`model-catalog.json` is the initial catalog; `model-manifest.schema.json` is
the JSON Schema. The pure-standard-library `speechcraft_dataset.model_manifest`
parser validates the core contract and `verify_artifact()` checks a directory
without network access. `verified` means every declared size/hash matched;
`unverified` means files exist but the catalog has no integrity metadata for
them. Missing assets return `missing`, partially present/short assets return
`partial`, and a declared size or hash mismatch returns `corrupt`.

For a direct check from `workers/dataset`:

```bash
python -m speechcraft_dataset.model_manifest model-catalog.json diarization linux-x86_64 /path/to/model
```

## Next downloader PR

- Resolve the explicit release blockers in the catalog before presenting those
  artifacts as installable, including immutable revisions, complete file
  inventories, licenses/access terms and hashes from authoritative sources.
- Fetch into a temporary directory, support resumable multi-file artifacts,
  verify each file, then atomically publish a completed revision.
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
  bytes are not currently catalog-pinned.
- Speaker purity: production still uses NeMo TitaNet. The sherpa-onnx TitaNet
  export exists in an experiment only; source identity and redistribution status
  remain release blockers. The frozen VR slicer is not changed by this catalog.
- ASR: faster-whisper is present (`1.2.1` worker lock; backend declares
  `>=1.1,<2`) and can use Hugging Face cache/downloads or a configured local
  path. There is no MLX ASR implementation or verified macOS artifact in this
  repository.
