import datetime
import logging
from enum import IntEnum
from math import floor

import discord
from discord.ext import commands, tasks
from discord.utils import format_dt
from django.utils import timezone

from discord_bot.components import FIGURE_SPACE
from discord_bot.constants import DISCORD_POLL_ANSWER_ORDER_MAP
from discord_bot.models import ActiveMatchMessage, DiscordGuildPool, DiscordTeamEmoji
from predictions.models import MAX_POLL_LOOKAHEAD_DAYS, PoolConfiguration
from sports.models import Match, MatchOutcome

logger = logging.getLogger(__name__)


def matches_needing_polls(*, pool_id: int, season_id: int, start, end):
    """Matches in the window that this pool has not already posted a poll for.

    The exclusion is on ActiveMatchMessage alone. It used to also require a
    Prediction (`exclude(predictions__pool_id=..., active_messages__pool_id=...)`),
    and because both conditions had to hold to exclude a match, one that had a
    poll but no votes yet was not excluded - so the next batch posted a second
    poll for it. Whether anyone has voted says nothing about whether a poll
    exists; the ActiveMatchMessage row is the record of that.
    """
    return (
        Match.objects.filter(
            kickoff__gte=start,
            kickoff__lte=end,
            stage__season_id=season_id,
        )
        .exclude(active_messages__pool_id=pool_id)
        .select_related("home_team", "away_team", "stage")
        .order_by("kickoff")
    )


#: What a team with no custom emoji falls back to on a poll answer.
DEFAULT_HOME_EMOJI = "⚪"
DEFAULT_AWAY_EMOJI = "⚫"


async def aget_team_emojis(bot: commands.Bot, team_ids) -> dict[int, object]:
    """team id -> the application emoji to put on its poll answer.

    Two lookups, not one per team: the application's emojis come from Discord
    in a single call and the DiscordTeamEmoji rows in a single query. A team
    with no emoji is simply absent, and the caller falls back.
    """
    emojis = {emoji.id: emoji for emoji in await bot.fetch_application_emojis()}
    return {
        row.team_id: emojis[row.id]
        async for row in DiscordTeamEmoji.objects.filter(team_id__in=list(team_ids)).aiterator()
        if row.id in emojis
    }


def build_poll_content(match: Match, home_emoji, away_emoji) -> str:
    """The message the poll rides on: who, which round, and when."""
    content = f"# **{home_emoji} {match.home_team}** vs. **{match.away_team} {away_emoji}**"
    content += f"\n### Stage: `{match.stage.name}`"
    # Figure spaces, not a run of ordinary ones: a markdown renderer collapses
    # those to a single space, so the gap after the icon only ever survived by
    # accident. Same reason the scoreboard pads with them.
    content += f"\n### 📅{FIGURE_SPACE * 3}{format_dt(match.kickoff, style='F')}"
    content += f"\n### ⏳{FIGURE_SPACE * 3}{format_dt(match.kickoff, style='R')}"
    content += "\n-# Polls may close early, so don't vote on the last second"
    return content


def build_match_poll(match: Match, home_emoji, away_emoji, duration: datetime.timedelta) -> discord.Poll | None:
    """The poll itself, or None when the stage type has no answer ordering.

    A stage absent from DISCORD_POLL_ANSWER_ORDER_MAP is a stage nobody has
    decided about - whether a draw is possible in it is not something to
    guess - so the match is skipped and the gap is loud in the log.
    """
    answer_order = DISCORD_POLL_ANSWER_ORDER_MAP.get(match.stage.stage_type)
    if answer_order is None:
        logger.error(f"No answers found for match {match.id} in stage {match.stage.id}")
        return None

    poll = discord.Poll(question=f"{match.home_team} vs. {match.away_team}", duration=duration)
    for outcome in answer_order:
        match outcome:
            case None:
                continue
            case MatchOutcome.HOME_WIN:
                poll.add_answer(text=match.home_team.name, emoji=home_emoji)
            case MatchOutcome.DRAW:
                poll.add_answer(text="Draw")
            case MatchOutcome.AWAY_WIN:
                poll.add_answer(text=match.away_team.name, emoji=away_emoji)
    return poll


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

            # Belt and braces over the field validator: a poll runs until its
            # match kicks off, so a lookahead past Discord's maximum poll
            # duration would make it reject every poll in the batch.
            effective_lookahead_days = min(pool_config.poll_creation_lookahead_days, MAX_POLL_LOOKAHEAD_DAYS)
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
                async for match in matches_needing_polls(
                    pool_id=guild_pool.pool_id,
                    season_id=guild_pool.pool.season_id,
                    start=local_now,
                    end=lookahead_limit,
                ).aiterator()
            ]

            if not upcoming_matches:
                continue

            team_ids = {m.home_team_id for m in upcoming_matches} | {m.away_team_id for m in upcoming_matches}
            team_emojis = await aget_team_emojis(self.bot, team_ids)

            logger.info(f"Found {len(upcoming_matches)} upcoming matches.")
            announcement = f"The new polls are ready! {notification_role.mention if notification_role else ''}"
            first_kickoff = upcoming_matches[0].kickoff.strftime("%Y-%m-%d")
            last_kickoff = upcoming_matches[-1].kickoff.strftime("%Y-%m-%d")
            announcement += f"\n-# Matches from {first_kickoff} to {last_kickoff}"

            try:
                await channel.send(announcement)
            except discord.DiscordException as e:
                logger.error(f"Error announcing new polls in channel {channel.id}: {e}")

            try:
                for match in upcoming_matches:
                    home_emoji = team_emojis.get(match.home_team_id, DEFAULT_HOME_EMOJI)
                    away_emoji = team_emojis.get(match.away_team_id, DEFAULT_AWAY_EMOJI)

                    content = build_poll_content(match, home_emoji, away_emoji)

                    duration = match.kickoff - timezone.now()
                    if duration.total_seconds() < 0:
                        continue

                    logger.info(f"Creating poll for {match.id} for {floor(duration.total_seconds()/60/60)} hours.")
                    poll = build_match_poll(match, home_emoji, away_emoji, duration)
                    if poll is None:
                        continue
                    logger.info(f"Poll created: {poll}")
                    logger.info(content)
                    poll_msg = await channel.send(content=content, poll=poll)
                    logger.info(f"Poll message created: {poll_msg.id}")
                    await ActiveMatchMessage.objects.acreate(
                        match=match,
                        guild_id=guild_pool.guild_id,
                        pool_id=guild_pool.pool_id,
                        channel_id=channel.id,
                        poll_message_id=poll_msg.id,
                    )
                    # Pinned so the open polls stay reachable in a busy channel;
                    # MatchTickerCog unpins each one at kickoff, which is what
                    # keeps this under Discord's 50-pin ceiling.
                    try:
                        await poll_msg.pin(reason="Open prediction poll")
                    except discord.DiscordException as e:
                        logger.warning(f"Failed to pin poll message {poll_msg.id}: {e}")
            except Exception as e:
                # Whatever was posted before the failure is already tracked in
                # ActiveMatchMessage, so the next run picks up only the
                # remaining matches - nothing to unwind here.
                logger.error(f"Error executing poll generation context: {e}")
