import datetime
import logging

import discord
from discord.ext import commands, tasks
from django.utils import timezone

from discord_bot.cogs.poll_creation import (
    DEFAULT_AWAY_EMOJI,
    DEFAULT_HOME_EMOJI,
    aget_team_emojis,
    build_match_poll,
    build_poll_content,
)
from discord_bot.components import ComponentGalleryView
from discord_bot.models import (
    ActiveMatchMessage,
    DiscordProfile,
    MatchMessageState,
    MessagePreviewRequest,
    PreviewMessageKind,
    PreviewStatus,
)
from predictions.models import MAX_POLL_LOOKAHEAD_DAYS, PredictionPool

logger = logging.getLogger(__name__)

#: How long a preview poll runs when its match has already kicked off. Discord
#: refuses a poll with a duration in the past, and a preview has to work on
#: last season's fixtures - that is usually the only finished match around.
PREVIEW_POLL_DURATION = datetime.timedelta(hours=1)

#: Nothing a preview posts ever notifies anyone. The reminder's whole point is
#: that it pings, so previewing it as-is would ping a role about a match that
#: is not really starting - the names still render, they just do not fire.
SILENT = discord.AllowedMentions.none()

STATE_KINDS = {
    PreviewMessageKind.STARTING_SOON: MatchMessageState.STARTING_SOON,
    PreviewMessageKind.IN_PROGRESS: MatchMessageState.IN_PROGRESS,
    PreviewMessageKind.RESULT_POSTED: MatchMessageState.RESULT_POSTED,
}


class MessagePreviewCog(commands.Cog):
    """Posts the test messages the admin asked for, and takes them back down.

    Standing a pool up and previewing it both happen in the `web` container,
    which has no Discord connection - so a MessagePreviewRequest row is the
    signal, the same way a DiscordGuildPool row is for the welcome post.

    Everything is rendered through the code the real flow uses: the poll comes
    out of `poll_creation`'s builders and each status message out of
    `MatchTickerCog`'s own state renderers. A preview that drew its own
    version of these would agree with the channel right up until it stopped,
    which is the one thing a preview must not do.

    Nothing it posts is tracked as an ActiveMatchMessage, so the poll loop
    still posts the real poll for that match later and the ticker never edits
    a preview. Votes on a preview poll are ignored for the same reason.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.preview_loop.start()

    def cog_unload(self) -> None:
        self.preview_loop.cancel()

    @tasks.loop(seconds=15)
    async def preview_loop(self) -> None:
        pending = (
            MessagePreviewRequest.objects.filter(status=PreviewStatus.PENDING)
            .select_related(
                "guild_pool",
                "match",
                "match__stage",
                "match__home_team",
                "match__away_team",
            )
            .order_by("created_at")
        )
        async for request in pending.aiterator():
            try:
                await self.post_preview(request)
            except Exception as e:
                logger.error(f"Preview request {request.id} failed: {e}")
                await self.afail(request, str(e))

        cleanups = MessagePreviewRequest.objects.filter(
            cleanup_requested=True,
            cleaned_at__isnull=True,
        ).select_related("guild_pool")
        async for request in cleanups.aiterator():
            try:
                await self.remove_preview(request)
            except Exception as e:
                logger.error(f"Preview cleanup {request.id} failed: {e}")

    @preview_loop.before_loop
    async def before_preview_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

    async def post_preview(self, request: MessagePreviewRequest) -> None:
        channel = await self.aresolve_channel(request)
        if channel is None:
            await self.afail(request, "The pool's channel is not reachable from the bot.")
            return

        match = request.match
        guild_pool = request.guild_pool
        posted: list[int] = []

        # A test message is indistinguishable from a real one otherwise, and
        # these sit in a channel people read. The header says what they are and
        # is deleted along with them.
        header = await channel.send(
            f"-# 🧪 **Test messages** for {match.home_team} vs. {match.away_team} — nothing here is scored, "
            "no votes are counted, and nobody is pinged.",
            allowed_mentions=SILENT,
        )
        posted.append(header.id)

        try:
            for kind in request.kinds:
                message_id = await self.post_one(channel, request, kind)
                if message_id is not None:
                    posted.append(message_id)
        finally:
            # Whatever went out has to be recorded even if a later message
            # failed, or the cleanup button cannot reach it.
            request.posted_message_ids = posted
            await request.asave(update_fields=["posted_message_ids"])

        request.status = PreviewStatus.POSTED
        request.processed_at = timezone.now()
        await request.asave(update_fields=["status", "processed_at"])
        logger.info(f"Posted preview {request.id} ({len(posted)} message(s)) into channel {channel.id}.")

    async def post_one(self, channel, request: MessagePreviewRequest, kind: str) -> int | None:
        if kind == PreviewMessageKind.POLL:
            return await self.post_poll(channel, request.match)

        if kind == PreviewMessageKind.COMPONENTS:
            return await self.post_components(channel, request)

        state = STATE_KINDS.get(kind)
        if state is None:
            logger.warning(f"Preview {request.id} asked for unknown message kind {kind!r}.")
            return None
        return await self.post_state(channel, request, state)

    async def post_poll(self, channel, match) -> int | None:
        team_emojis = await aget_team_emojis(self.bot, {match.home_team_id, match.away_team_id})
        home_emoji = team_emojis.get(match.home_team_id, DEFAULT_HOME_EMOJI)
        away_emoji = team_emojis.get(match.away_team_id, DEFAULT_AWAY_EMOJI)

        # A poll runs until its match kicks off, which for anything already
        # played is a duration Discord rejects - and the cap is Discord's
        # maximum poll duration, not a preference.
        duration = match.kickoff - timezone.now()
        if duration <= PREVIEW_POLL_DURATION:
            duration = PREVIEW_POLL_DURATION
        duration = min(duration, datetime.timedelta(days=MAX_POLL_LOOKAHEAD_DAYS))

        poll = build_match_poll(match, home_emoji, away_emoji, duration)
        if poll is None:
            # The stage type has no answer ordering, which is exactly the
            # failure this preview is worth catching before a poll night.
            raise RuntimeError(
                f"Stage '{match.stage.name}' ({match.stage.stage_type}) has no poll answer ordering, "
                "so the real poll for this match would be skipped too."
            )

        message = await channel.send(
            content=build_poll_content(match, home_emoji, away_emoji),
            poll=poll,
            allowed_mentions=SILENT,
        )
        return message.id

    async def post_components(self, channel, request: MessagePreviewRequest) -> int | None:
        """The clickable components, as themselves.

        Live on purpose: a mock-up would prove the layout renders and nothing
        about whether the button dispatches, which is the half that breaks.
        Clicking one really does what it says - the notification setting it
        writes is the requester's own.
        """
        # Queried rather than walked through `guild_pool.pool`: a lazy
        # relation access here would be a sync ORM call on the bot's event
        # loop, and whether it is cached depends on the caller's
        # select_related - which is not a thing to rely on.
        pool_id = request.guild_pool.pool_id
        pool_name = await PredictionPool.objects.filter(pk=pool_id).values_list("name", flat=True).afirst()
        view = ComponentGalleryView(pool_id, pool_name or "this pool")
        message = await channel.send(view=view, allowed_mentions=SILENT)
        return message.id

    async def post_state(self, channel, request: MessagePreviewRequest, state: MatchMessageState) -> int | None:
        ticker = self.bot.get_cog("MatchTickerCog")
        if ticker is None:
            raise RuntimeError("MatchTickerCog is not loaded, so its status messages cannot be rendered.")

        # Unsaved on purpose: the renderers only ever read ids off this, and a
        # saved row would make the poll loop skip the match and hand the
        # preview to the ticker to edit.
        active_msg = ActiveMatchMessage(
            match=request.match,
            guild_id=request.guild_pool.guild_id,
            pool_id=request.guild_pool.pool_id,
            channel_id=request.guild_pool.channel_id,
        )

        mentions = []
        if state is MatchMessageState.STARTING_SOON:
            mentions = await ticker._missing_voter_mentions(active_msg)
            if not mentions:
                # Everyone has voted, or the role is empty. The interesting
                # half of this message is the name list and the button beside
                # it, so borrow the requester rather than preview a layout the
                # channel would not get - the mention cannot fire anyway.
                mentions = await self.arequester_mention(request)

        view, _allowed_mentions = await ticker._render(state, active_msg, mentions)
        message = await channel.send(view=view, allowed_mentions=SILENT)
        return message.id

    @staticmethod
    async def arequester_mention(request: MessagePreviewRequest) -> list[str]:
        profile_id = (
            await DiscordProfile.objects.filter(user_id=request.requested_by_id).values_list("id", flat=True).afirst()
        )
        return [f"<@{profile_id}>"] if profile_id else []

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def remove_preview(self, request: MessagePreviewRequest) -> None:
        channel = await self.aresolve_channel(request)
        if channel is not None:
            for message_id in request.posted_message_ids:
                try:
                    await channel.get_partial_message(message_id).delete()
                except discord.NotFound:
                    pass  # already gone by hand, which is the same outcome
                except discord.DiscordException as e:
                    logger.warning(f"Could not delete preview message {message_id}: {e}")

        request.status = PreviewStatus.CLEANED
        request.posted_message_ids = []
        request.cleaned_at = timezone.now()
        await request.asave(update_fields=["status", "posted_message_ids", "cleaned_at"])
        logger.info(f"Removed the messages of preview {request.id}.")

    # ------------------------------------------------------------------
    # Pieces
    # ------------------------------------------------------------------

    async def aresolve_channel(self, request: MessagePreviewRequest):
        channel_id = request.guild_pool.channel_id
        if channel_id is None:
            return None
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except discord.DiscordException as e:
            logger.warning(f"Preview channel {channel_id} unreachable: {e}")
            return None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    @staticmethod
    async def afail(request: MessagePreviewRequest, error: str) -> None:
        request.status = PreviewStatus.FAILED
        request.error = error
        request.processed_at = timezone.now()
        await request.asave(update_fields=["status", "error", "processed_at"])
