import asyncio
import logging

from django.core.management.base import BaseCommand

from predictions.models import PoolStageRule, Prediction
from sports.models import MatchStatus

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Updates the points for all predictions"

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Updates all predictions, not just unprocessed ones",
        )

    async def async_handle(self, *args, **options):
        rules_cache: dict[tuple[int, int | None], int] = {
            (r.pool_id, r.stage_id): r.points_per_correct async for r in PoolStageRule.objects.all().aiterator()
        }

        query = Prediction.objects.filter(
            match__status=MatchStatus.FINISHED,
        ).select_related("match", "pool")

        if not options["all"]:
            query = query.filter(is_processed=False)

        counter = 0
        async for prediction in query.aiterator():
            p_id = prediction.pool_id
            s_id = prediction.match.stage_id

            points = rules_cache.get((p_id, s_id))
            if points is None:
                points = rules_cache.get((p_id, None), 3)

            await prediction.aupdate_points(
                force=options["all"],
                cached_points=points,
            )
            counter += 1
        logger.info(f"Updated points for {counter} predictions")

    def handle(self, *args, **options):
        asyncio.run(self.async_handle(*args, **options))
        return
