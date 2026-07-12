import datetime
import logging
from enum import IntEnum
from math import floor

import discord
from discord.ext import commands, tasks
from discord.utils import format_dt
from django.utils import timezone

from discord_bot.constants import DISCORD_POLL_ANSWER_ORDER_MAP
from discord_bot.models import ActiveMatchMessage, DiscordGuildPool, DiscordTeamEmoji
from predictions.models import PoolConfiguration
from sports.models import Match, MatchOutcome

logger = logging.getLogger(__name__)


class DayOfWeek(IntEnum):
    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


class PollCreationCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.interval_sync_loop.start()

    def cog_unload(self) -> None:
        if self.interval_sync_loop.is_running():
            self.interval_sync_loop.cancel()
        if self.poll_creation_loop.is_running():
            self.poll_creation_loop.cancel()

    @tasks.loop(minutes=1)
    async def interval_sync_loop(self):
        logger.info("Initializing dynamic poll intervals from database...")
        try:
            current_tz = timezone.get_current_timezone()
            distinct_times = set()
            async for config in (
                PoolConfiguration.objects.filter(pool__is_active=True).select_related("pool").aiterator()
            ):
                t = config.poll_creation_time
                distinct_times.add(datetime.time(hour=t.hour, minute=t.minute, tzinfo=current_tz))

            if distinct_times:
                times_list = list(distinct_times)
                self.poll_creation_loop.change_interval(time=times_list)
                now = timezone.now()
                logger.info(
                    f"Poll creation times: {times_list}. Now: {[datetime.time(hour=now.hour, minute=now.minute),]}"
                )
            else:
                self.poll_creation_loop.change_interval(time=datetime.time(hour=0, minute=0))
                logger.info("No poll creation times found, setting to midnight.")
            if not self.poll_creation_loop.is_running():
                self.poll_creation_loop.start()
        except Exception as e:
            logger.error(f"Error loading poll creation times: {e}")

    @tasks.loop()
    async def poll_creation_loop(self):
        local_now = timezone.localtime(timezone.now())
        current_weekday = local_now.weekday()
        current_time = datetime.time(hour=local_now.hour, minute=local_now.minute)

        logger.info(f"Poll creation loop triggered at {local_now.strftime('%H:%M')} (Weekday: {current_weekday})")

        emojis = {emoji.id: emoji for emoji in await self.bot.fetch_application_emojis()}

        guild_pools_iterator = (
            DiscordGuildPool.objects.select_related("pool", "pool__season", "pool__configuration")
            .filter(
                is_active=True,
                pool__is_active=True,
                pool__configuration__poll_creation_time=current_time,
            )
            .aiterator()
        )

        async for guild_pool in guild_pools_iterator:
            logger.info(f"Processing guild pool {guild_pool.id}...")
            if not guild_pool.pool:
                logger.warning(f"Guild pool {guild_pool.id} has no pool, skipping.")
                continue

            pool_config = guild_pool.pool.configuration

            if current_weekday not in pool_config.poll_creation_weekdays:
                continue

            try:
                guild = self.bot.get_guild(guild_pool.guild_id) or await self.bot.fetch_guild(guild_pool.guild_id)
                channel = self.bot.get_channel(guild_pool.channel_id) or await self.bot.fetch_channel(
                    guild_pool.channel_id
                )
            except discord.NotFound:
                logger.warning(f"Guild {guild_pool.guild_id} or Channel {guild_pool.channel_id} not found, skipping.")
                continue

            if not isinstance(channel, discord.abc.Messageable):
                continue

            effective_lookahead_days = min(pool_config.poll_creation_lookahead_days, 7)
            lookahead_limit = local_now + datetime.timedelta(days=effective_lookahead_days)

            notification_role_id = guild_pool.notification_role_id
            notification_role = None
            if notification_role_id:
                try:
                    notification_role = guild.get_role(notification_role_id) or await guild.fetch_role(
                        notification_role_id
                    )
                except discord.NotFound:
                    logger.warning(f"Notification role {notification_role_id} missing from server.")

            upcoming_matches = [
                match
                async for match in Match.objects.filter(
                    kickoff__gte=local_now,
                    kickoff__lte=lookahead_limit,
                    stage__season_id=guild_pool.pool.season_id,
                )
                .select_related("home_team", "away_team", "stage")
                .exclude(predictions__pool_id=guild_pool.pool_id, active_messages__pool_id=guild_pool.pool_id)
                .order_by("kickoff")
                .aiterator()
            ]

            if not upcoming_matches:
                continue

            team_ids = {m.home_team_id for m in upcoming_matches} | {m.away_team_id for m in upcoming_matches}
            emoji_mapping = {
                e.team_id: e async for e in DiscordTeamEmoji.objects.filter(team_id__in=team_ids).aiterator()
            }

            logger.info(f"Found {len(upcoming_matches)} upcoming matches @{notification_role.mention}.")
            thread_start_message = (
                f"The new polls are ready! {notification_role.mention if notification_role else ''}"
            )
            logger.info(f"Sending thread start message: {thread_start_message}")
            logger.info(f"first match: {upcoming_matches[0].home_team} vs. {upcoming_matches[0].away_team}")
            # return
            try:
                thread_start_msg = await channel.send(thread_start_message)
                thread_name = f"Polls {upcoming_matches[0].kickoff.strftime('%Y-%m-%d')} - {upcoming_matches[-1].kickoff.strftime('%Y-%m-%d')}"
                logger.info(f"Creating thread: {thread_name}")
                thread = await thread_start_msg.create_thread(
                    name=thread_name,
                    auto_archive_duration=10080,
                )
            except Exception as e:
                logger.error(f"Error creating thread: {e}")
                continue

            try:
                for match in upcoming_matches:
                    db_home_emoji = emoji_mapping.get(match.home_team_id)
                    db_away_emoji = emoji_mapping.get(match.away_team_id)

                    home_emoji = emojis.get(db_home_emoji.id if db_home_emoji else 0, "⚪")
                    away_emoji = emojis.get(db_away_emoji.id if db_away_emoji else 0, "⚫")

                    content = f"# **{home_emoji} {match.home_team}** vs. **{match.away_team} {away_emoji}**"
                    content += f"\n### Stage: `{match.stage.name}`"
                    content += f"\n### 📅       {format_dt(match.kickoff, style='F')}"
                    content += f"\n### ⏳       {format_dt(match.kickoff, style='R')}"
                    content += f"\n-# Polls may close early, so don't vote on the last second"

                    duration = match.kickoff - timezone.now()
                    if duration.total_seconds() < 0:
                        continue

                    logger.info(f"Creating poll for {match.id} for {floor(duration.total_seconds()/60/60)} hours.")
                    poll = discord.Poll(
                        question=f"{match.home_team} vs. {match.away_team}",
                        duration=duration,
                    )
                    answer_order = DISCORD_POLL_ANSWER_ORDER_MAP.get(match.stage.stage_type)
                    if answer_order is None:
                        logger.error(f"No answers found for match {match.id} in stage {match.stage.id}")
                        continue
                    for outcome in answer_order:
                        match outcome:
                            case None:
                                continue
                            case MatchOutcome.HOME_WIN:
                                poll.add_answer(text=match.home_team.name, emoji=home_emoji)
                                continue
                            case MatchOutcome.DRAW:
                                poll.add_answer(text="Draw")
                                continue
                            case MatchOutcome.AWAY_WIN:
                                poll.add_answer(text=match.away_team.name, emoji=away_emoji)
                                continue
                    logger.info(f"Poll created: {poll}")
                    logger.info(content)
                    poll_msg = await thread.send(content=content, poll=poll)
                    logger.info(f"Poll message created: {poll_msg}")
                    await ActiveMatchMessage.objects.acreate(
                        match=match,
                        guild_id=guild_pool.guild_id,
                        pool_id=guild_pool.pool_id,
                        channel_id=channel.id,
                        thread_id=thread.id,
                        poll_message_id=poll_msg.id,
                    )
            except Exception as e:
                logger.error(f"Error executing poll generation context: {e}")
                try:
                    await thread.delete()
                except Exception:
                    pass
