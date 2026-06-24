from django.contrib import admin

from discord_bot.models import (
    DiscordProfile,
    ActiveMatchMessage,
    DiscordGuild,
    DiscordChannel,
    DiscordTeamEmoji,
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
