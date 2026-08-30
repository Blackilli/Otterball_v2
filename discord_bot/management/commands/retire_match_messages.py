"""Take ActiveMatchMessage rows out of the bot's loops by hand.

The bot retires a row on its own when the match is over and Discord refuses its
channel (see MatchTickerCog._retire_unreachable), but that only reaches rows the
ticker still walks. Restoring a finished season into a new deployment - a new
guild, a new bot application - leaves rows pointing at channels that will never
resolve again, and this is how you tell the bot to stop caring about them.

It never talks to Discord: it cannot, there is no bot here. What counts as dead
is the operator's call, which is why at least one filter is required.
"""

import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from discord_bot.models import ActiveMatchMessage


class Command(BaseCommand):
    help = "Mark ActiveMatchMessage rows finalized so the bot stops trying to reach their channels."

    def add_arguments(self, parser):
        parser.add_argument("--pool", type=int, help="Only rows of this PredictionPool id.")
        parser.add_argument("--season", type=int, help="Only rows whose match belongs to this Season id.")
        parser.add_argument("--guild", type=int, help="Only rows in this guild (Discord snowflake).")
        parser.add_argument("--channel", type=int, help="Only rows whose container is this channel or thread id.")
        parser.add_argument(
            "--before",
            help="Only matches that kicked off before this date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be retired and change nothing.",
        )

    def handle(self, *args, **options):
        queryset = ActiveMatchMessage.objects.filter(is_poll_finalized=False) | ActiveMatchMessage.objects.filter(
            is_ticker_finalized=False
        )
        queryset = queryset.distinct().select_related("match", "pool")

        filtered = False

        if options["pool"]:
            queryset = queryset.filter(pool_id=options["pool"])
            filtered = True
        if options["season"]:
            queryset = queryset.filter(match__stage__season_id=options["season"])
            filtered = True
        if options["guild"]:
            queryset = queryset.filter(guild_id=options["guild"])
            filtered = True
        if options["channel"]:
            container = options["channel"]
            # container_id is thread_id or channel_id, so match either.
            queryset = queryset.filter(thread_id=container) | queryset.filter(
                thread_id__isnull=True, channel_id=container
            )
            queryset = queryset.distinct()
            filtered = True
        if options["before"]:
            queryset = queryset.filter(match__kickoff__lt=self._parse_before(options["before"]))
            filtered = True

        if not filtered:
            raise CommandError(
                "Refusing to retire every unfinalized row. Narrow it with at least one of "
                "--pool, --season, --guild, --channel or --before."
            )

        rows = list(queryset.order_by("match__kickoff"))
        if not rows:
            self.stdout.write("Nothing to retire.")
            return

        for row in rows:
            self.stdout.write(
                f"  match {row.match_id} ({row.match}) in container {row.container_id}, pool {row.pool_id}"
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"Would retire {len(rows)} row(s). Nothing was changed."))
            return

        updated = ActiveMatchMessage.objects.filter(id__in=[row.id for row in rows]).update(
            is_poll_finalized=True,
            is_ticker_finalized=True,
        )
        self.stdout.write(self.style.SUCCESS(f"Retired {updated} row(s)."))
        self.stdout.write("Their polls stay in Discord untouched; the bot simply stops revisiting them.")

    @staticmethod
    def _parse_before(value: str) -> datetime.datetime:
        try:
            day = datetime.date.fromisoformat(value)
        except ValueError as e:
            raise CommandError(f"--before wants a YYYY-MM-DD date: {e}") from e
        return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min), timezone.get_current_timezone())
