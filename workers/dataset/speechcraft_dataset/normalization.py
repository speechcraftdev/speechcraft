from __future__ import annotations


DANGER_SYMBOLS = set("$%@#*+=&€£¥©™^_")


def hazard_reason_codes(symbols: list[str], contains_numeric: bool) -> list[str]:
    reasons = ["contains_numeric_token"] if contains_numeric else []
    if symbols:
        reasons.append("contains_danger_symbol")
    if any(symbol in "$€£¥" for symbol in symbols):
        reasons.append("contains_currency_symbol")
    if "%" in symbols:
        reasons.append("contains_percent_symbol")
    return sorted(set(reasons))
