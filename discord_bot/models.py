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
    thread_id = models.BigIntegerField()
    poll_message_id = models.BigIntegerField(unique=True)
    poll_use_fallback_answer_ordering = models.BooleanField(default=False)
    is_poll_finalized = models.BooleanField(default=False)
    ticker_message_id = models.BigIntegerField(null=True, blank=True, unique=True)
    is_ticker_finalized = models.BooleanField(default=False)

    class Meta:
        unique_together = ("match", "guild", "pool")

        indexes = [
            models.Index(fields=["poll_message_id"]),
            models.Index(fields=["ticker_message_id"]),
        ]

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
