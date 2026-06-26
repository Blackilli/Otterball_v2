import logging
from typing import Any

import discord
from discord.ext import commands, tasks
from django.utils import timezone

from discord_bot.models import DiscordGuildPool, DiscordProfile
from predictions.models import PoolStageRule, Prediction
from users.models import User

logger = logging.getLogger(__name__)


class LeaderboardSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.leaderboards: dict[int, tuple[[tuple[Any, ...], tuple[Any, ...]]]] = {}
        self.leaderboard_sync_loop.start()
        self.counter = 0

    def cog_unload(self) -> None:
        self.leaderboard_sync_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        async for db_guild_pool in DiscordGuildPool.objects.filter(is_active=True).select_related(
            "pool", "pool__season"
        ):
            if db_guild_pool.leaderboard_msg:
                await self.update_leaderboard_msg(db_guild_pool)
                continue
            try:
                guild = self.bot.get_guild(db_guild_pool.guild_id) or await self.bot.fetch_guild(
                    db_guild_pool.guild_id
                )
                channel = self.bot.get_channel(db_guild_pool.channel_id) or await guild.fetch_channel(
                    db_guild_pool.channel_id
                )
            except discord.NotFound:
                logger.warning(
                    f"Guild {db_guild_pool.guild_id} or Channel {db_guild_pool.channel_id} not found during initialization."
                )
                continue

            if not isinstance(channel, discord.abc.Messageable):
                continue

            msg = await channel.send("Leaderboard\n-# soon™")
            if msg:
                logger.info(f"Leaderboard message sent: {msg.id}")
                db_guild_pool.leaderboard_msg = msg.id
                await db_guild_pool.asave()
                await msg.pin()

    async def update_leaderboard_msg(self, guild_pool: DiscordGuildPool, force: bool = False):
        if not guild_pool.leaderboard_msg:
            return

        user_cache = {
            profile.user_id: profile
            async for profile in DiscordProfile.objects.filter(
                user__is_active=True,
                user__predictions__pool_id=guild_pool.pool_id,
            ).distinct()
        }

        raw_leaderboard_data = []
        async for rank, user, points in guild_pool.pool.aget_leaderboard():
            profile = user_cache.get(user.id)
            if profile:
                raw_leaderboard_data.append((rank, profile.global_name, points))

        raw_rules_data = []
        async for stage_rule in (
            PoolStageRule.objects.filter(pool_id=guild_pool.pool_id)
            .select_related("stage")
            .order_by("level")
            .aiterator()
        ):
            stage_name = stage_rule.stage.name if stage_rule.stage else "Global Fallback / Baseline"
            raw_rules_data.append((stage_name, stage_rule.points_per_correct))

        current_fingerprint = (tuple(raw_leaderboard_data), tuple(raw_rules_data))

        if self.leaderboards.get(guild_pool.id) == current_fingerprint and not force:
            logger.info(f"Leaderboard for Pool {guild_pool.id} unchanged, skipping update.")
            return

        try:
            guild = self.bot.get_guild(guild_pool.guild_id) or await self.bot.fetch_guild(guild_pool.guild_id)
            channel = self.bot.get_channel(guild_pool.channel_id) or await guild.fetch_channel(guild_pool.channel_id)
            msg = await channel.fetch_message(guild_pool.leaderboard_msg)
        except discord.NotFound:
            logger.warning(f"Leaderboard infrastructure component missing for Pool {guild_pool.id}, skipping update.")
            return

        if not msg.pinned:
            try:
                await msg.pin()
            except discord.HTTPException:
                logger.warning(f"Failed to pin leaderboard message {msg.id}")

        leaderboard_embed = discord.Embed(
            title="**Leaderboard**",
            color=discord.Color.blurple(),
            timestamp=timezone.now(),
        )
        last_displayed_rank = 0

        async for rank, user, points in guild_pool.pool.aget_leaderboard():
            profile = user_cache.get(user.id)
            if not profile:
                logger.warning(f"User {user.id} not found in cache, skipping.")
                continue

            field_value = f"**{profile.global_name}** ({points})"

            rank_header = "———`{rank}`———"

            if last_displayed_rank < 10 and rank > last_displayed_rank + 1:
                for r in range(last_displayed_rank + 1, min(rank, 11)):
                    field_name = rank_header.format(rank=r)
                    leaderboard_embed.add_field(name=field_name, value="  ", inline=True)
                last_displayed_rank = rank

            if rank <= 10:
                field_name = rank_header.format(rank=rank)
                if len(leaderboard_embed.fields) > 0 and leaderboard_embed.fields[-1].name == field_name:
                    current_val = leaderboard_embed.fields[-1].value
                    if len(current_val) + len(field_value) < 1000:
                        leaderboard_embed.set_field_at(
                            len(leaderboard_embed.fields) - 1,
                            name=field_name,
                            value=f"{current_val}\n{field_value}",
                            inline=True,
                        )
                        last_displayed_rank = rank
                        continue

                leaderboard_embed.add_field(
                    name=field_name,
                    inline=True,
                    value=field_value,
                )
                last_displayed_rank = rank
            else:
                field_value = f"`#{rank}` {field_value}"
                field_name = rank_header.format(rank="Plebs")
                if len(leaderboard_embed.fields) > 0 and leaderboard_embed.fields[-1].name == field_name:
                    current_val = leaderboard_embed.fields[-1].value

                    if len(current_val) + len(field_value) < 1000:
                        leaderboard_embed.set_field_at(
                            len(leaderboard_embed.fields) - 1,
                            name=field_name,
                            value=f"{current_val}\n{field_value}",
                            inline=False,
                        )
                        continue
                leaderboard_embed.add_field(
                    name=field_name,
                    inline=False,
                    value=field_value,
                )

        leaderboard_embed.set_footer(text=f"Last updated")

        point_distribution_embed = discord.Embed(title="Point Distribution")

        async for stage_rule in (
            PoolStageRule.objects.filter(pool_id=guild_pool.pool_id)
            .select_related("stage")
            .order_by("level")
            .aiterator()
        ):
            stage_name = stage_rule.stage.name if stage_rule.stage else "Global Fallback / Baseline"
            point_distribution_embed.add_field(
                name=stage_name,
                value=f"**{stage_rule.points_per_correct}** points per correct answer",
            )
        logger.info(f"leaderboard: {leaderboard_embed.to_dict()}")
        logger.info(f"point distribution: {point_distribution_embed.to_dict()}")
        await msg.edit(content="", embeds=[leaderboard_embed, point_distribution_embed])

        self.leaderboards[guild_pool.id] = current_fingerprint
        logger.info(f"Leaderboard for Pool {guild_pool.id} updated.")

    @tasks.loop(seconds=30)
    async def leaderboard_sync_loop(self):
        async for db_guild_pool in (
            DiscordGuildPool.objects.filter(is_active=True).select_related("pool", "pool__configuration").aiterator()
        ):
            try:
                await self.update_leaderboard_msg(db_guild_pool, force=self.counter == 0)
                self.counter = (self.counter + 1) % 10
                logger.info(
                    f"Background database sync loop for GuildPool {db_guild_pool.id} completed. Counter: {self.counter}"
                )
            except Exception as e:
                logger.error(f"Failed background database sync loop for GuildPool {db_guild_pool.id}: {e}")
