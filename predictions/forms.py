"""Forms for the guided pool setup page in the admin."""

import datetime

from django import forms
from django.db.models import Count
from django.utils import timezone

from discord_bot.models import (
    DiscordChannel,
    DiscordGuild,
    DiscordGuildPool,
    DiscordGuildRole,
    PreviewMessageKind,
)
from predictions.models import (
    DEFAULT_POLL_LOOKAHEAD_DAYS,
    DEFAULT_REMINDER_LEAD_MINUTES,
    MAX_POLL_LOOKAHEAD_DAYS,
    MAX_REMINDER_LEAD_MINUTES,
    DayOfWeek,
)
from sports.models import Match, Season


class SeasonChoiceField(forms.ModelChoiceField):
    """Seasons labelled with what has actually been ingested for them.

    A season with no stages cannot score and one with no upcoming matches has
    nothing to poll on - both are the usual reason a new pool does nothing, and
    both are invisible in a plain dropdown of season names.
    """

    def label_from_instance(self, season: Season) -> str:
        return f"{season.name} — {season.stage_count} stage(s), {season.match_count} match(es)"


class GuildChoiceField(forms.ModelChoiceField):
    """Guilds by name. DiscordGuild.__str__ carries the snowflake, which is the
    right thing in an admin list and noise in a picker."""

    def label_from_instance(self, guild) -> str:
        return guild.name


class GuildScopedSelect(forms.Select):
    """A picker whose options carry the guild they belong to.

    `pool_setup.js` filters the list off `data-guild` when a guild is chosen,
    which needs no round trip and no endpoint. With scripting off the full list
    is still there, still labelled with its guild, and `PoolSetupForm.clean`
    still rejects a mismatched pair - the filtering is a convenience, never the
    thing that keeps the data right.
    """

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, "instance", None)
        if instance is not None:
            option["attrs"]["data-guild"] = str(instance.guild_id)
        return option


class GuildScopedChoiceField(forms.ModelChoiceField):
    """A channel or role, labelled with the guild it belongs to.

    The guild name is on the option itself so the list is usable unfiltered -
    which is what a visitor without JavaScript gets.
    """

    widget = GuildScopedSelect

    def label_from_instance(self, obj) -> str:
        return f"{obj.guild.name} · {obj.name}"


class PoolSetupForm(forms.Form):
    """Everything `manage.py create_pool` takes, as one page.

    Points per round are deliberately absent: they depend on which season is
    picked, which is not known until this form is submitted. The pool's own
    admin page already edits them inline, and setup sends you there.
    """

    season = SeasonChoiceField(
        queryset=Season.objects.none(),
        help_text="The competition season this pool plays. Ingest the sport data first if it is missing.",
    )
    name = forms.CharField(
        max_length=255,
        help_text="Shown in Discord and on the public pages, e.g. 'NFL 2026'.",
    )

    poll_creation_weekdays = forms.MultipleChoiceField(
        choices=DayOfWeek.choices,
        widget=forms.CheckboxSelectMultiple,
        initial=[DayOfWeek.SUNDAY],
        help_text="Days the batch of polls is posted. Pick at least one, or polls never post.",
    )
    poll_creation_time = forms.TimeField(
        initial=datetime.time(18, 0),
        # Without an explicit format the initial renders as "18:00:00", which
        # reads like a precision the field does not have.
        widget=forms.TimeInput(format="%H:%M"),
        help_text="Time of day the batch posts, in the project timezone.",
    )
    poll_creation_lookahead_days = forms.IntegerField(
        initial=DEFAULT_POLL_LOOKAHEAD_DAYS,
        min_value=1,
        max_value=MAX_POLL_LOOKAHEAD_DAYS,
        help_text=(
            f"Days of matches in one batch (1-{MAX_POLL_LOOKAHEAD_DAYS}). The ceiling is Discord's "
            "own maximum poll duration, not a preference."
        ),
    )
    reminder_lead_minutes = forms.IntegerField(
        initial=DEFAULT_REMINDER_LEAD_MINUTES,
        min_value=0,
        max_value=MAX_REMINDER_LEAD_MINUTES,
        help_text="How long before kickoff to ping players who have not voted. 0 turns the reminder off.",
    )

    guild = GuildChoiceField(
        queryset=DiscordGuild.objects.order_by("name"),
        required=False,
        help_text="Leave blank to bind the pool to Discord later.",
    )
    channel = GuildScopedChoiceField(
        queryset=DiscordChannel.objects.select_related("guild").order_by("guild__name", "name"),
        required=False,
        help_text="Where polls, the status messages and the pinned leaderboard go.",
    )
    notification_role = GuildScopedChoiceField(
        queryset=DiscordGuildRole.objects.select_related("guild").order_by("guild__name", "name"),
        required=False,
        help_text="Pinged when a new batch of polls is posted. Its members are also who the reminder counts.",
    )
    announce_welcome = forms.BooleanField(
        required=False,
        initial=True,
        label="Announce the new season",
        help_text=(
            "The bot posts one message introducing the pool - schedule, points per round, how to vote - "
            "and pings the notification role. It is edited in place afterwards, so setting the points "
            "later corrects it without a second ping."
        ),
    )

    class Media:
        # Wrapped in DOMContentLoaded on its own, because form media renders
        # into <head> without defer.
        js = ("predictions/js/pool_setup.js",)

    #: (heading, note, field names) - the page renders the form in these
    #: groups so it reads as the three decisions it actually is, rather than as
    #: nine fields in a column. Kept here rather than in the template so a
    #: renamed field breaks loudly instead of quietly dropping out of the page.
    GROUPS = (
        ("The pool", "", ("season", "name")),
        (
            "When polls post",
            "The bot re-reads this every minute, so changes take effect without a restart.",
            ("poll_creation_weekdays", "poll_creation_time", "poll_creation_lookahead_days", "reminder_lead_minutes"),
        ),
        (
            "Where it posts",
            "Optional - a pool with no binding is created fine and simply never posts, so you can come "
            "back and bind it once the bot has seen your server.",
            ("guild", "channel", "notification_role", "announce_welcome"),
        ),
    )

    def groups(self):
        for heading, note, names in self.GROUPS:
            yield {"heading": heading, "note": note, "fields": [self[name] for name in names]}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["season"].queryset = (
            Season.objects.annotate(
                stage_count=Count("stages", distinct=True),
                match_count=Count("stages__matches", distinct=True),
            )
            .filter(stage_count__gt=0)
            .select_related("competition")
            .order_by("-year", "name")
        )

    def clean_poll_creation_weekdays(self) -> list[int]:
        """The widget hands back strings; the model field stores JSON ints."""
        return sorted(int(day) for day in self.cleaned_data.get("poll_creation_weekdays", []))

    def clean(self):
        cleaned = super().clean()
        guild = cleaned.get("guild")
        channel = cleaned.get("channel")
        role = cleaned.get("notification_role")

        if not guild:
            if channel or role:
                self.add_error("guild", "Pick the guild these belong to.")
            return cleaned

        # A binding with no channel passes every model constraint and then
        # never posts anything, which is the single most confusing way for a
        # new pool to fail. Ask for it up front.
        if not channel:
            self.add_error("channel", "A pool bound to a guild needs a channel, or it has nowhere to post.")
        elif channel.guild_id != guild.id:
            self.add_error("channel", f"That channel is in {channel.guild.name}, not {guild.name}.")

        if role and role.guild_id != guild.id:
            self.add_error("notification_role", f"That role is in {role.guild.name}, not {guild.name}.")

        return cleaned


class MatchChoiceField(forms.ModelChoiceField):
    """Fixtures labelled with kickoff and state, since that is what is picked.

    Which match you preview decides which messages make sense: only a finished
    one has a score to render at full time, and only an upcoming one has a
    poll that would really run.
    """

    def label_from_instance(self, match) -> str:
        kickoff = timezone.localtime(match.kickoff).strftime("%Y-%m-%d %H:%M")
        score = ""
        if match.home_score is not None and match.away_score is not None:
            score = f" {match.home_score}:{match.away_score}"
        return f"{kickoff} · {match.home_team} vs. {match.away_team} · {match.get_status_display()}{score}"


class GuildPoolChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, guild_pool) -> str:
        return f"{guild_pool.guild.name} · #{guild_pool.channel.name}"


class MessagePreviewForm(forms.Form):
    """Post one match's messages into a pool's channel, to look at them.

    Scoped to a pool because that is what decides how they render - the poll's
    answers come from the match's stage, the reminder's names from the pool's
    notification role, and the full-time list from its predictions.
    """

    guild_pool = GuildPoolChoiceField(
        queryset=DiscordGuildPool.objects.none(),
        label="Channel",
        help_text="Where the test messages go. Only bindings with a channel are offered.",
    )
    match = MatchChoiceField(
        queryset=Match.objects.none(),
        help_text="Nothing about the match is changed, and no prediction is created.",
    )
    kinds = forms.MultipleChoiceField(
        choices=PreviewMessageKind.choices,
        widget=forms.CheckboxSelectMultiple,
        initial=[kind.value for kind in PreviewMessageKind],
        label="Messages to post",
        help_text="Posted in this order, as separate messages - the real ticker edits one message instead.",
    )

    def __init__(self, pool, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool = pool
        self.fields["guild_pool"].queryset = (
            DiscordGuildPool.objects.filter(pool=pool, is_active=True, channel__isnull=False)
            .select_related("guild", "channel")
            .order_by("guild__name")
        )
        matches = (
            Match.objects.filter(stage__season_id=pool.season_id)
            .select_related("home_team", "away_team", "stage")
            .order_by("kickoff")
        )
        self.fields["match"].queryset = matches
        self.fields["guild_pool"].initial = self.fields["guild_pool"].queryset.first()
        self.fields["match"].initial = self.default_match(matches)

    @staticmethod
    def default_match(matches):
        """The next match to be played, or the last one played if none is left.

        Same rule the public front page uses to pick a season: whatever is
        about to happen is what someone is most likely to be checking.
        """
        now = timezone.now()
        return matches.filter(kickoff__gte=now).first() or matches.order_by("-kickoff").first()

    def clean_kinds(self) -> list[str]:
        # Stored on the request row and read back by the bot in this order, so
        # it has to be the order the channel would see them in.
        order = [kind.value for kind in PreviewMessageKind]
        return sorted(self.cleaned_data["kinds"], key=order.index)
