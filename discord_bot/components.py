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
from typing import Callable, Sequence

import discord
from discord import ui

from discord_bot.services import aget_missing_vote_reminders, aset_missing_vote_reminders
from predictions.models import PredictionPool

NOTIFICATION_BUTTON_TEMPLATE = r"otterball:notifications:(?P<pool_id>\d+)"


class NotificationSettingsModal(ui.Modal, title="Notification settings"):
    """The reminder's opt-out, as a form that shows the setting it is changing.

    A button alone can only *act*: it cannot say whether reminders are
    currently on, and a plain "Mute" is a decision nobody can take back from a
    channel they are not allowed to write in. The modal opens with the
    checkbox already reflecting the stored preference, so reading the state and
    changing it are the same gesture.

    Built fresh per click rather than registered as a persistent view - the
    button that opens it is what has to survive a restart, and it does.
    """

    def __init__(self, pool_id: int, pool_name: str, *, enabled: bool):
        super().__init__()
        self.pool_id = pool_id
        # Discord caps a label at 45 characters and its description at 100,
        # and rejects the whole modal rather than trimming for you.
        self.reminders = ui.Label(
            text="Remind me before kickoff"[:45],
            description=f"Ping me about missing picks in {pool_name}."[:100],
            component=ui.Checkbox(default=enabled),
        )
        self.add_item(self.reminders)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        enabled = self.reminders.component.value
        await aset_missing_vote_reminders(interaction.user, self.pool_id, enabled=enabled)
        await interaction.response.send_message(
            (
                "🔔 Reminders are **on**. You will be pinged before kickoff when you have no pick."
                if enabled
                else "🔕 Reminders are **off**. You will not be pinged about missing picks in this pool."
            ),
            ephemeral=True,
        )


class NotificationSettingsButton(ui.DynamicItem[ui.Button], template=NOTIFICATION_BUTTON_TEMPLATE):
    """ "Notification settings" on a pre-kickoff reminder, for its own pool.

    A DynamicItem rather than a stored view: the pool id rides in the custom_id
    and is parsed back out on click, so the button keeps working across bot
    restarts without anything having to be re-registered per message. The bot
    registers the class once (`add_dynamic_items`) in setup_hook.

    It exists because a pool channel is read-only for the people playing in it:
    this button is the only notification control they can reach, which is why
    it opens a form they can set either way rather than performing a one-way
    mute.
    """

    def __init__(self, pool_id: int):
        self.pool_id = pool_id
        super().__init__(
            ui.Button(
                label="Notification settings",
                emoji="🔔",
                style=discord.ButtonStyle.secondary,
                custom_id=f"otterball:notifications:{pool_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match: re.Match[str]):
        return cls(int(match["pool_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        # Read before opening: the checkbox has to arrive already showing the
        # stored setting, and send_modal must be the response to this click.
        enabled = await aget_missing_vote_reminders(interaction.user, self.pool_id)
        pool_name = await PredictionPool.objects.filter(pk=self.pool_id).values_list("name", flat=True).afirst()
        await interaction.response.send_modal(
            NotificationSettingsModal(self.pool_id, pool_name or "this pool", enabled=enabled)
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


class WelcomeView(ui.LayoutView):
    """The season opener, as one card the pool's own settings fill in.

    Components V2 rather than an embed for one reason that is not cosmetic: a
    V2 message can carry a button, so the notification opt-out sits in the
    welcome itself instead of only on a reminder nobody sees until they are
    already being pinged - and the poll channel is read-only, so this is the
    one message a player can act on.

    Takes finished strings, like MatchStatusView: the cog owns the wording and
    reads every value off PoolConfiguration, this owns the layout.
    """

    def __init__(
        self,
        *,
        heading: str,
        subheading: str,
        steps: "Sequence[tuple[str, str]]",
        footer: str,
        mention: str | None = None,
        settings_pool_id: int | None = None,
        settings_step: int | None = None,
    ):
        # No timeout, for the same reason MatchStatusView has none: the message
        # outlives any view instance and the button is dispatched by its
        # dynamic template.
        super().__init__(timeout=None)

        container = ui.Container(accent_colour=discord.Color.blurple())
        container.add_item(ui.TextDisplay(heading))
        if mention:
            # A mention inside a V2 component still notifies, subject to the
            # AllowedMentions the cog passes - which is how this post reaches
            # the role at all now that it has no `content`.
            container.add_item(ui.TextDisplay(mention))
        container.add_item(ui.TextDisplay(subheading))
        container.add_item(ui.Separator())

        for index, (title, body) in enumerate(steps):
            text = ui.TextDisplay(f"**{title}**\n{body}")
            if settings_pool_id is not None and index == settings_step:
                # Beside the step that explains the ping, so the control and
                # what it controls are one thing to read.
                container.add_item(
                    ui.Section(text, accessory=NotificationSettingsButton(settings_pool_id)),
                )
            else:
                container.add_item(text)

        container.add_item(ui.Separator())
        container.add_item(ui.TextDisplay(f"-# {footer}"))
        self.add_item(container)


@dataclass(frozen=True)
class InteractiveComponent:
    """One clickable thing the bot puts in a channel, and what it does.

    The gallery message is the only way to try a **modal**: Discord opens one
    in response to an interaction and never on its own, so a modal cannot be
    posted - it can only be reached through the component that opens it.

    Kept as a list rather than hand-written into the gallery so a new button
    is one entry here and is previewable the moment it exists.
    """

    title: str
    description: str
    #: pool id -> the item itself. The real one: these are dispatched by their
    #: dynamic templates, so a gallery button behaves exactly like the one on a
    #: reminder, including writing to the database.
    build: "Callable[[int], ui.Item]"


INTERACTIVE_COMPONENTS: "tuple[InteractiveComponent, ...]" = (
    InteractiveComponent(
        title="Notification settings",
        description=(
            "Opens the notification-settings modal for this pool. Its checkbox arrives showing your real "
            "setting, and submitting it really saves - so this is a live control, not a mock-up."
        ),
        build=NotificationSettingsButton,
    ),
)


class ComponentGalleryView(ui.LayoutView):
    """Every interactive component the bot posts, in one message you can click.

    Its whole purpose is that the components are real. A gallery of look-alikes
    would prove that the layout renders and nothing about whether the thing
    actually works - which is the half that breaks, since a button is dispatched
    by a custom_id template that no rendering test can exercise.
    """

    def __init__(self, pool_id: int, pool_name: str):
        super().__init__(timeout=None)

        container = ui.Container(accent_colour=discord.Color.blurple())
        container.add_item(ui.TextDisplay("### 🧪 Buttons & modals"))
        container.add_item(ui.TextDisplay(f"-# Every control below belongs to **{pool_name}** and is the real one."))
        container.add_item(ui.Separator())

        for entry in INTERACTIVE_COMPONENTS:
            container.add_item(
                ui.Section(
                    ui.TextDisplay(f"**{entry.title}**\n{entry.description}"),
                    accessory=entry.build(pool_id),
                )
            )

        container.add_item(ui.Separator())
        container.add_item(
            ui.TextDisplay("-# A modal only opens from an interaction, so this message is how one is reached at all.")
        )
        self.add_item(container)


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
                        accessory=NotificationSettingsButton(mute_pool_id),
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
