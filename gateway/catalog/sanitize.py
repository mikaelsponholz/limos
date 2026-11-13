"""Conservative source parsing with aggregate, data-free degradation counters."""

import math
from collections import Counter
from typing import Any

Counts = Counter[str]


def degraded(counts: Counts | None, kind: str) -> None:
    if counts is not None:
        counts[kind] += 1


def text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def number(
    value: object, *, minimum: float = 0, positive: bool = False, maximum: float | None = None
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = float(value.replace(",", ".") if isinstance(value, str) else value)
    except (ValueError, OverflowError):
        return None
    if (
        not math.isfinite(parsed)
        or parsed < minimum
        or (positive and parsed == 0)
        or (maximum is not None and parsed > maximum)
    ):
        return None
    return parsed


def items(value: object, kind: str, counts: Counts | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        degraded(counts, kind)
        return []
    result = []
    for entry in value:
        if isinstance(entry, dict):
            result.append(entry)
        else:
            degraded(counts, kind)
    return result


def classifications(value: object, counts: Counts | None = None) -> list[dict[str, Any]]:
    result = []
    for group in items(value, "classifications", counts):
        name = text(group.get("name"))
        if name is None:
            degraded(counts, "classifications.name")
            continue
        values = []
        for entry in items(group.get("values", []), "classifications.values", counts):
            code = text(entry.get("synkkaId")) or text(entry.get("id"))
            if code is None:
                degraded(counts, "classifications.code")
                continue
            status = text(entry.get("unit"))
            if "unit" in entry and status is None:
                degraded(counts, "classifications.status")
            values.append({"id": code, "unit": status})
        result.append({"name": name, "values": values})
    return result
