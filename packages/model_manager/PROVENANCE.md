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
