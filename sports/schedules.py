"""The periodic schedule the ingestion tasks need, as data.

Celery Beat here is database-driven (`CELERY_BEAT_SCHEDULER =
django_celery_beat.schedulers:DatabaseScheduler`), so a task existing in
`sports/tasks.py` does **nothing at all** until a `PeriodicTask` row exists for
it. Those rows used to be created by hand in `/admin/`, which is exactly the
kind of step that gets missed - and when it is missed nothing announces it: the
data simply never refreshes, and the first symptom is a pool with no upcoming
matches weeks later.

Declaring them here lets `manage.py ensure_schedule` install them on deploy and
`predictions/readiness.py` report on them, from one definition rather than two
that can disagree.
"""

import datetime
from dataclasses import dataclass

from sports.models import Sport


@dataclass(frozen=True)
class ScheduledTask:
    """One ingestion task and how often it has to run."""

    #: The dotted name the task is registered under - what `PeriodicTask.task`
    #: holds, and how an existing row is recognised regardless of its label.
    task: str
    #: The label the row gets when this command creates it.
    name: str
    every: datetime.timedelta
    sport: Sport
    purpose: str
    #: False for a task whose absence degrades rather than breaks a pool, so
    #: readiness reports it as a warning instead of a failure.
    critical: bool = True


SCHEDULED_TASKS: tuple[ScheduledTask, ...] = (
    ScheduledTask(
        task="sports.tasks.sync_nfl_infrastructure",
        name="NFL infrastructure sync",
        every=datetime.timedelta(days=1),
        sport=Sport.AMERICAN_FOOTBALL,
        purpose="season skeleton, teams, the nflverse abbreviation bridge and the schedule",
    ),
    ScheduledTask(
        task="sports.tasks.sync_nfl_live_games",
        name="NFL live match sync",
        every=datetime.timedelta(minutes=2),
        sport=Sport.AMERICAN_FOOTBALL,
        purpose="status and scores - this is what moves a match to FINISHED and fires scoring",
    ),
    ScheduledTask(
        task="sports.tasks.sync_nflverse_results",
        name="nflverse results backstop",
        every=datetime.timedelta(hours=6),
        sport=Sport.AMERICAN_FOOTBALL,
        purpose="independent second opinion on final results",
        # ESPN is the live source; losing this costs a backstop, not the pool.
        critical=False,
    ),
    ScheduledTask(
        task="sports.tasks.sync_daily_infrastructure",
        name="FIFA infrastructure sync",
        every=datetime.timedelta(days=1),
        sport=Sport.SOCCER,
        purpose="competitions, national teams, seasons, stages and upcoming matches",
    ),
    ScheduledTask(
        task="sports.tasks.sync_live_games",
        name="FIFA live match sync",
        every=datetime.timedelta(minutes=2),
        sport=Sport.SOCCER,
        purpose="status and scores - this is what moves a match to FINISHED and fires scoring",
    ),
)


def tasks_for_sport(sport: str) -> list[ScheduledTask]:
    """The ingestion a pool of this sport depends on."""
    return [scheduled for scheduled in SCHEDULED_TASKS if scheduled.sport == sport]


def describe_interval(every: datetime.timedelta) -> str:
    """ "every 2 minutes", "every 6 hours", "every day"."""
    seconds = int(every.total_seconds())
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds % size == 0:
            count = seconds // size
            return f"every {unit}" if count == 1 else f"every {count} {unit}s"
    return f"every {seconds} seconds"
