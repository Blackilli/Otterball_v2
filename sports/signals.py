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
def notify_match_update(sender, instance: Match, created: bool, raw: bool = False, **kwargs):
    # `raw` means a fixture load (import_db/loaddata): the rows are a restore, not live match
    # movement, so nothing should be broadcast for them.
    if created or raw:
        return
    try:
        payload = MatchUpdatePayload(
            match_id=instance.id,
            status=MatchStatus(instance.status),
            home_score=instance.home_score,
            away_score=instance.away_score,
        )

        message_json = payload.model_dump_json()
        match_id = instance.id

        def publish():
            # An unreachable Redis is an expected, survivable condition, not a defect: a
            # developer box with no valkey running, or a machine being restored onto. Catching
            # it here keeps that to one warning line. Leaving it to robust=True instead logs at
            # ERROR with a full traceback - and LOGGING sets tracebacks_show_locals, so that is
            # a couple of hundred lines per match saved, which buries the ingestion output the
            # run was actually for.
            try:
                redis_client.publish(settings.REDIS_MATCH_UPDATE_TOPIC, message_json)
            except redis.RedisError as err:
                logger.warning(f"Match {match_id} update not published, Redis is unreachable: {err}")

        # robust=True: the publish runs after the commit, so nothing raised here can take the
        # writing command down with it - the Redis failure above is handled, and this covers
        # whatever is not. MatchTickerCog's 1-minute loop re-reads the match anyway, so a
        # dropped message costs latency, not correctness.
        transaction.on_commit(publish, robust=True)
    except Exception as e:
        logger.error(f"Error notifying match update: {e}")
