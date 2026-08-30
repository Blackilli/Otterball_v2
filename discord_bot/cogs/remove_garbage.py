import datetime
import logging

import discord
from discord.ext import commands
from django.db.models import Q
from django.utils import timezone

from discord_bot.models import ActiveMatchMessage, DiscordGuildPool

logger = logging.getLogger(__name__)


class RemoveGarbageCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cleanup_running = False

    # Discord posts a system message both when a poll ends and when a message is
    # pinned. Polls now live in the pool channel and every one of them is pinned
    # at creation and unpinned at kickoff, so leaving these in place would bury
    # the channel in "X pinned a message" noise.
    GARBAGE_MESSAGE_TYPES = (
        discord.MessageType.poll_result,
        discord.MessageType.pins_add,
    )

    def _is_garbage(self, message: discord.Message) -> bool:
        """Should this system message be cleaned up?

        Discord authors a pin notice as *whoever pinned the message*, which is
        what makes the check possible: the bot pins a poll at creation and
        unpins it at kickoff, and cleaning up after itself is the whole point -
        a moderator pinning something in the same channel gets to keep their
        notice.
        """
        if message.type not in self.GARBAGE_MESSAGE_TYPES:
            return False

        if message.type is discord.MessageType.pins_add:
            return self.bot.user is not None and message.author.id == self.bot.user.id

        return True

    async def _is_managed_channel(self, channel_id: int) -> bool:
        """Is this a channel the bot posts polls in?

        Worth one query: pin notices are ordinary traffic everywhere else in a
        server, and deleting them outside the pool's own channels would be the
        bot silently moderating rooms it was never pointed at.
        """
        if await DiscordGuildPool.objects.filter(channel_id=channel_id, is_active=True).aexists():
            return True
        return await ActiveMatchMessage.objects.filter(Q(thread_id=channel_id) | Q(channel_id=channel_id)).aexists()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self._is_garbage(message):
            return

        if not await self._is_managed_channel(message.channel.id):
            return

        try:
            await message.delete()
            logger.debug(f"Intercepted and deleted a {message.type} system message in {message.channel.id}")
        except discord.Forbidden:
            logger.warning(f"Missing permissions to delete garbage message in channel {message.channel.id}")
        except discord.DiscordException as e:
            logger.error(f"Failed to delete live garbage message: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        if self.cleanup_running:
            logger.info("Garbage cleanup already running or executed, skipping secondary trigger.")
            return

        self.cleanup_running = True
        logger.info("Garbage removal historical sweep started.")

        two_weeks_ago = timezone.now() - datetime.timedelta(days=13, hours=23)

        # One sweep per place, not per poll: with the polls posted straight into
        # the pool channel, iterating ActiveMatchMessage would re-read the same
        # channel history once per match of the season.
        # Not `.aiterator()`: values_list(flat=False) is the one iterable whose
        # __iter__ *returns* the compiler's result iterator instead of yielding
        # from it, so aiterator() builds it on the event loop, asks for a
        # chunked cursor there, and Django raises SynchronousOnlyOperation.
        # Plain `async for` goes through __aiter__, which fetches in a thread.
        container_ids = {
            thread_id or channel_id
            async for thread_id, channel_id in ActiveMatchMessage.objects.values_list("thread_id", "channel_id")
        }
        # A pool channel that has not posted a poll yet still collects pin
        # notices from the leaderboard message.
        container_ids |= {
            channel_id
            async for channel_id in DiscordGuildPool.objects.filter(
                is_active=True,
                channel_id__isnull=False,
            ).values_list("channel_id", flat=True)
        }

        for container_id in container_ids:
            try:
                container = self.bot.get_channel(container_id)
                if not container:
                    try:
                        container = await self.bot.fetch_channel(container_id)
                    except discord.NotFound:
                        continue

                # Both expose history()/delete_messages(); a plain Messageable does not.
                if not isinstance(container, (discord.TextChannel, discord.Thread)):
                    continue

                messages_to_bulk_delete = []
                single_delete_fallback = []

                async for message in container.history(limit=100):
                    if self._is_garbage(message):
                        if message.created_at > two_weeks_ago:
                            messages_to_bulk_delete.append(message)
                        else:
                            single_delete_fallback.append(message)

                if messages_to_bulk_delete:
                    for i in range(0, len(messages_to_bulk_delete), 100):
                        chunk = messages_to_bulk_delete[i : i + 100]
                        try:
                            await container.delete_messages(chunk, reason="Garbage cleanup")
                            logger.info(f"Bulk-deleted {len(chunk)} system messages in {container_id}")
                        except discord.Forbidden:
                            logger.warning(f"Missing Manage Messages permission in {container_id}")
                            break

                for old_message in single_delete_fallback:
                    try:
                        await old_message.delete()
                        logger.info(f"Single-deleted ancient system message {old_message.id}")
                    except discord.DiscordException:
                        pass

            except discord.Forbidden:
                logger.warning(f"Bot lacks permissions to read history in {container_id}")
            except Exception as e:
                logger.error(f"Error processing historical cleanup for {container_id}: {e}")

        logger.info("Garbage removal historical sweep completed.")
        self.cleanup_running = False
