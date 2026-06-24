import logging

import discord
from discord.ext import commands

from discord_bot.models import DiscordGuildRole

logger = logging.getLogger(__name__)


class RoleSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        db_guild = await DiscordGuildRole.objects.filter(id=role.guild.id).afirst()
        if not db_guild:
            return

        await DiscordGuildRole.objects.aupdate_or_create(
            id=role.id,
            defaults={
                "guild_id": db_guild.id,
                "name": role.name,
                "position": role.position,
                "is_active": True,
            },
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if before.name == after.name and before.position == after.position:
            return
        await DiscordGuildRole.objects.filter(id=after.id).aupdate(
            name=after.name,
            position=after.position,
            is_active=True,
        )
        logger.info(f"Role #{before.position} {before.name} updated to #{after.position} {after.name}")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        await DiscordGuildRole.objects.filter(id=role.id).aupdate(is_active=False)
        logger.info(f"Role #{role.position} {role.name} deleted")
