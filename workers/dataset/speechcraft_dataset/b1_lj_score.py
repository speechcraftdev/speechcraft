"""Frozen B1-LJ transcript-confidence score.

Pure scoring helpers only. No model, audio, manifest, or filesystem dependencies.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Sequence


B1_LJ_FEATURES = ("m1", "m2")
B1_LJ_SCALER_MEAN = (75.864291, 91.250489)
B1_LJ_SCALER_SCALE = (18.577465, 10.974781)
B1_LJ_COEF_M1 = -0.482025
B1_LJ_COEF_M2 = -0.422582
B1_LJ_INTERCEPT = -3.165108

_LEADING_TRAILING_PUNCT_RE = re.compile(r"^[^\w']+|[^\w']+$", re.UNICODE)


@dataclass(frozen=True)
class B1LjResult:
    m1: float
    m2: float
    gap: float
    harmful_probability: float
    transcript_match_score: float


@dataclass(frozen=True)
class LexicalWord:
    raw_text: str
    normalized_text: str
    probability_0_100: float


def _reject_non_finite(value: float, *, label: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite; got {value!r}")
    return value


def detect_probability_scale(values: Sequence[float]) -> str:
    """Return ``unit`` (0..1) or ``percent`` (0..100). Reject mixed/ambiguous sets."""
    if not values:
        raise ValueError("probability list is empty")
    unit_like = 0
    percent_only = 0
    for index, raw in enumerate(values):
        value = _reject_non_finite(float(raw), label=f"probability[{index}]")
        if value < 0.0 or value > 100.0:
            raise ValueError(f"probability[{index}] out of range [0, 100]: {value}")
        if value <= 1.0:
            unit_like += 1
        else:
            percent_only += 1
    if percent_only and unit_like:
        raise ValueError(
            "mixed probability scales detected: values <= 1.0 and values > 1.0 cannot be combined"
        )
    if percent_only:
        return "percent"
    # All values are in [0, 1]. Treat as unit probabilities.
    return "unit"


def normalize_probabilities_0_100(values: Iterable[float]) -> list[float]:
    """Normalize supported probability ranges to a single 0..100 scale."""
    materialized = [float(value) for value in values]
    scale = detect_probability_scale(materialized)
    out: list[float] = []
    for index, raw in enumerate(materialized):
        value = _reject_non_finite(float(raw), label=f"probability[{index}]")
        if scale == "unit":
            if value < 0.0 or value > 1.0:
                raise ValueError(f"probability[{index}] out of unit range [0, 1]: {value}")
            out.append(value * 100.0)
        else:
            if value < 0.0 or value > 100.0:
                raise ValueError(f"probability[{index}] out of percent range [0, 100]: {value}")
            out.append(value)
    return out


def normalize_lexical_token(raw_text: str) -> str | None:
    """Return normalized lexical text, or None for punctuation-only / empty tokens."""
    text = str(raw_text or "").strip()
    if not text:
        return None
    if not any(character.isalnum() for character in text):
        return None
    normalized = _LEADING_TRAILING_PUNCT_RE.sub("", text)
    normalized = normalized.strip()
    if not normalized or not any(character.isalnum() for character in normalized):
        return None
    return normalized


def extract_lexical_words(words: Iterable[dict]) -> list[LexicalWord]:
    """Extract lexical words and normalize their probabilities to 0..100."""
    candidates: list[tuple[str, str, float]] = []
    raw_probs: list[float] = []
    for index, word in enumerate(words):
        if not isinstance(word, dict):
            raise ValueError(f"word[{index}] must be a dict")
        raw_text = str(word.get("word") or word.get("text") or "").strip()
        normalized = normalize_lexical_token(raw_text)
        if normalized is None:
            continue
        probability = word.get("probability", word.get("confidence"))
        if probability is None:
            raise ValueError(f"lexical word[{index}] ({raw_text!r}) is missing probability")
        value = float(probability)
        candidates.append((raw_text, normalized, value))
        raw_probs.append(value)

    if not candidates:
        return []

    normalized_probs = normalize_probabilities_0_100(raw_probs)
    return [
        LexicalWord(raw_text=raw_text, normalized_text=normalized, probability_0_100=prob)
        for (raw_text, normalized, _), prob in zip(candidates, normalized_probs)
    ]


def m1_m2_gap_from_probs(word_probs_0_100: Iterable[float]) -> tuple[float, float, float]:
    """Compute m1/m2/gap from already-normalized 0..100 lexical probabilities.

    Empty input is rejected. Callers that need unscored empty-lexical handling must
    decide that at the pipeline boundary instead of inventing a perfect score.
    """
    sorted_probs = sorted(_reject_non_finite(float(x), label="probability") for x in word_probs_0_100)
    if not sorted_probs:
        raise ValueError("cannot compute B1-LJ features from an empty lexical probability list")
    for index, value in enumerate(sorted_probs):
        if value < 0.0 or value > 100.0:
            raise ValueError(f"probability[{index}] out of range [0, 100]: {value}")
    m1 = sorted_probs[0]
    m2 = sorted_probs[1] if len(sorted_probs) > 1 else sorted_probs[0]
    return round(m1, 4), round(m2, 4), round(m2 - m1, 4)


def predict_b1_lj_score(m1: float, m2: float) -> tuple[float, float]:
    """Apply the frozen B1-LJ logistic formula.

    Returns ``(harmful_probability, transcript_match_score)``.
    """
    m1_value = _reject_non_finite(float(m1), label="m1")
    m2_value = _reject_non_finite(float(m2), label="m2")
    x1 = (m1_value - B1_LJ_SCALER_MEAN[0]) / B1_LJ_SCALER_SCALE[0]
    x2 = (m2_value - B1_LJ_SCALER_MEAN[1]) / B1_LJ_SCALER_SCALE[1]
    logit = B1_LJ_INTERCEPT + B1_LJ_COEF_M1 * x1 + B1_LJ_COEF_M2 * x2
    harmful_probability = 1.0 / (1.0 + math.exp(-logit))
    transcript_match_score = (1.0 - harmful_probability) * 100.0
    return round(harmful_probability, 8), round(transcript_match_score, 4)


def score_b1_lj_from_probs(word_probs_0_100: Iterable[float]) -> B1LjResult:
    m1, m2, gap = m1_m2_gap_from_probs(word_probs_0_100)
    harmful_probability, transcript_match_score = predict_b1_lj_score(m1, m2)
    return B1LjResult(
        m1=m1,
        m2=m2,
        gap=gap,
        harmful_probability=harmful_probability,
        transcript_match_score=transcript_match_score,
    )


def score_b1_lj_from_lexical_words(words: Sequence[LexicalWord]) -> B1LjResult:
    return score_b1_lj_from_probs(word.probability_0_100 for word in words)
