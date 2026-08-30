import logging

from discord.ext import commands

from discord_bot.models import (
    ActiveMatchMessage,
    DiscordChannel,
    DiscordGuild,
    DiscordGuildRole,
)
from discord_bot.services import aget_discord_profile_cache, sync_predictions_from_poll

logger = logging.getLogger(__name__)


class ReconciliationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.polls_reconciled = False

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Reconciliation check started.")
        # The guild/channel/role upserts are cache reads and cheap, so they run
        # again on every reconnect and pick up anything changed while offline.
        await self.reconcile_roles()
        await self.reconcile_channels()

        # The poll sweep is not: it is one Discord fetch per poll that was never
        # finalized, and `on_ready` fires on every gateway reconnect, not just
        # startup. A restored season with a hundred open rows replayed all of it
        # every time the connection blipped.
        if self.polls_reconciled:
            logger.info("Polls already reconciled this run; skipping the sweep.")
            return

        self.polls_reconciled = True
        await self.reconcile_active_polls()

    async def reconcile_channels(self):
        for guild in self.bot.guilds:
            guild_row, _ = await DiscordGuild.objects.aupdate_or_create(
                id=guild.id,
                defaults={
                    "name": guild.name,
                },
            )

            live_channel_ids = [channel.id for channel in guild.channels]

            for channel in guild.channels:
                await DiscordChannel.objects.aupdate_or_create(
                    id=channel.id,
                    defaults={
                        "guild_id": guild_row.id,
                        "name": channel.name,
                        "channel_type": str(channel.type),
                        "is_active": True,
                        "position": channel.position,
                    },
                )

            # Once per guild, not once per channel: nested in the loop above
            # this ran the same UPDATE for every channel the guild has.
            await DiscordChannel.objects.filter(
                guild_id=guild_row.id,
                is_active=True,
            ).exclude(
                id__in=live_channel_ids,
            ).aupdate(is_active=False)

    async def reconcile_roles(self):
        for guild in self.bot.guilds:
            guild_row, _ = await DiscordGuild.objects.aupdate_or_create(
                id=guild.id,
                defaults={
                    "name": guild.name,
                },
            )

            live_role_ids = [role.id for role in guild.roles]

            for role in guild.roles:
                await DiscordGuildRole.objects.aupdate_or_create(
                    id=role.id,
                    defaults={
                        "guild_id": guild_row.id,
                        "name": role.name,
                        "is_active": True,
                        "position": role.position,
                    },
                )

            # Once per guild, not once per role: nested in the loop above
            # this ran the same UPDATE for every role the guild has.
            await DiscordGuildRole.objects.filter(
                guild_id=guild_row.id,
                is_active=True,
            ).exclude(
                id__in=live_role_ids,
            ).aupdate(is_active=False)

    async def reconcile_active_polls(self):
        profile_cache = await aget_discord_profile_cache()

        synced_count = 0

        async for match_msg in (
            ActiveMatchMessage.objects.filter(is_poll_finalized=False)
            .select_related("match", "match__stage")
            .aiterator()
        ):
            written = await sync_predictions_from_poll(self.bot, match_msg, profile_cache)
            if written > 0:
                synced_count += written

        logger.info(f"Reconciliation complete. Successfully synchronized {synced_count} live votes.")
