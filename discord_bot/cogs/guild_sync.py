import logging

import discord
from discord.ext import commands

from discord_bot.models import DiscordGuild

logger = logging.getLogger(__name__)


class GuildSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        logger.info(f"Joined guild: {guild.name} ({guild.id})")
        await DiscordGuild.objects.aupdate_or_create(
            guild_id=guild.id,
            defaults={
                "name": guild.name,
            },
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        await DiscordGuild.objects.filter(guild_id=guild.id).adelete()
