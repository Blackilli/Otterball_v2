import logging

import discord

from discord_bot.constants import (
    DISCORD_DRAWABLE_POLL_ANSWER_ORDER,
    DISCORD_KO_POLL_ANSWER_ORDER,
    DISCORD_POLL_ANSWER_ORDER_MAP,
)
from discord_bot.models import ActiveMatchMessage, DiscordProfile
from discord_bot.utils import resolve_message_container
from predictions.models import Prediction
from users.models import User

logger = logging.getLogger(__name__)


async def aget_discord_profile_cache() -> dict[int, DiscordProfile]:
    return {
        profile.id: profile
        async for profile in DiscordProfile.objects.select_related("user").filter(user__is_active=True).aiterator()
    }


def _resolve_answer_order(match_msg: ActiveMatchMessage, poll: discord.Poll):
    answer_order = DISCORD_POLL_ANSWER_ORDER_MAP.get(match_msg.match.stage.stage_type)

    if match_msg.poll_use_fallback_answer_ordering:
        # TODO: Fix this fallback
        if len(poll.answers) == 3:
            answer_order = DISCORD_DRAWABLE_POLL_ANSWER_ORDER
        elif len(poll.answers) == 2:
            answer_order = DISCORD_KO_POLL_ANSWER_ORDER

    return answer_order


async def sync_predictions_from_poll(
    bot: discord.Client,
    match_msg: ActiveMatchMessage,
    profile_cache: dict[int, DiscordProfile] | None = None,
) -> int:
    """Re-derive every Prediction for one poll from that poll's current voters.

    This is the tamper-proof pass: it is a full re-derivation, so a vote that
    was retracted while the bot was offline disappears here too, not just
    additions and changes. It runs on startup for every unfinalized poll
    (ReconciliationCog) and once more at kickoff, right after the poll is
    ended (MatchTickerCog) - the live listener can miss events, an ended poll
    cannot change any more.

    `match_msg` must come with `match` and `match__stage` selected.
    Returns the number of predictions written, or -1 when Discord would not
    hand over the poll.
    """
    if profile_cache is None:
        profile_cache = await aget_discord_profile_cache()

    container = await resolve_message_container(bot, match_msg)
    if container is None:
        return -1

    try:
        message = await container.fetch_message(match_msg.poll_message_id)
    except (discord.NotFound, discord.Forbidden) as e:
        logger.warning(f"Skipping poll synchronization for match {match_msg.match_id}: {e}")
        return -1

    if not message.poll:
        logger.warning(f"Poll not found for match {match_msg.match_id}")
        return -1

    answer_order = _resolve_answer_order(match_msg, message.poll)

    match_predictions = []
    try:
        for answer in message.poll.answers:
            if answer_order is None or len(answer_order) <= answer.id:
                logger.error(f"Invalid poll map for match {match_msg.match_id} in stage {match_msg.match.stage_id}")
                continue

            predicted_outcome = answer_order[answer.id]
            if not predicted_outcome:
                logger.error(f"Invalid poll answer: {answer.id}")
                continue

            async for voter in answer.voters():
                profile = profile_cache.get(voter.id)
                if not profile:
                    logger.info(f"Creating user for Discord ID: {voter.id} ({voter.name})")
                    user = await User.objects.acreate_user(username=voter.name, is_active=True)
                    profile = await DiscordProfile.objects.acreate(
                        user=user,
                        id=voter.id,
                        username=voter.name,
                        global_name=voter.global_name,
                    )
                    profile_cache[profile.id] = profile
                    user_id = user.id
                else:
                    user_id = profile.user_id

                match_predictions.append((user_id, predicted_outcome))
    except (discord.NotFound, discord.Forbidden) as e:
        logger.warning(
            f"Skipping poll synchronization for match {match_msg.match_id} due to discord permissions: {e}"
        )
        return -1

    voted_user_ids = {user_id for user_id, _ in match_predictions}
    await Prediction.objects.filter(
        pool_id=match_msg.pool_id,
        match_id=match_msg.match_id,
    ).exclude(user_id__in=voted_user_ids).adelete()

    for user_id, predicted_outcome in match_predictions:
        await Prediction.objects.aupdate_or_create(
            pool_id=match_msg.pool_id,
            user_id=user_id,
            match_id=match_msg.match_id,
            defaults={"predicted_outcome": predicted_outcome},
        )

    return len(match_predictions)
