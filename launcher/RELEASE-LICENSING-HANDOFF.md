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
| WeSpeaker ECAPA-TDNN512-LM ONNX | `Wespeaker/wespeaker-ecapa-tdnn512-LM`, pinned revision `a2f3dcb1c8702caccc7a55ceb57f5e8d1842112b`; SHA-256 `d71b85d9b48058ef68004f04f1b78acebefb9dfcf542e19b976a12a5ad1f10b0` | CC BY 4.0, publisher model card | Downloaded by setup after install. The setup Models row now shows WeSpeaker attribution, immutable revision, model-page link, and CC BY 4.0 license link before and after download. The artifact is used unchanged and remains hash-verified. |
| Silero VAD 6.2.1 ONNX | `snakers4/silero-vad`, pinned commit in the runtime manifest | MIT | Downloaded and hash-verified during setup; see model catalog and manifest. |
| Whisper Large-v3 | `Systran/faster-whisper-large-v3`, pinned revision in the model catalog; macOS selects the separate MLX publisher artifact | MIT, as recorded from publisher metadata | Downloaded and hash-verified during setup; no weights in the application archive. |
| pyannote Community-1 | `pyannote/speaker-diarization-community-1`, pinned revision in the model catalog | CC BY 4.0 publisher metadata; gated access terms also apply | Downloaded only after the user accepts the upstream terms and supplies a read token. It is not bundled. Review the model-card terms and attribution for the product flow. |
| FFmpeg and FFprobe | BtbN release `autobuild-2026-10-14-13-03`, release commit `63ac3265b0789ea16c7b1f8351cd2ad195a28722`, asset SHA-256 `602ca2985139dbab87e0d7c70f48afc69548060463dcfab44c0e5f50ebe39df8` | BtbN identifies `linux64-lgpl 8.1` as LGPL; pinned recipe uses `--enable-version3 --disable-debug`, omits GPL/nonfree flags, and includes upstream `COPYING.LGPLv3` | Downloaded directly from BtbN over HTTPS to the user's per-user data directory. SpeechCraft does not bundle or mirror the binaries; it invokes them as subprocesses and retains the LGPL text. The exact open question is whether this setup-triggered direct download is conveyance by SpeechCraft and, if so, whether BtbN's pinned source/recipe plus the stored LGPL text suffice for its static-dependency CLI build or corresponding-source hosting/additional notices are required. Codec patent review also needs a scoped decision. See `setup-controller/resources/FFMPEG-LICENSE-NOTICE.md`. |
| Backend, worker, and model-manager Python dependencies | PyPI and the pinned PyTorch CPU/CUDA or Apple Silicon package indexes recorded in the lockfiles | Individual package licenses | Installed into per-user managed environments. Audit the resolved Linux CPU lock first for this release; CUDA, macOS, and WSL2 profiles have separate lockfiles. |
| Node.js production dependencies | Pinned by `frontend/bun.lock`; standalone production files are included in the archive | Individual package licenses | Included in the Linux archive. Check the packaged dependency tree and required notices against the exact generated archive. |
| Node.js executable | Official Node.js 24.22.0 Linux x64 release, pinned by `launcher/scripts/fetch-node-runtime.mjs` | Node.js license and bundled third-party notices | Executable is included in the archive. Verify that the archive's notice files cover the executable and bundled components. |

## Publication items for review

1. Confirm the setup-flow ECAPA notice against the publisher's CC BY 4.0
   conditions. The visible notice now names WeSpeaker and the model, links to
   the source and license, and identifies the pinned revision. Keep it aligned
   with `PROVENANCE.md` and the model catalog.
2. Resolve the exact FFmpeg decision quoted in
   `setup-controller/resources/FFMPEG-LICENSE-NOTICE.md`: whether setup's
   direct upstream download is conveyance by SpeechCraft; if so, whether the
   pinned source, build recipe, upstream LGPL text, and source links suffice
   for this static-dependency CLI artifact; and whether patent review is
   required for the intended testing release.
3. Check the exact Linux archive's Node.js and frontend production dependency
   notices, then check the locked Python runtime dependencies and their
   delivery/notice flow. Record any missing notices before publishing.
4. Recheck the gated Community-1 model-card terms for the optional user-driven
   download path. Do not add its weights to the application archive.

TitaNet is historical only and is absent from active setup, the model catalog,
and dataset speaker-purity processing. Do not restore it as a fallback.
