import logging

import discord
from discord.ext import commands

from discord_bot.constants import (
    DISCORD_DRAWABLE_POLL_ANSWER_ORDER,
    DISCORD_KO_POLL_ANSWER_ORDER,
    DISCORD_POLL_ANSWER_ORDER_MAP,
)
from discord_bot.models import ActiveMatchMessage, DiscordProfile
from discord_bot.services import aget_or_create_user_id
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

        answer_order = DISCORD_POLL_ANSWER_ORDER_MAP.get(match_msg.match.stage.stage_type)
        if match_msg.poll_use_fallback_answer_ordering:
            # TODO: Fix this fallback
            message = await self.bot.fetch_message(payload.message_id)
            if len(message.poll.answers) == 3:
                answer_order = DISCORD_DRAWABLE_POLL_ANSWER_ORDER
            elif len(message.poll.answers) == 2:
                answer_order = DISCORD_KO_POLL_ANSWER_ORDER

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
