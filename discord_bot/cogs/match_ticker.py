import asyncio
import datetime
import json
import logging

import discord
import redis.asyncio as aioredis
from discord.ext import commands, tasks
from django.conf import settings
from django.utils import timezone

from discord_bot.models import ActiveMatchMessage
from sports.models import Match, MatchStatus
from sports.schemas import MatchUpdatePayload

logger = logging.getLogger(__name__)


class MatchTickerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.kickoff_lock_loop.start()
        self.pubsub_loop.start()

    def cog_unload(self) -> None:
        self.kickoff_lock_loop.cancel()
        self.pubsub_loop.cancel()

    @tasks.loop(count=1)
    async def pubsub_loop(self):
        logger.info("📻 Launching asynchronous Redis Pub/Sub subscriber context...")

        redis_connection = aioredis.from_url(settings.REDIS_URL)
        pubsub = redis_connection.pubsub()
        await pubsub.subscribe(settings.REDIS_MATCH_UPDATE_TOPIC)

        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )

                if not message:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    raw_data = message["data"].decode("utf-8")

                    event = MatchUpdatePayload.model_validate_json(raw_data)

                    self.bot.loop.create_task(self.process_live_update(event))

                except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as e:
                    logger.error(f"Error decoding Redis message: {e}")
                    continue

        except asyncio.CancelledError:
            logger.warning("Redis subscription loop requested shutdown. Cleaning connections...")
            await pubsub.unsubscribe()
            await redis_connection.close()
            logger.info("Redis subscriber channel cleanly disconnected.")

    async def process_live_update(self, event: MatchUpdatePayload) -> None:
        active_interfaces = ActiveMatchMessage.objects.filter(match_id=event.match_id, is_ticker_finalized=False)

        async for active_msg in active_interfaces:
            thread = self.bot.get_channel(active_msg.thread_id)
            if not thread:
                continue

            match event.status:
                case MatchStatus.LIVE:
                    pass

                case MatchStatus.FINISHED:
                    pass

    @pubsub_loop.before_loop
    async def before_pubsub_loop(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1.0)
    async def kickoff_lock_loop(self):
        now = timezone.now()

        unfinalized_messages = ActiveMatchMessage.objects.filter(
            is_ticker_finalized=False,
            match__kickoff__lte=now + datetime.timedelta(minutes=15),
        ).select_related("match__home_team", "match__away_team")

        async for active_msg in unfinalized_messages:
            try:
                channel = self.bot.get_channel(active_msg.thread_id)
                if not channel:
                    channel = await self.bot.fetch_channel(active_msg.thread_id)

                if not isinstance(channel, discord.abc.Messageable):
                    continue

                match = active_msg.match

                try:
                    poll_message = await channel.fetch_message(active_msg.poll_message_id)
                    if poll_message.poll and not poll_message.poll.is_finalised():
                        await poll_message.end_poll()
                except discord.NotFound:
                    pass

                if active_msg.ticker_message_id is None:
                    msg = await self._send_new_ticker(channel, match)
                    active_msg.ticker_message_id = msg.id
                else:
                    msg = channel.get_message(active_msg.ticker_message_id)
                    if msg is None:
                        msg = await channel.fetch_message(active_msg.ticker_message_id)

                match match.status:
                    case MatchStatus.LIVE:
                        if not active_msg.ticker_message_id:
                            ticker_msg = await channel.send(
                                f"🔒 **Predictions Locked!** The match has officially started.\n"
                                f"🔴 **Live Score:** {match.home_team.name} 0 - 0 {match.away_team.name}"
                            )
                            active_msg.ticker_message_id = ticker_msg.id
                            await active_msg.asave()
                        else:
                            pass
                    case MatchStatus.FINISHED:
                        final_text = (
                            f"🏁 **Match Finished!**\n"
                            f"🏆 **Final Score:** {match.home_team.name} {match.home_score} - {match.away_score} {match.away_team.name}\n"
                            f"ℹ️ *Leaderboards are updating shortly.*"
                        )

                        await self._update_or_send_final_message(channel, active_msg, final_text)
                        active_msg.is_ticker_finalized = True
                        await active_msg.asave()
                    case MatchStatus.POSTPONED | MatchStatus.CANCELLED:
                        status_label = (
                            "⚠️ **Match Postponed!**"
                            if match.status == MatchStatus.POSTPONED
                            else "❌ **Match Cancelled!**"
                        )
                        alert_text = (
                            f"{status_label}\n"
                            f"The fixture between {match.home_team.name} and {match.away_team.name} has been dropped from the schedule.\n"
                            f"ℹ️ *All predictions for this specific fixture have been voided.*"
                        )

                        await self._update_or_send_final_message(channel, active_msg, alert_text)
                        active_msg.is_ticker_finalized = True
                        await active_msg.asave()
            except discord.NotFound, discord.Forbidden:
                continue

    async def _send_new_ticker(self, channel: discord.abc.Messageable, match: Match) -> discord.Message:
        new_ticker = await channel.send("text")
        return new_ticker

    async def _update_or_send_final_message(
        self,
        channel: discord.abc.Messageable,
        active_msg: ActiveMatchMessage,
        text: str,
    ):
        if active_msg.ticker_message_id:
            try:
                old_ticker = await channel.fetch_message(active_msg.ticker_message_id)
                await old_ticker.edit(content=text)
                return
            except discord.NotFound:
                pass
        new_ticker = await channel.send(text)
        active_msg.ticker_message_id = new_ticker.id
        await active_msg.asave()
