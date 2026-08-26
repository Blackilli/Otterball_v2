"""Components V2 layouts for the per-match status message.

Components V2 replaces `content` and `embeds` with a component tree, which buys
three things the embed version could not have: a thumbnail sitting beside the
text it belongs to rather than floating top-right, real divider rules, and an
interactive Mute button next to the reminder that would otherwise have to tell
people to go and type a slash command.

Constraints that shaped what is here, from Discord's component reference:

- A message carrying these components sets the `IS_COMPONENTS_V2` flag, and
  "once the message has been sent, the flag cannot be removed". Every state of
  the status message therefore has to be V2, which is fine - it is one message
  edited in place.
- Mentions still notify: "pingable mentions (@user, @role, etc) present in this
  component will ping and send notifications based on the value of the allowed
  mention object". So the pre-kickoff reminder keeps working, and still honours
  the AllowedMentions the cog passes.
- **A message cannot carry both these components and a poll.** That is why only
  the status message moved; the poll message itself stays a classic
  content-plus-poll message and must never be converted.
- A message allows up to 40 components in total and 4000 characters across all
  text displays, and a Section takes one to three children plus its accessory.
  The layouts below sit far under those, but the mention and winner lists are
  the parts that could grow, so the cog truncates them before they get here.
"""

import re
from dataclasses import dataclass
from typing import Sequence

import discord
from discord import ui

from discord_bot.services import aset_missing_vote_reminders

MUTE_BUTTON_TEMPLATE = r"otterball:mute:(?P<pool_id>\d+)"


class MuteRemindersButton(ui.DynamicItem[ui.Button], template=MUTE_BUTTON_TEMPLATE):
    """ "Mute reminders" on a pre-kickoff reminder, for the pool it belongs to.

    A DynamicItem rather than a stored view: the pool id rides in the custom_id
    and is parsed back out on click, so the button keeps working across bot
    restarts without anything having to be re-registered per message. The bot
    registers the class once (`add_dynamic_items`) in setup_hook.
    """

    def __init__(self, pool_id: int):
        self.pool_id = pool_id
        super().__init__(
            ui.Button(
                label="Mute reminders",
                emoji="🔕",
                style=discord.ButtonStyle.secondary,
                custom_id=f"otterball:mute:{pool_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match: re.Match[str]):
        return cls(int(match["pool_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await aset_missing_vote_reminders(interaction.user, self.pool_id, enabled=False)
        await interaction.response.send_message(
            "🔕 Muted. You will not be pinged about missing picks in this pool again.\n"
            "-# Turn it back on with `/notifications enabled:True`.",
            ephemeral=True,
        )


# Discord has no text alignment, so a score is positioned with padding or not
# at all. U+2007 is defined as the width of a digit, which makes "two in" mean
# the same thing whatever face Discord renders; ordinary spaces get collapsed.
FIGURE_SPACE = "\N{FIGURE SPACE}"

# There is no half-figure-space character. U+2005 is a quarter em, which is the
# closest standard width to half a digit - it is what nudges a single-digit
# score onto the same axis as a two-digit one instead of leaving it a half
# character to the left.
HALF_DIGIT = "\N{FOUR-PER-EM SPACE}"

# How far the score sits in from the margin, in digit widths.
SCORE_INDENT = 2

# Stands in for a score that does not exist yet, so a match before kickoff has
# the same shape as one in progress. Never a zero: an unplayed game is not 0-0,
# and rendering it as one would read as a result.
NO_SCORE = "\N{EN DASH}"


def score_line(score: int | str, widest: int) -> str:
    """The score as its own heading line, indented and centred against `widest`.

    `widest` is the digit count of the longest score in the match, so 7 next to
    24 gets an extra half-digit and the two numbers share a centre line.
    """
    shortfall = max(widest - len(str(score)), 0)
    pad = FIGURE_SPACE * (SCORE_INDENT + shortfall // 2)
    if shortfall % 2:
        pad += HALF_DIGIT
    return f"# {pad}{score}"


@dataclass(frozen=True)
class TeamRow:
    """One side of the scoreboard: a badge, a name, and maybe a score.

    Rendered as two lines inside one Section - the name as a heading and the
    score beneath it at the largest size Discord offers - with the badge
    alongside, spanning both.

    Discord offers no way to tint or dim an image, so anything the layout wants
    to say about who is ahead has to be said in the name line; here, bold.
    """

    name: str
    logo_url: str | None = None
    #: None renders as a dash, so every state has the same two-line shape.
    score: int | str | None = None
    widest_score: int = 1
    marker: str = ""
    ahead: bool = False

    @property
    def lines(self) -> list[str]:
        name = f"**{self.name}**" if self.ahead else self.name
        if self.marker:
            name = f"{name} {self.marker}"
        score = NO_SCORE if self.score is None else self.score
        return [f"## {name}", score_line(score, self.widest_score)]


class MatchStatusView(ui.LayoutView):
    """The one status message per poll, in whichever state it currently holds.

    Takes finished strings rather than a Match: the cog owns the wording and the
    truncation, this owns the layout.
    """

    def __init__(
        self,
        *,
        heading: str,
        footer: str,
        accent: discord.Colour,
        teams: "Sequence[TeamRow]" = (),
        body: str | None = None,
        detail: str | None = None,
        mentions: str | None = None,
        mute_pool_id: int | None = None,
    ):
        # No timeout: the message outlives any view lifetime, and the button is
        # dispatched by its dynamic template rather than by this instance.
        super().__init__(timeout=None)

        container = ui.Container(accent_colour=accent)
        container.add_item(ui.TextDisplay(heading))
        # Rule under the status line on every state, so the heading reads as a
        # header rather than as the first line of the body.
        container.add_item(ui.Separator())

        if body:
            container.add_item(ui.TextDisplay(body))

        # One Section per team, because a Section carries exactly one accessory
        # and both badges have to show.
        for team in teams:
            lines = [ui.TextDisplay(text) for text in team.lines]
            if team.logo_url:
                container.add_item(ui.Section(*lines, accessory=ui.Thumbnail(team.logo_url)))
            else:
                # A Section requires an accessory, so a team with no badge gets
                # plain text displays instead of an empty one.
                for line in lines:
                    container.add_item(line)

        if mentions:
            container.add_item(ui.Separator())
            if mute_pool_id is not None:
                # The button as the section's accessory puts the way out right
                # next to the ping it is a way out of.
                container.add_item(
                    ui.Section(
                        ui.TextDisplay(mentions),
                        accessory=MuteRemindersButton(mute_pool_id),
                    )
                )
            else:
                container.add_item(ui.TextDisplay(mentions))

        if detail:
            container.add_item(ui.Separator())
            container.add_item(ui.TextDisplay(detail))

        container.add_item(ui.Separator())
        container.add_item(ui.TextDisplay(f"-# {footer}"))

        self.add_item(container)
