import datetime
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from predictions.models import PredictionPool
from sports.catchup import LOCK_KEY, run_catchup
from sports.models import Competition, Season, Sport


# Local cache: the lock is the only thing here that needs one, and the suite
# must not require a running Redis to exercise it.
@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class IngestionCatchupTests(TestCase):
    """The net under Celery Beat: a worker starting up runs the overdue
    infrastructure syncs once, so a spell with Beat down does not leave a pool
    stranded without fixtures."""

    def setUp(self):
        cache.delete(LOCK_KEY)
        self.addCleanup(cache.delete, LOCK_KEY)

        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)
        self.interval = IntervalSchedule.objects.create(every=86400, period=IntervalSchedule.SECONDS)

    def schedule(self, task, *, enabled=True, last_run_at=None):
        return PeriodicTask.objects.create(
            name=task, task=task, interval=self.interval, enabled=enabled, last_run_at=last_run_at
        )

    def dispatch(self):
        """Run the catch-up, returning what it sent rather than sending it."""
        with patch("otterball_v2.celery.app.send_task") as send_task:
            dispatched = run_catchup()
        return dispatched, [call.args[0] for call in send_task.call_args_list]

    def test_a_task_that_has_never_run_is_caught_up(self):
        self.schedule("sports.tasks.sync_nfl_infrastructure")

        dispatched, sent = self.dispatch()

        self.assertEqual(dispatched, ["sports.tasks.sync_nfl_infrastructure"])
        self.assertEqual(sent, ["sports.tasks.sync_nfl_infrastructure"])

    def test_a_long_unrun_task_is_caught_up(self):
        self.schedule(
            "sports.tasks.sync_nfl_infrastructure",
            last_run_at=timezone.now() - datetime.timedelta(days=5),
        )

        dispatched, _ = self.dispatch()

        self.assertEqual(dispatched, ["sports.tasks.sync_nfl_infrastructure"])

    def test_a_recently_run_task_is_left_alone(self):
        self.schedule(
            "sports.tasks.sync_nfl_infrastructure",
            last_run_at=timezone.now() - datetime.timedelta(hours=2),
        )

        dispatched, sent = self.dispatch()

        self.assertEqual((dispatched, sent), ([], []))

    def test_the_live_syncs_are_never_caught_up(self):
        """They run every two minutes; a one-shot at startup settles nothing
        the next tick would not have."""
        self.schedule("sports.tasks.sync_nfl_live_games")

        dispatched, _ = self.dispatch()

        self.assertEqual(dispatched, [])

    def test_a_sport_with_no_active_pool_is_skipped(self):
        """Nobody is playing it, so there is nothing to strand."""
        self.pool.is_active = False
        self.pool.save()
        self.schedule("sports.tasks.sync_nfl_infrastructure")

        dispatched, _ = self.dispatch()

        self.assertEqual(dispatched, [])

    def test_another_sports_schedule_is_not_touched(self):
        self.schedule("sports.tasks.sync_daily_infrastructure")

        dispatched, _ = self.dispatch()

        self.assertEqual(dispatched, [])

    def test_a_disabled_schedule_is_respected(self):
        """Someone turned it off deliberately; the net does not override that."""
        self.schedule("sports.tasks.sync_nfl_infrastructure", enabled=False)

        dispatched, _ = self.dispatch()

        self.assertEqual(dispatched, [])

    def test_an_unscheduled_task_is_not_resurrected(self):
        """A missing PeriodicTask row is a missing schedule, which
        ensure_schedule installs and readiness reports as a failure. Running it
        from here would paper over exactly that."""
        dispatched, _ = self.dispatch()

        self.assertEqual(dispatched, [])

    def test_only_one_worker_in_a_restart_wave_dispatches(self):
        self.schedule("sports.tasks.sync_nfl_infrastructure")

        first, _ = self.dispatch()
        second, sent_again = self.dispatch()

        self.assertEqual(first, ["sports.tasks.sync_nfl_infrastructure"])
        self.assertEqual((second, sent_again), ([], []))

    @override_settings(INGESTION_CATCHUP_ENABLED=False)
    def test_it_can_be_switched_off(self):
        self.schedule("sports.tasks.sync_nfl_infrastructure")

        dispatched, _ = self.dispatch()

        self.assertEqual(dispatched, [])

    def test_it_does_not_stamp_last_run_at(self):
        """The catch-up must not make a dead Beat look alive: last_run_at is
        Beat's, so check_pool goes on reporting the schedule as stale."""
        row = self.schedule("sports.tasks.sync_nfl_infrastructure")

        self.dispatch()

        row.refresh_from_db()
        self.assertIsNone(row.last_run_at)

    def test_a_broken_cache_does_not_stop_the_catch_up(self):
        """A worker that cannot reach the cache should still start, and
        duplicate ingestion is idempotent where a crashed worker is not."""
        self.schedule("sports.tasks.sync_nfl_infrastructure")

        with patch("sports.catchup.cache.add", side_effect=ConnectionError("no redis")):
            dispatched, _ = self.dispatch()

        self.assertEqual(dispatched, ["sports.tasks.sync_nfl_infrastructure"])
