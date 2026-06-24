import logging

import discord
from discord.ext import commands

from discord_bot.constants import DISCORD_POLL_MAP
from discord_bot.models import (
    ActiveMatchMessage,
    DiscordChannel,
    DiscordGuild,
    DiscordGuildRole,
    DiscordProfile,
)
from predictions.models import Prediction
from users.models import User

logger = logging.getLogger(__name__)


class ReconciliationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        logger = logging.getLogger(__name__)
        logger.info("Reconciliation check started.")
        await self.reconcile_roles()
        await self.reconcile_channels()
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

                await DiscordChannel.objects.filter(
                    guild_id=guild_row.id,
                    is_active=True,
                ).exclude(
                    id__in=live_role_ids,
                ).aupdate(is_active=False)

    async def reconcile_active_polls(self):
        discord_profile_cache = {
            profile.id: profile
            async for profile in DiscordProfile.objects.select_related("user")
            .filter(user__is_active=True)
            .aiterator()
        }

        predictions_to_sync = []

        async for match_msg in ActiveMatchMessage.objects.filter(is_poll_finalized=False).aiterator():
            try:
                channel = self.bot.get_channel(match_msg.channel_id)
                if not channel:
                    channel = await self.bot.fetch_channel(match_msg.channel_id)

                thread = channel.get_thread(match_msg.thread_id)
                if not thread:
                    logger.warning(f"Thread not found for match {match_msg.match_id}")
                    continue

                message = await thread.fetch_message(match_msg.poll_message_id)
                if not message.poll:
                    logger.warning(f"Poll not found for match {match_msg.match_id}")
                    continue

                for answer in message.poll.answers:
                    predicted_outcome = DISCORD_POLL_MAP.get(answer.id)
                    if not predicted_outcome:
                        logger.error(f"Invalid poll answer: {answer.id}")
                        continue

                    async for voter in answer.voters():
                        profile = discord_profile_cache.get(voter.id)
                        if not profile:
                            logger.info(f"Creating user for Discord ID: {voter.id} ({voter.name})")
                            user = await User.objects.acreate_user(username=voter.name, is_active=True)
                            profile = await DiscordProfile.objects.acreate(
                                user=user,
                                id=voter.id,
                                username=voter.name,
                                global_name=voter.global_name,
                            )
                            discord_profile_cache[profile.id] = profile
                            user_id = user.id
                        else:
                            user_id = profile.user_id

                        predictions_to_sync.append(
                            {
                                "pool_id": match_msg.pool_id,
                                "user_id": user_id,
                                "match_id": match_msg.match_id,
                                "predicted_outcome": predicted_outcome,
                            }
                        )
            except (discord.NotFound, discord.Forbidden) as e:
                logger.warning(
                    f"Skipping poll synchronization for match {match_msg.match_id} due to discord permissions: {e}"
                )
                continue

        synced_count = 0
        for pred_data in predictions_to_sync:
            await Prediction.objects.aupdate_or_create(
                pool_id=pred_data["pool_id"],
                user_id=pred_data["user_id"],
                match_id=pred_data["match_id"],
                defaults={"predicted_outcome": pred_data["predicted_outcome"]},
            )
            synced_count += 1

        logger.info(f"Reconciliation complete. Successfully synchronized {synced_count} live votes.")
