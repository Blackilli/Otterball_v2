import datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from sports.schedules import SCHEDULED_TASKS, describe_interval


class DescribeIntervalTests(TestCase):
    def test_reads_as_a_cadence(self):
        self.assertEqual(describe_interval(datetime.timedelta(minutes=2)), "every 2 minutes")
        self.assertEqual(describe_interval(datetime.timedelta(hours=6)), "every 6 hours")
        self.assertEqual(describe_interval(datetime.timedelta(days=1)), "every day")


class EnsureScheduleCommandTests(TestCase):
    """Beat is database-driven, so an unscheduled task never runs and nothing
    says so. This command is what stops that being a manual admin step."""

    def run_command(self, **options):
        out = StringIO()
        call_command("ensure_schedule", stdout=out, **options)
        return out.getvalue()

    def test_installs_every_declared_task(self):
        self.run_command()

        scheduled = set(PeriodicTask.objects.values_list("task", flat=True))
        self.assertEqual(scheduled, {task.task for task in SCHEDULED_TASKS})
        self.assertTrue(all(PeriodicTask.objects.values_list("enabled", flat=True)))

    def test_intervals_match_the_declaration(self):
        self.run_command()

        row = PeriodicTask.objects.get(task="sports.tasks.sync_nfl_live_games")
        self.assertEqual(row.interval.every, 120)
        self.assertEqual(row.interval.period, IntervalSchedule.SECONDS)

    def test_is_idempotent(self):
        self.run_command()
        before = set(PeriodicTask.objects.values_list("id", flat=True))

        output = self.run_command()

        self.assertEqual(set(PeriodicTask.objects.values_list("id", flat=True)), before)
        self.assertIn("Every ingestion task is scheduled.", output)

    def test_an_existing_row_is_matched_on_its_task_not_its_label(self):
        """A hand-made row can be called anything; matching on our label would
        add a second row scheduling the same task."""
        interval = IntervalSchedule.objects.create(every=30, period=IntervalSchedule.SECONDS)
        existing = PeriodicTask.objects.create(
            name="Live Match Sync", task="sports.tasks.sync_live_games", interval=interval, enabled=False
        )

        self.run_command()

        self.assertEqual(PeriodicTask.objects.filter(task="sports.tasks.sync_live_games").count(), 1)
        existing.refresh_from_db()
        self.assertEqual((existing.name, existing.interval.every, existing.enabled), ("Live Match Sync", 30, False))

    def test_reset_puts_an_existing_row_back_to_the_declared_cadence(self):
        interval = IntervalSchedule.objects.create(every=30, period=IntervalSchedule.SECONDS)
        existing = PeriodicTask.objects.create(
            name="Live Match Sync", task="sports.tasks.sync_live_games", interval=interval, enabled=False
        )

        self.run_command(reset=True)

        existing.refresh_from_db()
        self.assertEqual(existing.interval.every, 120)
        self.assertTrue(existing.enabled)

    def test_dry_run_writes_nothing(self):
        output = self.run_command(dry_run=True)

        self.assertFalse(PeriodicTask.objects.exists())
        self.assertIn("Would create", output)

    def test_a_taken_label_does_not_block_the_install(self):
        """PeriodicTask.name is unique, and the label is only cosmetic."""
        interval = IntervalSchedule.objects.create(every=30, period=IntervalSchedule.SECONDS)
        PeriodicTask.objects.create(name="NFL live match sync", task="something.else", interval=interval)

        self.run_command()

        row = PeriodicTask.objects.get(task="sports.tasks.sync_nfl_live_games")
        self.assertIn("sports.tasks.sync_nfl_live_games", row.name)
