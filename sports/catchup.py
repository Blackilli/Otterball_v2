"""One-shot ingestion catch-up when a Celery worker starts.

Celery Beat is what runs the ingestion. This is the net under it: if the
schedule has gone unrun for long enough that a pool would start to strand -
Beat was down, the container was misconfigured, the season was set up before
anything was scheduled - a starting worker runs the overdue infrastructure
syncs once so the data is current.

**It deliberately does not hide a dead Beat.** The catch-up dispatches the task
itself and never touches `PeriodicTask.last_run_at`, which only Beat writes. So
the data gets refreshed while `check_pool` and the admin's Ready? column go on
reporting the schedule as stale - the fault stays visible, the pool keeps
working. That split is the whole point; do not "fix" it by stamping last_run_at
here.

Guarded three ways: only tasks whose sport has an active pool, only tasks
marked `catch_up` (not the every-two-minute live syncs, where a one-shot
settles nothing the next tick would not), and a cache lock so a rolling restart
of several workers dispatches one round, not one per worker.
"""

import logging

from celery.signals import worker_ready
from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError
from django.utils import timezone

logger = logging.getLogger(__name__)

#: How long the lock is held. Long enough to cover a rolling restart or a crash
#: loop dispatching the same expensive sync over and over, short enough that a
#: genuinely overdue task is picked up again before the day is out.
LOCK_SECONDS = 15 * 60

LOCK_KEY = "sports:ingestion-catchup"


def overdue_tasks():
    """Scheduled catch-up tasks that are overdue for a sport actually in play.

    A task with no `PeriodicTask` row at all is **not** included: that is a
    missing schedule, `manage.py ensure_schedule` is what installs it, and
    running it from here would paper over the very thing readiness reports as
    a failure. Never-run and long-unrun are the cases this exists for.
    """
    from django_celery_beat.models import PeriodicTask

    from predictions.models import PredictionPool
    from predictions.readiness import MIN_STALE_GRACE, STALE_AFTER_INTERVALS
    from sports.schedules import SCHEDULED_TASKS

    played_sports = set(
        PredictionPool.objects.filter(is_active=True).values_list("season__competition__sport", flat=True)
    )
    now = timezone.now()
    overdue = []

    for scheduled in SCHEDULED_TASKS:
        if not scheduled.catch_up or scheduled.sport not in played_sports:
            continue

        row = PeriodicTask.objects.filter(task=scheduled.task).first()
        if row is None or not row.enabled:
            continue

        if row.last_run_at is None:
            overdue.append((scheduled, "it has never run"))
            continue

        allowance = max(scheduled.every * STALE_AFTER_INTERVALS, MIN_STALE_GRACE)
        age = now - row.last_run_at
        if age > allowance:
            overdue.append((scheduled, f"it last ran {age.days}d {age.seconds // 3600}h ago"))

    return overdue


def run_catchup() -> list[str]:
    """Dispatch the overdue syncs, at most one worker at a time.

    Returns the task names dispatched, so the caller and the tests can see what
    it decided without reading the log.
    """
    if not getattr(settings, "INGESTION_CATCHUP_ENABLED", True):
        logger.info("Ingestion catch-up is disabled; leaving the schedule to Beat.")
        return []

    try:
        overdue = overdue_tasks()
    except DatabaseError as e:
        # A worker wins the race against the `web` container's migrate on a
        # fresh deploy often enough to matter, and the tables it reads are not
        # there yet. Beat's next tick covers everything this pass would have,
        # so this is one line rather than a traceback that reads like a crash.
        logger.warning(f"Skipping ingestion catch-up: the database is not ready yet ({type(e).__name__}: {e}).")
        return []

    if not overdue:
        return []

    try:
        # add() only succeeds if the key is unset, so exactly one worker in a
        # restart wave takes the round. A cache that is down should not stop a
        # worker starting, so the failure falls through to running anyway -
        # duplicate ingestion is idempotent, a crashed worker is not.
        claimed = cache.add(LOCK_KEY, timezone.now().isoformat(), LOCK_SECONDS)
    except Exception as e:
        logger.warning(f"Could not take the ingestion catch-up lock ({e}); running anyway.")
        claimed = True

    if not claimed:
        logger.info("Another worker already started an ingestion catch-up; skipping.")
        return []

    from otterball_v2.celery import app

    dispatched = []
    for scheduled, reason in overdue:
        # Loud on purpose. This is compensation for a schedule that did not
        # run, not business as usual, and it is the only line that says so.
        logger.warning(
            f"Ingestion catch-up: dispatching {scheduled.task} because {reason}. "
            "Beat should be running this - check the beat container."
        )
        app.send_task(scheduled.task)
        dispatched.append(scheduled.task)

    return dispatched


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):
    """Run the catch-up when a worker comes up.

    Wrapped: nothing here is worth refusing to start a worker over.
    """
    try:
        run_catchup()
    except Exception as e:
        logger.error(f"Ingestion catch-up failed: {e}", exc_info=True)
