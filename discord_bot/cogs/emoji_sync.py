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

    async def register_team_application_emoji(self, team: Team) -> discord.Emoji | None:
        if not team.logo:
            return None

        emoji_name = _get_emoji_name(team)
        try:

            def read_image_bytes():
                with team.logo.open("rb") as logo_file:
                    return logo_file.read()

            image_bytes = await asyncio.to_thread(read_image_bytes)

            application_emoji = await self.bot.create_application_emoji(
                name=emoji_name, image=image_bytes
            )

            print(
                f"✅ Application emoji created: {application_emoji} (ID: {application_emoji.id})"
            )
            return application_emoji

        except discord.HTTPException as err:
            print(
                f"❌ Discord API registration failed for Team {team.id} with emoji_name {emoji_name}: {err}"
            )
            return None
        except Exception as err:
            print(f"❌ Unexpected filesystem or context error: {err}")
            return None

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Emoji sync started.")
        async for team in Team.objects.filter(logo__isnull=False).aiterator():
            if await DiscordTeamEmoji.objects.filter(team=team).aexists():
                continue
            if _get_emoji_name(team) in [
                emoji.name for emoji in await self.bot.fetch_application_emojis()
            ]:
                continue
            emoji = await self.register_team_application_emoji(team)
            if emoji:
                try:
                    await DiscordTeamEmoji.objects.acreate(
                        team=team,
                        id=emoji.id,
                        name=emoji.name,
                    )
                except Exception as e:
                    logger.error(f"Error saving emoji for team {team.id}: {e}")
                    await emoji.delete()
                    continue
        logger.info("Emoji sync completed.")
