# OpenVPI audio-slicer attribution

Vendored for the Buckeye Phase-9 external baseline only. The algorithm in
`slicer2.py` is unmodified.

- Project: OpenVPI audio-slicer
- Upstream repository: https://github.com/openvpi/audio-slicer
- Upstream file: `slicer2.py`
- Revision: `9958eede8f38fb6ce26914b1673e202ecfce70f3` (2023-05-24, "Fix inconsistent shape comparison")
- License: MIT (see `LICENSE`; Copyright 2022 Team OpenVPI)
- Vendored `slicer2.py` SHA-256: `505a7fceda62275f391cea11f3bd5ad3d4b54b460ff4567a7ca8747daeaf1957`

Do not silently edit `slicer2.py`. Adapter-side timestamp recovery and
buffer mapping live in `adapter/`, not in this directory.
