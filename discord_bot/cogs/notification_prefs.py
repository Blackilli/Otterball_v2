import logging

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.models import DiscordGuildPool, PoolNotificationPreference
from discord_bot.services import aget_or_create_user_id, aset_missing_vote_reminders

logger = logging.getLogger(__name__)


class NotificationPreferenceCog(commands.Cog):
    """`/notifications` - per-user, per-pool control of the missing-vote ping.

    The setting is per pool on purpose: someone can want the NFL reminders and
    not the World Cup ones. Which pool is inferred from the channel the command
    is used in, since that is how a pool is bound to Discord anyway; the `pool`
    option only matters in a guild running more than one.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="notifications",
        description="Turn the 'you haven't voted yet' reminder on or off for a pool.",
    )
    @app_commands.describe(
        enabled="On pings you before kickoff when you have no pick. Leave empty to see your current setting.",
        pool="Only needed if this server runs more than one pool.",
    )
    @app_commands.guild_only()
    async def notifications(
        self,
        interaction: discord.Interaction,
        enabled: bool | None = None,
        pool: str | None = None,
    ) -> None:
        guild_pool = await self._resolve_guild_pool(interaction, pool)
        if guild_pool is None:
            return

        if enabled is None:
            user_id = await aget_or_create_user_id(interaction.user)
            preference = await PoolNotificationPreference.objects.filter(
                user_id=user_id,
                pool_id=guild_pool.pool_id,
            ).afirst()
            # No row means nobody ever changed it, and the default is on.
            state = "on" if preference is None or preference.notify_missing_votes else "off"
            await interaction.response.send_message(
                f"Missing-vote reminders for **{guild_pool.pool.name}** are **{state}**.",
                ephemeral=True,
            )
            return

        await aset_missing_vote_reminders(interaction.user, guild_pool.pool_id, enabled=enabled)
        state = "on" if enabled else "off"
        await interaction.response.send_message(
            f"Missing-vote reminders for **{guild_pool.pool.name}** are now **{state}**.",
            ephemeral=True,
        )

    @notifications.autocomplete("pool")
    async def pool_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []

        return [
            app_commands.Choice(name=guild_pool.pool.name, value=str(guild_pool.pool_id))
            async for guild_pool in DiscordGuildPool.objects.filter(
                guild_id=interaction.guild_id,
                is_active=True,
                pool__is_active=True,
                pool__name__icontains=current,
            ).select_related("pool")[:25]
        ]

    async def _resolve_guild_pool(
        self,
        interaction: discord.Interaction,
        pool: str | None,
    ) -> DiscordGuildPool | None:
        """Which pool the command applies to, answering the interaction on failure."""
        guild_pools = DiscordGuildPool.objects.filter(
            guild_id=interaction.guild_id,
            is_active=True,
            pool__is_active=True,
        ).select_related("pool")

        if pool is not None:
            try:
                guild_pool = await guild_pools.aget(pool_id=int(pool))
            except ValueError, DiscordGuildPool.DoesNotExist:
                await interaction.response.send_message(
                    "I don't know that pool. Pick one from the list.",
                    ephemeral=True,
                )
                return None
            return guild_pool

        # Unambiguous cases first: the pool bound to this channel, then - for a
        # guild that only runs one pool - that one, wherever it was invoked.
        candidates = [gp async for gp in guild_pools.filter(channel_id=interaction.channel_id)]
        if not candidates:
            candidates = [gp async for gp in guild_pools]

        if len(candidates) == 1:
            return candidates[0]

        if not candidates:
            await interaction.response.send_message(
                "There is no active pool in this server yet.",
                ephemeral=True,
            )
            return None

        names = ", ".join(f"**{gp.pool.name}**" for gp in candidates)
        await interaction.response.send_message(
            f"This server runs several pools ({names}). Run the command in a pool's channel, "
            f"or name one with the `pool` option.",
            ephemeral=True,
        )
        return None
