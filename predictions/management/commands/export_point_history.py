import csv
import logging
from itertools import groupby

from django.core.management.base import BaseCommand, CommandError

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

        usernames = sorted({prediction.user.username for prediction in predictions})
        totals = dict.fromkeys(usernames, 0)

        rows = []
        for _match_id, match_predictions in groupby(predictions, key=lambda p: p.match_id):
            match_predictions = list(match_predictions)
            match = match_predictions[0].match
            for prediction in match_predictions:
                totals[prediction.user.username] += prediction.points_awarded
            rows.append(
                [
                    match.kickoff.isoformat(),
                    f"{match.home_team} vs. {match.away_team}",
                    *(totals[username] for username in usernames),
                ]
            )

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
