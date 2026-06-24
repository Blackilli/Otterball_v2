import asyncio
import logging

from django.core.management.base import BaseCommand

from sports.services.ingestion import (
    ingest_all_fifa_competitions,
    ingest_fifa_live_matches,
    ingest_fifa_national_teams,
    ingest_fifa_seasons,
    ingest_fifa_stages,
    ingest_upcoming_matches,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Seeds initial core infrastructure entries directly from the official FIFA data stream."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sync-teams",
            action="store_true",
            help="Triggers the mass-ingestion of national teams",
        )
        parser.add_argument(
            "--sync-competitions",
            action="store_true",
            help="Triggers the mass-ingestion of all global competitions",
        )
        parser.add_argument(
            "--sync-seasons",
            action="store_true",
            help="Triggers the mass-ingestion of all featured seasons",
        )
        parser.add_argument(
            "--sync-stages",
            action="store_true",
            help="Triggers the mass-ingestion of all featured stages",
        )
        parser.add_argument(
            "--sync-upcoming-matches",
            action="store_true",
            help="Triggers the mass-ingestion of upcoming matches",
        )
        parser.add_argument(
            "--sync-live-matches",
            action="store_true",
            help="Triggers the mass-ingestion of live matches",
        )

    def handle(self, *args, **options):
        if options.get("sync_teams"):
            self.stdout.write(self.style.WARNING("Syncing FIFA teams..."))
            try:
                asyncio.run(ingest_fifa_national_teams())
                self.stdout.write(self.style.SUCCESS("Teams synced successfully."))
            except Exception as e:
                # 3. Secure the full stack trace in your logs
                logger.exception("FIFA team mass-ingestion failed.")
                # 4. Keep terminal feedback clean for the operator
                self.stderr.write(self.style.ERROR(f"Sync failed: {e}"))

        if options.get("sync_competitions"):
            self.stdout.write(self.style.WARNING("Syncing FIFA competitions..."))
            try:
                asyncio.run(ingest_all_fifa_competitions())
                self.stdout.write(self.style.SUCCESS("Competitions synced successfully."))
            except Exception as e:
                logger.exception("FIFA competition mass-ingestion failed.")
                self.stderr.write(self.style.ERROR(f"Sync failed: {e}"))

        if options.get("sync_seasons"):
            self.stdout.write(self.style.WARNING("Syncing FIFA seasons..."))
            try:
                asyncio.run(ingest_fifa_seasons())
                self.stdout.write(self.style.SUCCESS("Seasons synced successfully."))
            except Exception as e:
                logger.exception("FIFA season mass-ingestion failed.")
                self.stderr.write(self.style.ERROR(f"Sync failed: {e}"))

        if options.get("sync_stages"):
            self.stdout.write(self.style.WARNING("Syncing FIFA stages..."))
            try:
                asyncio.run(ingest_fifa_stages())
                self.stdout.write(self.style.SUCCESS("Stages synced successfully."))
            except Exception as e:
                logger.exception("FIFA stage mass-ingestion failed.")

        if options.get("sync_upcoming_matches"):
            self.stdout.write(self.style.WARNING("Syncing upcoming matches..."))
            try:
                asyncio.run(ingest_upcoming_matches())
                self.stdout.write(self.style.SUCCESS("Upcoming matches synced successfully."))
            except Exception as e:
                logger.exception("FIFA upcoming match mass-ingestion failed.")
                self.stderr.write(self.style.ERROR(f"Sync failed: {e}"))

        if options.get("sync_live_matches"):
            self.stdout.write(self.style.WARNING("Syncing live matches..."))
            try:
                asyncio.run(ingest_fifa_live_matches())
                self.stdout.write(self.style.SUCCESS("Live matches synced successfully."))
            except Exception as e:
                logger.exception("FIFA live match mass-ingestion failed.")
                self.stderr.write(self.style.ERROR(f"Sync failed: {e}"))
