import asyncio
from collections import deque
from time import monotonic


class SlidingWindowLimiter:
    def __init__(self, max_keys: int = 10_000) -> None:
        self._events: dict[str, deque[float]] = {}
        self._expires: dict[str, float] = {}
        self._max_keys = max_keys
        self._last_cleanup = 0.0
        self._lock = asyncio.Lock()

    async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        """Record one attempt and return whether it is inside the limit."""
        now = monotonic()
        async with self._lock:
            self._cleanup(now)
            events = self._prune(key, now, window_seconds)
            if len(events) >= limit or (
                key not in self._events and len(self._events) >= self._max_keys
            ):
                return False
            events.append(now)
            self._events[key] = events
            self._expires[key] = now + window_seconds
            return True

    async def is_blocked(self, key: str, limit: int, window_seconds: int) -> bool:
        now = monotonic()
        async with self._lock:
            self._cleanup(now)
            if key not in self._events and len(self._events) >= self._max_keys:
                return True
            return len(self._prune(key, now, window_seconds)) >= limit

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._events.pop(key, None)
            self._expires.pop(key, None)

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < 1:
            return
        for key in [key for key, expires in self._expires.items() if expires <= now]:
            self._events.pop(key, None)
            self._expires.pop(key, None)
        self._last_cleanup = now

    def _prune(self, key: str, now: float, window_seconds: int) -> deque[float]:
        events = self._events.get(key, deque())
        cutoff = now - window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            self._events.pop(key, None)
            self._expires.pop(key, None)
        return events
