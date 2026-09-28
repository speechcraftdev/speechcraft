"""TTS-harmful transcript comparison for binary QC evaluation.

Only punctuation and capitalization are TTS-neutral.
All other differences use the harmful mismatch rules below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

DiffKind = Literal["harmful", "neutral", "debatable"]


@dataclass(frozen=True)
class TtsDiff:
    kind: DiffKind
    category: str
    detail: str


@dataclass
class TtsHarmfulCompareResult:
    """Result of comparing reference text (ground truth) to hypothesis (e.g. Whisper)."""

    harmful_mismatch: bool
    diffs: list[TtsDiff] = field(default_factory=list)
    reference_words: list[str] = field(default_factory=list)
    hypothesis_words: list[str] = field(default_factory=list)

    @property
    def match(self) -> bool:
        return not self.harmful_mismatch


def _strip_punctuation(word: str) -> str:
    return re.sub(r"^[^a-z0-9']+|[^a-z0-9']+$", "", word.lower())


def normalize_tts_words(text: str) -> list[str]:
    """Lowercase, strip punctuation; no other neutralization."""
    text = text.replace("\n", " ").strip().lower()
    text = re.sub(r"[^\w\s'-]", " ", text)
    words: list[str] = []
    for raw in text.split():
        clean = _strip_punctuation(raw)
        if clean:
            words.append(clean)
    return words


def _classify_word_diff(reference_word: str, hypothesis_word: str) -> TtsDiff:
    if reference_word == hypothesis_word:
        return TtsDiff(kind="neutral", category="identical", detail=reference_word)
    return TtsDiff(
        kind="harmful",
        category="content_word",
        detail=f"{reference_word!r} vs {hypothesis_word!r}",
    )


def compare_tts_harmful(
    reference_text: str,
    hypothesis_text: str,
    *,
    strict_fillers: bool = False,
) -> TtsHarmfulCompareResult:
    """Return True when hypothesis differs from reference in a TTS-harmful way."""
    del strict_fillers  # retained for call-site compatibility; no debatable fillers anymore
    reference_words = normalize_tts_words(reference_text)
    hypothesis_words = normalize_tts_words(hypothesis_text)

    if reference_words == hypothesis_words:
        return TtsHarmfulCompareResult(
            harmful_mismatch=False,
            reference_words=reference_words,
            hypothesis_words=hypothesis_words,
        )

    if sorted(reference_words) == sorted(hypothesis_words):
        return TtsHarmfulCompareResult(
            harmful_mismatch=True,
            diffs=[
                TtsDiff(
                    kind="harmful",
                    category="word_order",
                    detail=f"{' '.join(reference_words)!r} vs {' '.join(hypothesis_words)!r}",
                )
            ],
            reference_words=reference_words,
            hypothesis_words=hypothesis_words,
        )

    diffs: list[TtsDiff] = []
    matcher = SequenceMatcher(None, reference_words, hypothesis_words)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            for word in hypothesis_words[j1:j2]:
                diffs.append(TtsDiff(kind="harmful", category="extra_word", detail=word))
        elif tag == "delete":
            for word in reference_words[i1:i2]:
                diffs.append(TtsDiff(kind="harmful", category="missing_word", detail=word))
        elif tag == "replace":
            ref_slice = reference_words[i1:i2]
            hyp_slice = hypothesis_words[j1:j2]
            for ref_word, hyp_word in zip(ref_slice, hyp_slice):
                diffs.append(_classify_word_diff(ref_word, hyp_word))
            if len(ref_slice) != len(hyp_slice):
                longer, shorter, category = (
                    (ref_slice, hyp_slice, "missing_word")
                    if len(ref_slice) > len(hyp_slice)
                    else (hyp_slice, ref_slice, "extra_word")
                )
                for word in longer[len(shorter) :]:
                    diffs.append(TtsDiff(kind="harmful", category=category, detail=word))

    harmful = [d for d in diffs if d.kind == "harmful"]

    return TtsHarmfulCompareResult(
        harmful_mismatch=bool(harmful),
        diffs=diffs,
        reference_words=reference_words,
        hypothesis_words=hypothesis_words,
    )
