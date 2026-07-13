import asyncio
import logging
from functools import partial

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from predictions.models import PoolStageRule, Prediction
from sports.models import Match, MatchStatus
from sports.schemas import MatchUpdatePayload

logger = logging.getLogger(__name__)


def process_match_update(match: Match) -> None:
    logger.info(f"Processing match update: {match.id}")
    rules_cache: dict[tuple[int, int | None], int] = {
        (r.pool_id, r.stage_id): r.points_per_correct for r in PoolStageRule.objects.all()
    }

    query = Prediction.objects.filter(
        match_id=match.id,
    ).select_related("pool")

    counter = 0
    for prediction in query.iterator():
        p_id = prediction.pool_id
        s_id = match.stage_id

        points = rules_cache.get((p_id, s_id))
        if points is None:
            points = rules_cache.get((p_id, None), 3)

        prediction.update_points(
            force=True,
            cached_points=points,
            cached_outcome=match.outcome,
            cached_match=match,
        )
        counter += 1
    logger.info(f"Updated points for {counter} predictions")


@receiver(post_save, sender=Match)
def receive_match_update(sender, instance: Match, created: bool, **kwargs):
    if created or instance.status != MatchStatus.FINISHED:
        return
    try:
        transaction.on_commit(partial(process_match_update, instance), robust=True)
    except Exception as e:
        logger.error(f"Error notifying match update: {e}")
