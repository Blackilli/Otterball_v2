import logging
import pathlib

import discord
from discord.ext import commands, tasks

from discord_bot.cogs.emoji_sync import EmojiSyncCog

logger = logging.getLogger("discord_bot")


class OtterBallBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.polls = True
        intents.message_content = False
        # Privileged, and must also be enabled under "Server Members Intent" in
        # the Discord developer portal or the bot fails to connect. Without it
        # a role's membership is invisible, and the pre-kickoff reminder cannot
        # work out who has not voted yet.
        intents.members = True
        self.heartbeat_file = pathlib.Path("/tmp/bot_heartbeat")

        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        self.bot_heartbeat_loop.start()

        # The notification-settings button on a pre-kickoff reminder carries its
        # pool id in its custom_id, so registering the class once is what makes
        # every such button - including ones posted before the last restart -
        # dispatchable.
        from discord_bot.components import NotificationSettingsButton

        self.add_dynamic_items(NotificationSettingsButton)

        logger.info("Initializing bot cogs...")
        from discord_bot.cogs import (
            ChannelSyncCog,
            GuildSyncCog,
            LeaderboardSyncCog,
            MatchTickerCog,
            MessagePreviewCog,
            PollCreationCog,
            PollPredictionCog,
            PoolOnboardingCog,
            ReconciliationCog,
            RemoveGarbageCog,
            RoleSyncCog,
        )

        await self.add_cog(PollCreationCog(self))
        await self.add_cog(ChannelSyncCog(self))
        await self.add_cog(GuildSyncCog(self))
        await self.add_cog(ReconciliationCog(self))
        await self.add_cog(PollPredictionCog(self))
        await self.add_cog(RoleSyncCog(self))
        await self.add_cog(MatchTickerCog(self))
        await self.add_cog(EmojiSyncCog(self))
        await self.add_cog(LeaderboardSyncCog(self))
        await self.add_cog(PoolOnboardingCog(self))
        await self.add_cog(RemoveGarbageCog(self))
        await self.add_cog(MessagePreviewCog(self))
        logger.info("Syncing application command tree...")
        await self.tree.sync()

    @tasks.loop(minutes=1.0)
    async def bot_heartbeat_loop(self):
        self.heartbeat_file.touch(exist_ok=True)
