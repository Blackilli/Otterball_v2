from django.contrib import admin

from discord_bot.models import (
    ActiveMatchMessage,
    DiscordChannel,
    DiscordGuild,
    DiscordGuildPool,
    DiscordGuildRole,
    DiscordProfile,
    DiscordTeamEmoji,
    MessagePreviewRequest,
    PoolNotificationPreference,
)


# Register your models here.
@admin.register(DiscordProfile)
class DiscordProfileAdmin(admin.ModelAdmin):
    pass


@admin.register(ActiveMatchMessage)
class ActiveMatchMessageAdmin(admin.ModelAdmin):
    pass


@admin.register(DiscordGuild)
class DiscordGuildAdmin(admin.ModelAdmin):
    pass


@admin.register(DiscordChannel)
class DiscordChannelAdmin(admin.ModelAdmin):
    pass


@admin.register(DiscordTeamEmoji)
class DiscordTeamEmojiAdmin(admin.ModelAdmin):
    pass


@admin.register(DiscordGuildRole)
class DiscordGuildRoleAdmin(admin.ModelAdmin):
    list_display = ("name", "guild", "position", "is_active")
    list_filter = ("guild", "is_active")
    search_fields = ("name",)


@admin.register(DiscordGuildPool)
class DiscordGuildPoolAdmin(admin.ModelAdmin):
    """The row that makes a pool actually do anything.

    Both PollCreationCog and LeaderboardSyncCog iterate DiscordGuildPool, so
    without one a pool exists but never posts a poll or a leaderboard. It used
    to be creatable only from `manage.py shell`.

    Note the guild, channel and role dropdowns are populated by the bot's
    ReconciliationCog on startup - if they are empty, the bot has not connected
    yet.
    """

    list_display = (
        "pool",
        "guild",
        "channel",
        "notification_role",
        "is_active",
        "announce_welcome",
        "has_welcome",
        "has_leaderboard",
    )
    list_filter = ("is_active", "guild")
    list_select_related = ("pool", "guild", "channel", "notification_role")
    autocomplete_fields = ("notification_role",)
    readonly_fields = ("created_at",)

    @admin.display(boolean=True, description="Welcome posted")
    def has_welcome(self, guild_pool: DiscordGuildPool) -> bool:
        """Clearing welcome_msg with announce_welcome on reposts the opener."""
        return guild_pool.welcome_msg is not None

    @admin.display(boolean=True, description="Leaderboard posted")
    def has_leaderboard(self, guild_pool: DiscordGuildPool) -> bool:
        return guild_pool.leaderboard_msg is not None

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Only offer channels/roles that still exist in the guild.
        if db_field.name == "channel":
            kwargs["queryset"] = DiscordChannel.objects.filter(is_active=True).select_related("guild")
        if db_field.name == "notification_role":
            kwargs["queryset"] = DiscordGuildRole.objects.filter(is_active=True).select_related("guild")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(PoolNotificationPreference)
class PoolNotificationPreferenceAdmin(admin.ModelAdmin):
    """Only users who changed the setting have a row here.

    Reminders default to on, so an absent row means "notify" - do not read an
    empty list as "nobody wants reminders".
    """

    list_display = ("user", "pool", "notify_missing_votes", "updated_at")
    list_filter = ("pool", "notify_missing_votes")
    list_select_related = ("user", "pool")
    search_fields = ("user__username",)


@admin.register(MessagePreviewRequest)
class MessagePreviewRequestAdmin(admin.ModelAdmin):
    """The test messages the pool's own "Preview messages" page queues.

    Read-only here: a row is an order for the bot, and editing one after it has
    been carried out only makes the record disagree with the channel. Queue and
    remove them from the pool page instead.
    """

    list_display = ("created_at", "guild_pool", "match", "status", "cleanup_requested", "requested_by")
    list_filter = ("status", "cleanup_requested", "guild_pool__pool")
    list_select_related = ("guild_pool", "guild_pool__pool", "match", "requested_by")
    readonly_fields = tuple(field.name for field in MessagePreviewRequest._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False
