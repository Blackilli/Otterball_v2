import discord
from discord.ext import commands

from discord_bot.constants import DISCORD_POLL_MAP
from discord_bot.models import ActiveMatchMessage, DiscordProfile
from predictions.models import Prediction


class PollPredictionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_poll_vote_add(self, payload: discord.RawPollVoteActionEvent):
        prediction_outcome = DISCORD_POLL_MAP.get(payload.answer_id)
        if not prediction_outcome:
            return

        match_msg = await ActiveMatchMessage.objects.filter(
            poll_message_id=payload.message_id,
        ).afirst()
        if not match_msg:
            return

        profile = await DiscordProfile.objects.filter(id=payload.user_id).afirst()
        if not profile:
            return

        await Prediction.objects.aupdate_or_create(
            pool_id=match_msg.pool_id,
            user_id=profile.user_id,
            match_id=match_msg.match_id,
            defaults={"predicted_outcome": prediction_outcome},
        )

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

        await Prediction.objects.filter(
            pool_id=match_msg.pool_id,
            user_id=profile.user_id,
            match_id=match_msg.match_id,
        ).adelete()
