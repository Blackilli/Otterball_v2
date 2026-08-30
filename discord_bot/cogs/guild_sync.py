import logging

import discord
from discord.ext import commands

from discord_bot.models import DiscordChannel, DiscordGuild, DiscordGuildPool, DiscordGuildRole

logger = logging.getLogger(__name__)


class GuildSyncCog(commands.Cog):
    """Keeps DiscordGuild in step as the bot is added to and removed from servers.

    Between restarts this is the only thing that notices; ReconciliationCog
    rebuilds everything from `bot.guilds` on startup.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        logger.info(f"Joined guild: {guild.name} ({guild.id})")
        # `id` is the Discord snowflake and the primary key - these models have
        # no separate guild_id column.
        await DiscordGuild.objects.aupdate_or_create(
            id=guild.id,
            defaults={"name": guild.name},
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """Deactivate, never delete.

        DiscordGuildPool and ActiveMatchMessage both cascade off DiscordGuild,
        so deleting the row would take a pool's binding and every poll it has
        ever posted with it - for what is often a temporary removal. Marking
        things inactive stops the bot posting and leaves the history intact, the
        same way a deleted channel or role is handled.
        """
        logger.info(f"Removed from guild: {guild.name} ({guild.id}) - deactivating its rows")
        await DiscordGuildPool.objects.filter(guild_id=guild.id).aupdate(is_active=False)
        await DiscordChannel.objects.filter(guild_id=guild.id).aupdate(is_active=False)
        await DiscordGuildRole.objects.filter(guild_id=guild.id).aupdate(is_active=False)
