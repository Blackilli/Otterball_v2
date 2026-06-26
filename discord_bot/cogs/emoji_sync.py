import asyncio
import logging
import unicodedata

import discord
from discord.ext import commands

from discord_bot.models import DiscordTeamEmoji
from sports.models import Team

logger = logging.getLogger(__name__)


def text_to_ascii(text):
    # Normalize the string to separate base characters from diacritics
    normalized = unicodedata.normalize("NFKD", text)
    # Encode to ASCII, ignoring characters that can't be converted, then decode back to a string
    return normalized.encode("ascii", "ignore").decode("ascii")


def _get_emoji_name(team: Team) -> str:
    emoji_name = text_to_ascii(team.name)
    emoji_name = "".join(c if c.isalnum() else "_" for c in emoji_name)
    emoji_name = emoji_name.strip("_")[:32]
    return emoji_name


class EmojiSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sync_in_progress = False

    async def register_team_application_emoji(self, team: Team) -> discord.Emoji | None:
        if not team.logo:
            return None

        emoji_name = _get_emoji_name(team)
        try:

            def read_image_bytes():
                with team.logo.open("rb") as logo_file:
                    return logo_file.read()

            image_bytes = await asyncio.to_thread(read_image_bytes)

            application_emoji = await self.bot.create_application_emoji(name=emoji_name, image=image_bytes)

            logger.info(f"✅ Application emoji created: {application_emoji} (ID: {application_emoji.id})")
            return application_emoji

        except discord.HTTPException as err:
            logger.error(f"❌ Discord API registration failed for Team {team.id} with emoji_name {emoji_name}: {err}")
            return None
        except Exception as err:
            logger.error(f"❌ Unexpected filesystem or context error: {err}")
            return None

    @commands.Cog.listener()
    async def on_ready(self):
        if self._sync_in_progress:
            logger.info("Emoji sync already in progress, skipping.")
            return
        logger.info("Emoji sync started.")

        try:
            discord_emojis = await self.bot.fetch_application_emojis()
            existing_emoji_names = {emoji.name for emoji in discord_emojis}

            synced_team_ids = {
                entry["team_id"] async for entry in DiscordTeamEmoji.objects.values("team_id").aiterator()
            }

            async for team in Team.objects.filter(logo__isnull=False).aiterator():
                if team.id in synced_team_ids:
                    continue

                emoji_name = _get_emoji_name(team)
                if emoji_name in existing_emoji_names:
                    logger.debug(f"Emoji {emoji_name} already exists, skipping.")
                    continue

                emoji = await self.register_team_application_emoji(team)
                if emoji:
                    try:
                        await DiscordTeamEmoji.objects.acreate(
                            team=team,
                            id=emoji.id,
                            name=emoji.name,
                        )
                        synced_team_ids.add(team.id)
                        existing_emoji_names.add(emoji.name)
                    except Exception as e:
                        logger.error(f"Error saving emoji for team {team.id}: {e}")
                        await emoji.delete()
                        continue
            logger.info("Emoji sync completed.")
        except Exception as e:
            logger.error(f"Emoji sync failed: {e}")
        finally:
            self._sync_in_progress = False
