from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models

if TYPE_CHECKING:
    from predictions.models import PredictionPool
    from sports.models import Match
    from users.models import User


# Create your models here.
class DiscordGuild(models.Model):
    id = models.BigIntegerField(primary_key=True)
    name = models.CharField(max_length=100, blank=True, null=True)

    active_pools = models.ManyToManyField(
        "predictions.PredictionPool",
        through="DiscordGuildPool",
        related_name="active_in_guilds",
        blank=True,
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.id})"


class DiscordGuildRole(models.Model):
    id = models.BigIntegerField(primary_key=True)
    guild = models.ForeignKey(
        "discord_bot.DiscordGuild",
        on_delete=models.CASCADE,
        related_name="roles",
    )
    name = models.CharField(max_length=100)
    position = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)


class DiscordChannel(models.Model):
    id = models.BigIntegerField(primary_key=True)
    guild = models.ForeignKey(
        "DiscordGuild",
        on_delete=models.CASCADE,
        related_name="channels",
    )
    name = models.CharField(max_length=100)
    position = models.IntegerField(default=0)
    channel_type = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True, db_index=True)
    last_synced_at = models.DateTimeField(auto_now=True)

    guild: DiscordGuild

    class Meta:
        ordering = ["guild_id", "position"]

    def __str__(self) -> str:
        status = "🟢" if self.is_active else "🔴 (Deleted)"
        return f"{status} #{self.name} ({self.channel_type})"


class DiscordProfile(models.Model):
    id = models.BigIntegerField(primary_key=True)
    user: User = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="discord_profile",
    )
    username = models.CharField(max_length=100)
    global_name = models.CharField(max_length=100, blank=True, null=True)
    wants_notifications = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} ({self.id})"


class MatchMessageState(models.IntegerChoices):
    """Lifecycle of the single status message that accompanies a poll.

    It is one Discord message edited in place, not one message per state -
    STARTING_SOON becomes the live score, which becomes the final result.
    """

    UNKNOWN = 0, "Unknown"
    STARTING_SOON = 1, "Starting soon"
    IN_PROGRESS = 2, "In progress"
    RESULT_POSTED = 3, "Result posted"


class ActiveMatchMessage(models.Model):
    match: Match = models.ForeignKey(
        "sports.Match",
        on_delete=models.CASCADE,
        related_name="active_messages",
    )
    guild: DiscordGuild = models.ForeignKey(
        "discord_bot.DiscordGuild",
        on_delete=models.CASCADE,
        related_name="active_messages",
    )
    pool: PredictionPool = models.ForeignKey(
        "predictions.PredictionPool",
        on_delete=models.CASCADE,
        related_name="active_messages",
    )
    channel = models.ForeignKey(
        "discord_bot.DiscordChannel",
        on_delete=models.CASCADE,
        related_name="active_messages",
    )
    # NULL means the poll lives directly in the channel, which is how every
    # poll is posted now. Rows created before that change still carry the id of
    # the thread they were posted into, so `container_id` is what code should
    # read - never `thread_id` on its own, or the old season's polls stop
    # reconciling.
    thread_id = models.BigIntegerField(null=True, blank=True)
    poll_message_id = models.BigIntegerField(unique=True)
    poll_use_fallback_answer_ordering = models.BooleanField(default=False)
    is_poll_finalized = models.BooleanField(default=False)
    ticker_message_id = models.BigIntegerField(null=True, blank=True, unique=True)
    ticker_state = models.IntegerField(
        choices=MatchMessageState.choices,
        default=MatchMessageState.UNKNOWN,
    )
    is_ticker_finalized = models.BooleanField(default=False)

    class Meta:
        unique_together = ("match", "guild", "pool")

        indexes = [
            models.Index(fields=["poll_message_id"]),
            models.Index(fields=["ticker_message_id"]),
        ]

    @property
    def container_id(self) -> int:
        """Where this match's messages live: its thread, else the channel."""
        return self.thread_id or self.channel_id

    def __str__(self):
        return f"Match {self.match_id} - Guild ID {self.guild_id}"


class DiscordGuildPool(models.Model):
    guild = models.ForeignKey(
        "DiscordGuild",
        on_delete=models.CASCADE,
        related_name="pool_configurations",
    )
    pool = models.ForeignKey(
        "predictions.PredictionPool",
        on_delete=models.CASCADE,
        related_name="guild_configurations",
    )
    channel: DiscordChannel | None = models.ForeignKey(
        "DiscordChannel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_pools",
    )
    notification_role = models.ForeignKey(
        "DiscordGuildRole",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notification_roles",
    )
    leaderboard_msg = models.BigIntegerField(null=True, blank=True, unique=True)
    # The one-off "welcome to the new season" post. NULL means it has not been
    # posted yet, which is what PoolOnboardingCog looks for - so the id is not
    # decoration, it is the record that stops the channel being welcomed twice.
    welcome_msg = models.BigIntegerField(null=True, blank=True, unique=True)
    announce_welcome = models.BooleanField(
        default=True,
        help_text=(
            "Post a welcome message in the channel introducing this pool. "
            "It goes out once, pings the notification role, and is then kept up to date by edits."
        ),
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    guild: DiscordGuild
    pool: PredictionPool
    channel: DiscordChannel

    class Meta:
        unique_together = ("guild", "pool")

    def __str__(self) -> str:
        channel_name = f"#{self.channel.name}" if self.channel else "Unassigned Channel"
        return f"{self.guild.name} -> {self.pool.name} in {channel_name}"


class DiscordTeamEmoji(models.Model):
    id = models.BigIntegerField(primary_key=True, unique=True)
    team = models.OneToOneField("sports.Team", on_delete=models.CASCADE, related_name="emoji")
    name = models.CharField(max_length=100)


class PoolNotificationPreference(models.Model):
    """Per-user, per-pool opt-out of the "you haven't voted yet" ping.

    A user plays several pools in the same guild, so the setting is keyed on
    (user, pool) rather than on the profile: opting out of the NFL reminders
    must not silence the World Cup ones.

    Notifications default to on, which means an *absent* row is a consenting
    row - rows are only written when someone actually changes the setting.
    Read this through `aget_muted_user_ids`, never by iterating members and
    expecting a row per person.
    """

    user: "User" = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pool_notification_preferences",
    )
    pool: "PredictionPool" = models.ForeignKey(
        "predictions.PredictionPool",
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    notify_missing_votes = models.BooleanField(
        default=True,
        help_text="Ping this user before kickoff when they have not voted on a match in this pool.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "pool")

    def __str__(self) -> str:
        state = "on" if self.notify_missing_votes else "off"
        return f"User #{self.user_id} - Pool #{self.pool_id}: missing-vote pings {state}"

    @classmethod
    async def aget_muted_user_ids(cls, pool_id: int) -> set[int]:
        """User ids that have explicitly turned missing-vote pings off for this pool."""
        return {
            user_id
            async for user_id in cls.objects.filter(
                pool_id=pool_id,
                notify_missing_votes=False,
            ).values_list("user_id", flat=True)
        }


class PreviewMessageKind(models.TextChoices):
    """Which of a match's messages a preview asks for.

    The poll and the three ticker states are the four things a channel ever
    sees for a match, so previewing is picking from this list.
    """

    POLL = "poll", "Prediction poll"
    STARTING_SOON = "starting_soon", "Reminder — starting soon"
    IN_PROGRESS = "in_progress", "Live score"
    RESULT_POSTED = "result", "Full time"
    #: Not a match message at all: every clickable component in one place, so
    #: the buttons and the modal behind them can actually be tried. A modal
    #: opens from an interaction and never on its own, so it cannot be posted.
    COMPONENTS = "components", "Buttons & modals"


class PreviewStatus(models.TextChoices):
    PENDING = "pending", "Waiting for the bot"
    POSTED = "posted", "Posted"
    FAILED = "failed", "Failed"
    CLEANED = "cleaned", "Removed again"


class MessagePreviewRequest(models.Model):
    """A "post these test messages" order the admin leaves for the bot.

    The admin runs in the `web` container and has no Discord connection, so it
    cannot post anything itself - the row *is* the signal, exactly as
    DiscordGuildPool is for the welcome post. `MessagePreviewCog` picks it up
    within 15 seconds, renders through the same code the real flow uses (the
    poll builder in `poll_creation`, the state renderers on `MatchTickerCog`),
    and writes back what it posted so the same page can delete it again.

    It deliberately creates **no ActiveMatchMessage**. That row is what the
    poll loop reads as "this match already has a poll" and what the ticker
    walks, so a preview that wrote one would suppress the real poll and then
    let the ticker edit the preview. The cost is that votes on a preview poll
    are ignored by `PollPredictionCog` - which is what a preview should do.
    """

    guild_pool: DiscordGuildPool = models.ForeignKey(
        "DiscordGuildPool",
        on_delete=models.CASCADE,
        related_name="preview_requests",
        help_text="Which pool binding to post into - that is the guild and the channel.",
    )
    match = models.ForeignKey(
        "sports.Match",
        on_delete=models.CASCADE,
        related_name="preview_requests",
        help_text="The fixture to render. Any match of the season will do; nothing about it is changed.",
    )
    kinds = models.JSONField(
        default=list,
        help_text="PreviewMessageKind values to post, in the order the channel would see them.",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="message_preview_requests",
    )
    status = models.CharField(max_length=20, choices=PreviewStatus.choices, default=PreviewStatus.PENDING)
    error = models.TextField(blank=True)
    #: Everything this request put in the channel, header included, so the
    #: admin can take it all back out without hunting for it by hand.
    posted_message_ids = models.JSONField(default=list, blank=True)
    cleanup_requested = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    cleaned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Preview of match #{self.match_id} for pool #{self.guild_pool.pool_id} ({self.status})"

    @property
    def kind_labels(self) -> list[str]:
        labels = dict(PreviewMessageKind.choices)
        return [labels.get(kind, kind) for kind in self.kinds]
