"""Client for ESPN's public (unauthenticated) NFL endpoints.

Two hosts are involved, because neither one alone covers the whole job:

* ``sports.core.api.espn.com`` - the structured "core" API. Owns the season
  skeleton (season types, weeks) and the schedule. Its payloads are
  HATEOAS-style: nested entities arrive as ``{"$ref": "https://..."}`` links
  rather than inline objects. Chasing every ref would mean four extra requests
  per game, so the ids we need (team, season type, week) are parsed straight
  out of the ref URLs instead - see ``_ref_segment``.
* ``site.web.api.espn.com`` - the fantasy scoreboard. Returns status *and*
  scores for a whole date range in a single request, which is what makes the
  live-score loop cheap.

The two are joined for free: a core ``Event.id`` is the same value as the
scoreboard's ``competitionId``, so one ``MatchMapping`` row serves both. That
is the key difference from the old Otterball-NFL bot, which had to fuzzy-match
two providers on (team abbreviation, kickoff +- 24h) and silently lost live
scores whenever the match failed.

Note that ``site.api.espn.com`` - the host the old bot used for teams and the
scoreboard - now returns 403 (Akamai "Access Denied"); the two hosts above are
the working replacements.
"""

import datetime
import io
import logging
import re
from enum import IntEnum, StrEnum
from typing import Any, AsyncGenerator
from urllib.parse import urlsplit

import httpx2
from httpx2 import Response
from PIL import Image
from PIL.ImageFile import ImageFile
from pydantic import BaseModel, Field, TypeAdapter, field_validator

from sports.integrations.base import RateLimitedAsyncTransport

logger = logging.getLogger(__name__)

CORE_BASE_URL = "https://sports.core.api.espn.com/"
SCOREBOARD_BASE_URL = "https://site.web.api.espn.com/"
NFL_PATH = "v2/sports/football/leagues/nfl"

# The scoreboard host sits behind a CDN that rejects requests without a
# browser-ish User-Agent.
BROWSER_USER_AGENT = "Mozilla/5.0"

ESPN_DATE_FORMAT = "%Y%m%d"


class SeasonType(IntEnum):
    """``/seasons/{year}/types/{id}``."""

    PRESEASON = 1
    REGULAR = 2
    POSTSEASON = 3
    OFFSEASON = 4


class EventStatus(StrEnum):
    """The scoreboard's ``status`` field."""

    PRE = "pre"
    IN = "in"
    POST = "post"


def _ref_segment(ref: str | None, segment: str) -> str | None:
    """Pull the id that follows ``segment`` out of a core-API ``$ref`` URL.

    ``.../seasons/2026/types/2/weeks/1?lang=en`` -> ``("types") == "2"``.
    Cheaper than dereferencing the link, which is the whole point.
    """
    if not ref:
        return None
    found = re.search(rf"/{re.escape(segment)}/([^/?]+)", urlsplit(ref).path)
    return found.group(1) if found else None


class Ref(BaseModel):
    ref: str | None = Field(default=None, alias="$ref")


class RefIndex(BaseModel):
    """The core API's list envelope - a page of ``$ref`` links, not entities."""

    count: int = 0
    page_index: int = Field(default=1, alias="pageIndex")
    page_size: int = Field(default=25, alias="pageSize")
    page_count: int = Field(default=1, alias="pageCount")
    items: list[Ref] = Field(default_factory=list)


class Logo(BaseModel):
    href: str | None = None
    width: int | None = None
    height: int | None = None
    rel: list[str] = Field(default_factory=list)


class Team(BaseModel):
    id: str
    abbreviation: str | None = None
    display_name: str | None = Field(default=None, alias="displayName")
    short_display_name: str | None = Field(default=None, alias="shortDisplayName")
    name: str | None = None
    location: str | None = None
    color: str | None = None
    alternate_color: str | None = Field(default=None, alias="alternateColor")
    is_active: bool = Field(default=True, alias="isActive")
    logos: list[Logo] = Field(default_factory=list)

    @property
    def full_name(self) -> str:
        """ "Los Angeles Rams" - ``displayName``, with a fallback for safety."""
        if self.display_name:
            return self.display_name
        return " ".join(part for part in (self.location, self.name) if part) or self.abbreviation or f"Team {self.id}"

    @property
    def logo_url(self) -> str | None:
        """The plain full-colour logo, skipping the ``dark``/``scoreboard`` variants."""
        for logo in self.logos:
            if logo.href and "default" in logo.rel:
                return logo.href
        for logo in self.logos:
            if logo.href and "dark" not in logo.rel:
                return logo.href
        return None

    @property
    def hex_color(self) -> str | None:
        """ESPN returns bare hex ("003594"); Team.color wants "#003594"."""
        if not self.color:
            return None
        return f"#{self.color.lstrip('#')}"


class SeasonInfo(BaseModel):
    """``/seasons/{year}`` - bounds for the whole league year.

    Wider than any single season type: it opens with the preseason and closes
    after the Super Bowl, which is the window a pool should count as active.
    """

    year: int
    display_name: str | None = Field(default=None, alias="displayName")
    start_date: datetime.datetime | None = Field(default=None, alias="startDate")
    end_date: datetime.datetime | None = Field(default=None, alias="endDate")


class SeasonTypeInfo(BaseModel):
    id: str
    type: int | None = None
    name: str | None = None
    abbreviation: str | None = None
    year: int | None = None
    start_date: datetime.datetime | None = Field(default=None, alias="startDate")
    end_date: datetime.datetime | None = Field(default=None, alias="endDate")


class Week(BaseModel):
    number: int
    text: str | None = None
    start_date: datetime.datetime | None = Field(default=None, alias="startDate")
    end_date: datetime.datetime | None = Field(default=None, alias="endDate")


class Competitor(BaseModel):
    id: str
    home_away: str | None = Field(default=None, alias="homeAway")
    winner: bool | None = None
    team: Ref = Field(default_factory=Ref)

    @property
    def team_id(self) -> str | None:
        return _ref_segment(self.team.ref, "teams")


class Competition(BaseModel):
    id: str
    date: datetime.datetime | None = None
    competitors: list[Competitor] = Field(default_factory=list)


class Event(BaseModel):
    """One scheduled game, from ``/events`` or ``/events/{id}``."""

    id: str
    date: datetime.datetime
    name: str | None = None
    short_name: str | None = Field(default=None, alias="shortName")
    season_type: Ref = Field(default_factory=Ref, alias="seasonType")
    week: Ref = Field(default_factory=Ref)
    competitions: list[Competition] = Field(default_factory=list)

    @property
    def season_type_id(self) -> int | None:
        raw = _ref_segment(self.season_type.ref, "types")
        return int(raw) if raw and raw.isdigit() else None

    @property
    def season_year(self) -> int | None:
        raw = _ref_segment(self.season_type.ref, "seasons") or _ref_segment(self.week.ref, "seasons")
        return int(raw) if raw and raw.isdigit() else None

    @property
    def week_number(self) -> int | None:
        raw = _ref_segment(self.week.ref, "weeks")
        return int(raw) if raw and raw.isdigit() else None

    def _competitor(self, home_away: str) -> Competitor | None:
        for competition in self.competitions:
            for competitor in competition.competitors:
                if competitor.home_away == home_away:
                    return competitor
        return None

    @property
    def home(self) -> Competitor | None:
        return self._competitor("home")

    @property
    def away(self) -> Competitor | None:
        return self._competitor("away")


class ScoreboardCompetitor(BaseModel):
    id: str
    home_away: str | None = Field(default=None, alias="homeAway")
    abbreviation: str | None = None
    name: str | None = None
    winner: bool | None = None
    score: int | None = None

    @field_validator("score", mode="before")
    @classmethod
    def parse_score(cls, value: Any) -> Any:
        """Unplayed games report ``score: ""``, not ``null``.

        The old bot coerced that to ``0``, which makes a not-yet-played game
        look like a 0-0 draw. Keep it ``None`` so ``Match.outcome`` stays
        ``None`` until there is a real score.
        """
        if value is None or value == "":
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        return int(float(value))


class ScoreboardEvent(BaseModel):
    id: str
    competition_id: str | None = Field(default=None, alias="competitionId")
    date: datetime.datetime | None = None
    status: EventStatus | None = None
    summary: str | None = None
    period: int | None = None
    clock: str | None = None
    competitors: list[ScoreboardCompetitor] = Field(default_factory=list)

    @property
    def event_id(self) -> str:
        """The value that matches a core ``Event.id`` (and so ``MatchMapping``)."""
        return self.competition_id or self.id

    def _competitor(self, home_away: str) -> ScoreboardCompetitor | None:
        for competitor in self.competitors:
            if competitor.home_away == home_away:
                return competitor
        return None

    @property
    def home(self) -> ScoreboardCompetitor | None:
        return self._competitor("home")

    @property
    def away(self) -> ScoreboardCompetitor | None:
        return self._competitor("away")


class Scoreboard(BaseModel):
    events: list[ScoreboardEvent] = Field(default_factory=list)


class EspnClient:
    def __init__(self, timeout: float = 10.0, requests_per_second: float = 8.0):
        self.timeout = timeout
        self.requests_per_second = requests_per_second
        self._client: httpx2.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx2.AsyncClient(
            timeout=self.timeout,
            transport=RateLimitedAsyncTransport(requests_per_second=self.requests_per_second),
            headers={"User-Agent": BROWSER_USER_AGENT},
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
            raise RuntimeError("EspnClient must be used inside an 'async with' block!")
        return self._client

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response: Response = await self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def _iter_refs(self, url: str, params: dict[str, Any] | None = None) -> AsyncGenerator[str]:
        """Walk a core-API index, following its pagination, yielding ``$ref`` URLs."""
        params = dict(params or {})
        params.setdefault("limit", 100)
        page = 1

        while True:
            params["page"] = page
            index = RefIndex.model_validate(await self._get_json(url, params=params))

            for item in index.items:
                if item.ref:
                    yield item.ref

            if page >= index.page_count or not index.items:
                break
            page += 1

    async def _get_refs_as[T](self, url: str, model: type[T], params: dict[str, Any] | None = None) -> list[T]:
        """Resolve every ``$ref`` in an index into a parsed model.

        Failures are logged and skipped rather than raised - one bad entity
        should not take down a whole sync.
        """
        adapter = TypeAdapter(model)
        results: list[T] = []

        async for ref in self._iter_refs(url, params=params):
            try:
                results.append(adapter.validate_python(await self._get_json(ref)))
            except Exception as e:
                logger.error(f"Error resolving ESPN ref {ref}: {e}")
        return results

    # ---- core API -------------------------------------------------------

    async def get_teams(self, season: int) -> list[Team]:
        return await self._get_refs_as(f"{CORE_BASE_URL}{NFL_PATH}/seasons/{season}/teams", Team)

    async def get_season(self, season: int) -> SeasonInfo:
        return SeasonInfo.model_validate(await self._get_json(f"{CORE_BASE_URL}{NFL_PATH}/seasons/{season}"))

    async def get_season_types(self, season: int) -> list[SeasonTypeInfo]:
        return await self._get_refs_as(f"{CORE_BASE_URL}{NFL_PATH}/seasons/{season}/types", SeasonTypeInfo)

    async def get_season_type(self, season: int, season_type: SeasonType | int) -> SeasonTypeInfo:
        season_type = int(season_type)
        data = await self._get_json(f"{CORE_BASE_URL}{NFL_PATH}/seasons/{season}/types/{season_type}")
        return SeasonTypeInfo.model_validate(data)

    async def get_weeks(self, season: int, season_type: SeasonType | int) -> list[Week]:
        season_type = int(season_type)
        return await self._get_refs_as(
            f"{CORE_BASE_URL}{NFL_PATH}/seasons/{season}/types/{season_type}/weeks",
            Week,
        )

    async def get_events(
        self,
        start: datetime.date,
        end: datetime.date,
    ) -> list[Event]:
        """Every game kicking off in ``[start, end]`` (inclusive), across season types."""
        dates = f"{start.strftime(ESPN_DATE_FORMAT)}-{end.strftime(ESPN_DATE_FORMAT)}"
        return await self._get_refs_as(f"{CORE_BASE_URL}{NFL_PATH}/events", Event, params={"dates": dates})

    async def get_event(self, event_id: str) -> Event:
        return Event.model_validate(await self._get_json(f"{CORE_BASE_URL}{NFL_PATH}/events/{event_id}"))

    # ---- scoreboard (status + scores in one request) ---------------------

    async def get_scoreboard(
        self,
        start: datetime.date,
        end: datetime.date,
    ) -> list[ScoreboardEvent]:
        """Status and scores for every game in ``[start, end]``, in one request."""
        dates = f"{start.strftime(ESPN_DATE_FORMAT)}-{end.strftime(ESPN_DATE_FORMAT)}"
        data = await self._get_json(
            f"{SCOREBOARD_BASE_URL}apis/fantasy/v2/games/ffl/games",
            params={"dates": dates, "pbpOnly": "true"},
        )
        return Scoreboard.model_validate(data).events

    # ---- media ----------------------------------------------------------

    async def get_picture_by_url(self, url: str) -> ImageFile | None:
        try:
            response: Response = await self.client.get(url)
            response.raise_for_status()
            return Image.open(io.BytesIO(response.content))
        except Exception as e:
            logger.error(f"Error fetching picture from URL {url}: {e}")
            return None
