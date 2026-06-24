import logging

import redis
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from sports.models import Match, MatchStatus
from sports.schemas import MatchUpdatePayload

logger = logging.getLogger(__name__)

redis_client = redis.Redis.from_url(settings.REDIS_URL)


@receiver(post_save, sender=Match)
def notify_match_update(sender, instance: Match, created: bool, **kwargs):
    if not created:
        payload = MatchUpdatePayload(
            match_id=instance.id,
            status=MatchStatus(instance.status),
            home_score=instance.home_score,
            away_score=instance.away_score,
        )

        redis_client.publish("match_update", payload.model_dump_json())
