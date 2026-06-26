from django import forms
from django.contrib import admin

from predictions.models import DayOfWeek, PoolConfiguration, PoolStageRule, Prediction, PredictionPool

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
    pass


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


@admin.register(PredictionPool)
class PredictionPoolAdmin(admin.ModelAdmin):
    inlines = [PoolConfigurationInline]
    list_display = ("name", "season", "is_active")
