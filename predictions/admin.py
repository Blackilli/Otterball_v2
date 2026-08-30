from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from discord_bot.models import DiscordGuildPool
from predictions.forms import PoolSetupForm
from predictions.models import (
    DayOfWeek,
    PoolConfiguration,
    PoolStageRule,
    Prediction,
    PredictionPool,
    sync_pool_stage_rules,
)
from predictions.readiness import FAIL, check_environment, check_pool

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
    list_display = ("name", "season", "is_active", "stage_rule_count", "readiness")
    # readiness() reads season.competition.sport for every row.
    list_select_related = ("season", "season__competition")
    change_list_template = "admin/predictions/predictionpool/change_list.html"

    @admin.display(description="Stage rules")
    def stage_rule_count(self, pool: PredictionPool) -> int:
        return pool.stage_rules.count()

    @admin.display(description="Ready?")
    def readiness(self, pool: PredictionPool):
        """The one column that answers "why is this pool doing nothing?".

        Every failure it reports is otherwise silent, so it belongs on the list
        rather than behind a command nobody runs. It costs a handful of queries
        per row, which is fine for a table that holds a season's pools and not
        much else.
        """
        report = check_pool(pool)
        url = f"{reverse('admin:predictions_predictionpool_setup')}?created={pool.pk}"
        problems = report.problems
        if not problems:
            return format_html('<a href="{}">All checks pass</a>', url)

        colour = "#ba2121" if report.status == FAIL else "#996f00"
        return format_html(
            '<a href="{}" style="color: {}" title="{}">{} · {} to fix</a>',
            url,
            colour,
            "; ".join(check.label for check in problems),
            report.status,
            len(problems),
        )

    # -- guided setup ------------------------------------------------------

    def get_urls(self):
        return [
            path(
                "setup/",
                self.admin_site.admin_view(self.setup_view),
                name="predictions_predictionpool_setup",
            ),
            *super().get_urls(),
        ]

    def setup_view(self, request):
        """The whole "start a new pool" flow on one page.

        It does what `manage.py create_pool` does - pool, configuration, stage
        rules, Discord binding - and then reports on it with the same checks
        `manage.py check_pool` runs, because the point of the page is that
        none of those failures announce themselves.
        """
        if not self.has_add_permission(request):
            raise PermissionDenied

        created_pool = None
        pool_id = request.GET.get("created")
        if pool_id:
            created_pool = PredictionPool.objects.filter(pk=pool_id).select_related("season").first()

        if request.method == "POST":
            form = PoolSetupForm(request.POST)
            if form.is_valid():
                pool = self.create_pool(form.cleaned_data)
                self.message_user(request, f"Pool '{pool.name}' is set up. Set the points per round below.")
                # POST/redirect/GET, so a refresh of the result does not try to
                # create the pool a second time.
                return redirect(f"{request.path}?created={pool.pk}")
        else:
            form = PoolSetupForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Start a new pool",
            "opts": self.opts,
            "form": form,
            "environment": check_environment(),
            "created_pool": created_pool,
            "report": check_pool(created_pool) if created_pool else None,
            "pool_change_url": (
                reverse("admin:predictions_predictionpool_change", args=[created_pool.pk]) if created_pool else None
            ),
        }
        return render(request, "admin/predictions/predictionpool/pool_setup.html", context)

    @transaction.atomic
    def create_pool(self, data) -> PredictionPool:
        """Same steps, same order, same idempotence as `manage.py create_pool`.

        get_or_create rather than create: re-submitting the same name and
        season updates that pool instead of raising or duplicating it, which
        is how the command behaves and what makes the page safe to re-run
        after fixing one field.
        """
        pool, _created = PredictionPool.objects.get_or_create(
            name=data["name"],
            season=data["season"],
            defaults={"is_active": True},
        )

        # The post_save receiver on PredictionPool creates this for a new pool;
        # get_or_create covers a pool that predates it.
        config, _ = PoolConfiguration.objects.get_or_create(pool=pool)
        config.poll_creation_weekdays = data["poll_creation_weekdays"]
        config.poll_creation_time = data["poll_creation_time"]
        config.poll_creation_lookahead_days = data["poll_creation_lookahead_days"]
        config.reminder_lead_minutes = data["reminder_lead_minutes"]
        config.save()

        # Without these every pick scores the hardcoded fallback of 3.
        sync_pool_stage_rules(pool)

        if data.get("guild"):
            DiscordGuildPool.objects.update_or_create(
                guild=data["guild"],
                pool=pool,
                defaults={
                    "channel": data["channel"],
                    "notification_role": data.get("notification_role"),
                    "is_active": True,
                },
            )
        return pool

    def save_related(self, request, form, formsets, change):
        """Seed missing stage rules *after* the inlines have been written.

        This has to run here rather than in save_model. Django saves inline
        formsets in save_related, which runs after save_model - so seeding
        earlier would insert a rule for a stage the user had just added a row
        for, and their row would then collide with the unique (pool, stage)
        constraint and 500 the request. Since sync_pool_stage_rules only
        creates rules for stages that don't have one, running last lets the
        user's own rows win and tops up the rest.

        Runs on every save, not just creation: a season gains its playoff
        stages partway through, and those need rules too.
        """
        super().save_related(request, form, formsets, change)

        seeded = sync_pool_stage_rules(form.instance)
        if seeded:
            self.message_user(request, f"Seeded {len(seeded)} stage rule(s) - set the points below.")
