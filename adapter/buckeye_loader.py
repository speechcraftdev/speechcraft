"""Load one frozen Buckeye recording into Phase-1 RecordingReference + audio.

Uses canonical normalized phones/uncertainty and raw NeMo target regions as
allowed scopes. Does not rebuild annotations, rerun NeMo, or copy audio.
"""

from __future__ import annotations

import csv
import hashlib
import wave
from dataclasses import dataclass
from pathlib import Path

from adapter.canonical_executor import _ensure_speaker_ts_eval_on_path
from referee.intervals import intersect_interval, subtract_union
from referee.types import BufferScope, PhoneInterval, RecordingReference, UncertaintyInterval

# First accepted recording with a target NeMo mapping from speakers s01–s04.
# These IDs come from the historical vad_geometry experiment coverage table
# (nonzero A clips). Not chosen from Phase-4 results.
VALIDATION_SUBSET: tuple[tuple[str, str], ...] = (
    ("s01", "s0101b"),
    ("s02", "s0201b"),
    ("s03", "s0301a"),
    ("s04", "s0401b"),
)

SUBSET_SELECTION_RULE = (
    "First accepted recording with a target NeMo cluster mapping from each of "
    "speakers s01, s02, s03, s04 in the historical vad_geometry experiment."
)

FULL_COHORT_SELECTION_RULE = (
    "Every recording from speakers with accepted_recording_count > 0 in "
    "canonical/acoustic_matrix/cohort/speaker_cohort_summary.csv that is in "
    "the frozen reconciliation, not excluded, and has at least one raw NeMo "
    "target buffer. Review-required and excluded recordings are not added."
)


def _lab_root() -> Path:
    return Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BuckeyePaths:
    normalized_root: Path
    cohort_root: Path
    cluster_mapping_csv: Path

    @staticmethod
    def canonical(lab_root: Path | None = None) -> BuckeyePaths:
        root = lab_root if lab_root is not None else _lab_root()
        cohort = root / "canonical" / "acoustic_matrix" / "cohort"
        return BuckeyePaths(
            normalized_root=root / "canonical" / "normalized" / "corpus",
            cohort_root=cohort,
            cluster_mapping_csv=cohort / "benchmark_cluster_mapping.csv",
        )


@dataclass(frozen=True)
class LoadedRecording:
    speaker_id: str
    recording_id: str
    audio_path: Path
    sample_rate_hz: int
    reference: RecordingReference

    @property
    def buffers(self) -> tuple[BufferScope, ...]:
        return self.reference.buffers


def validation_subset() -> tuple[tuple[str, str], ...]:
    return VALIDATION_SUBSET


@dataclass(frozen=True)
class FullCohort:
    recordings: tuple[tuple[str, str], ...]
    speaker_ids: tuple[str, ...]
    speaker_count: int
    recording_count: int
    allowed_buffer_duration_sec: float
    fingerprint: str
    paths: BuckeyePaths
    speaker_cohort_summary_csv: Path
    selection_rule: str = FULL_COHORT_SELECTION_RULE

    def identity_lines(self) -> tuple[str, ...]:
        return tuple(f"{speaker_id}/{recording_id}" for speaker_id, recording_id in self.recordings)


# Frozen accepted-cohort identity from the corrected Phase-5 run.
PHASE5_COHORT_FINGERPRINT = "f3177a2fd9b69434a0cc91856bd13da45d9b0a1f4a8a6b240b7fa16e213644dd"
HISTORICAL_DIVERGENT_RECORDING = ("s03", "s0301a")


def cohort_identity_fingerprint(recordings: tuple[tuple[str, str], ...]) -> str:
    """SHA-256 of ordered speaker_id/recording_id lines. Audit-only."""
    blob = "\n".join(f"{speaker_id}/{recording_id}" for speaker_id, recording_id in recordings)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def default_spot_check_ids(
    recordings: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """First, middle, last accepted recording, plus historical s0301a if present."""
    if not recordings:
        raise RuntimeError("cannot choose spot-check recordings from an empty cohort")
    chosen: list[tuple[str, str]] = [
        recordings[0],
        recordings[len(recordings) // 2],
        recordings[-1],
    ]
    if HISTORICAL_DIVERGENT_RECORDING in recordings:
        chosen.append(HISTORICAL_DIVERGENT_RECORDING)
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for item in chosen:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return tuple(out)


def derive_full_cohort(paths: BuckeyePaths | None = None) -> FullCohort:
    """Deterministic accepted Buckeye cohort from frozen canonical artifacts.

    Does not rerun NeMo, change mappings, or repair annotations.
    """
    active = paths if paths is not None else BuckeyePaths.canonical()
    summary_csv = active.cohort_root / "speaker_cohort_summary.csv"
    summary_rows = [
        row
        for row in _read_csv(summary_csv)
        if int(row["accepted_recording_count"]) > 0
    ]
    summary_rows.sort(key=lambda row: str(row["speaker_id"]))
    if not summary_rows:
        raise RuntimeError(f"no accepted speakers in {summary_csv}")

    _ensure_speaker_ts_eval_on_path()
    from speaker_ts_eval.repaired_buckeye_benchmark import (  # type: ignore[import-not-found]
        build_raw_nemo_target_buffers,
        load_nemo_cluster_mapping,
        load_recording_eligibility,
        require_parser_artifacts,
    )

    recordings: list[tuple[str, str]] = []
    buffer_duration = 0.0
    derived_counts: dict[str, int] = {}
    for row in summary_rows:
        speaker_id = str(row["speaker_id"])
        speaker_dir = active.normalized_root / speaker_id
        if not speaker_dir.is_dir():
            raise FileNotFoundError(
                f"normalized speaker directory missing for {speaker_id}: {speaker_dir}"
            )
        eval_run = _eval_run(active, speaker_id)
        if not eval_run.is_dir():
            raise FileNotFoundError(f"NeMo eval_run missing for {speaker_id}: {eval_run}")
        artifacts = require_parser_artifacts(speaker_dir)
        included, excluded = load_recording_eligibility(
            reference_reconciliation_csv=artifacts["reference_reconciliation"],
            excluded_recordings_csv=artifacts["excluded_recordings"],
        )
        mapping = load_nemo_cluster_mapping(active.cluster_mapping_csv, speaker_id=speaker_id)
        nemo_buffers = build_raw_nemo_target_buffers(
            speaker_id=speaker_id,
            eval_run=eval_run,
            cluster_mapping=mapping,
            included_recording_ids=included,
            excluded_recording_ids=excluded,
        )
        rec_ids = sorted({str(buf.recording_id) for buf in nemo_buffers})
        if not rec_ids:
            raise RuntimeError(
                f"accepted speaker {speaker_id} has no raw NeMo target-buffer recordings"
            )
        derived_counts[speaker_id] = len(rec_ids)
        expected = int(row["accepted_recording_count"])
        if len(rec_ids) != expected:
            raise RuntimeError(
                f"derived recording count for {speaker_id} is {len(rec_ids)}, "
                f"speaker_cohort_summary.csv says {expected}"
            )
        for recording_id in rec_ids:
            rec_buffers = [buf for buf in nemo_buffers if str(buf.recording_id) == recording_id]
            buffer_duration += sum(float(buf.duration_sec) for buf in rec_buffers)
            recordings.append((speaker_id, recording_id))

    recordings_t = tuple(sorted(recordings, key=lambda item: (item[0], item[1])))
    speaker_ids = tuple(str(row["speaker_id"]) for row in summary_rows)
    summary_counts = {
        str(row["speaker_id"]): int(row["accepted_recording_count"]) for row in summary_rows
    }
    if derived_counts != summary_counts:
        raise RuntimeError(
            "derived per-speaker cohort counts drifted from speaker_cohort_summary.csv: "
            f"derived={derived_counts!r} summary={summary_counts!r}"
        )
    return FullCohort(
        recordings=recordings_t,
        speaker_ids=speaker_ids,
        speaker_count=len(speaker_ids),
        recording_count=len(recordings_t),
        allowed_buffer_duration_sec=buffer_duration,
        fingerprint=cohort_identity_fingerprint(recordings_t),
        paths=active,
        speaker_cohort_summary_csv=summary_csv,
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"required canonical artifact missing: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _eval_run(paths: BuckeyePaths, speaker_id: str) -> Path:
    return paths.cohort_root / "eval_runs" / speaker_id


def _require_accepted(paths: BuckeyePaths, speaker_id: str, recording_id: str) -> None:
    speaker_dir = paths.normalized_root / speaker_id
    if not speaker_dir.is_dir():
        raise FileNotFoundError(
            f"normalized speaker directory missing for {speaker_id}: {speaker_dir}"
        )
    _ensure_speaker_ts_eval_on_path()
    from speaker_ts_eval.repaired_buckeye_benchmark import (  # type: ignore[import-not-found]
        load_recording_eligibility,
        require_parser_artifacts,
    )

    artifacts = require_parser_artifacts(speaker_dir)
    included, excluded = load_recording_eligibility(
        reference_reconciliation_csv=artifacts["reference_reconciliation"],
        excluded_recordings_csv=artifacts["excluded_recordings"],
    )
    if recording_id in excluded:
        raise RuntimeError(
            f"recording {recording_id} is excluded from the frozen accepted set"
        )
    if recording_id not in included:
        raise RuntimeError(
            f"recording {recording_id} is not in the frozen accepted reconciliation "
            f"for speaker {speaker_id}"
        )


def _interviewer_mask(rows_words: list[dict[str, str]], rows_phones: list[dict[str, str]]) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for row in rows_words:
        event_type = str(row.get("event_type") or "")
        raw_label = str(row.get("raw_label") or "")
        if event_type == "interviewer" or raw_label.strip().upper().startswith("<IVER"):
            intervals.append((float(row["start_sec"]), float(row["end_sec"])))
    for row in rows_phones:
        if str(row.get("phone_class") or "") == "interviewer" or str(row.get("raw_label") or "").strip().upper() == "IVER":
            intervals.append((float(row["start_sec"]), float(row["end_sec"])))
    return intervals


def _clip_to_buffers(
    *,
    recording_id: str,
    start_sec: float,
    end_sec: float,
    buffers: tuple[BufferScope, ...],
) -> list[tuple[str, float, float]]:
    pieces: list[tuple[str, float, float]] = []
    for buf in buffers:
        hit = intersect_interval((start_sec, end_sec), (buf.start_sec, buf.end_sec))
        if hit is None:
            continue
        pieces.append((buf.buffer_id, hit[0], hit[1]))
    return pieces


def load_recording(
    speaker_id: str,
    recording_id: str,
    *,
    paths: BuckeyePaths | None = None,
) -> LoadedRecording:
    """Load frozen scopes + evaluator annotations for one recording.

    Raises if the recording/reference/audio/buffers are missing. Never returns
    a silently empty stand-in.
    """
    active = paths if paths is not None else BuckeyePaths.canonical()
    _require_accepted(active, speaker_id, recording_id)

    speaker_dir = active.normalized_root / speaker_id
    eval_run = _eval_run(active, speaker_id)
    if not eval_run.is_dir():
        raise FileNotFoundError(f"NeMo eval_run missing for {speaker_id}: {eval_run}")

    _ensure_speaker_ts_eval_on_path()
    from speaker_ts_eval.repaired_buckeye_benchmark import (  # type: ignore[import-not-found]
        build_raw_nemo_target_buffers,
        load_nemo_cluster_mapping,
        load_recording_eligibility,
        require_parser_artifacts,
    )

    artifacts = require_parser_artifacts(speaker_dir)
    included, excluded = load_recording_eligibility(
        reference_reconciliation_csv=artifacts["reference_reconciliation"],
        excluded_recordings_csv=artifacts["excluded_recordings"],
    )
    mapping = load_nemo_cluster_mapping(active.cluster_mapping_csv, speaker_id=speaker_id)
    nemo_buffers = build_raw_nemo_target_buffers(
        speaker_id=speaker_id,
        eval_run=eval_run,
        cluster_mapping=mapping,
        included_recording_ids=included,
        excluded_recording_ids=excluded,
    )
    rec_buffers = [buf for buf in nemo_buffers if buf.recording_id == recording_id]
    if not rec_buffers:
        raise RuntimeError(
            f"no frozen raw NeMo target buffers for {speaker_id}/{recording_id}"
        )

    wav_paths = {Path(buf.wav_path) for buf in rec_buffers}
    if len(wav_paths) != 1:
        raise RuntimeError(
            f"recording {recording_id} maps to multiple wav paths: {sorted(str(p) for p in wav_paths)}"
        )
    audio_path = wav_paths.pop()
    if not audio_path.is_file():
        raise FileNotFoundError(f"canonical audio missing for {recording_id}: {audio_path}")
    with wave.open(str(audio_path), "rb") as reader:
        sample_rate_hz = int(reader.getframerate())
        n_channels = int(reader.getnchannels())
    if sample_rate_hz != 16000:
        raise RuntimeError(
            f"audio sample rate {sample_rate_hz} != 16000 for {recording_id}"
        )
    if n_channels != 1:
        raise RuntimeError(f"audio is not mono for {recording_id}: channels={n_channels}")

    buffers = tuple(
        BufferScope(buffer_id=buf.buffer_id, start_sec=float(buf.buffer_start_sec), end_sec=float(buf.buffer_end_sec))
        for buf in rec_buffers
    )

    phone_rows = [
        row
        for row in _read_csv(speaker_dir / "reference_phones.csv")
        if str(row["recording_id"]) == recording_id
    ]
    word_rows = [
        row
        for row in _read_csv(speaker_dir / "reference_words.csv")
        if str(row["recording_id"]) == recording_id
    ]
    if not phone_rows:
        raise RuntimeError(f"no reference phones for {recording_id}")

    interviewer = _interviewer_mask(word_rows, phone_rows)
    phones: list[PhoneInterval] = []
    for row in phone_rows:
        if not _as_bool(row.get("is_speech_phone")):
            continue
        start_sec = float(row["start_sec"])
        end_sec = float(row["end_sec"])
        label = str(row.get("normalized_label") or row.get("raw_label") or "")
        target_pieces = subtract_union([(start_sec, end_sec)], interviewer)
        for piece_start, piece_end in target_pieces:
            for buffer_id, clipped_start, clipped_end in _clip_to_buffers(
                recording_id=recording_id,
                start_sec=piece_start,
                end_sec=piece_end,
                buffers=buffers,
            ):
                phones.append(
                    PhoneInterval(
                        recording_id=recording_id,
                        buffer_id=buffer_id,
                        start_sec=clipped_start,
                        end_sec=clipped_end,
                        phone=label,
                    )
                )

    uncertainty_rows = _read_csv(speaker_dir / "uncertainty_masks.csv")
    uncertainty: list[UncertaintyInterval] = []
    for row in uncertainty_rows:
        if str(row["recording_id"]) != recording_id:
            continue
        start_sec = float(row["start_sec"])
        end_sec = float(row["end_sec"])
        for buffer_id, clipped_start, clipped_end in _clip_to_buffers(
            recording_id=recording_id,
            start_sec=start_sec,
            end_sec=end_sec,
            buffers=buffers,
        ):
            uncertainty.append(
                UncertaintyInterval(
                    recording_id=recording_id,
                    buffer_id=buffer_id,
                    start_sec=clipped_start,
                    end_sec=clipped_end,
                )
            )

    reference = RecordingReference(
        recording_id=recording_id,
        buffers=buffers,
        phones=tuple(phones),
        uncertainty_intervals=tuple(uncertainty),
    )
    return LoadedRecording(
        speaker_id=speaker_id,
        recording_id=recording_id,
        audio_path=audio_path,
        sample_rate_hz=sample_rate_hz,
        reference=reference,
    )
