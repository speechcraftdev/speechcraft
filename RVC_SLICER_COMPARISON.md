# RVC slicer vs OpenVPI audio-slicer

```text
RVC slicer inspected
algorithmically equivalent / derived from OpenVPI baseline
not counted as an independent competitor
```

## Files inspected

- Official RVC-WebUI `train/dataset/slicer2.py`
  (imported from `train/preprocess.py` as `from train.dataset.slicer2 import Slicer`)
  https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI
- Official RVC-WebUI `train/preprocess.py` wrapper (not a second slicer)
- Mangio-RVC-Fork `slicer2.py` (common RVC-ecosystem copy)

OpenVPI reference: vendored `vendor/openvpi_audio_slicer/slicer2.py` at
`9958eede8f38fb6ce26914b1673e202ecfce70f3`.

## Algorithm

RVC / Mangio `slicer2.py` is the OpenVPI RMS-silence slicer: `get_rms` plus
the same `sil_tags` / min-RMS cut logic. It is an RVC / slicer2 ecosystem
implementation, derived from or closely based on OpenVPI audio-slicer.

## Differences

1. Early-return condition. OpenVPI commit `9958eede` compares RMS *frame*
   count to `min_length`. RVC/Mangio still compare raw sample count to
   `min_length` (already stored in hop units). That only matters for very
   short waveforms. Buckeye allowed buffers are seconds long, so it is not
   a material competitor difference.
2. RVC *preprocess pipeline* (`train/preprocess.py`) is wrapper policy, not
   a different slicer: high-pass filter, Slicer kwargs
   `threshold=-42, min_length=1500, min_interval=400, hop_size=15,
   max_sil_kept=500`, then greedy 3.7 s chunks with 0.3 s overlap.

## Decision

Attribution-only. No separate benchmark row. Running it would duplicate
OpenVPI and inflate the competitor count.
