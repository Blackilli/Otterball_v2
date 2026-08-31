import contextlib
import datetime
import logging

import discord
from discord.ext import commands, tasks
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from discord_bot.components import WelcomeView
from discord_bot.models import DiscordGuildPool
from predictions.models import DayOfWeek, PoolStageRule

logger = logging.getLogger(__name__)

#: What the leaderboard message is created with, on the way to being rendered.
#: `onboard` hands it straight to LeaderboardSyncCog, so this is only ever read
#: by someone watching the channel in the same second - and by the next sync
#: pass, at most 30s later, if that cog is not loaded.
LEADERBOARD_PLACEHOLDER = "**Leaderboard**\n-# Standings appear here as soon as the first match is scored."


def next_poll_creation(config, now: datetime.datetime | None = None) -> datetime.datetime | None:
    """When polls next go up, in the server's timezone, or None if never.

    Used to render the schedule as a Discord timestamp, so a pool whose
    players are spread across timezones reads the hour in its own.
    """
    if not config.poll_creation_weekdays:
        return None

    now = now or timezone.localtime()
    weekdays = {int(day) for day in config.poll_creation_weekdays}
    tz = timezone.get_current_timezone()
    # DayOfWeek numbers Monday 0, which is date.weekday()'s numbering too.
    for offset in range(8):
        day = (now + datetime.timedelta(days=offset)).date()
        if day.weekday() not in weekdays:
            continue
        candidate = timezone.make_aware(datetime.datetime.combine(day, config.poll_creation_time), tz)
        if candidate > now:
            return candidate
    return None


def describe_window(days: int) -> str:
    """The lookahead as something a person would say.

    "The next 7 day(s) of matches" was the machine showing through: nobody
    calls a week seven days, and nobody writes "day(s)".
    """
    if days == 7:
        return "A week of fixtures"
    if days == 14:
        return "A fortnight of fixtures"
    if days == 1:
        return "The next day's fixtures"
    return f"The next {days} days of fixtures"


def describe_lead(minutes: int) -> str:
    """The reminder's lead time, in the units it was probably meant in."""
    if minutes == 60:
        return "An hour before"
    if minutes % 60 == 0:
        return f"{minutes // 60} hours before"
    return f"{minutes} minutes before"


def describe_points(points: int) -> str:
    return f"**{points}** point" if points == 1 else f"**{points}** points"


def season_links(season_id: int) -> str:
    """Where the same season can be read on the web.

    Built with `reverse` rather than by hand so a change to the URLconf breaks
    loudly here instead of posting three dead links to a whole role, and
    prefixed with PUBLIC_SITE_URL because a Discord message has no request to
    make these absolute from.
    """
    base = settings.PUBLIC_SITE_URL
    return (
        f"[Fixtures]({base}{reverse('sports:season-matches', args=[season_id])}) · "
        f"[Leaderboard]({base}{reverse('sports:season-leaderboard', args=[season_id])}) · "
        f"[Stats]({base}{reverse('sports:season-stats', args=[season_id])})"
    )


def describe_poll_schedule(config) -> str:
    days = ", ".join(DayOfWeek(day).label for day in sorted(config.poll_creation_weekdays))
    window = describe_window(config.poll_creation_lookahead_days)
    if not days:
        # check_pool reports this as a FAIL; saying so here beats rendering
        # "matches go up every  at 18:00", which reads like a broken template.
        return f"{window} would go up here, but no poll day is set - so nothing posts on its own yet."

    # Rendered as Discord's own short-time markup rather than the server's
    # clock: the pool's players are not all in the server's timezone, and
    # "18:00 (Europe/Berlin)" makes every one of them do the arithmetic. It is
    # anchored on the *next* occurrence rather than a fixed date so the hour
    # stays right across a DST change - which does mean the welcome post is
    # re-rendered once per poll day, and an edit never pings.
    upcoming = next_poll_creation(config)
    if upcoming is None:  # unreachable while `days` is non-empty; belt and braces
        clock = f"**{config.poll_creation_time:%H:%M}** ({timezone.get_current_timezone_name()})"
    else:
        clock = f"<t:{int(upcoming.timestamp())}:t>"
    return f"{window} lands every **{days}** at {clock}, pinned to this channel."


async def build_welcome_message(guild_pool: DiscordGuildPool, role: discord.Role | None) -> WelcomeView:
    """The post that opens a pool: what to do, in the order you do it.

    Structured as the three steps of actually playing - the polls arrive, you
    pick before kickoff, the points land - rather than as a list of the pool's
    settings. It is the first thing a new player reads and the only message
    that pings the whole role, so it answers "what do I do?" before it
    describes anything.

    Every value in it is still read from the pool's own configuration, so a
    channel cannot be told a schedule or a point distribution the pool does
    not actually run.
    """
    pool = guild_pool.pool
    config = getattr(pool, "configuration", None)

    steps: list[tuple[str, str]] = []
    settings_step = None

    if config:
        steps.append(("1️⃣ Wait for the polls", describe_poll_schedule(config)))

        pick = "Each poll closes when its match kicks off."
        if config.reminder_lead_minutes:
            pick += (
                f" {describe_lead(config.reminder_lead_minutes)} that, anyone without a pick gets named — "
                "the button switches that ping off, and back on."
            )
            # Only offered when a reminder actually exists to switch off; the
            # same rule the reminder itself follows.
            settings_step = len(steps)
        steps.append(("2️⃣ Pick before kickoff", pick))

    rules = [
        rule
        async for rule in PoolStageRule.objects.filter(pool_id=guild_pool.pool_id)
        .select_related("stage")
        .order_by("level")
        .aiterator()
    ]
    if rules:
        distribution = "\n".join(
            f"{rule.stage.name if rule.stage else 'Every other round'} — "
            f"{describe_points(rule.points_per_correct)} per correct pick"
            for rule in rules
        )
        steps.append(("3️⃣ Collect", distribution))

    steps.append(
        (
            "📌 Where you stand",
            "The pinned leaderboard keeps itself current. Tied on points, the better hit rate takes it — "
            "that is the share of your picks that scored.",
        )
    )
    steps.append(
        (
            "🌐 On the web",
            f"{season_links(pool.season_id)}\n"
            "Everything the channel shows, plus the rank-over-time chart and the season's own numbers.",
        )
    )

    return WelcomeView(
        heading=f"# 🦦 Welcome to {pool.name}",
        mention=role.mention if role else None,
        subheading=f"-# {pool.season.name} · Three things and you are playing.",
        steps=steps,
        # Votes are only ever read off the poll, so a message in chat is not a pick.
        footer="Vote in the polls, not in chat.",
        settings_pool_id=guild_pool.pool_id if settings_step is not None else None,
        settings_step=settings_step,
    )


class PoolOnboardingCog(commands.Cog):
    """Posts what a freshly bound pool needs in its channel, and keeps it true.

    Standing up a pool happens in the admin or in `manage.py create_pool`,
    i.e. in a container that is not the bot and cannot talk to Discord. So the
    binding is the signal: this loop watches for an active DiscordGuildPool
    that has not been welcomed or has no leaderboard message yet, and posts
    them - in that order, with no bot restart needed. `announce_welcome` is
    the opt-out, and it is off for every binding that predates this cog.

    The welcome post is **edited in place** afterwards, not left as written.
    A pool is created before its points per round are set, so the first render
    of "what a pick is worth" is the flat default and would otherwise stay
    wrong in the channel forever. Editing also means only the first post pings
    the notification role, which is the same reason the match ticker is one
    edited message rather than one per state.

    Creating the leaderboard message lives here rather than in
    LeaderboardSyncCog so the two posts cannot race into a new channel in the
    wrong order; that cog now only renders into a message that already exists.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        #: guild_pool id -> the rendered welcome, so an unchanged pool costs no
        #: API calls. Rebuilt from scratch on a restart, which is worth one
        #: no-op edit per pool.
        self.welcomes: dict[int, tuple[str, str]] = {}
        self.onboarding_loop.start()

    def cog_unload(self) -> None:
        self.onboarding_loop.cancel()

    @tasks.loop(minutes=1)
    async def onboarding_loop(self) -> None:
        async for guild_pool in (
            DiscordGuildPool.objects.filter(is_active=True, pool__is_active=True, channel__isnull=False)
            .select_related("pool", "pool__season", "pool__configuration")
            .aiterator()
        ):
            try:
                await self.onboard(guild_pool)
            except Exception as e:
                logger.error(f"Failed onboarding GuildPool {guild_pool.id}: {e}")

    @onboarding_loop.before_loop
    async def before_onboarding_loop(self) -> None:
        # get_channel/fetch_channel are useless before the gateway is up, and a
        # failed first pass would leave a new channel silent for a minute.
        await self.bot.wait_until_ready()

    async def onboard(self, guild_pool: DiscordGuildPool) -> None:
        if guild_pool.leaderboard_msg and not guild_pool.welcome_msg and not guild_pool.announce_welcome:
            return  # nothing to post, and no welcome to keep up to date

        # No early-out when the welcome *is* posted: working out whether it is
        # still accurate means rendering it, and that is one cached guild
        # lookup plus one query for the stage rules, once a minute.
        guild = self.bot.get_guild(guild_pool.guild_id) or await self.bot.fetch_guild(guild_pool.guild_id)
        channel = self.bot.get_channel(guild_pool.channel_id) or await guild.fetch_channel(guild_pool.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            logger.warning(f"Channel {guild_pool.channel_id} for GuildPool {guild_pool.id} is not messageable.")
            return

        await self.upsert_welcome(guild_pool, guild, channel)

        if not guild_pool.leaderboard_msg:
            msg = await channel.send(LEADERBOARD_PLACEHOLDER)
            guild_pool.leaderboard_msg = msg.id
            await guild_pool.asave(update_fields=["leaderboard_msg"])
            logger.info(f"Leaderboard message {msg.id} created for GuildPool {guild_pool.id}.")
            try:
                await msg.pin()
            except discord.HTTPException:
                logger.warning(f"Failed to pin leaderboard message {msg.id}")
            await self.render_leaderboard(guild_pool)

    async def render_leaderboard(self, guild_pool: DiscordGuildPool) -> None:
        """Fill the message in straight away rather than leave a placeholder up.

        LeaderboardSyncCog would get to it within 30 seconds, but the pool's
        opening post is what the role ping brings people to, so the standings
        under it should not be a stub when they arrive.
        """
        leaderboard_cog = self.bot.get_cog("LeaderboardSyncCog")
        if leaderboard_cog is None:  # not loaded (tests, or a trimmed bot)
            return
        try:
            await leaderboard_cog.update_leaderboard_msg(guild_pool, force=True)
        except Exception as e:
            # The 30s loop will have another go; a failure here must not stop
            # the rest of the onboarding pass.
            logger.error(f"Failed first leaderboard render for GuildPool {guild_pool.id}: {e}")

    async def upsert_welcome(self, guild_pool: DiscordGuildPool, guild, channel) -> None:
        if not guild_pool.welcome_msg and not guild_pool.announce_welcome:
            # Every binding that existed before this cog did has the flag off,
            # so a deploy cannot welcome a pool halfway through its season.
            return

        role = None
        if guild_pool.notification_role_id:
            try:
                role = guild.get_role(guild_pool.notification_role_id) or await guild.fetch_role(
                    guild_pool.notification_role_id
                )
            except discord.NotFound:
                logger.warning(f"Notification role {guild_pool.notification_role_id} missing from server.")

        view = await build_welcome_message(guild_pool, role)
        # The components as Discord will store them, which is the only thing an
        # edit can actually change - cheaper to compare than to enumerate every
        # value that feeds them.
        fingerprint = str(view.to_components())

        if not guild_pool.welcome_msg:
            self.warn_if_unpingable(role, channel)
            await self.post_welcome(guild_pool, channel, view, ping=True)
            self.welcomes[guild_pool.id] = fingerprint
            return

        if not guild_pool.welcome_is_v2:
            # The IS_COMPONENTS_V2 flag cannot be added to a message that was
            # sent without it, so a welcome from the embed era cannot be edited
            # into this one - it has to be replaced. Silently: the role was
            # already told about this season, and telling it again on nothing
            # but a deploy is exactly what announce_welcome exists to prevent.
            logger.info(f"Replacing the pre-V2 welcome for GuildPool {guild_pool.id}.")
            old_message_id = guild_pool.welcome_msg
            await self.post_welcome(guild_pool, channel, view, ping=False)
            with contextlib.suppress(discord.HTTPException):
                await channel.get_partial_message(old_message_id).delete()
            self.welcomes[guild_pool.id] = fingerprint
            return

        if self.welcomes.get(guild_pool.id) == fingerprint:
            return

        try:
            msg = await channel.fetch_message(guild_pool.welcome_msg)
            await msg.edit(view=view)
        except discord.NotFound:
            # Someone deleted it. Reposting would ping the role again for a
            # season that is already under way, so take it as intentional -
            # and cache the render so this stops refetching every minute.
            logger.warning(f"Welcome message {guild_pool.welcome_msg} for GuildPool {guild_pool.id} is gone.")
        self.welcomes[guild_pool.id] = fingerprint

    @staticmethod
    def warn_if_unpingable(role: discord.Role | None, channel) -> None:
        """Say so when the role mention will render but notify nobody.

        Discord only delivers a role ping if the role is mentionable *or* the
        bot has "Mention @everyone, @here and All Roles" - `allowed_mentions`
        alone does not grant it. The message looks perfect either way, so the
        failure is otherwise completely silent, and this post only goes out
        once per season.
        """
        if role is None:
            logger.info("The welcome has no notification role to ping - the binding names none.")
            return
        if getattr(role, "mentionable", True):
            return

        me = getattr(getattr(channel, "guild", None), "me", None)
        permissions = channel.permissions_for(me) if me is not None else None
        if permissions is not None and permissions.mention_everyone:
            return

        logger.warning(
            f"Role {role.id} is not mentionable and the bot lacks 'Mention @everyone, @here and All Roles' "
            f"in channel {channel.id}: the welcome will name the role but ping nobody. Make the role "
            "mentionable, or grant the bot that permission."
        )

    @staticmethod
    async def post_welcome(guild_pool: DiscordGuildPool, channel, view: WelcomeView, *, ping: bool) -> None:
        """Send the card and record it as this pool's welcome.

        Deliberately not pinned: a pool's pins are for its polls and its
        leaderboard, and Discord stops accepting them at 50 per channel. The
        role ping is this post's delivery mechanism instead - and a mention
        inside a V2 component still notifies, so moving off the embed did not
        cost that.
        """
        mentions = discord.AllowedMentions(roles=ping, everyone=False, users=False)
        msg = await channel.send(view=view, allowed_mentions=mentions)
        guild_pool.welcome_msg = msg.id
        guild_pool.welcome_is_v2 = True
        await guild_pool.asave(update_fields=["welcome_msg", "welcome_is_v2"])
        logger.info(f"Welcome message {msg.id} posted for GuildPool {guild_pool.id}.")
