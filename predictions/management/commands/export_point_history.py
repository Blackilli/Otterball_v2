import csv
import logging

from django.core.management.base import BaseCommand, CommandError

from predictions.history import build_rank_history
from predictions.models import Prediction, PredictionPool
from sports.models import MatchStatus

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Exports a pool's point distribution over time as CSV: one row per finished match "
        "(ordered by kickoff) with a cumulative points column per user, ready to plot as a line chart"
    )

    def add_arguments(self, parser):
        parser.add_argument("pool_id", type=int)
        parser.add_argument("output", nargs="?", help="Output CSV path (defaults to stdout)")

    def handle(self, *args, **options):
        try:
            pool = PredictionPool.objects.get(id=options["pool_id"])
        except PredictionPool.DoesNotExist:
            raise CommandError(f"PredictionPool with id {options['pool_id']} does not exist")

        predictions = list(
            Prediction.objects.filter(pool=pool, match__status=MatchStatus.FINISHED)
            .select_related("user", "match__home_team", "match__away_team")
            .order_by("match__kickoff", "match_id")
        )

        usernames_by_id = {prediction.user_id: prediction.user.username for prediction in predictions}
        usernames = sorted(usernames_by_id.values())
        user_ids_by_username = {username: user_id for user_id, username in usernames_by_id.items()}

        # Shared with the stats page's rank-over-time chart, so the CSV and the
        # chart cannot disagree about what the table looked like at match 40.
        # A player with no pick yet is absent from `standings` and columns out
        # as 0, which is what the running total said before they joined too.
        rows = [
            [
                entry.match.kickoff.isoformat(),
                f"{entry.match.home_team} vs. {entry.match.away_team}",
                *(
                    standing.points if (standing := entry.standings.get(user_ids_by_username[username])) else 0
                    for username in usernames
                ),
            ]
            for entry in build_rank_history(predictions)
        ]

        output = options["output"]
        stream = open(output, "w", newline="") if output else self.stdout
        try:
            writer = csv.writer(stream)
            writer.writerow(["kickoff", "match", *usernames])
            writer.writerows(rows)
        finally:
            if output:
                stream.close()
                self.stdout.write(
                    self.style.SUCCESS(f"Wrote {len(rows)} matches for {len(usernames)} users to {output}")
                )
