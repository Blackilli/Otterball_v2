import datetime
import logging

import discord
from discord.ext import commands
from django.utils import timezone

from discord_bot.models import ActiveMatchMessage

logger = logging.getLogger(__name__)


class RemoveGarbageCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cleanup_running = False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.type != discord.MessageType.poll_result:
            return

        try:
            await message.delete()
            logger.debug(
                f"Intercepted and deleted live poll result in thread {message.channel.id}"
            )
        except discord.Forbidden:
            logger.warning(
                f"Missing permissions to delete garbage message in channel {message.channel.id}"
            )
        except discord.DiscordException as e:
            logger.error(f"Failed to delete live garbage message: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        if self.cleanup_running:
            logger.info(
                "Garbage cleanup already running or executed, skipping secondary trigger."
            )
            return

        self.cleanup_running = True
        logger.info("Garbage removal historical sweep started.")

        two_weeks_ago = timezone.now() - datetime.timedelta(days=13, hours=23)

        async for match_msg in ActiveMatchMessage.objects.aiterator():
            try:
                thread = self.bot.get_channel(match_msg.thread_id)
                if not thread:
                    try:
                        thread = await self.bot.fetch_channel(match_msg.thread_id)
                    except discord.NotFound:
                        continue

                if not isinstance(thread, discord.Thread):
                    continue

                messages_to_bulk_delete = []
                single_delete_fallback = []

                async for message in thread.history(limit=100):
                    if message.type == discord.MessageType.poll_result:
                        if message.created_at > two_weeks_ago:
                            messages_to_bulk_delete.append(message)
                        else:
                            single_delete_fallback.append(message)

                if messages_to_bulk_delete:
                    for i in range(0, len(messages_to_bulk_delete), 100):
                        chunk = messages_to_bulk_delete[i : i + 100]
                        try:
                            await thread.delete_messages(
                                chunk, reason="Garbage cleanup"
                            )
                            logger.info(
                                f"Bulk-deleted {len(chunk)} poll results in thread {thread.id}"
                            )
                        except discord.Forbidden:
                            logger.warning(
                                f"Missing Manage Messages permission in thread {thread.id}"
                            )
                            break

                for old_message in single_delete_fallback:
                    try:
                        await old_message.delete()
                        logger.info(
                            f"Single-deleted ancient poll result {old_message.id}"
                        )
                    except discord.DiscordException:
                        pass

            except discord.Forbidden:
                logger.warning(
                    f"Bot lacks permissions to read history in thread {match_msg.thread_id}"
                )
            except Exception as e:
                logger.error(
                    f"Error processing historical cleanup for thread {match_msg.thread_id}: {e}"
                )

        logger.info("Garbage removal historical sweep completed.")
        self.cleanup_running = False
