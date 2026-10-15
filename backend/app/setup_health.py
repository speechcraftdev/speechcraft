from __future__ import annotations

import hmac


def matches_setup_health_identity(expected: str | None, presented: str | None) -> bool:
    return bool(expected and presented and hmac.compare_digest(expected, presented))
