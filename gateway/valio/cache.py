"""Per-product TTL cache; callers include all request context in the key."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from time import monotonic
from typing import Literal


@dataclass(frozen=True)
class CacheKey:
    kind: Literal["price", "availability"]
    product_code: str
    requested_date: date
    customer: str


class TTLCache[T]:
    def __init__(self, ttl_seconds: float = 300, clock: Callable[[], float] = monotonic) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Cache TTL must be positive.")
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[CacheKey, tuple[float, T]] = {}

    def get(self, key: CacheKey) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return value

    def set(self, key: CacheKey, value: T) -> None:
        now = self._clock()
        # Purge expired entries even when subsequent requests use different codes.
        self._entries = {key: entry for key, entry in self._entries.items() if entry[0] > now}
        self._entries[key] = (now + self._ttl, value)
