import logging

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
        self.leaderboards: dict[int, list[tuple[User, int]]] = {}

    @commands.Cog.listener()
    async def on_ready(self):
        async for db_guild_pool in DiscordGuildPool.objects.filter(is_active=True).select_related(
            "pool", "pool__season"
        ):
            if db_guild_pool.leaderboard_msg:
                await self.update_leaderboard_msg(db_guild_pool)
                continue
            guild = self.bot.get_guild(db_guild_pool.guild_id)
            if not guild:
                guild = await self.bot.fetch_guild(db_guild_pool.guild_id)
                if not guild:
                    logger.warning(f"Guild {db_guild_pool.guild_id} not found, skipping.")
                    continue
            channel = guild.get_channel(db_guild_pool.channel_id)
            if not channel:
                channel = await guild.fetch_channel(db_guild_pool.channel_id)
                if not isinstance(channel, discord.abc.Messageable):
                    logger.warning(f"Channel {db_guild_pool.channel_id} not found, skipping.")
                    continue

            msg = await channel.send("Leaderboard\n-# soon™")
            if msg:
                logger.info(f"Leaderboard message sent: {msg.id}")
                db_guild_pool.leaderboard_msg = msg.id
                await db_guild_pool.asave()
                await msg.pin()

    async def update_leaderboard_msg(self, guild_pool: DiscordGuildPool):
        user_cache = {
            profile.user_id: profile async for profile in DiscordProfile.objects.filter(user__is_active=True)
        }
        if not guild_pool.leaderboard_msg:
            return
        guild = self.bot.get_guild(guild_pool.guild_id)
        if not guild:
            guild = await self.bot.fetch_guild(guild_pool.guild_id)
            if not guild:
                logger.warning(f"Guild {guild_pool.guild_id} not found, skipping.")
        channel = guild.get_channel(guild_pool.channel_id)
        if not channel:
            channel = await guild.fetch_channel(guild_pool.channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                logger.warning(f"Channel {guild_pool.channel_id} not found, skipping.")
        msg = await channel.fetch_message(guild_pool.leaderboard_msg)

        if not msg:
            logger.warning(f"Leaderboard message {guild_pool.leaderboard_msg} not found, skipping.")
        if not msg.pinned:
            await msg.pin()

        leaderboard_embed = discord.Embed(
            title="**Leaderboard**",
            color=discord.Color.blurple(),
            timestamp=timezone.now(),
        )

        last_points = None
        rank = 1
        counter = 0
        last_displayed_rank = 0

        async for user, points in guild_pool.pool.aget_user_with_points():
            profile = user_cache.get(user.id)
            if not profile:
                logger.warning(f"User {user.id} not found in cache, skipping.")
                continue

            counter += 1

            if last_points is None or last_points != points:
                rank = counter

            last_points = points
            field_value = f"**{profile.global_name}** ({points})"

            if last_displayed_rank < 10 and rank > last_displayed_rank + 1:
                for r in range(last_displayed_rank + 1, min(rank, 11)):
                    leaderboard_embed.add_field(name=f"{r}.", value=" ----- ", inline=True)
                last_displayed_rank = rank

            if rank <= 10:
                if len(leaderboard_embed.fields) > 0 and leaderboard_embed.fields[-1].name == f"{rank}.":
                    current_val = leaderboard_embed.fields[-1].value
                    if len(current_val) + len(field_value) < 1000:
                        leaderboard_embed.set_field_at(
                            len(leaderboard_embed.fields) - 1,
                            name=f"{rank}.",
                            value=f"{current_val}\n{field_value}",
                            inline=True,
                        )
                        last_displayed_rank = rank
                        continue
                leaderboard_embed.add_field(
                    name=f"{rank}.",
                    inline=True,
                    value=field_value,
                )
                last_displayed_rank = rank
            else:
                field_value = f"{rank}.: {field_value}"
                if len(leaderboard_embed.fields) > 0 and leaderboard_embed.fields[-1].name == "Plebs":
                    current_val = leaderboard_embed.fields[-1].value

                    if len(current_val) + len(field_value) < 1000:
                        leaderboard_embed.set_field_at(
                            len(leaderboard_embed.fields) - 1,
                            name="Plebs",
                            value=f"{current_val}\n{field_value}",
                            inline=False,
                        )
                        continue
                leaderboard_embed.add_field(
                    name="Plebs",
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
            point_distribution_embed.add_field(
                name=f"{stage_rule.stage.name}",
                value=f"**{stage_rule.points_per_correct}** points per correct answer",
            )
        logger.info(f"leaderboard: {leaderboard_embed.to_dict()}")
        logger.info(f"point distribution: {point_distribution_embed.to_dict()}")
        await msg.edit(content="", embeds=[leaderboard_embed, point_distribution_embed])

        # await msg.edit(content=content)

    @tasks.loop(seconds=30)
    async def leaderboard_sync_loop(self):
        prediction_cache = Prediction.objects.filter()
        async for db_guild_pool in DiscordGuildPool.objects.filter(is_active=True):
            leaderboard: list[tuple[User, int]] = []
