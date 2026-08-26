import logging

import discord

from discord_bot.models import ActiveMatchMessage

logger = logging.getLogger(__name__)


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
    """
    container_id = active_msg.container_id

    container = bot.get_channel(container_id)
    if container is None:
        try:
            container = await bot.fetch_channel(container_id)
        except (discord.NotFound, discord.Forbidden) as e:
            logger.warning(f"Container {container_id} for match {active_msg.match_id} unavailable: {e}")
            return None

    if not isinstance(container, discord.abc.Messageable):
        logger.warning(f"Container {container_id} for match {active_msg.match_id} is not messageable.")
        return None

    return container
