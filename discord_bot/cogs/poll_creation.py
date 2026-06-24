import datetime
import logging
from enum import IntEnum
from math import floor

import discord
from discord.ext import commands, tasks
from discord.utils import format_dt
from django.utils import timezone

from discord_bot.models import ActiveMatchMessage, DiscordGuildPool, DiscordTeamEmoji
from sports.models import Match

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
    utc = datetime.timezone.utc

    time = datetime.time(hour=0, minute=30, tzinfo=utc)

    def __init__(self, bot: commands.Bot):
        logger.info("Poll creation loop started.")
        self.bot = bot
        self.poll_creation_loop.start()

    def cog_unload(self) -> None:
        self.poll_creation_loop.cancel()

    @tasks.loop(time=time)
    async def poll_creation_loop(self):
        # if timezone.now().weekday() != DayOfWeek.SUNDAY:
        #     logger.info("Poll creation loop skipped, not a Sunday.")
        #     return
        emojis = {
            emoji.id: emoji for emoji in await self.bot.fetch_application_emojis()
        }

        async for guild_pool in (
            DiscordGuildPool.objects.select_related("pool")
            .select_related("pool__season")
            .filter(pool__is_active=True)
            .filter(is_active=True)
            .aiterator()
        ):
            if not guild_pool.pool:
                logger.warning(f"Guild pool {guild_pool.id} has no pool, skipping.")
                continue

            guild = self.bot.get_guild(guild_pool.guild_id)
            if not guild:
                guild = await self.bot.fetch_guild(guild_pool.guild_id)

            channel = self.bot.get_channel(guild_pool.channel_id)
            if not channel:
                channel = await self.bot.fetch_channel(guild_pool.channel_id)

            if not isinstance(channel, discord.abc.Messageable):
                logger.warning(f"Channel {channel.id} is not a messageable, skipping.")
                continue

            notification_role_id = guild_pool.notification_role_id
            notification_role = None
            if notification_role_id:
                notification_role = guild.get_role(notification_role_id)
                if not notification_role:
                    notification_role = await guild.fetch_role(notification_role_id)

            upcoming_matches = [
                match
                async for match in Match.objects.filter(
                    kickoff__gte=datetime.datetime.now(self.utc),
                    kickoff__lte=datetime.datetime.now(self.utc)
                    + datetime.timedelta(days=7),
                    stage__season_id=guild_pool.pool.season_id,
                )
                .select_related("home_team", "away_team")
                .exclude(predictions__pool_id=guild_pool.pool_id)
                .order_by("kickoff")
                .aiterator()
            ]

            if not upcoming_matches:
                continue

            logger.info(
                f"Found {len(upcoming_matches)} upcoming matches @{notification_role.mention}."
            )
            thread_start_message = f"The new polls are ready! {notification_role.mention if notification_role else ''}"
            logger.info(f"Sending thread start message: {thread_start_message}")
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
                    db_home_emoji = await DiscordTeamEmoji.objects.filter(
                        team_id=match.home_team_id
                    ).afirst()
                    db_away_emoji = await DiscordTeamEmoji.objects.filter(
                        team_id=match.away_team_id
                    ).afirst()
                    home_emoji = emojis.get(
                        db_home_emoji.id if db_home_emoji else 0, ":flag_white:"
                    )
                    away_emoji = emojis.get(
                        db_away_emoji.id if db_away_emoji else 0, ":flag_black:"
                    )

                    content = f"# **{home_emoji} {match.home_team}** vs. **{match.away_team} {away_emoji}**"
                    content += f"\n### 📅   {format_dt(match.kickoff, style='F')} "
                    content += f"\n### ⏳   {format_dt(match.kickoff, style='R')}"
                    content += (
                        f"\n-# Polls may close early, so don't vote on the last second"
                    )
                    duration = match.kickoff - timezone.now()
                    logger.info(
                        f"Creating poll for {match.id} for {floor(duration.total_seconds()/60/60)} hours."
                    )
                    poll = (
                        discord.Poll(
                            question=f"**{match.home_team}** vs. **{match.away_team}**",
                            duration=duration,
                        )
                        .add_answer(text=match.home_team.name, emoji=home_emoji)
                        .add_answer(text="Draw")
                        .add_answer(text=match.away_team.name, emoji=away_emoji)
                    )
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
                logger.error(f"Error creating polls: {e}")
                await thread.delete()
