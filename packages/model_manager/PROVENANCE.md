# Silero artifact provenance (2026-10-14)

Inspected SpeechCraft HEAD `c411984`. The frozen VR loader calls
`load_silero_vad(onnx=True)` without an opset override. In `silero-vad==6.2.1`,
that selects **opset 16**, package resource `silero_vad/data/silero_vad.onnx` and
`OnnxWrapper(..., force_onnx_cpu=True)`. It does not select the opset-15 variant,
16k-only variants, the half model, or the JIT model. The separate ordinary VAD
path uses the package's JIT default; this ONNX preparation does not replace it.

Authoritative evidence:

- [PyPI 6.2.1 release](https://pypi.org/project/silero-vad/6.2.1/)
- [Pinned upstream loader](https://github.com/snakers4/silero-vad/blob/7e30209a3e901f9842f81b225f3e93d8199902b1/src/silero_vad/model.py)
- [Pinned ONNX wrapper](https://github.com/snakers4/silero-vad/blob/7e30209a3e901f9842f81b225f3e93d8199902b1/src/silero_vad/utils_vad.py)
- [Tag resolution](https://api.github.com/repos/snakers4/silero-vad/git/ref/tags/v6.2.1): commit `7e30209a3e901f9842f81b225f3e93d8199902b1`

Downloaded the official wheel independently and verified its size and SHA-256
against PyPI JSON metadata and the existing worker `uv.lock`:

- `silero_vad-6.2.1-py3-none-any.whl`, **9,146,242 bytes**
- SHA-256 `09de93c4d874bb19c53e62a47dd38be5f163cedad2b5599583231f2a84ef79cb`
- [Exact wheel URL](https://files.pythonhosted.org/packages/0b/2b/48566f29a8b53d856ceb1994f209122749b3fda0a733a07e82047257de7a/silero_vad-6.2.1-py3-none-any.whl)

Compared the wheel member byte-for-byte with the installed worker resource and
the raw GitHub file at that full commit. All three were identical:

| Installed artifact path | Size | SHA-256 |
| --- | ---: | --- |
| `silero_vad.onnx` | 2,327,524 | `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3` |
| `LICENSE` | 1,075 | `2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b` |

The catalog downloads directly from these immutable upstream resources:

- [ONNX bytes](https://raw.githubusercontent.com/snakers4/silero-vad/7e30209a3e901f9842f81b225f3e93d8199902b1/src/silero_vad/data/silero_vad.onnx)
- [MIT license notice](https://raw.githubusercontent.com/snakers4/silero-vad/7e30209a3e901f9842f81b225f3e93d8199902b1/LICENSE)

An offline metadata inspection using the already-existing experiment ONNX
environment found opset 16 and **345 tensors, zero external tensor references**,
including tensors within nested subgraphs. No inference was performed. The ONNX
file is the complete runtime model artifact; LICENSE is included for distribution.
Python wrappers, Torch and ONNX Runtime belong to the later runtime environment,
not this model payload. No ONNX/ML dependency was added to preparation tooling.

The same architecture-independent ONNX bytes apply to Linux x86_64 and macOS
arm64; CPU runtime availability and actual inference are separately deferred.
The downloader uses only the pinned catalog, not mutable tags, aliases or PyPI
resolution at install time. Other model entries remain unresolved as before.

## TitaNet-Large Hugging Face artifact (2026-10-14)

The public repository API independently resolved
`csukuangfj/speaker-embedding-models` to immutable commit
`0743f301363dec56491a490f6d6cbc9d67f9a3bf`. At that exact revision, the requested
file is `nemo_en_titanet_large.onnx`; Hugging Face returned a 101,405,493-byte
file response. The file was downloaded with `huggingface_hub==0.36.2` and
`hf-xet==1.5.0` into a temporary directory and hashed locally:

- SHA-256: `d51abcf31717ef28162f26acb9d44dd4127c3d44c9b8624f699f3425daca8e77`
- Pinned file URL: <https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/0743f301363dec56491a490f6d6cbc9d67f9a3bf/nemo_en_titanet_large.onnx>
- Repository revision API: <https://huggingface.co/api/models/csukuangfj/speaker-embedding-models/revision/0743f301363dec56491a490f6d6cbc9d67f9a3bf>
- File page: <https://huggingface.co/csukuangfj/speaker-embedding-models/blob/0743f301363dec56491a490f6d6cbc9d67f9a3bf/nemo_en_titanet_large.onnx>

The repository page currently has no model card or license declaration. The
NVIDIA source model card identifies
`nvidia/speakerverification_en_titanet_large` as CC-BY-4.0 and describes
NVIDIA TitaNet-Large. sherpa-onnx documents NeMo speaker embedding exports and
points to its speaker-verification export scripts. However, the Hugging Face
file repository does not identify the exact source checkpoint or establish
that its exported ONNX is covered by those terms. The catalog therefore records
CC-BY-4.0 as upstream information while leaving license/provenance unresolved
and retaining explicit release blockers. Do not bundle or publicly distribute
this artifact until those blockers are confirmed and attribution is prepared.

References:

- [Hugging Face repository at pinned commit](https://huggingface.co/csukuangfj/speaker-embedding-models/tree/0743f301363dec56491a490f6d6cbc9d67f9a3bf)
- [NVIDIA TitaNet-Large model card and CC-BY-4.0 terms](https://huggingface.co/nvidia/speakerverification_en_titanet_large)
- [CC-BY-4.0 license text](https://creativecommons.org/licenses/by/4.0/)
- [sherpa NeMo model documentation and export scripts](https://github.com/k2-fsa/sherpa/blob/master/docs/source/onnx/nemo/index.rst)

The same pinned ONNX bytes are catalogued for Linux x86-64 and macOS ARM64.
Linux acquisition/file-integrity was exercised. macOS has not been tested; see
the Kavin handoff in `README.md`. Neither platform has inference readiness from
this downloader milestone.

## Whisper Large-v3 source records (2026-10-14)

The `asr` role has separate catalog variants; there is no fallback between the
formats. Windows WSL2 resolves to the Linux CTranslate2 artifact and is required
to keep its store on the WSL Linux filesystem. Apple Silicon selects the full
MLX `whisper-large-v3-mlx` repository.

The Linux repository `Systran/faster-whisper-large-v3` resolves to immutable
commit `edaa852ec7e145841d8ffdb056a99866b5f0a478`. Its pinned Hub file listing
contains `.gitattributes`, `README.md`, and the five runtime/download files
below. The `faster-whisper==1.2.1` loader's model/tokenizer discovery supports
`config.json`, `model.bin`, `tokenizer.json`, and `preprocessor_config.json`;
the pinned model card specifies FP16 CTranslate2 weights (no smaller model or
reduced-bit variant). Its own download allowlist also includes `vocabulary.*`,
so SpeechCraft retains
`vocabulary.json` as part of the complete selected repository artifact.
Required total: **3,090,835,702 bytes**.

| Relative path | Bytes | SHA-256 source |
| --- | ---: | --- |
| `config.json` | 2,394 | Downloaded from the pinned revision and locally hashed |
| `model.bin` | 3,087,284,237 | Hugging Face Xet/LFS SHA-256 metadata; confirmed by real install smoke |
| `preprocessor_config.json` | 340 | Downloaded from the pinned revision and locally hashed |
| `tokenizer.json` | 2,480,617 | Downloaded from the pinned revision and locally hashed |
| `vocabulary.json` | 1,068,114 | Downloaded from the pinned revision and locally hashed |

The repository's Hub card declares MIT and the pinned snapshot is public without
authentication. It describes this as a CTranslate2 FP16 conversion of
`openai/whisper-large-v3`; preserve attribution to OpenAI and the Systran
conversion repository in the product's third-party notices. Sources:
[OpenAI model card](https://huggingface.co/openai/whisper-large-v3), [pinned repository](https://huggingface.co/Systran/faster-whisper-large-v3/tree/edaa852ec7e145841d8ffdb056a99866b5f0a478).

The MLX repository `mlx-community/whisper-large-v3-mlx` resolves to immutable
commit `49e6aa286ad60c14352c404340ded53710378a11`. Its pinned inventory is
`.gitattributes`, `README.md`, `config.json`, and `weights.npz`; `mlx-whisper`
loads the latter two from a local MLX Whisper model directory and bundles the
multilingual tokenizer assets in its runtime package. The repository is the
non-quantized `whisper-large-v3-mlx` variant (not a `-4bit`/`-8bit` repository).
Required total: **3,083,520,685 bytes**.

| Relative path | Bytes | SHA-256 source |
| --- | ---: | --- |
| `config.json` | 269 | Downloaded from the pinned revision and locally hashed |
| `weights.npz` | 3,083,520,416 | Hugging Face Xet/LFS SHA-256 metadata |

The pinned Hub card declares MIT and directs users to MLX Whisper. Preserve
attribution to OpenAI Whisper and the MLX Community conversion in product
third-party notices. The current candidate runtime package is
`mlx-whisper==0.4.3` (Python `>=3.8`); that version and file format still need
a native M4 Pro load/transcription smoke before Mac runtime support is claimed.
Sources: [pinned MLX repository](https://huggingface.co/mlx-community/whisper-large-v3-mlx/tree/49e6aa286ad60c14352c404340ded53710378a11), [mlx-whisper model loading guide](https://github.com/ml-explore/mlx-examples/tree/main/whisper), [pinned faster-whisper loader source](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/faster_whisper/utils.py).

For both repos, the large weight SHA-256 values come from Hub Xet/LFS object
metadata at the pinned commit. The catalog hashes for the small JSON files were
computed by downloading those exact immutable files and hashing the bytes
locally. The Linux real-network install smoke independently hashes the 3.09 GB
weight during acquisition. The MLX 3.08 GB weight's local transfer/hash remains
for Kavin's Mac handoff because this Linux host cannot test or smoke the Mac
download. The catalog keeps the verified Hub SHA-256 metadata for it.

Neither artifact establishes inference readiness. Linux/WSL2 runtime testing is
deferred to the setup/runtime integration milestone. The separate MLX artifact
does not provide a CPU fallback; whether to provision another large CTranslate2
artifact for Mac CPU use remains a runtime-integration decision.
