import logging

import discord
from discord.ext import commands

from discord_bot.models import DiscordChannel, DiscordGuild

logger = logging.getLogger(__name__)


class ChannelSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        guild_row = await DiscordGuild.objects.filter(guild_id=channel.guild.id).afirst()
        if not guild_row:
            return

        await DiscordChannel.objects.aupdate_or_create(
            channel_id=channel.id,
            defaults={
                "guild_id": guild_row.id,
                "name": channel.name,
                "channel_type": str(channel.type),
                "is_active": True,
                "position": channel.position,
            },
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        if before.name == after.name and before.position == after.position:
            return
        await DiscordChannel.objects.filter(channel_id=after.id).aupdate(
            name=after.name,
            position=after.position,
        )
        logger.info(f"Channel #{before.position} {before.name} updated to #{after.position} {after.name}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await DiscordChannel.objects.filter(channel_id=channel.id).aupdate(is_active=False)
        logger.info(f"Channel #{channel.position} {channel.name} deleted")
