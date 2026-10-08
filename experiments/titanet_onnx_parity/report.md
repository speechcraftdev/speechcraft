# TitaNet-Large backend comparison

**Scope:** replacement-model comparison using the frozen 24-source pyannote-SP archive. Both branches share the production guillotine, centroid, silence handling, windowing, aggregation and threshold code via `rescore.py`. No production code was changed.

**Reused source:** `/home/aaravthegreat/Projects/speaker_ts_eval/runs/2026-09-09_pyannote_sp_incremental` (798 exact clip WAVs, 24 source IDs, gold labels, 480 ordered enrollment chunks). Origin scripts and artifact provenance are documented in `README.md`; audio and embedding arrays stay outside git.

**Metric direction:** contamination is positive; ROC-AUC and Average Precision use negative raw SP scores, so lower purity means higher contamination risk. Average Precision is reported as AP, not literal PR-curve area.

## Main metrics

| Metric | NeMo | sherpa-onnx | Difference (candidate − baseline) |
|---|---:|---:|---:|
| successfully_scored | 798 | 798 | 0 |
| expected | 798 | 798 | 0 |
| contamination_prevalence | 0.07017543859649122 | 0.07017543859649122 | 0.0 |
| any_overlap_roc_auc | 0.7751010781671159 | 0.7480626684636119 | -0.02703840970350402 |
| any_overlap_average_precision | 0.23335864228591804 | 0.2263716780239788 | -0.0069869642619392325 |
| fpr_at_50pct_recall | 0.1347708894878706 | 0.12668463611859837 | -0.00808625336927224 |
| fpr_at_80pct_recall | 0.4299191374663073 | 0.4636118598382749 | 0.03369272237196763 |
| sp70_flagged_clips | 265 | 296 | 31 |
| roc_auc_gt_0.5s | 0.8165388381201044 | 0.7807196475195822 | -0.03581919060052219 |
| average_precision_gt_0.5s | 0.21197398408025464 | 0.20583063681579364 | -0.006143347264461002 |
| positive_count_gt_0.5s | 32 | 32 | 0 |
| roc_auc_gt_1s | 0.795758354755784 | 0.7518637532133676 | -0.04389460154241642 |
| average_precision_gt_1s | 0.18831811006481966 | 0.2077441501394254 | 0.019426040074605744 |
| positive_count_gt_1s | 20 | 20 | 0 |
| roc_auc_gt_2s | 0.8498417721518987 | 0.8281645569620253 | -0.021677215189873422 |
| average_precision_gt_2s | 0.19459079204085572 | 0.24047375103702143 | 0.04588295899616571 |
| positive_count_gt_2s | 8 | 8 | 0 |

## Paired scores and decisions

- Paired scored clips: 798; exact clip IDs and gold labels checked.
- Pearson: 0.9668150536060534; Spearman/rank agreement: 0.9552752162846838.
- Median / 95th percentile absolute raw score difference: 0.015127500000000016 / 0.05079715000000008.
- Legacy SP@70 decision agreement: 0.9010025062656641; changed: 79 (9.899749373433583%).
- Contaminated flagged→unflagged: 1; clean unflagged→flagged: 52.

## Paired source bootstrap

Intervals are differences (candidate minus NeMo), with each resample drawing source recordings and all their clips. One-class replicates are skipped and counted.
- any_overlap: ROC-AUC Δ -0.027 [-0.057, -0.005]; Average Precision Δ -0.007 [-0.033, 0.022]; valid/skipped 1000/0.
- gt_0.5s: ROC-AUC Δ -0.036 [-0.063, -0.009]; Average Precision Δ -0.006 [-0.047, 0.038]; valid/skipped 1000/0.
- gt_1.0s: ROC-AUC Δ -0.044 [-0.073, -0.005]; Average Precision Δ 0.019 [-0.027, 0.092]; valid/skipped 1000/0.
- gt_2.0s: ROC-AUC Δ -0.022 [-0.079, 0.013]; Average Precision Δ 0.046 [-0.016, 0.208]; valid/skipped 997/3.

## Embeddings and enrollment

- Corpus embedding validity: 6623 per backend, 192-D; dimension mismatches NeMo/sherpa 0/0; non-finite 0/0; zero vectors 0/0.
- Raw norm medians NeMo/sherpa 0.26722551696101765 / 0.2696448854068502; normalized norm medians 1.0 / 1.0; same-audio cross-backend cosine median/p05/p95 0.9316608313684682 / 0.8975451619407872 / 0.955626854240762.
- Same-audio pairwise-matrix Pearson/Spearman: 0.9880616767425101 / 0.949611595324148; cosine matrix MAE 0.027513629905495836 across 160 items.
- Target/wrong-speaker enrollment pair medians NeMo: 0.6571793611009749 / 0.5653698918827674; sherpa: 0.6462440902626143 / 0.5476604782549654.
- Edge probes: 42/42 and 42/42 valid; input hashes matched; exact repeated output 42/42 NeMo and 42/42 sherpa.
- Enrollment exact guillotine-set agreement: 9/24 sources; differing kept/rejected memberships: 38 chunks (19 retained only by each backend); median target-centroid cosine 0.941035504767239.
- Across 17 sources with both gold classes, median clean-minus-contaminated score separation: NeMo 0.08637649999999997; sherpa 0.08093550000000005. Per-source values are in enrollment_diagnostics.csv.
- Gold-labeled wrong-speaker enrollment chunks kept: NeMo 22, sherpa 20.

## Model identity, runtime and packaging

- ONNX SHA256: `d51abcf31717ef28162f26acb9d44dd4127c3d44c9b8624f699f3425daca8e77`; ONNX weight bytes: 101405493.
- NeMo cached checkpoint SHA256: `e838520693f269e7984f55bc8eb3c2d60ccf246bf4b896d4be9bcabe3e4b0fe3`; checkpoint bytes: 101621760.
- Direct state/initializer fingerprints found 25 exact same-name, same-shape, byte-identical tensors (the ONNX contains 64 initializers; the NeMo checkpoint has 124 state tensors). The ONNX metadata identifies the NeMo TitaNet-Large NGC source. This is strong evidence of a shared source checkpoint, but the upstream repository has no revision-specific model card; unmatched tensors remain unverified. The strict result is therefore a **replacement-model comparison**, not complete checkpoint identity proof.
- NeMo front end config: 16 kHz, 80 mel features, Hann window, 25 ms window, 10 ms stride, per-feature normalization, dither 1e-5 in the archived config. sherpa-onnx model metadata: 16 kHz, 80 features, Hann window, 25 ms/10 ms and per-feature normalization; candidate metadata does not disclose dither or all frontend details. Both receive identical decoded mono float32 sample arrays; no resampling or amplitude normalization is added.
- Load/extraction/shared-score seconds: NeMo 74.48/1953.66/23.13, sherpa 2.24/2584.49/90.17. Both process 19,869 audio seconds; throughput is 10.17x versus 7.69x real time. Peak RSS is 2230 MiB versus 709 MiB. NeMo loaded from the warm filesystem cache in a new process; sherpa likewise. Repeated single-chunk CPU inference was bit-exact in both backends.
- CPU-only execution was validated on Linux x86-64. PyPI metadata shows CPython 3.12 Linux x86-64 and macOS arm64 wheels for sherpa-onnx 1.13.8; macOS execution was not tested. The sherpa-onnx API/core installed files total about 44.6 MB, and the isolated analysis environment is 298.0 MB. The existing worker site-packages are 8.5 GB but include Torch, pyannote, CUDA and other packages, so that total cannot be attributed to NeMo.
- Warm repeat probe on one identical enrollment chunk: model-load NeMo/sherpa 6.90/0.24 s; repeat calls NeMo [0.05664228599925991, 0.03186524299962912] s and sherpa [0.04223814399665571, 0.05150264199983212] s; both outputs were bit-exact.
- Removing NeMo could simplify that part of the dependency set, but it would not remove Torch or independently make the current worker cross-platform because pyannote remains and the worker environment is CUDA/Linux oriented.

## Limitations

- Any-overlap prevalence is low (7.0%); the >2 s leak group has only 8 clips. Its point estimates and confidence intervals are uncertain.
- No independent noise-condition labels are present in the reused archive, so the edge suite has no curated noisy-speech class.
- ONNX and NeMo front-end metadata agree on sample rate, mel size, window and normalization, but exact numerical feature preprocessing equivalence was not established. The export identity is strongly supported by matching tensors but not fully proven.
- The fixed diagnostic pairwise matrix uses 160 deterministic corpus embeddings; all 6,623 embeddings are covered by same-audio norm/cosine and validity summaries.
- No threshold was tuned. SP@70 is evaluated as-is.

## Recommendation

**C. KEEP NEMO FOR NOW.** The candidate is broadly rank-correlated, but any-overlap ROC-AUC difference was -0.027 with paired source-bootstrap 95% CI [-0.057, -0.005], and the >0.5 s and >1 s ROC-AUC differences were also negative with intervals below zero. SP@70 agreement was 90.1%; 79 clips changed, including 52 clean clips newly flagged and one contaminated clip newly unflagged. This is a measurable loss in discrimination and operational agreement on the frozen data. The >2 s result is inconclusive at n=8. Do not change the threshold to compensate.
The ONNX export can be evaluated further because it had full coverage, stable CPU outputs, a smaller runtime RSS footprint, and published Linux/macOS arm64 wheels. Keep NeMo until the score shifts are explained and the export/checkpoint provenance and product-level impact receive more validation.
