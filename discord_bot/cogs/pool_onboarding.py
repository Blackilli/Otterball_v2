import logging

import discord
from discord.ext import commands, tasks
from django.utils import timezone

from discord_bot.models import DiscordGuildPool
from predictions.models import DayOfWeek, PoolStageRule

logger = logging.getLogger(__name__)

#: Placeholder the leaderboard message is created with. LeaderboardSyncCog
#: overwrites it with the real embeds on its next pass, at most 30s later.
LEADERBOARD_PLACEHOLDER = "Leaderboard\n-# soon™"


def describe_poll_schedule(config) -> str:
    days = ", ".join(DayOfWeek(day).label for day in sorted(config.poll_creation_weekdays))
    window = f"{config.poll_creation_lookahead_days} day(s)"
    if not days:
        # check_pool reports this as a FAIL; saying so here beats rendering
        # "matches go up every  at 18:00", which reads like a broken template.
        return f"Polls are not scheduled yet, so the next {window} of matches will not appear on their own."
    return (
        f"The next {window} of matches go up every **{days}** at "
        f"**{config.poll_creation_time:%H:%M}** ({timezone.get_current_timezone_name()})."
    )


async def build_welcome_message(guild_pool: DiscordGuildPool, role: discord.Role | None) -> tuple[str, discord.Embed]:
    """The post that opens a pool: how to play, and what a pick is worth.

    Everything in it is read from the pool's own configuration rather than
    written by hand, so a channel cannot be told a schedule or a point
    distribution the pool does not actually run.
    """
    pool = guild_pool.pool
    config = getattr(pool, "configuration", None)

    content = f"# 🦦 Welcome to {pool.name}!"
    if role:
        content += f"\n{role.mention}"

    embed = discord.Embed(
        title=pool.season.name,
        color=discord.Color.blurple(),
        description="Predict every match in a Discord poll. Right pick, points on the board.",
    )

    if config:
        embed.add_field(name="🗳️ When polls appear", value=describe_poll_schedule(config), inline=False)
        if config.reminder_lead_minutes:
            embed.add_field(
                name="⏰ Reminders",
                value=(
                    f"{config.reminder_lead_minutes} minutes before kickoff, anyone without a pick gets named. "
                    "Turn that off with the **Mute reminders** button on the reminder, or `/notifications`."
                ),
                inline=False,
            )

    rules = [
        rule
        async for rule in PoolStageRule.objects.filter(pool_id=guild_pool.pool_id)
        .select_related("stage")
        .order_by("level")
        .aiterator()
    ]
    if rules:
        distribution = "\n".join(
            f"**{rule.stage.name if rule.stage else 'Every other round'}** — "
            f"{rule.points_per_correct} point(s) per correct pick"
            for rule in rules
        )
        embed.add_field(name="🏆 What a pick is worth", value=distribution, inline=False)

    embed.add_field(
        name="📌 Standings",
        value=(
            "The pinned leaderboard keeps itself up to date. Level on points is split by hit rate — "
            "the share of your picks that scored."
        ),
        inline=False,
    )
    # Votes are only ever read off the poll, so a message in chat is not a pick.
    embed.set_footer(text="Vote in the polls, not in chat. Polls close at kickoff.")
    return content, embed


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

        content, embed = await build_welcome_message(guild_pool, role)
        # The embed as Discord will store it, which is the only thing an edit
        # can actually change - cheaper to compare than to enumerate every
        # field that feeds it.
        fingerprint = (content, str(embed.to_dict()))

        if not guild_pool.welcome_msg:
            # The role ping is this post's whole delivery mechanism, so it is
            # not pinned: a pool's pins are for its polls and its leaderboard,
            # and Discord stops accepting them at 50 per channel.
            msg = await channel.send(
                content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False),
            )
            guild_pool.welcome_msg = msg.id
            await guild_pool.asave(update_fields=["welcome_msg"])
            self.welcomes[guild_pool.id] = fingerprint
            logger.info(f"Welcome message {msg.id} posted for GuildPool {guild_pool.id}.")
            return

        if self.welcomes.get(guild_pool.id) == fingerprint:
            return

        try:
            msg = await channel.fetch_message(guild_pool.welcome_msg)
            await msg.edit(content=content, embed=embed)
        except discord.NotFound:
            # Someone deleted it. Reposting would ping the role again for a
            # season that is already under way, so take it as intentional -
            # and cache the render so this stops refetching every minute.
            logger.warning(f"Welcome message {guild_pool.welcome_msg} for GuildPool {guild_pool.id} is gone.")
        self.welcomes[guild_pool.id] = fingerprint
