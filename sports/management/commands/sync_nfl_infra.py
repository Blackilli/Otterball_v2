import asyncio
import datetime
import logging

from django.core.management.base import BaseCommand

from sports.services.ingestion import (
    current_nfl_season_year,
    ingest_espn_nfl_infrastructure,
    ingest_espn_nfl_live_matches,
    ingest_espn_nfl_matches,
    ingest_espn_nfl_teams,
    ingest_nflverse_nfl_matches,
    ingest_nflverse_team_mappings,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Seeds NFL infrastructure from ESPN's public API. "
        "With no flags, runs the full setup (skeleton, teams, schedule) - "
        "which is what you want when starting a new NFL pool."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--season",
            type=int,
            default=None,
            help="NFL season year (defaults to the current one; a season is named for the year it kicks off in)",
        )
        parser.add_argument(
            "--sync-infrastructure",
            action="store_true",
            help="Creates the NFL competition, the season, and its five scoreable rounds",
        )
        parser.add_argument(
            "--sync-teams",
            action="store_true",
            help="Triggers the mass-ingestion of the 32 NFL franchises (logos + colors)",
        )
        parser.add_argument(
            "--sync-upcoming-matches",
            action="store_true",
            help="Triggers the mass-ingestion of upcoming matches",
        )
        parser.add_argument(
            "--sync-nflverse",
            action="store_true",
            help="Cross-checks the schedule and pulls final results from nflverse (also attaches ESPN event ids)",
        )
        parser.add_argument(
            "--sync-live-matches",
            action="store_true",
            help="Triggers a status/score refresh for matches that are live or already due",
        )
        parser.add_argument(
            "--lookahead-days",
            type=int,
            default=14,
            help="How far ahead --sync-upcoming-matches looks (default: 14)",
        )

    def _step(self, label: str, coro_factory):
        self.stdout.write(self.style.WARNING(f"{label}..."))
        try:
            asyncio.run(coro_factory())
            self.stdout.write(self.style.SUCCESS(f"{label} completed successfully."))
            return True
        except Exception as e:
            logger.exception(f"{label} failed.")
            self.stderr.write(self.style.ERROR(f"Sync failed: {e}"))
            return False

    def handle(self, *args, **options):
        season = options.get("season") or current_nfl_season_year()

        selected = {
            "infrastructure": options.get("sync_infrastructure"),
            "teams": options.get("sync_teams"),
            "upcoming": options.get("sync_upcoming_matches"),
            "live": options.get("sync_live_matches"),
            "nflverse": options.get("sync_nflverse"),
        }
        # No flags at all means "set this season up for me".
        if not any(selected.values()):
            selected["infrastructure"] = selected["teams"] = selected["upcoming"] = True
            selected["nflverse"] = True

        self.stdout.write(f"NFL season {season}")

        if selected["infrastructure"]:
            self._step(
                "Syncing NFL competition, season and rounds",
                lambda: ingest_espn_nfl_infrastructure(season),
            )

        if selected["teams"]:
            self._step("Syncing NFL teams", lambda: ingest_espn_nfl_teams(season))
            self._step("Bridging team abbreviations to nflverse", lambda: ingest_nflverse_team_mappings(season))

        if selected["upcoming"]:
            lookahead = datetime.timedelta(days=options["lookahead_days"])
            self._step(
                f"Syncing NFL matches for the next {options['lookahead_days']} days",
                lambda: ingest_espn_nfl_matches(lookahead),
            )

        if selected["nflverse"]:
            self._step(
                f"Cross-checking the {season} schedule and results against nflverse",
                lambda: ingest_nflverse_nfl_matches({season}),
            )

        if selected["live"]:
            self._step("Syncing live NFL matches", ingest_espn_nfl_live_matches)
