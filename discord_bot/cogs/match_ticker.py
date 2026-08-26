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

from discord_bot.components import MatchStatusView, TeamRow
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
from sports.models import Match, MatchStatus, Team
from sports.schemas import MatchUpdatePayload

logger = logging.getLogger(__name__)

# Discord caps a message at 2000 characters and a mention costs ~22 of them.
# Past this many non-voters the message names a count instead of everyone.
MAX_MENTIONS = 40

# A live leader is shown by bold alone - Discord cannot tint the badge, and a
# marker on every in-progress row was more noise than signal. A finished match
# is the one place a marker earns its keep.
WINNER_MARKER = "\N{TROPHY}"

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

    @commands.Cog.listener()
    async def on_prediction_change(self, active_msg_id: int) -> None:
        """A vote was cast or retracted - refresh that match's reminder now.

        Dispatched by PollPredictionCog rather than called directly, so the two
        cogs stay independent. The per-message lock plus the fingerprint make a
        burst of votes collapse into one edit: a second vote arriving mid-edit
        waits, then re-reads and finds the list already current.

        Nobody added back to the list is notified by this - an edit never
        pings. Only the first post does.
        """
        active_msg = await (
            ActiveMatchMessage.objects.filter(id=active_msg_id, is_ticker_finalized=False)
            .select_related("match", "match__stage", "match__home_team", "match__away_team")
            .afirst()
        )
        if active_msg is None:
            return

        try:
            await self.sync_state_message(active_msg)
        except Exception as e:
            logger.error(f"Failed to refresh reminder for match {active_msg.match_id}: {e}", exc_info=True)

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

        # Resolved before the fingerprint rather than inside the renderer, so a
        # vote cast or retracted while the reminder is up actually counts as a
        # change - the score has not moved, and without this the message would
        # keep naming someone who has since voted.
        mentions = await self._missing_voter_mentions(active_msg) if state is MatchMessageState.STARTING_SOON else []

        fingerprint = (state, match.home_score, match.away_score, tuple(mentions))
        if self.rendered.get(active_msg.id) == fingerprint and active_msg.ticker_message_id:
            return

        view, allowed_mentions = await self._render(state, active_msg, mentions)

        posted = await self._upsert_ticker(
            container,
            active_msg,
            view=view,
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
        mentions: list[str],
    ) -> tuple[MatchStatusView, discord.AllowedMentions | None]:
        match state:
            case MatchMessageState.STARTING_SOON:
                return self._render_starting_soon(active_msg, mentions)
            case MatchMessageState.IN_PROGRESS:
                return self._render_live(active_msg.match), None
            case _:
                return await self._render_final(active_msg), None

    def _render_starting_soon(
        self,
        active_msg: ActiveMatchMessage,
        mentions: list[str],
    ) -> tuple[MatchStatusView, discord.AllowedMentions | None]:
        match = active_msg.match
        leader = self._leading_team(match)

        mention_block = None
        allowed_mentions = None
        if mentions:
            shown = mentions[:MAX_MENTIONS]
            mention_block = f"Still without a pick: {' '.join(shown)}"
            if len(mentions) > MAX_MENTIONS:
                mention_block += f" *and {len(mentions) - MAX_MENTIONS} more*"
            allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)

        view = MatchStatusView(
            heading=f"### ⏳ Last call! Kickoff {format_dt(match.kickoff, style='R')}",
            footer="Vote on the poll above - it closes at kickoff.",
            accent=self._team_color(leader),
            teams=self._team_rows(match),
            mentions=mention_block,
            # Only offered when someone is actually being pinged, so the way
            # out sits next to the ping rather than on an unrelated message.
            mute_pool_id=active_msg.pool_id if mention_block else None,
        )
        return view, allowed_mentions

    def _render_live(self, match: Match) -> MatchStatusView:
        return self._scoreline_view(match, heading="### 🔴 Predictions locked", footer="Live score")

    async def _render_final(self, active_msg: ActiveMatchMessage) -> MatchStatusView:
        match = active_msg.match

        if match.status in (MatchStatus.POSTPONED, MatchStatus.CANCELLED):
            label = "Postponed" if match.status == MatchStatus.POSTPONED else "Cancelled"
            return MatchStatusView(
                heading=f"### ⚠️ Match {label}",
                body=(
                    f"{match.home_team.name} vs. {match.away_team.name} has been dropped from the schedule.\n"
                    f"All predictions for this fixture are void."
                ),
                footer="No points are awarded for this match.",
                accent=discord.Color.dark_grey(),
            )

        winners = await self._winner_mentions(active_msg)
        if winners:
            shown = winners[:MAX_MENTIONS]
            detail = f"**🎯 Called it ({len(winners)})**\n{' '.join(shown)}"
            if len(winners) > MAX_MENTIONS:
                detail += f" *and {len(winners) - MAX_MENTIONS} more*"
        else:
            detail = "**🎯 Called it**\nNobody. Brutal."

        return self._scoreline_view(
            match,
            heading="### 🏁 Full time",
            footer="Leaderboard updates within a minute",
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Pieces
    # ------------------------------------------------------------------

    @staticmethod
    def _team_ahead(match: Match) -> "Team | None":
        """The team actually ahead, or None when level or not yet played.

        Distinct from `_leading_team` on purpose: a 0-0 game and a 14-14 game
        have no leader, and marking the home side as one would be a plain lie -
        NFL regular season games really can tie.
        """
        if match.home_score is None or match.away_score is None:
            return None
        if match.home_score > match.away_score:
            return match.home_team
        if match.away_score > match.home_score:
            return match.away_team
        return None

    @classmethod
    def _leading_team(cls, match: Match) -> "Team":
        """Whose colour the message wears, falling back to home when level.

        The accent has to be *something*, so a tie or an unplayed match takes
        the home side. Only `_team_ahead` decides who is marked as ahead.
        """
        return cls._team_ahead(match) or match.home_team

    def _team_rows(self, match: Match) -> list[TeamRow]:
        """Both sides, home first, with the one ahead marked.

        Home always comes first - reordering by who is winning would make the
        scoreboard shift under the reader mid-match.
        """
        ahead = self._team_ahead(match)
        # Only full time gets a marker; a leader mid-match is carried by bold.
        marker = WINNER_MARKER if match.status == MatchStatus.FINISHED else ""

        # Both scores are measured against the longer of the two so a single
        # digit lands on the same centre line as a double.
        sides = ((match.home_team, match.home_score), (match.away_team, match.away_score))
        widest = max((len(str(score)) for _, score in sides if score is not None), default=1)

        rows = []
        for team, score in sides:
            is_ahead = ahead is not None and team.id == ahead.id
            rows.append(
                TeamRow(
                    name=team.name,
                    # Team.logo is a local ImageField behind a relative
                    # MEDIA_URL, so it has no absolute URL for Discord to fetch.
                    # logo_url is the provider's own CDN link, which is public -
                    # and null for a team ingested without one, hence no badge
                    # rather than a broken image.
                    logo_url=team.logo_url or None,
                    score=score,
                    widest_score=widest,
                    marker=marker if is_ahead else "",
                    ahead=is_ahead,
                )
            )
        return rows

    def _scoreline_view(
        self, match: Match, *, heading: str, footer: str, detail: str | None = None
    ) -> MatchStatusView:
        """The live and full-time layouts, which differ only in wording.

        Both badges show, and the accent colour comes from whoever is ahead, so
        the message visibly swings when the lead changes.
        """
        return MatchStatusView(
            heading=heading,
            footer=footer,
            accent=self._team_color(self._leading_team(match)),
            teams=self._team_rows(match),
            detail=detail,
        )

    @staticmethod
    def _team_color(team: "Team") -> discord.Color:
        try:
            return discord.Color.from_str(team.color)
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
        view: MatchStatusView,
        allowed_mentions: discord.AllowedMentions | None = None,
    ) -> bool:
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
                # Every state of this message is Components V2, so there is
                # never a content/embed to clear first. A status message left
                # over from the embed era would reject this edit - none exist,
                # and the IS_COMPONENTS_V2 flag is one-way anyway.
                await message.edit(view=view)
                return True
            except discord.NotFound:
                # Someone deleted it - fall through and post a fresh one.
                active_msg.ticker_message_id = None
            except discord.DiscordException as e:
                logger.warning(f"Failed to edit state message for match {active_msg.match_id}: {e}")
                return False

        try:
            message = await container.send(
                view=view,
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
