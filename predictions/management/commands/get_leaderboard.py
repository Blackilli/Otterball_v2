import asyncio
import logging

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from predictions.models import PredictionPool

logger = logging.getLogger(__name__)

User = get_user_model()


class Command(BaseCommand):
    help = "Gets the leaderboard for a given pool"

    def add_arguments(self, parser):
        parser.add_argument("pool_id", type=int)

    async def async_handle(self, *args, **options):
        pool = await PredictionPool.objects.aget(id=options["pool_id"])
        print(
            {
                user.username: points
                async for user, points in pool.aget_user_with_points()
            }
        )

    def handle(self, *args, **options):
        asyncio.run(self.async_handle(*args, **options))
