import logging
import time

import discord

from discord_bot.models import ActiveMatchMessage

logger = logging.getLogger(__name__)

#: How long a container that Discord refused stays written off. Long enough
#: that a restored season's dead threads cost one API call an hour instead of
#: one per match per minute, short enough that granting the bot access to a
#: channel takes effect without a restart.
UNREACHABLE_TTL_SECONDS = 60 * 60

#: container_id -> monotonic deadline. Process-local on purpose: it is a
#: rate-limit guard, not state worth persisting, and a restart should retry.
_unreachable: dict[int, float] = {}


def _is_written_off(container_id: int) -> bool:
    deadline = _unreachable.get(container_id)
    if deadline is None:
        return False
    if time.monotonic() >= deadline:
        del _unreachable[container_id]
        return False
    return True


def write_off_container(container_id: int) -> None:
    _unreachable[container_id] = time.monotonic() + UNREACHABLE_TTL_SECONDS


def is_container_unreachable(container_id: int) -> bool:
    """Did the last attempt at this container fail with a 403/404?

    Lets a caller tell "Discord will not give me this channel" apart from the
    other reasons resolve_message_container returns None, without it having to
    catch discord exceptions of its own.
    """
    return _is_written_off(container_id)


def forget_unreachable_containers() -> None:
    """Drop the write-offs. For tests, and for anything that wants a retry now."""
    _unreachable.clear()


async def resolve_message_container(
    bot: discord.Client,
    active_msg: ActiveMatchMessage,
) -> discord.abc.Messageable | None:
    """Return the channel (or legacy thread) a match's messages live in.

    Polls used to be posted into a per-batch thread and are now posted straight
    into the pool channel, so both shapes exist in the database at once. Going
    through `ActiveMatchMessage.container_id` and `fetch_channel` covers both:
    `fetch_channel` resolves a thread id just as happily as a channel id, while
    the old `channel.get_thread(...)` returned None for any thread that was not
    in the bot's cache.

    A container Discord refuses is written off for an hour: a restored season
    whose threads are gone would otherwise cost one fetch and one warning per
    match on every pass, and the reconciliation sweep alone walks every poll
    that was never finalized.
    """
    container_id = active_msg.container_id

    container = bot.get_channel(container_id)
    if container is None:
        if _is_written_off(container_id):
            return None

        try:
            container = await bot.fetch_channel(container_id)
        except (discord.NotFound, discord.Forbidden) as e:
            write_off_container(container_id)
            logger.warning(
                f"Container {container_id} for match {active_msg.match_id} unavailable: {e}. "
                f"Not retrying it for {UNREACHABLE_TTL_SECONDS // 60} minutes."
            )
            return None

    if not isinstance(container, discord.abc.Messageable):
        logger.warning(f"Container {container_id} for match {active_msg.match_id} is not messageable.")
        return None

    return container
