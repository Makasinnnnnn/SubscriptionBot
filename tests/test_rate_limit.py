import pytest

from app.core.rate_limit import SlidingWindowLimiter


@pytest.mark.asyncio
async def test_sliding_window_limiter_blocks_and_resets() -> None:
    limiter = SlidingWindowLimiter()

    assert await limiter.allow("client", limit=2, window_seconds=60)
    assert await limiter.allow("client", limit=2, window_seconds=60)
    assert not await limiter.allow("client", limit=2, window_seconds=60)
    assert await limiter.is_blocked("client", limit=2, window_seconds=60)

    await limiter.reset("client")
    assert not await limiter.is_blocked("client", limit=2, window_seconds=60)
    assert limiter._events == {}


@pytest.mark.asyncio
async def test_block_check_does_not_retain_unknown_keys() -> None:
    limiter = SlidingWindowLimiter()

    for index in range(100):
        assert not await limiter.is_blocked(f"unknown-{index}", limit=2, window_seconds=60)

    assert limiter._events == {}
