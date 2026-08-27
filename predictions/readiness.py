"""Is a pool actually ready to run?

Almost every way a pool can be misconfigured fails silently: no stage rules and
every pick scores the hardcoded fallback of 3; a stage type missing from
DISCORD_POLL_ANSWER_ORDER_MAP and poll creation skips that whole round; no
Discord binding and the pool simply never posts. None of it raises, and none of
it is visible on the pool's admin page.

The checks live here rather than in the management command because two things
need them - `manage.py check_pool`, which is also usable as a deploy gate, and
the guided setup page in the admin. A second copy of "is this pool ready" would
be a copy that disagrees.
"""

import datetime
from dataclasses import dataclass, field

from django.utils import timezone
from django_celery_beat.models import PeriodicTask
from django_celery_results.models import TaskResult

from discord_bot.constants import DISCORD_POLL_ANSWER_ORDER_MAP
from discord_bot.models import DiscordChannel, DiscordGuild, DiscordGuildPool, DiscordTeamEmoji
from predictions.models import DEFAULT_POLL_LOOKAHEAD_DAYS, DayOfWeek, PredictionPool
from sports.models import Match, Season, Stage
from sports.schedules import describe_interval, tasks_for_sport

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

#: How far past its interval a task may drift before it is called stale. Beat
#: dispatches on a best-effort tick and a worker can be busy, so a task is not
#: late the second it is due - but three missed cycles is not a hiccup.
STALE_AFTER_INTERVALS = 3

#: Floor under that allowance, so a two-minute task is not reported stale after
#: six minutes of a slow worker.
MIN_STALE_GRACE = datetime.timedelta(minutes=30)

#: Worst-wins ordering, so a report's overall status is the worst line in it.
SEVERITY = {OK: 0, WARN: 1, FAIL: 2}


def worst(*statuses: str) -> str:
    return max(statuses, key=lambda status: SEVERITY[status], default=OK)


@dataclass(frozen=True)
class Check:
    """One line of a report."""

    status: str
    label: str
    detail: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status == OK


@dataclass(frozen=True)
class Readiness:
    pool: PredictionPool
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> str:
        return worst(*(check.status for check in self.checks))

    @property
    def is_ready(self) -> bool:
        return self.status != FAIL

    @property
    def problems(self) -> list[Check]:
        return [check for check in self.checks if not check.is_ok]


def check_environment() -> list[Check]:
    """Whether the install can support a pool at all, before one is asked for.

    The two prerequisites are easy to hit in the wrong order and both fail
    unhelpfully: without ingested sport data there is no season to attach a
    pool to, and until the bot has connected once there are no guilds or
    channels to bind it to - `ReconciliationCog` is what creates those rows, so
    the admin dropdowns are simply empty until then.
    """
    seasons = Season.objects.filter(stages__isnull=False).distinct().count()
    guilds = DiscordGuild.objects.count()
    channels = DiscordChannel.objects.count()

    checks = []
    if seasons:
        checks.append(Check(OK, f"{seasons} season(s) with stages ingested", "pick one below"))
    else:
        checks.append(
            Check(
                FAIL,
                "no season has any stages",
                "run the ingestion for your sport first, e.g. `manage.py sync_nfl_infra`",
            )
        )

    if guilds and channels:
        checks.append(Check(OK, f"{guilds} guild(s) and {channels} channel(s) known", "the bot has connected"))
    elif guilds:
        checks.append(Check(WARN, f"{guilds} guild(s) but no channels", "let the bot finish a startup sync"))
    else:
        checks.append(
            Check(
                FAIL,
                "no Discord guilds known",
                "start the bot once (`manage.py runbot`) - it is what creates the guild, "
                "channel and role rows this page binds to",
            )
        )
    return checks


def check_pool(pool: PredictionPool) -> Readiness:
    checks: list[Check] = []
    for check in (
        _check_active,
        _check_stages,
        _check_stage_rules,
        _check_poll_answer_orders,
        _check_configuration,
        _check_matches,
        _check_discord_binding,
        _check_ingestion,
        _check_emoji,
    ):
        checks.extend(check(pool))
    return Readiness(pool=pool, checks=checks)


# -- individual checks -----------------------------------------------------


def _check_active(pool: PredictionPool) -> list[Check]:
    if not pool.is_active:
        return [Check(WARN, "pool is inactive", "no polls or leaderboard updates will run")]
    if not pool.season.is_active:
        return [Check(WARN, "season is inactive", f"'{pool.season.name}' is outside its date range")]
    return [Check(OK, "pool and season are active")]


def _check_stages(pool: PredictionPool) -> list[Check]:
    count = Stage.objects.filter(season_id=pool.season_id).count()
    if not count:
        return [Check(FAIL, "season has no stages", "run the ingestion command for this sport")]
    return [Check(OK, f"season has {count} stage(s)")]


def _check_stage_rules(pool: PredictionPool) -> list[Check]:
    stages = list(Stage.objects.filter(season_id=pool.season_id).order_by("level"))
    if not stages:
        return []  # already reported by _check_stages

    rules = {rule.stage_id: rule for rule in pool.stage_rules.all()}
    fallback = rules.get(None)
    missing = [stage for stage in stages if stage.id not in rules]

    if missing and not fallback:
        names = ", ".join(stage.name for stage in missing)
        return [
            Check(
                FAIL,
                f"{len(missing)} stage(s) have no scoring rule",
                f"{names} - picks there score the hardcoded fallback of 3. Re-save the pool to seed them",
            )
        ]

    checks: list[Check] = []
    if missing:
        names = ", ".join(stage.name for stage in missing)
        checks.append(Check(WARN, f"{len(missing)} stage(s) fall back to the pool-wide rule", names))

    distribution = ", ".join(
        f"{stage.name}={rules[stage.id].points_per_correct}" for stage in stages if stage.id in rules
    )
    checks.append(Check(OK, "scoring", distribution or "pool-wide fallback only"))

    values = {rules[stage.id].points_per_correct for stage in stages if stage.id in rules}
    if len(values) == 1 and len(stages) > 1:
        checks.append(
            Check(
                WARN,
                "every stage scores the same",
                "intentional for a flat pool; if you meant to scale per round, set the points below",
            )
        )
    return checks


def _check_poll_answer_orders(pool: PredictionPool) -> list[Check]:
    """An unmapped stage_type makes poll_creation log an error and skip the
    match outright, so a whole round can silently never get polls."""
    unmapped = [
        stage
        for stage in Stage.objects.filter(season_id=pool.season_id)
        if stage.stage_type not in DISCORD_POLL_ANSWER_ORDER_MAP
    ]
    if unmapped:
        detail = ", ".join(f"{stage.name} ({stage.stage_type})" for stage in unmapped)
        return [
            Check(
                FAIL,
                f"{len(unmapped)} stage(s) have no poll layout",
                f"{detail} - poll creation skips these matches entirely. "
                "Add the stage type to DISCORD_POLL_ANSWER_ORDER_MAP",
            )
        ]
    return [Check(OK, "every stage type has a poll layout")]


def _check_configuration(pool: PredictionPool) -> list[Check]:
    config = getattr(pool, "configuration", None)
    if config is None:
        return [Check(FAIL, "no PoolConfiguration", "polls will never be scheduled")]
    if not config.poll_creation_weekdays:
        return [Check(FAIL, "no poll creation weekdays set", "polls will never post")]

    days = ", ".join(DayOfWeek(day).label for day in sorted(config.poll_creation_weekdays))
    checks = [
        Check(
            OK,
            "poll schedule",
            f"{days} at {config.poll_creation_time:%H:%M}, {config.poll_creation_lookahead_days}d lookahead",
        )
    ]

    # Not a FAIL: switching the reminder off is a legitimate choice, it is just
    # an easy one to make by accident and then wonder about.
    if config.reminder_lead_minutes:
        checks.append(Check(OK, "missing-vote reminder", f"{config.reminder_lead_minutes} min before kickoff"))
    else:
        checks.append(Check(WARN, "missing-vote reminder disabled", "reminder_lead_minutes is 0"))
    return checks


def _check_matches(pool: PredictionPool) -> list[Check]:
    config = getattr(pool, "configuration", None)
    lookahead = config.poll_creation_lookahead_days if config else DEFAULT_POLL_LOOKAHEAD_DAYS
    now = timezone.now()

    upcoming = Match.objects.filter(
        stage__season_id=pool.season_id,
        kickoff__gte=now,
        kickoff__lte=now + datetime.timedelta(days=lookahead),
    ).count()
    total_future = Match.objects.filter(stage__season_id=pool.season_id, kickoff__gte=now).count()

    if not total_future:
        return [Check(FAIL, "no upcoming matches on this season", "run the schedule ingestion")]
    if not upcoming:
        next_match = (
            Match.objects.filter(stage__season_id=pool.season_id, kickoff__gte=now).order_by("kickoff").first()
        )
        return [
            Check(
                WARN,
                f"no matches in the next {lookahead}d",
                f"{total_future} later; next is {next_match.kickoff:%Y-%m-%d %H:%M}",
            )
        ]
    return [Check(OK, f"{upcoming} match(es) in the next {lookahead}d", f"{total_future} upcoming in total")]


def _check_discord_binding(pool: PredictionPool) -> list[Check]:
    bindings = list(
        DiscordGuildPool.objects.filter(pool=pool).select_related("guild", "channel", "notification_role")
    )
    if not bindings:
        return [
            Check(
                FAIL,
                "no Discord binding",
                "the pool will never post. Bind it to a guild and channel "
                "(the bot must have connected once to populate them)",
            )
        ]

    checks: list[Check] = []
    for binding in bindings:
        if not binding.is_active:
            checks.append(Check(WARN, f"binding to {binding.guild.name} is inactive"))
            continue
        if not binding.channel:
            checks.append(Check(FAIL, f"binding to {binding.guild.name} has no channel", "nowhere to post"))
            continue

        role = f", pings @{binding.notification_role.name}" if binding.notification_role else ", no ping role"
        pinned = "leaderboard posted" if binding.leaderboard_msg else "leaderboard not posted yet"
        checks.append(Check(OK, f"bound to {binding.guild.name} #{binding.channel.name}", f"{pinned}{role}"))
    return checks


def _humanise(age: datetime.timedelta) -> str:
    seconds = int(age.total_seconds())
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}min ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _check_ingestion(pool: PredictionPool) -> list[Check]:
    """Is anything actually keeping this pool's sport data up to date?

    The failure this exists for is silent by construction: Beat is
    database-driven, so an unscheduled task is not an error anywhere - the
    matches simply stop arriving, and the pool looks fine until it runs out of
    fixtures. `manage.py ensure_schedule` installs the rows; this reports on
    them.

    Freshness is read from `PeriodicTask.last_run_at` rather than from a task
    result, because `celery.backend_cleanup` deletes results after a day by
    default and a daily task's last success would keep vanishing. The most
    recent `TaskResult` is used only to notice that the runs are failing -
    Beat happily keeps dispatching to a worker that throws every time.
    """
    checks: list[Check] = []
    now = timezone.now()

    for scheduled in tasks_for_sport(pool.season.competition.sport):
        row = PeriodicTask.objects.filter(task=scheduled.task).first()
        cadence = describe_interval(scheduled.every)

        if row is None:
            checks.append(
                Check(
                    FAIL if scheduled.critical else WARN,
                    f"nothing schedules the {scheduled.name}",
                    f"{scheduled.purpose} - it will never run. Fix with `manage.py ensure_schedule`",
                )
            )
            continue

        if not row.enabled:
            checks.append(Check(WARN, f"{scheduled.name} is disabled", f"'{row.name}' in the admin, {cadence}"))
            continue

        if row.last_run_at is None:
            checks.append(
                Check(
                    WARN,
                    f"{scheduled.name} has never run",
                    f"scheduled {cadence} - is the beat container running?",
                )
            )
            continue

        allowance = max(scheduled.every * STALE_AFTER_INTERVALS, MIN_STALE_GRACE)
        age = now - row.last_run_at
        if age > allowance:
            checks.append(Check(WARN, f"{scheduled.name} last ran {_humanise(age)}", f"scheduled {cadence}"))
        else:
            checks.append(Check(OK, f"{scheduled.name} ran {_humanise(age)}", cadence))

        latest = TaskResult.objects.filter(task_name=scheduled.task).order_by("-date_created").first()
        if latest is not None and latest.status == "FAILURE":
            detail = (latest.result or "").strip().splitlines()
            checks.append(
                Check(
                    WARN,
                    f"the last recorded {scheduled.name} failed",
                    detail[-1][:160] if detail else "see the worker log",
                )
            )

    return checks


def _check_emoji(pool: PredictionPool) -> list[Check]:
    """Missing emoji are cosmetic - polls fall back to a white/black circle."""
    now = timezone.now()
    upcoming = Match.objects.filter(stage__season_id=pool.season_id, kickoff__gte=now)
    team_ids = set(
        upcoming.values_list("home_team_id", flat=True).union(upcoming.values_list("away_team_id", flat=True))
    )
    if not team_ids:
        return []

    have = set(DiscordTeamEmoji.objects.filter(team_id__in=team_ids).values_list("team_id", flat=True))
    missing = team_ids - have
    if missing:
        return [
            Check(
                WARN,
                f"{len(missing)} of {len(team_ids)} teams have no emoji",
                "cosmetic; the bot registers them on startup",
            )
        ]
    return [Check(OK, f"all {len(team_ids)} upcoming teams have emoji")]
