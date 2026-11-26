"""Keep every ``tasks.loop`` of the bot running when one pass fails.

discord.py ends a ``tasks.loop`` for good on any exception it does not retry itself (only ``OSError``,
``GatewayNotFound``, ``ConnectionClosed``, ``aiohttp.ClientError`` and ``TimeoutError`` are). A database
error is none of those, so a single Postgres restart stopped the leaderboard, ticker and onboarding loops
until the container restarted - on 2026-11-14 for twelve days, while the pod looked healthy: discord.py
only logged "Unhandled exception in internal background task" once, and the heartbeat loop kept going.

``survive_errors`` turns such a pass into one logged failure; the loop simply runs again at its next
interval. Cancellation (``CancelledError`` is a ``BaseException``) still stops the loop as before.
"""

import functools
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Marker set on every coroutine function wrapped by ``survive_errors``, so a test can assert that every
# bot loop carries the wrapper (a new loop without it could die silently again).
SURVIVES_ERRORS_ATTR = "_survives_errors"


def survive_errors[**P, R](
    func: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R | None]]:
    """Log and swallow an exception of one pass instead of letting it end the loop."""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
        try:
            return await func(*args, **kwargs)
        except Exception:
            logger.exception(f"Pass of loop {func.__qualname__} failed; the loop keeps running")
            return None

    setattr(wrapper, SURVIVES_ERRORS_ATTR, True)
    return wrapper
