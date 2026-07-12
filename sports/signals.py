import logging

import redis
from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from sports.models import Match, MatchStatus
from sports.schemas import MatchUpdatePayload

logger = logging.getLogger(__name__)

redis_client = redis.Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
)


@receiver(post_save, sender=Match)
def notify_match_update(sender, instance: Match, created: bool, **kwargs):
    if created:
        return
    try:
        payload = MatchUpdatePayload(
            match_id=instance.id,
            status=MatchStatus(instance.status),
            home_score=instance.home_score,
            away_score=instance.away_score,
        )

        message_json = payload.model_dump_json()

        transaction.on_commit(lambda: redis_client.publish(settings.REDIS_MATCH_UPDATE_TOPIC, message_json))
    except Exception as e:
        logger.error(f"Error notifying match update: {e}")
