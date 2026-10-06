"""Database connection hygiene for the long-running async code (Celery ingestion, the bot's loops).

Django closes a request's connection when the request ends (``close_old_connections`` on
``request_started``/``request_finished``), and Celery's Django fixup does the same around each task -
but only for connections of the thread it runs in. The async ORM (``aget``, ``aiterator``, ...) runs its
queries through ``sync_to_async(thread_sensitive=True)``, i.e. in asgiref's shared executor thread,
which has its own connection that nothing ever closes. When the database goes away once (a restart,
a failover to the standby), that connection is dead for good and every later query in that process
fails with ``connection already closed`` until the container is restarted: the worker's live sync and
the bot's poll-creation loop did exactly that for eleven hours after a database failover.

``close_old_connections`` drops a connection that is unusable or past ``CONN_MAX_AGE``; Django then
opens a fresh one on the next query. It has to run *in the thread that owns the connection*, which is
why it goes through the same ``sync_to_async`` the ORM uses rather than being called directly.
"""

import functools
from collections.abc import Callable, Coroutine
from typing import Any

from asgiref.sync import sync_to_async
from django.db import close_old_connections

# Marker set on every coroutine function wrapped by ``with_fresh_db_connections``, so a test can assert
# that every bot loop carries the wrapper (a new loop without it would bring the failure back silently).
FRESH_DB_CONNECTIONS_ATTR = "_closes_stale_db_connections"


async def aclose_old_connections() -> None:
    """Drop broken/expired connections of the thread the async ORM runs its queries in."""
    await sync_to_async(close_old_connections)()


def with_fresh_db_connections[**P, R](
    func: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Run ``aclose_old_connections`` before every call of an async function (e.g. a ``tasks.loop``).

    Each pass of a loop is the bot's equivalent of a request, so it starts on a connection that is known
    to be usable - one dead connection then costs one failed pass instead of every pass until a restart.
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        await aclose_old_connections()
        return await func(*args, **kwargs)

    setattr(wrapper, FRESH_DB_CONNECTIONS_ATTR, True)
    return wrapper
