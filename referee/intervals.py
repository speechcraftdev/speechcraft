"""Boring interval arithmetic for coverage metrics.

Intervals are half-open for union merging convenience in the usual sense of
touching endpoints, but phone membership for cutpoints uses a strict open
interval check elsewhere (a < t < b). Duration math uses closed-length
end - start on each disjoint segment after merging.
"""

from __future__ import annotations


Interval = tuple[float, float]


def duration(intervals: list[Interval]) -> float:
    return sum(end - start for start, end in intervals)


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Union of intervals; overlapping or touching segments are merged."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: (iv[0], iv[1]))
    merged: list[Interval] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def intersect_interval(a: Interval, b: Interval) -> Interval | None:
    start = max(a[0], b[0])
    end = min(a[1], b[1])
    if start < end:
        return (start, end)
    return None


def intersect_with_union(base: list[Interval], masks: list[Interval]) -> list[Interval]:
    """Return pieces of base intervals that overlap the union of masks."""
    if not base or not masks:
        return []
    merged_masks = merge_intervals(masks)
    out: list[Interval] = []
    for b in base:
        for m in merged_masks:
            piece = intersect_interval(b, m)
            if piece is not None:
                out.append(piece)
    return merge_intervals(out)


def subtract_union(base: list[Interval], masks: list[Interval]) -> list[Interval]:
    """Subtract the union of masks from base intervals."""
    if not base:
        return []
    if not masks:
        return merge_intervals(base)
    merged_masks = merge_intervals(masks)
    result: list[Interval] = []
    for b_start, b_end in merge_intervals(base):
        pieces = [(b_start, b_end)]
        for m_start, m_end in merged_masks:
            next_pieces: list[Interval] = []
            for p_start, p_end in pieces:
                if m_end <= p_start or m_start >= p_end:
                    next_pieces.append((p_start, p_end))
                    continue
                if p_start < m_start:
                    next_pieces.append((p_start, m_start))
                if m_end < p_end:
                    next_pieces.append((m_end, p_end))
            pieces = next_pieces
        result.extend(pieces)
    return merge_intervals(result)


def intervals_overlap(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]
