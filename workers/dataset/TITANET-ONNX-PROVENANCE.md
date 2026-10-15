# TitaNet-Large ONNX provenance review

**Disposition: technically tested, still blocked for production model distribution/use approval.** This review does not change the selected checkpoint or the catalog's experimental blocker.

## What is established

- SpeechCraft's selected bytes are `csukuangfj/speaker-embedding-models/nemo_en_titanet_large.onnx` at immutable repository commit `0743f301363dec56491a490f6d6cbc9d67f9a3bf`, exactly 101,405,493 bytes, SHA-256 `d51abcf31717ef28162f26acb9d44dd4127c3d44c9b8624f699f3425daca8e77`. The pinned repository commit's LFS pointer records the same hash and size.
- NVIDIA's canonical `nvidia/speakerverification_en_titanet_large` model card identifies TitaNet-Large and declares CC-BY-4.0 for that upstream model. NVIDIA's NGC listing separately says its model use is covered by the NeMo toolkit license. Resolve that source-of-truth relationship with NVIDIA/release counsel before using it as blanket permission for a converted file.
- The selected `csukuangfj` repository lists the ONNX file but does not provide a repository-level model card, license file or conversion record linking the exact ONNX bytes to a specific upstream `.nemo` checkpoint and exporter/toolchain revision. A filename and upstream hash are not proof that the conversion and distribution have the same permissions.
- A different community ONNX mirror documents its own CC-BY attribution and claims an export from NVIDIA's checkpoint; it is not the selected artifact and has not been substituted.
- SpeechCraft exercised the selected local ONNX bytes through sherpa-onnx CPU: 192-dimensional finite embeddings, L2-normalized outputs and repeat cosine similarity 1.0 on the CC0 speech fixture. This is a technical result only.

## Required before production enablement

1. Obtain an author/releaser statement tying this exact LFS SHA-256 and repository revision to the canonical NVIDIA checkpoint, including the checkpoint revision/hash and exporter/toolchain commit/configuration.
2. Confirm that conversion/derivative use, redistribution, product use and attribution are covered by the applicable upstream license, and capture required notices/citation.
3. Have the product release/legal owner approve those terms and document whether SpeechCraft may fetch this artifact directly and redistribute or provide it to users.
4. Keep the model-manager role blocked and do not set the experimental flag automatically until that approval is recorded.

Sources checked 2026-10-15:

- [NVIDIA TitaNet-Large model card and license](https://huggingface.co/nvidia/speakerverification_en_titanet_large)
- [NVIDIA NGC TitaNet-L model entry](https://catalog.ngc.nvidia.com/orgs/nvidia/nemo/models/titanet_large/)
- [Pinned selected ONNX repository](https://huggingface.co/csukuangfj/speaker-embedding-models/tree/0743f301363dec56491a490f6d6cbc9d67f9a3bf)
- [Pinned commit showing the LFS pointer change](https://huggingface.co/csukuangfj/speaker-embedding-models/commit/0743f301363dec56491a490f6d6cbc9d67f9a3bf)
- [Separate Recogment ONNX mirror, not used by SpeechCraft](https://huggingface.co/Recogment/titanet-large-onnx)
