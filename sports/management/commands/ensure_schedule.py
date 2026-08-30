import logging

from django.core.management.base import BaseCommand
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from sports.schedules import SCHEDULED_TASKS, ScheduledTask, describe_interval

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Creates the PeriodicTask rows the ingestion tasks need. Celery Beat is database-driven "
        "here, so a task in sports/tasks.py never runs until one of these rows exists - and "
        "nothing announces its absence. Idempotent: a task that is already scheduled is left "
        "exactly as it is, however it was configured."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Also put existing rows back to the declared interval and re-enable them.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        created, updated, kept = [], [], []

        for scheduled in SCHEDULED_TASKS:
            # Matched on the dotted task name, not the label: an existing row
            # may be called anything ("Live Match Sync"), and matching on our
            # label would add a second row scheduling the same task.
            existing = PeriodicTask.objects.filter(task=scheduled.task).first()

            if existing and not options["reset"]:
                kept.append((scheduled, existing))
                continue

            if options["dry_run"]:
                (updated if existing else created).append((scheduled, existing))
                continue

            interval = self.interval_for(scheduled)
            if existing:
                existing.interval = interval
                existing.crontab = None
                existing.enabled = True
                existing.save(update_fields=["interval", "crontab", "enabled"])
                updated.append((scheduled, existing))
            else:
                created.append((scheduled, self.create(scheduled, interval)))

        self.report(created, updated, kept, dry_run=options["dry_run"])

    @staticmethod
    def interval_for(scheduled: ScheduledTask) -> IntervalSchedule:
        """Reuse an equivalent schedule rather than piling up duplicates.

        Expressed in seconds so two tasks an hour apart share one row instead
        of one saying 60 minutes and the other 1 hour.
        """
        interval, _ = IntervalSchedule.objects.get_or_create(
            every=int(scheduled.every.total_seconds()),
            period=IntervalSchedule.SECONDS,
        )
        return interval

    @staticmethod
    def create(scheduled: ScheduledTask, interval: IntervalSchedule) -> PeriodicTask:
        # PeriodicTask.name is unique; fall back to the dotted name so an
        # unrelated row that already owns the label cannot block the install.
        name = scheduled.name
        if PeriodicTask.objects.filter(name=name).exists():
            name = f"{scheduled.name} ({scheduled.task})"
        return PeriodicTask.objects.create(
            name=name,
            task=scheduled.task,
            interval=interval,
            enabled=True,
            description=scheduled.purpose,
        )

    def report(self, created, updated, kept, *, dry_run: bool):
        verb = "Would create" if dry_run else "Created"
        for scheduled, row in created:
            self.stdout.write(self.style.SUCCESS(f"{verb}  {scheduled.task}  ({describe_interval(scheduled.every)})"))
        for scheduled, row in updated:
            self.stdout.write(
                self.style.WARNING(
                    f"{'Would reset' if dry_run else 'Reset'}  {scheduled.task}  "
                    f"({describe_interval(scheduled.every)})"
                )
            )
        for scheduled, row in kept:
            state = "enabled" if row.enabled else self.style.WARNING("disabled")
            self.stdout.write(f"Kept     {scheduled.task}  (already scheduled as '{row.name}', {state})")

        if not created and not updated:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Every ingestion task is scheduled."))
        elif not dry_run:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(f"{len(created)} created, {len(updated)} reset."))
            self.stdout.write("Beat picks the changes up on its next tick; no restart needed.")
