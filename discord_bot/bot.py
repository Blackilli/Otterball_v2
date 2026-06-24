import logging
import pathlib
from datetime import datetime

import discord
from discord.ext import commands, tasks
from django.utils import timezone

from discord_bot.cogs.emoji_sync import EmojiSyncCog

logger = logging.getLogger("discord_bot")


class OtterBallBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.polls = True
        intents.message_content = False
        self.heartbeat_file = pathlib.Path("/tmp/bot_heartbeat")

        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        self.bot_heartbeat_loop.start()
        logger.info("Initializing bot cogs...")
        from discord_bot.cogs import (
            ChannelSyncCog,
            LeaderboardSyncCog,
            PollCreationCog,
            PollPredictionCog,
            ReconciliationCog,
            RemoveGarbageCog,
            RoleSyncCog,
        )

        await self.add_cog(PollCreationCog(self))
        await self.add_cog(ChannelSyncCog(self))
        await self.add_cog(ReconciliationCog(self))
        await self.add_cog(PollPredictionCog(self))
        await self.add_cog(RoleSyncCog(self))
        # await self.add_cog(MatchTickerCog(self))
        await self.add_cog(EmojiSyncCog(self))
        await self.add_cog(LeaderboardSyncCog(self))
        await self.add_cog(RemoveGarbageCog(self))
        logger.info("Syncing application command tree...")
        await self.tree.sync()

    @tasks.loop(minutes=1.0)
    async def bot_heartbeat_loop(self):
        self.heartbeat_file.touch(exist_ok=True)

    async def on_ready(self):
        logger.info("Starting background database reconciliation check...")
        try:
            logger.info("Database reconciliation completed successfully.")
        except Exception as e:
            logger.error(f"Reconciliation error on startup: {e}", exc_info=True)
