"""Client for the nflverse schedule dataset.

nflverse publishes the community NFL data used by the R `nflverse` packages
and the Python `nfl_data_py` wrapper. This talks to the *data* directly - one
CSV asset on a GitHub release, no auth, no pandas - rather than going through
`nfl_data_py`, which pins numpy/pandas and whose classifiers stop at Python
3.12 (this project is 3.14+).

What it is good for: the schedule and **final** results, covering 1999 through
the current season. What it is not: a live feed. The file is a batch export
refreshed after games, so it never carries in-progress state - ESPN's
scoreboard stays the source for that.

The reason it is worth having alongside ESPN is the ``espn`` column: every row
carries the matching ESPN event id (verified at 100% coverage for 2025 and
2026). That is a free, precomputed cross-reference between the two providers -
the exact thing the old Otterball-NFL bot had to derive by fuzzy-matching on
(team abbreviation, kickoff +- 24h).

Watch the team abbreviations: nflverse and ESPN agree on 30 of 32, but
nflverse says ``LA``/``WAS`` where ESPN says ``LAR``/``WSH``. Mappings are
keyed by nflverse's own abbreviation; NFLVERSE_TO_ESPN_TEAM_ABBREVIATIONS in
sports/constants.py bridges the two.
"""

import csv
import datetime
import io
import logging
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
from httpx2 import Response
from pydantic import BaseModel, field_validator

from sports.integrations.base import RateLimitedAsyncTransport

logger = logging.getLogger(__name__)

NFLVERSE_GAMES_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"

# `gameday` + `gametime` are wall-clock Eastern, not UTC.
NFLVERSE_TIMEZONE = ZoneInfo("America/New_York")

# Values the CSV uses for "no value yet".
_EMPTY = {"", "NA", "N/A", "None", "null"}


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and value.strip() in _EMPTY:
        return None
    return value


class Game(BaseModel):
    """One row of games.csv.

    Only the columns ingestion needs are modelled; the file carries ~46,
    most of them betting lines, stadium details and alternate provider ids.
    """

    game_id: str
    season: int
    # REG / WC / DIV / CON / SB - the same keys as NFL_STAGE_BLUEPRINTS.
    game_type: str
    week: int | None = None
    gameday: datetime.date | None = None
    gametime: str | None = None
    away_team: str
    home_team: str
    away_score: int | None = None
    home_score: int | None = None
    # Home score minus away score; 0 means a tie, so test against None.
    result: int | None = None
    espn: str | None = None

    @field_validator("week", "gameday", "gametime", "away_score", "home_score", "result", "espn", mode="before")
    @classmethod
    def empty_string_is_none(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @property
    def kickoff(self) -> datetime.datetime | None:
        """Kickoff in UTC, or None if the slot is not scheduled yet."""
        if not self.gameday or not self.gametime:
            return None
        try:
            naive = datetime.datetime.strptime(f"{self.gameday:%Y-%m-%d} {self.gametime}", "%Y-%m-%d %H:%M")
        except ValueError:
            logger.warning(f"Unparseable kickoff for {self.game_id}: {self.gameday} {self.gametime}")
            return None
        return naive.replace(tzinfo=NFLVERSE_TIMEZONE).astimezone(datetime.timezone.utc)

    @property
    def is_final(self) -> bool:
        """Scores are only filled in once the game has been played."""
        return self.home_score is not None and self.away_score is not None


class NflverseClient:
    def __init__(self, timeout: float = 60.0, requests_per_second: float = 4.0):
        # The CSV is ~2MB; allow more time than the JSON APIs get.
        self.timeout = timeout
        self.requests_per_second = requests_per_second
        self._client: httpx2.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx2.AsyncClient(
            timeout=self.timeout,
            transport=RateLimitedAsyncTransport(requests_per_second=self.requests_per_second),
            # The release asset redirects to objects.githubusercontent.com.
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx2.AsyncClient:
        if self._client is None:
            raise RuntimeError("NflverseClient must be used inside an 'async with' block!")
        return self._client

    async def get_games(self, seasons: set[int] | None = None) -> list[Game]:
        """Every scheduled game, optionally narrowed to `seasons`.

        The file holds every season since 1999, so filtering early keeps the
        ~7.5k rows from turning into pointless model construction.
        """
        response: Response = await self.client.get(NFLVERSE_GAMES_URL)
        response.raise_for_status()
        return self.parse_games(response.text, seasons=seasons)

    @staticmethod
    def parse_games(csv_text: str, seasons: set[int] | None = None) -> list[Game]:
        games: list[Game] = []

        for row in csv.DictReader(io.StringIO(csv_text)):
            if seasons is not None:
                # The unparenthesized except group below is PEP 758, new in
                # Python 3.14 (which this project requires, and which the
                # Dockerfile pins). It reads as a SyntaxError on older
                # interpreters and to tooling that has not caught up; black
                # normalizes to this form, so don't "fix" the parentheses back.
                try:
                    if int(row["season"]) not in seasons:
                        continue
                except KeyError, TypeError, ValueError:
                    continue
            try:
                games.append(Game.model_validate(row))
            except Exception as e:
                logger.error(f"Skipping unparseable nflverse row {row.get('game_id')}: {e}")

        return games
