import logging

import discord
from discord.ext import commands

from discord_bot.constants import DISCORD_POLL_ANSWER_ORDER_MAP
from discord_bot.models import ActiveMatchMessage, DiscordProfile
from discord_bot.services import aget_or_create_user_id, resolve_answer_order
from discord_bot.utils import resolve_message_container
from predictions.models import Prediction

logger = logging.getLogger(__name__)


class PollPredictionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_poll_vote_add(self, payload: discord.RawPollVoteActionEvent):
        match_msg = (
            await ActiveMatchMessage.objects.filter(
                poll_message_id=payload.message_id,
            )
            .select_related("match", "match__stage")
            .afirst()
        )
        if match_msg is None or match_msg.match is None or match_msg.match.stage is None:
            return

        answer_order = await self._aresolve_answer_order(match_msg)
        if answer_order is None or len(answer_order) <= payload.answer_id:
            logger.error(f"Invalid poll answer: {payload.answer_id}")
            return

        prediction_outcome = answer_order[payload.answer_id]
        if not prediction_outcome:
            return

        user_id = await self._aresolve_user_id(payload.user_id)
        if user_id is None:
            return

        await Prediction.objects.aupdate_or_create(
            pool_id=match_msg.pool_id,
            user_id=user_id,
            match_id=match_msg.match_id,
            defaults={"predicted_outcome": prediction_outcome},
        )
        # Lets MatchTickerCog drop this voter from the "still without a pick"
        # list straight away instead of at the next minute tick.
        self.bot.dispatch("prediction_change", match_msg.id)

    async def _aresolve_answer_order(self, match_msg: ActiveMatchMessage):
        """Which outcome each answer index means, for this poll.

        Normally the stage type decides it and no API call is needed. Polls
        flagged `poll_use_fallback_answer_ordering` predate that mapping and
        have to be read off the poll itself, which needs the message - fetched
        through its channel, since `commands.Bot` has no `fetch_message` and
        the old call raised AttributeError on every vote on those polls, losing
        it until the next reconciliation pass picked it up.
        """
        if not match_msg.poll_use_fallback_answer_ordering:
            return DISCORD_POLL_ANSWER_ORDER_MAP.get(match_msg.match.stage.stage_type)

        container = await resolve_message_container(self.bot, match_msg)
        if container is None:
            return None
        try:
            message = await container.fetch_message(match_msg.poll_message_id)
        except (discord.NotFound, discord.Forbidden) as e:
            logger.warning(f"Could not read poll {match_msg.poll_message_id} for its answer order: {e}")
            return None
        if not message.poll:
            return None

        # Same resolution the reconciliation pass uses, so a live vote and the
        # full re-derivation cannot read the same poll differently.
        return resolve_answer_order(match_msg, message.poll)

    async def _aresolve_user_id(self, discord_user_id: int) -> int | None:
        """The users.User behind a Discord id, creating one on a first vote.

        This used to bail out when no DiscordProfile existed, which meant a
        first-time voter's pick was dropped entirely until the next full sync -
        and left them named on the pre-kickoff reminder they had just answered.
        """
        profile = await DiscordProfile.objects.filter(id=discord_user_id).afirst()
        if profile:
            return profile.user_id

        discord_user = self.bot.get_user(discord_user_id)
        if discord_user is None:
            try:
                discord_user = await self.bot.fetch_user(discord_user_id)
            except discord.DiscordException as e:
                logger.warning(f"Could not resolve voter {discord_user_id}: {e}")
                return None

        return await aget_or_create_user_id(discord_user)

    @commands.Cog.listener()
    async def on_raw_poll_vote_remove(self, payload: discord.RawPollVoteActionEvent):
        match_msg = await ActiveMatchMessage.objects.filter(
            poll_message_id=payload.message_id,
        ).afirst()
        if not match_msg:
            return

        profile = await DiscordProfile.objects.filter(
            id=payload.user_id,
        ).afirst()
        if not profile:
            return

        deleted, _ = await Prediction.objects.filter(
            pool_id=match_msg.pool_id,
            user_id=profile.user_id,
            match_id=match_msg.match_id,
        ).adelete()

        if deleted:
            # Puts them back on the reminder. They are not pinged again for it:
            # the message is edited, and an edit never notifies.
            self.bot.dispatch("prediction_change", match_msg.id)
