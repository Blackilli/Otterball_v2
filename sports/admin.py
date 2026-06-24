from django.contrib import admin
from django.utils.html import format_html

from sports.models import (
    Stage,
    Season,
    Match,
    Competition,
    Team,
    SeasonMapping,
    StageMapping,
    CompetitionMapping,
    TeamMapping,
    MatchMapping,
)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_filter = ("gender", "sport")
    readonly_fields = ("logo_preview",)

    @admin.display(description="Logo preview")
    def logo_preview(self, team: Team):
        if team.logo is None:
            return "No logo"
        return format_html(
            '<img src="{}" style="max-width:200px; max-height:200px; object-fit: contain;"/>',
            team.logo.url,
        )

    pass


@admin.register(TeamMapping)
class TeamMappingAdmin(admin.ModelAdmin):
    pass


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_filter = ("sport", "gender", "sport", "is_featured")
    pass


@admin.register(CompetitionMapping)
class CompetitionMappingAdmin(admin.ModelAdmin):
    pass


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):
    list_filter = ("competition", "year")
    fields = ("name", "competition", "year", "is_active")
    pass


@admin.register(SeasonMapping)
class SeasonMappingAdmin(admin.ModelAdmin):
    pass


@admin.register(Stage)
class StageAdmin(admin.ModelAdmin):
    list_display = ("name", "season", "stage_type", "level")
    list_filter = ("stage_type", "season")
    search_fields = ("name", "season__name")
    ordering = ("season", "level", "name")


@admin.register(StageMapping)
class StageMappingAdmin(admin.ModelAdmin):
    pass


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_filter = ("stage", "status")
    pass


@admin.register(MatchMapping)
class MatchMappingAdmin(admin.ModelAdmin):
    pass
