import logging

from celery import shared_task

from sports.services.ingestion import (
    ingest_all_fifa_competitions,
    ingest_fifa_live_matches,
    ingest_fifa_national_teams,
    ingest_fifa_seasons,
    ingest_fifa_stages,
    ingest_upcoming_matches,
)

logger = logging.getLogger(__name__)


@shared_task(name="sports.tasks.sync_daily_infrastructure")
def sync_daily_infrastructure():
    logger.info("Syncing daily infrastructure")
    try:
        ingest_all_fifa_competitions()
        ingest_fifa_national_teams()
        ingest_fifa_seasons()
        ingest_fifa_stages()
        ingest_upcoming_matches()
        logger.info("Daily infrastructure synced successfully")
    except Exception as e:
        logger.error(f"Error syncing daily infrastructure: {e}")
        raise e


@shared_task(name="sports.tasks.sync_live_games")
def sync_live_games():
    logger.info("Syncing live games")
    try:
        ingest_fifa_live_matches()
        logger.info("Live games synced successfully")
    except Exception as e:
        logger.error(f"Error syncing live games: {e}")
