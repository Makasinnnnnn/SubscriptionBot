import asyncio
from collections import deque
from time import monotonic


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        """Record one attempt and return whether it is inside the limit."""
        now = monotonic()
        async with self._lock:
            events = self._prune(key, now, window_seconds)
            if len(events) >= limit:
                return False
            events.append(now)
            self._events[key] = events
            return True

    async def is_blocked(self, key: str, limit: int, window_seconds: int) -> bool:
        now = monotonic()
        async with self._lock:
            return len(self._prune(key, now, window_seconds)) >= limit

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._events.pop(key, None)

    def _prune(self, key: str, now: float, window_seconds: int) -> deque[float]:
        events = self._events.get(key, deque())
        cutoff = now - window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            self._events.pop(key, None)
        return events
