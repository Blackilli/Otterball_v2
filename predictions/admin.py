from django import forms
from django.contrib import admin

from predictions.models import (
    DayOfWeek,
    PoolConfiguration,
    PoolStageRule,
    Prediction,
    PredictionPool,
    sync_pool_stage_rules,
)

# Register your models here.


@admin.register(Prediction)
class PredictionAdmin(admin.ModelAdmin):
    list_filter = ("pool", "match__stage")
    readonly_fields = (
        "created_at",
        "updated_at",
        "match",
        "pool",
        "user",
        "points_awarded",
        "is_processed",
        "predicted_outcome",
    )


@admin.register(PoolStageRule)
class PoolStageRuleAdmin(admin.ModelAdmin):
    list_display = ("pool", "stage", "level", "points_per_correct")
    list_filter = ("pool",)
    list_editable = ("points_per_correct",)
    list_select_related = ("pool", "stage")
    ordering = ("pool", "level")


class PoolConfigurationAdminForm(forms.ModelForm):
    # 🚀 Map choices to a user-friendly Checkbox Multiple Selector
    poll_creation_weekdays = forms.MultipleChoiceField(
        choices=DayOfWeek.choices,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="Select all weekdays on which poll generation routines should trigger.",
    )

    class Meta:
        model = PoolConfiguration
        fields = "__all__"

    def clean_poll_creation_weekdays(self):
        """
        The MultipleChoiceField natively outputs a list of strings (e.g., ['0', '4']).
        We clean and convert it to a sorted list of plain integers for JSON serialization.
        """
        data = self.cleaned_data.get("poll_creation_weekdays", [])
        return sorted([int(day) for day in data])


# Inline display allows managing configurations directly inside the PredictionPool screen
class PoolConfigurationInline(admin.StackedInline):
    model = PoolConfiguration
    form = PoolConfigurationAdminForm
    can_delete = False


class PoolStageRuleInline(admin.TabularInline):
    """Scoring shown on the pool page.

    Without a rule per stage, predictions/signals.py falls back to a hardcoded
    3 points and a pool meant to scale points per round scores every round the
    same, with nothing logged. Seeding them in save_model makes that visible.
    """

    model = PoolStageRule
    extra = 0
    fields = ("stage", "level", "points_per_correct")
    ordering = ("level",)


@admin.register(PredictionPool)
class PredictionPoolAdmin(admin.ModelAdmin):
    inlines = [PoolConfigurationInline, PoolStageRuleInline]
    list_display = ("name", "season", "is_active", "stage_rule_count")

    @admin.display(description="Stage rules")
    def stage_rule_count(self, pool: PredictionPool) -> int:
        return pool.stage_rules.count()

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Top up on every save, not just creation: a season gains its playoff
        # stages partway through, and those need rules too.
        seeded = sync_pool_stage_rules(obj)
        if seeded:
            self.message_user(request, f"Seeded {len(seeded)} stage rule(s) - set the points below.")
