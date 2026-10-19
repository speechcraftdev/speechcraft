# v0.1.1 release licensing handoff

This inventory is for the licensing review owner. It identifies the Linux x86-64
application archive and the runtime downloads it can acquire. It is not a legal
opinion or a claim that all distribution obligations are complete.

## Linux application archive

- `speechcraft-0.1.1-linux-x86_64.tar.gz` contains the application, the
  production standalone frontend and its Node.js production dependencies, and
  the official Node.js 24.22.0 Linux x64 executable. It contains no model
  weights and no FFmpeg binary.
- The archive includes the Speechcraft project `LICENSE`, model-manager
  `packages/model_manager/PROVENANCE.md`, model catalog and schema,
  `setup-controller/resources/FFMPEG-LICENSE-NOTICE.md`, and runtime manifests.
- The package is built from the pinned frontend lock and runtime manifests.
  Python environments and their locked dependencies are downloaded and
  installed per user during setup rather than bundled in the archive.

## Runtime-acquired assets

| Component | Publisher/source | Recorded license | Distribution path and handoff |
| --- | --- | --- | --- |
| WeSpeaker ECAPA-TDNN512-LM ONNX | `Wespeaker/wespeaker-ecapa-tdnn512-LM`, pinned revision `a2f3dcb1c8702caccc7a55ceb57f5e8d1842112b`; SHA-256 `d71b85d9b48058ef68004f04f1b78acebefb9dfcf542e19b976a12a5ad1f10b0` | CC BY 4.0, publisher model card | Downloaded by setup after install; attribution is retained in the catalog and provenance file. Confirm the required attribution and license are sufficiently visible to users in the setup/download flow. |
| Silero VAD 6.2.1 ONNX | `snakers4/silero-vad`, pinned commit in the runtime manifest | MIT | Downloaded and hash-verified during setup; see model catalog and manifest. |
| Whisper Large-v3 | `Systran/faster-whisper-large-v3`, pinned revision in the model catalog; macOS selects the separate MLX publisher artifact | MIT, as recorded from publisher metadata | Downloaded and hash-verified during setup; no weights in the application archive. |
| pyannote Community-1 | `pyannote/speaker-diarization-community-1`, pinned revision in the model catalog | CC BY 4.0 publisher metadata; gated access terms also apply | Downloaded only after the user accepts the upstream terms and supplies a read token. It is not bundled. Review the model-card terms and attribution for the product flow. |
| FFmpeg and FFprobe | BtbN pinned release and exact build identity in `setup-controller/resources/runtime-manifest.json` | The selected recipe is marked LGPL; the exact build's options and applicable obligations remain for review | Downloaded directly during setup; binaries are not in the Speechcraft archive. See `setup-controller/resources/FFMPEG-LICENSE-NOTICE.md`. Confirm the publisher's exact license/build configuration, notice/source obligations, and any patent considerations before publication. |
| Backend, worker, and model-manager Python dependencies | PyPI and the pinned PyTorch CPU/CUDA or Apple Silicon package indexes recorded in the lockfiles | Individual package licenses | Installed into per-user managed environments. Audit the resolved Linux CPU lock first for this release; CUDA, macOS, and WSL2 profiles have separate lockfiles. |
| Node.js production dependencies | Pinned by `frontend/bun.lock`; standalone production files are included in the archive | Individual package licenses | Included in the Linux archive. Check the packaged dependency tree and required notices against the exact generated archive. |
| Node.js executable | Official Node.js 24.22.0 Linux x64 release, pinned by `launcher/scripts/fetch-node-runtime.mjs` | Node.js license and bundled third-party notices | Executable is included in the archive. Verify that the archive's notice files cover the executable and bundled components. |

## Publication items for review

1. Confirm CC BY 4.0 attribution and license presentation for ECAPA in the
   installed setup flow. The immutable source revision, checksum, publisher,
   license, and attribution must stay aligned with `PROVENANCE.md` and the
   model catalog.
2. Review the exact BtbN FFmpeg/FFprobe build and the download-time product
   behavior. Determine the applicable source, configuration, notice, and
   other obligations for that artifact; the current notice records that this
   review is open.
3. Check the exact Linux archive's Node.js and frontend production dependency
   notices, then check the locked Python runtime dependencies and their
   delivery/notice flow. Record any missing notices before publishing.
4. Recheck the gated Community-1 model-card terms for the optional user-driven
   download path. Do not add its weights to the application archive.

TitaNet is historical only and is absent from active setup, the model catalog,
and dataset speaker-purity processing. Do not restore it as a fallback.
