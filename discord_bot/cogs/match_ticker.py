import asyncio
import datetime
import json
import logging

import discord
import redis.asyncio as aioredis
from discord.ext import commands, tasks
from discord.utils import format_dt
from django.conf import settings
from django.db.models import Max, Q
from django.utils import timezone

from discord_bot.models import (
    ActiveMatchMessage,
    DiscordGuildPool,
    DiscordProfile,
    MatchMessageState,
    PoolNotificationPreference,
)
from discord_bot.services import sync_predictions_from_poll
from discord_bot.utils import resolve_message_container
from predictions.models import DEFAULT_REMINDER_LEAD_MINUTES, PoolConfiguration, Prediction
from sports.models import Match, MatchStatus
from sports.schemas import MatchUpdatePayload

logger = logging.getLogger(__name__)

# Discord caps a message at 2000 characters and a mention costs ~22 of them.
# Past this many non-voters the message names a count instead of everyone.
MAX_MENTIONS = 40

# Statuses after which nothing more will happen to the match.
FINAL_STATUSES = (MatchStatus.FINISHED, MatchStatus.POSTPONED, MatchStatus.CANCELLED)


class MatchTickerCog(commands.Cog):
    """One status message per poll, edited in place through the match.

    It is deliberately a single message rather than one per event: it starts
    life as the "kickoff is in an hour, you still haven't voted" reminder,
    becomes the live score once the match starts, and ends as the final score
    plus the list of everyone who called it right. Editing keeps the channel
    readable and, usefully, only the first post pings anyone - later edits
    never re-notify.

    The same routine also closes the poll at kickoff: it ends the Discord poll,
    unpins it, and runs one last full vote re-derivation, after which
    `is_poll_finalized` takes the row out of the startup reconciliation sweep.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # active_msg.id -> last rendered (state, home_score, away_score). Purely
        # an API-call saver, like LeaderboardSyncCog's fingerprint: a restart
        # just means one redundant edit per live match.
        self.rendered: dict[int, tuple] = {}
        # One lock per status message. The minute loop and a Redis update can
        # reach the same match at once; without this both could find
        # ticker_message_id still unset and each post a status message, leaving
        # an orphan the database no longer points at.
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        # Started here rather than in __init__ so constructing the cog does not
        # need a running event loop - PollCreationCog does the same.
        self.state_sync_loop.start()
        self.pubsub_loop.start()

    def cog_unload(self) -> None:
        if self.state_sync_loop.is_running():
            self.state_sync_loop.cancel()
        if self.pubsub_loop.is_running():
            self.pubsub_loop.cancel()

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------

    @tasks.loop(minutes=1.0)
    async def state_sync_loop(self):
        """Steady heartbeat: catches kickoffs, reminders and missed updates."""
        # Each pool sets its own reminder lead time, so the query window has to
        # be the widest of them; the per-match check below then applies that
        # match's own pool setting.
        horizon = timezone.now() + await self._widest_reminder_window()

        async for active_msg in (
            ActiveMatchMessage.objects.filter(
                # A postponement or cancellation can land days before kickoff,
                # and its poll still has to be closed and unpinned.
                Q(match__kickoff__lte=horizon) | Q(match__status__in=FINAL_STATUSES),
                is_ticker_finalized=False,
            )
            .select_related("match", "match__stage", "match__home_team", "match__away_team", "pool__configuration")
            .aiterator()
        ):
            try:
                await self.sync_state_message(active_msg)
            except Exception as e:
                logger.error(f"Failed to sync state message for match {active_msg.match_id}: {e}", exc_info=True)

    @staticmethod
    async def _widest_reminder_window() -> datetime.timedelta:
        widest = await PoolConfiguration.objects.filter(pool__is_active=True).aaggregate(Max("reminder_lead_minutes"))
        minutes = widest["reminder_lead_minutes__max"]
        if minutes is None:
            minutes = DEFAULT_REMINDER_LEAD_MINUTES
        return datetime.timedelta(minutes=minutes)

    @staticmethod
    async def areminder_window(active_msg: ActiveMatchMessage) -> datetime.timedelta:
        """This pool's reminder lead time, defaulting if it has no configuration row.

        Queried by pool id rather than walked through `active_msg.pool` on
        purpose: a lazy relation access here would be a synchronous ORM call on
        the bot's event loop the moment a caller forgot to select_related it.
        """
        minutes = (
            await PoolConfiguration.objects.filter(pool_id=active_msg.pool_id)
            .values_list("reminder_lead_minutes", flat=True)
            .afirst()
        )
        if minutes is None:
            minutes = DEFAULT_REMINDER_LEAD_MINUTES
        return datetime.timedelta(minutes=minutes)

    @state_sync_loop.before_loop
    async def before_state_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

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

    @pubsub_loop.before_loop
    async def before_pubsub_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def process_live_update(self, event: MatchUpdatePayload) -> None:
        """Score/status changed in the DB - refresh that match's messages now.

        The minute loop would get there anyway; this just makes a live score
        land in Discord as soon as ingestion writes it.
        """
        async for active_msg in (
            ActiveMatchMessage.objects.filter(
                match_id=event.match_id,
                is_ticker_finalized=False,
            )
            .select_related("match", "match__stage", "match__home_team", "match__away_team", "pool__configuration")
            .aiterator()
        ):
            try:
                await self.sync_state_message(active_msg)
            except Exception as e:
                logger.error(f"Failed live update for match {active_msg.match_id}: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    async def sync_state_message(self, active_msg: ActiveMatchMessage) -> None:
        async with self._locks.setdefault(active_msg.id, asyncio.Lock()):
            await self._sync_state_message(active_msg)

    async def _sync_state_message(self, active_msg: ActiveMatchMessage) -> None:
        container = await resolve_message_container(self.bot, active_msg)
        if container is None:
            return

        match = active_msg.match
        now = timezone.now()

        # Kickoff is the usual trigger, but a match that was called off never
        # reaches its kickoff - without the status check its poll would stay
        # open and pinned indefinitely.
        should_close = now >= match.kickoff or match.status in FINAL_STATUSES
        if should_close and not active_msg.is_poll_finalized:
            await self._close_poll(container, active_msg)

        state = self._desired_state(match, now, await self.areminder_window(active_msg))
        if state is MatchMessageState.UNKNOWN:
            return

        fingerprint = (state, match.home_score, match.away_score)
        if self.rendered.get(active_msg.id) == fingerprint and active_msg.ticker_message_id:
            return

        content, embed, allowed_mentions = await self._render(state, active_msg)

        posted = await self._upsert_ticker(
            container,
            active_msg,
            content=content,
            embed=embed,
            allowed_mentions=allowed_mentions,
        )
        if not posted:
            return

        self.rendered[active_msg.id] = fingerprint
        active_msg.ticker_state = state
        active_msg.is_ticker_finalized = state == MatchMessageState.RESULT_POSTED
        await active_msg.asave(update_fields=["ticker_message_id", "ticker_state", "is_ticker_finalized"])

    @staticmethod
    def _desired_state(
        match: Match,
        now: datetime.datetime,
        reminder_window: datetime.timedelta,
    ) -> MatchMessageState:
        if match.status in FINAL_STATUSES:
            return MatchMessageState.RESULT_POSTED
        if now >= match.kickoff:
            return MatchMessageState.IN_PROGRESS
        # A pool with reminder_lead_minutes=0 never satisfies this before
        # kickoff, which is how the reminder is switched off.
        if match.kickoff - now <= reminder_window:
            return MatchMessageState.STARTING_SOON
        return MatchMessageState.UNKNOWN

    async def _render(
        self,
        state: MatchMessageState,
        active_msg: ActiveMatchMessage,
    ) -> tuple[str | None, discord.Embed | None, discord.AllowedMentions | None]:
        match state:
            case MatchMessageState.STARTING_SOON:
                return await self._render_starting_soon(active_msg)
            case MatchMessageState.IN_PROGRESS:
                return None, self._render_live_embed(active_msg.match), None
            case _:
                return None, await self._render_final_embed(active_msg), None

    async def _render_starting_soon(
        self,
        active_msg: ActiveMatchMessage,
    ) -> tuple[str, None, discord.AllowedMentions | None]:
        match = active_msg.match
        content = (
            f"⏳ **Last call!** {match.home_team.name} vs. {match.away_team.name} "
            f"kicks off {format_dt(match.kickoff, style='R')}."
        )

        mentions = await self._missing_voter_mentions(active_msg)
        if not mentions:
            return content, None, None

        shown = mentions[:MAX_MENTIONS]
        content += f"\nStill without a pick: {' '.join(shown)}"
        if len(mentions) > MAX_MENTIONS:
            content += f" *and {len(mentions) - MAX_MENTIONS} more*"
        content += "\n-# Don't want these? `/notifications enabled:False` in this channel."

        return content, None, discord.AllowedMentions(users=True, roles=False, everyone=False)

    def _render_live_embed(self, match: Match) -> discord.Embed:
        embed = discord.Embed(
            title="🔴 Predictions locked",
            description=self._score_line(match),
            color=self._embed_color(match),
            timestamp=timezone.now(),
        )
        embed.set_footer(text="Live score")
        return embed

    async def _render_final_embed(self, active_msg: ActiveMatchMessage) -> discord.Embed:
        match = active_msg.match

        if match.status in (MatchStatus.POSTPONED, MatchStatus.CANCELLED):
            label = "Postponed" if match.status == MatchStatus.POSTPONED else "Cancelled"
            embed = discord.Embed(
                title=f"⚠️ Match {label}",
                description=(
                    f"{match.home_team.name} vs. {match.away_team.name} has been dropped from the schedule.\n"
                    f"All predictions for this fixture are void."
                ),
                color=discord.Color.dark_grey(),
                timestamp=timezone.now(),
            )
            return embed

        embed = discord.Embed(
            title="🏁 Full time",
            description=self._score_line(match),
            color=self._embed_color(match),
            timestamp=timezone.now(),
        )

        winners = await self._winner_mentions(active_msg)
        if winners:
            shown = winners[:MAX_MENTIONS]
            value = " ".join(shown)
            if len(winners) > MAX_MENTIONS:
                value += f" *and {len(winners) - MAX_MENTIONS} more*"
            embed.add_field(name=f"🎯 Called it ({len(winners)})", value=value, inline=False)
        else:
            embed.add_field(name="🎯 Called it", value="Nobody. Brutal.", inline=False)

        embed.set_footer(text="Leaderboard updates within a minute")
        return embed

    # ------------------------------------------------------------------
    # Pieces
    # ------------------------------------------------------------------

    @staticmethod
    def _score_line(match: Match) -> str:
        home_score = "-" if match.home_score is None else match.home_score
        away_score = "-" if match.away_score is None else match.away_score
        return f"## {match.home_team.name} {home_score} : {away_score} {match.away_team.name}"

    @staticmethod
    def _embed_color(match: Match) -> discord.Color:
        leader = match.home_team
        if match.home_score is not None and match.away_score is not None and match.away_score > match.home_score:
            leader = match.away_team

        try:
            return discord.Color.from_str(leader.color)
        except ValueError, AttributeError:
            return discord.Color.blurple()

    async def _missing_voter_mentions(self, active_msg: ActiveMatchMessage) -> list[str]:
        """Members of the pool's notification role who still have no prediction.

        Everyone in the role is a participant, minus those who already voted,
        minus those who muted this specific pool. Needs the privileged members
        intent to see the role's membership at all - without it `role.members`
        is empty and the reminder simply goes out without names.
        """
        guild_pool = await DiscordGuildPool.objects.filter(
            guild_id=active_msg.guild_id,
            pool_id=active_msg.pool_id,
            is_active=True,
        ).afirst()
        if not guild_pool or not guild_pool.notification_role_id:
            return []

        guild = self.bot.get_guild(active_msg.guild_id)
        role = guild.get_role(guild_pool.notification_role_id) if guild else None
        if role is None:
            logger.warning(f"Notification role {guild_pool.notification_role_id} not visible for the reminder.")
            return []

        candidates = [member for member in role.members if not member.bot]
        if not candidates:
            logger.warning(
                f"Role {role.id} has no visible members - is the privileged members intent enabled for the bot?"
            )
            return []

        profile_user_ids = {
            profile_id: user_id
            async for profile_id, user_id in DiscordProfile.objects.filter(
                id__in=[member.id for member in candidates]
            ).values_list("id", "user_id")
        }

        voted_user_ids = {
            user_id
            async for user_id in Prediction.objects.filter(
                pool_id=active_msg.pool_id,
                match_id=active_msg.match_id,
            ).values_list("user_id", flat=True)
        }
        muted_user_ids = await PoolNotificationPreference.aget_muted_user_ids(active_msg.pool_id)

        mentions = []
        for member in candidates:
            # No profile means the member has never voted in any pool, so they
            # cannot have a preference row either - they are still missing.
            user_id = profile_user_ids.get(member.id)
            if user_id is not None and (user_id in voted_user_ids or user_id in muted_user_ids):
                continue
            mentions.append(member.mention)

        return mentions

    async def _winner_mentions(self, active_msg: ActiveMatchMessage) -> list[str]:
        outcome = active_msg.match.outcome
        if outcome is None:
            return []

        winner_user_ids = [
            user_id
            async for user_id in Prediction.objects.filter(
                pool_id=active_msg.pool_id,
                match_id=active_msg.match_id,
                predicted_outcome=outcome,
            ).values_list("user_id", flat=True)
        ]
        if not winner_user_ids:
            return []

        return [
            f"<@{profile_id}>"
            async for profile_id in DiscordProfile.objects.filter(user_id__in=winner_user_ids).values_list(
                "id", flat=True
            )
        ]

    async def _close_poll(self, container: discord.abc.Messageable, active_msg: ActiveMatchMessage) -> None:
        """End + unpin the poll at kickoff, then re-derive its votes one last time."""
        try:
            poll_message = await container.fetch_message(active_msg.poll_message_id)
        except (discord.NotFound, discord.Forbidden) as e:
            # Nothing left to close; stop revisiting this row every minute.
            logger.warning(f"Poll message for match {active_msg.match_id} unavailable ({e}), marking it finalized.")
            active_msg.is_poll_finalized = True
            await active_msg.asave(update_fields=["is_poll_finalized"])
            return

        try:
            # Discord expires the poll on its own duration, so by kickoff it is
            # usually already finalised - ending it again is a 400.
            if poll_message.poll and not poll_message.poll.is_finalised():
                await poll_message.end_poll()
            if poll_message.pinned:
                await poll_message.unpin(reason="Poll closed at kickoff")
        except discord.DiscordException as e:
            logger.warning(f"Failed to close poll for match {active_msg.match_id}: {e}")

        await sync_predictions_from_poll(self.bot, active_msg)

        active_msg.is_poll_finalized = True
        await active_msg.asave(update_fields=["is_poll_finalized"])

    async def _upsert_ticker(
        self,
        container: discord.abc.Messageable,
        active_msg: ActiveMatchMessage,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        allowed_mentions: discord.AllowedMentions | None = None,
    ) -> bool:
        embeds = [embed] if embed else []

        if active_msg.ticker_message_id:
            try:
                # A partial message edits without being fetched first, which
                # halves the requests this cog spends per update - it is the
                # difference between one and two calls against the per-channel
                # edit bucket every time a live score ticks.
                get_partial = getattr(container, "get_partial_message", None)
                if get_partial is not None:
                    message = get_partial(active_msg.ticker_message_id)
                else:
                    message = await container.fetch_message(active_msg.ticker_message_id)
                await message.edit(content=content, embeds=embeds)
                return True
            except discord.NotFound:
                # Someone deleted it - fall through and post a fresh one.
                active_msg.ticker_message_id = None
            except discord.DiscordException as e:
                logger.warning(f"Failed to edit state message for match {active_msg.match_id}: {e}")
                return False

        try:
            message = await container.send(
                content=content,
                embeds=embeds,
                allowed_mentions=allowed_mentions,
                reference=discord.MessageReference(
                    message_id=active_msg.poll_message_id,
                    channel_id=active_msg.container_id,
                    fail_if_not_exists=False,
                ),
            )
        except discord.DiscordException as e:
            logger.error(f"Failed to post state message for match {active_msg.match_id}: {e}")
            return False

        active_msg.ticker_message_id = message.id
        return True
