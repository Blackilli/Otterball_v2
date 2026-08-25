import asyncio
import logging
from typing import Any, Coroutine

from celery import shared_task

from sports.services.ingestion import (
    ingest_all_fifa_competitions,
    ingest_espn_nfl_infrastructure,
    ingest_espn_nfl_live_matches,
    ingest_espn_nfl_matches,
    ingest_espn_nfl_teams,
    ingest_fifa_live_matches,
    ingest_fifa_national_teams,
    ingest_fifa_seasons,
    ingest_fifa_stages,
    ingest_nflverse_nfl_matches,
    ingest_nflverse_team_mappings,
    ingest_upcoming_matches,
)

logger = logging.getLogger(__name__)


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive one async ingestion call from a synchronous Celery task.

    Every ingest_* function is `async def`. Calling one without awaiting it
    just builds a coroutine and throws it away, which is exactly what these
    tasks used to do - the syncs only ever ran via the management commands,
    which wrap them the same way this does.
    """
    return asyncio.run(coro)


@shared_task(name="sports.tasks.sync_daily_infrastructure")
def sync_daily_infrastructure():
    logger.info("Syncing daily infrastructure")
    try:
        _run(ingest_all_fifa_competitions())
        _run(ingest_fifa_national_teams())
        _run(ingest_fifa_seasons())
        _run(ingest_fifa_stages())
        _run(ingest_upcoming_matches())
        logger.info("Daily infrastructure synced successfully")
    except Exception as e:
        logger.error(f"Error syncing daily infrastructure: {e}")
        raise e


@shared_task(name="sports.tasks.sync_live_games")
def sync_live_games():
    logger.info("Syncing live games")
    try:
        _run(ingest_fifa_live_matches())
        logger.info("Live games synced successfully")
    except Exception as e:
        logger.error(f"Error syncing live games: {e}")


@shared_task(name="sports.tasks.sync_nfl_infrastructure")
def sync_nfl_infrastructure(season_year: int | None = None):
    """Daily: NFL competition/season/rounds, franchises, and the schedule.

    Order matters - matches resolve their stage and teams through the mapping
    tables the first two steps write.
    """
    logger.info("Syncing NFL infrastructure")
    try:
        _run(ingest_espn_nfl_infrastructure(season_year))
        _run(ingest_espn_nfl_teams(season_year))
        _run(ingest_nflverse_team_mappings(season_year))
        _run(ingest_espn_nfl_matches())
        logger.info("NFL infrastructure synced successfully")
    except Exception as e:
        logger.error(f"Error syncing NFL infrastructure: {e}")
        raise e


@shared_task(name="sports.tasks.sync_nfl_live_games")
def sync_nfl_live_games():
    logger.info("Syncing NFL live games")
    try:
        _run(ingest_espn_nfl_live_matches())
        logger.info("NFL live games synced successfully")
    except Exception as e:
        logger.error(f"Error syncing NFL live games: {e}")


@shared_task(name="sports.tasks.sync_nflverse_results")
def sync_nflverse_results(season_year: int | None = None):
    """Second opinion on the schedule and final results.

    nflverse is a batch export, so this is not a live path - run it a few
    times a day, not every couple of minutes. It backstops ESPN two ways: it
    finalizes any match ESPN left unsettled, and it attaches the ESPN event id
    it ships for every game, so matches stay correctly keyed even if the ESPN
    endpoints stop answering.
    """
    logger.info("Syncing nflverse results")
    try:
        _run(ingest_nflverse_nfl_matches({season_year} if season_year else None))
        logger.info("nflverse results synced successfully")
    except Exception as e:
        logger.error(f"Error syncing nflverse results: {e}")
