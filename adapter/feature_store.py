"""In-memory geometry-keyed feature reuse. Cross-geometry lookup is a hard failure.

VAD frames and RMS features in the canonical path are computed on geometry-
specific windows, so this store is the only allowed reuse API. It never keys
by recording_id / buffer_id / speaker_id alone.
"""

from __future__ import annotations

from typing import Any

from adapter.config import GeometryConfig
from adapter.diagnostics import geometry_fingerprint


class GeometryFingerprintMismatch(RuntimeError):
    """Raised when a caller tries to reuse features from a different geometry."""


class GeometryKeyedStore:
    """Per-process in-memory map: complete geometry fingerprint -> payload.

    Stored payloads keep their fingerprint. Lookup recomputes the expected
    fingerprint and refuses to return a payload from another geometry.
    """

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    def put(self, config: GeometryConfig, payload: Any) -> None:
        fingerprint = geometry_fingerprint(config)
        self._items[fingerprint] = {
            "geometry_fingerprint": fingerprint,
            "payload": payload,
        }

    def get(self, config: GeometryConfig) -> Any:
        expected = geometry_fingerprint(config)
        item = self._items.get(expected)
        if item is None:
            return None
        stored = str(item["geometry_fingerprint"])
        if stored != expected:
            raise GeometryFingerprintMismatch(
                "cached payload geometry fingerprint does not match lookup: "
                f"stored={stored} expected={expected}"
            )
        return item["payload"]

    def reuse(self, *, source: GeometryConfig, dest: GeometryConfig) -> Any:
        """Explicit attempt to apply source features under dest's geometry.

        Succeeds only when the complete feature-producing fingerprints match.
        """
        expected = geometry_fingerprint(dest)
        source_fp = geometry_fingerprint(source)
        item = self._items.get(source_fp)
        if item is None:
            raise KeyError(f"no cached payload for {source.name}")
        stored = str(item["geometry_fingerprint"])
        if stored != expected or source_fp != expected:
            raise GeometryFingerprintMismatch(
                f"refusing to reuse {source.name} features for {dest.name}: "
                f"source_fp={source_fp} dest_fp={expected} stored_fp={stored}"
            )
        return item["payload"]
