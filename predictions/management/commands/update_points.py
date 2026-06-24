import asyncio
import logging

from django.core.management.base import BaseCommand

from predictions.models import Prediction
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
        query = Prediction.objects.filter(
            match__status=MatchStatus.FINISHED,
        ).select_related("match", "match__stage", "pool")
        if not options["all"]:
            query = query.filter(is_processed=False)
        async for prediction in query.aiterator():
            await prediction.aupdate_points(force=options["all"])

    def handle(self, *args, **options):
        asyncio.run(self.async_handle(*args, **options))
        return
