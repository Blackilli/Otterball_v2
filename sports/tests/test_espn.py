import datetime
import json
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from sports.constants import (
    ESPN_NFL_COMPETITION_EXTERNAL_ID,
    NFL_STAGE_BLUEPRINTS,
    nfl_stage_external_id,
    nfl_stage_key,
)
from sports.integrations.espn import (
    EspnClient,
    Event,
    EventStatus,
    RefIndex,
    Scoreboard,
    ScoreboardEvent,
    SeasonInfo,
    SeasonTypeInfo,
)
from sports.integrations.espn import Team as EspnTeam
from sports.integrations.espn import Week, _ref_segment
from sports.models import (
    Competition,
    CompetitionMapping,
    Match,
    MatchMapping,
    MatchOutcome,
    MatchStatus,
    Season,
    SeasonMapping,
    Sport,
    SportsProvider,
    Stage,
    StageMapping,
    StageType,
    Team,
    TeamMapping,
)
from sports.services.ingestion import (
    current_nfl_season_year,
    ingest_espn_nfl_infrastructure,
    ingest_espn_nfl_live_matches,
    ingest_espn_nfl_matches,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

NFL_STAGE_BLUEPRINTS_NAMES = [bp.name for bp in NFL_STAGE_BLUEPRINTS]


def load_fixture(name):
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


class RealEspnApiResponseTests(TestCase):
    """Guards sports/integrations/espn.py's schemas against drift from the
    real API. Each fixture is a genuine response captured from ESPN via
    `curl "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/..."`
    (and site.web.api.espn.com for the scoreboard) for the 2026 season, plus
    one already-played 2025 week for the finished-game shapes.

    This deliberately does NOT mirror RealFifaApiResponseTests' "assert zero
    unmapped keys" check. ESPN's payloads carry a large amount we never model
    - odds, drives, play-by-play, broadcast listings, 30-odd $ref links per
    team - and modelling all of it just to satisfy the assertion would be
    noise. The drift that actually breaks ingestion is a key we *depend on*
    being renamed or dropped, so that is what is asserted here instead.

    The two scoreboard fixtures have their play-by-play bulk (scoringPlays,
    drive, lastPlay, odds, pickcenter, againstTheSpread, links, broadcasts)
    stripped and are cut to three events; every key the parser reads is
    untouched. Refresh them with the curl commands above when touching the
    schemas."""

    def assert_required_keys(self, data, required, label):
        missing = set(required) - set(data.keys())
        self.assertEqual(missing, set(), f"{label}: API no longer returns {missing}")

    def test_team_response(self):
        data = load_fixture("espn_team_14.json")
        self.assert_required_keys(
            data,
            {"id", "abbreviation", "displayName", "location", "name", "color", "logos"},
            "Team",
        )

        team = EspnTeam.model_validate(data)
        self.assertEqual(team.id, "14")
        self.assertEqual(team.abbreviation, "LAR")
        self.assertEqual(team.full_name, "Los Angeles Rams")
        # ESPN returns bare hex; Team.color needs the leading '#'.
        self.assertEqual(team.hex_color, "#003594")
        # The plain full-colour logo, not the dark or scoreboard variant.
        self.assertEqual(team.logo_url, "https://a.espncdn.com/i/teamlogos/nfl/500/lar.png")

    def test_season_response(self):
        data = load_fixture("espn_season.json")
        self.assert_required_keys(data, {"year", "startDate", "endDate"}, "Season")

        season = SeasonInfo.model_validate(data)
        self.assertEqual(season.year, 2026)
        # Spans preseason through the Super Bowl, i.e. into the next calendar year.
        self.assertEqual(season.start_date.year, 2026)
        self.assertEqual(season.end_date.year, 2027)

    def test_season_type_response(self):
        data = load_fixture("espn_season_type_regular.json")
        self.assert_required_keys(data, {"id", "type", "name", "startDate", "endDate"}, "SeasonType")

        season_type = SeasonTypeInfo.model_validate(data)
        self.assertEqual(season_type.id, "2")
        self.assertEqual(season_type.name, "Regular Season")

    def test_postseason_week_response(self):
        data = load_fixture("espn_postseason_week_wildcard.json")
        self.assert_required_keys(data, {"number", "text"}, "Week")

        week = Week.model_validate(data)
        self.assertEqual(week.number, 1)
        # The round names this maps onto come straight from `text`.
        self.assertEqual(week.text, "Wild Card")

    def test_event_response(self):
        data = load_fixture("espn_event_detail.json")
        self.assert_required_keys(data, {"id", "date", "name", "seasonType", "week", "competitions"}, "Event")

        event = Event.model_validate(data)
        self.assertEqual(event.id, "401872657")
        # Season/type/week ids are parsed out of the $ref URLs rather than
        # fetched - if ESPN restructures those paths, this is what catches it.
        self.assertEqual(event.season_year, 2026)
        self.assertEqual(event.season_type_id, 2)
        self.assertEqual(event.week_number, 1)
        self.assertEqual(event.home.team_id, "14")
        self.assertEqual(event.away.team_id, "25")

    def test_event_index_is_a_page_of_refs(self):
        data = load_fixture("espn_events_index.json")
        self.assert_required_keys(data, {"count", "pageIndex", "pageCount", "items"}, "EventIndex")
        self.assertIn("$ref", data["items"][0])

    def test_finished_scoreboard_response(self):
        data = load_fixture("espn_scoreboard_final.json")
        for event in data["events"]:
            self.assert_required_keys(
                event, {"id", "competitionId", "date", "status", "competitors"}, f"ScoreboardEvent {event['id']}"
            )
            for competitor in event["competitors"]:
                self.assert_required_keys(competitor, {"id", "homeAway", "score"}, "ScoreboardCompetitor")

        events = {e.event_id: e for e in Scoreboard.model_validate(data).events}

        browns_vikings = events["401772633"]
        self.assertEqual(browns_vikings.status, EventStatus.POST)
        self.assertEqual(browns_vikings.home.abbreviation, "CLE")
        self.assertEqual(browns_vikings.home.score, 17)
        self.assertEqual(browns_vikings.away.abbreviation, "MIN")
        self.assertEqual(browns_vikings.away.score, 21)

    def test_upcoming_scoreboard_reports_empty_string_scores(self):
        """A not-yet-played game reports `score: ""`, not null.

        The old Otterball-NFL bot coerced that to 0, which makes an unplayed
        game look like a 0-0 draw and would hand out points for it."""
        data = load_fixture("espn_scoreboard_upcoming.json")
        raw_scores = [c["score"] for e in data["events"] for c in e["competitors"]]
        self.assertIn("", raw_scores, "fixture no longer covers the empty-string score case")

        for event in Scoreboard.model_validate(data).events:
            self.assertEqual(event.status, EventStatus.PRE)
            self.assertIsNone(event.home.score)
            self.assertIsNone(event.away.score)

    def test_index_endpoints_return_pages_of_refs(self):
        """get_teams / get_weeks / get_season_types all walk a RefIndex and
        then resolve each $ref, so the envelope shape matters as much as the
        entities do."""
        for name, expected_count in (
            ("espn_teams_index.json", 32),
            ("espn_postseason_weeks_index.json", 5),
            ("espn_season_types.json", 4),
        ):
            with self.subTest(fixture=name):
                data = load_fixture(name)
                self.assert_required_keys(data, {"count", "pageCount", "items"}, name)

                index = RefIndex.model_validate(data)
                self.assertEqual(index.count, expected_count)
                self.assertTrue(all(item.ref for item in index.items))

    def test_postseason_has_five_weeks_of_which_four_are_scoreable(self):
        """Five postseason weeks, but week 4 is the Pro Bowl - so the round
        map covers four of them. A sixth week appearing here would mean the
        playoff structure changed and NFL_POSTSEASON_WEEK_KEYS is stale."""
        index = RefIndex.model_validate(load_fixture("espn_postseason_weeks_index.json"))

        self.assertEqual(index.count, 5)
        scoreable = [w for w in range(1, index.count + 1) if nfl_stage_key(3, w)]
        self.assertEqual(scoreable, [1, 2, 3, 5])

    def test_event_id_joins_the_two_hosts(self):
        """The core API's event id and the scoreboard's competitionId are the
        same value - that identity is what lets one MatchMapping serve both,
        and is the whole reason no fuzzy matching is needed."""
        core_event = Event.model_validate(load_fixture("espn_event_detail.json"))
        scoreboard = Scoreboard.model_validate(load_fixture("espn_scoreboard_upcoming.json"))
        self.assertIn(core_event.id, {e.event_id for e in scoreboard.events})


class IterRefsPaginationTests(TestCase):
    """Covers EspnClient._iter_refs, which walks a core-API index page by page.

    The client asks for limit=100 so today's 32 teams arrive in one page, but
    the endpoints are genuinely paginated (at the default page size of 25 the
    same call returns 4 pages). If the walk ever stopped after page one, two
    thirds of the league would silently go missing, so the multi-page path is
    exercised directly rather than left to chance."""

    def make_client(self, pages):
        client = EspnClient()
        self.requested_pages = []

        async def fake_get_json(url, params=None):
            self.requested_pages.append((params or {}).get("page"))
            return pages[((params or {}).get("page") or 1) - 1]

        client._get_json = fake_get_json
        return client

    async def test_follows_every_page(self):
        pages = [
            {"count": 5, "pageIndex": 1, "pageCount": 3, "items": [{"$ref": "a"}, {"$ref": "b"}]},
            {"count": 5, "pageIndex": 2, "pageCount": 3, "items": [{"$ref": "c"}, {"$ref": "d"}]},
            {"count": 5, "pageIndex": 3, "pageCount": 3, "items": [{"$ref": "e"}]},
        ]
        client = self.make_client(pages)

        refs = [ref async for ref in client._iter_refs("http://example/index")]

        self.assertEqual(refs, ["a", "b", "c", "d", "e"])
        self.assertEqual(self.requested_pages, [1, 2, 3])

    async def test_stops_on_a_single_page(self):
        client = self.make_client([{"count": 1, "pageIndex": 1, "pageCount": 1, "items": [{"$ref": "only"}]}])

        refs = [ref async for ref in client._iter_refs("http://example/index")]

        self.assertEqual(refs, ["only"])
        self.assertEqual(self.requested_pages, [1])

    async def test_empty_index_yields_nothing(self):
        client = self.make_client([{"count": 0, "pageIndex": 1, "pageCount": 1, "items": []}])

        self.assertEqual([ref async for ref in client._iter_refs("http://example/index")], [])


class RefSegmentTests(TestCase):
    """Covers _ref_segment (sports/integrations/espn.py), which pulls ids out
    of $ref URLs so nested entities don't have to be fetched."""

    REF = "http://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/2026/types/2/weeks/5?lang=en"

    def test_extracts_each_segment(self):
        self.assertEqual(_ref_segment(self.REF, "seasons"), "2026")
        self.assertEqual(_ref_segment(self.REF, "types"), "2")
        self.assertEqual(_ref_segment(self.REF, "weeks"), "5")

    def test_query_string_is_not_part_of_the_value(self):
        ref = "http://x/seasons/2026/teams/14?lang=en&region=us"
        self.assertEqual(_ref_segment(ref, "teams"), "14")

    def test_missing_segment_or_ref_is_none(self):
        self.assertIsNone(_ref_segment(self.REF, "athletes"))
        self.assertIsNone(_ref_segment(None, "teams"))


class NflStageKeyTests(TestCase):
    """Covers nfl_stage_key (sports/constants.py), which decides both which
    round a game is scored under and whether it is scoreable at all."""

    def test_regular_season_maps_to_one_stage_regardless_of_week(self):
        self.assertEqual(nfl_stage_key(2, 1), "REG")
        self.assertEqual(nfl_stage_key(2, 18), "REG")

    def test_postseason_weeks_map_to_rounds(self):
        self.assertEqual(nfl_stage_key(3, 1), "WC")
        self.assertEqual(nfl_stage_key(3, 2), "DIV")
        self.assertEqual(nfl_stage_key(3, 3), "CON")
        self.assertEqual(nfl_stage_key(3, 5), "SB")

    def test_pro_bowl_is_not_scoreable(self):
        """Postseason week 4 is the Pro Bowl - an exhibition nobody should be
        asked to predict."""
        self.assertIsNone(nfl_stage_key(3, 4))

    def test_preseason_and_offseason_are_not_scoreable(self):
        self.assertIsNone(nfl_stage_key(1, 1))
        self.assertIsNone(nfl_stage_key(4, None))
        self.assertIsNone(nfl_stage_key(None, None))

    def test_regular_season_is_the_only_drawable_round(self):
        """Only the regular season can tie, so it is the only round that gets
        a Draw poll answer - see DISCORD_POLL_ANSWER_ORDER_MAP."""
        by_key = {bp.key: bp for bp in NFL_STAGE_BLUEPRINTS}
        self.assertEqual(by_key["REG"].stage_type, StageType.LEAGUE)
        for key in ("WC", "DIV", "CON", "SB"):
            self.assertEqual(by_key[key].stage_type, StageType.KNOCK_OUT)

    def test_levels_escalate_towards_the_super_bowl(self):
        levels = [bp.level for bp in NFL_STAGE_BLUEPRINTS]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(NFL_STAGE_BLUEPRINTS[-1].key, "SB")


class CurrentNflSeasonYearTests(TestCase):
    """Covers current_nfl_season_year (sports/services/ingestion.py). A season
    is named for the year it kicks off in but runs into February, so the
    rollover is not January 1st."""

    def test_autumn_belongs_to_that_years_season(self):
        self.assertEqual(current_nfl_season_year(datetime.date(2026, 9, 10)), 2026)

    def test_january_and_february_still_belong_to_the_previous_season(self):
        # The 2026 Super Bowl is played in February 2027.
        self.assertEqual(current_nfl_season_year(datetime.date(2027, 1, 15)), 2026)
        self.assertEqual(current_nfl_season_year(datetime.date(2027, 2, 8)), 2026)

    def test_march_starts_the_new_season_year(self):
        self.assertEqual(current_nfl_season_year(datetime.date(2027, 3, 1)), 2027)


class FakeEspnClient:
    """Stands in for sports.integrations.espn.EspnClient inside `async with
    EspnClient() as client:` blocks - only implements what ingestion.py calls."""

    def __init__(self, season=None, teams=None, events=None, scoreboard=None):
        self._season = season
        self._teams = teams or []
        self._events = events or []
        self._scoreboard = scoreboard or []
        self.scoreboard_windows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get_season(self, season_year):
        if self._season is None:
            raise RuntimeError("no season configured")
        return self._season

    async def get_teams(self, season_year):
        return self._teams

    async def get_events(self, start, end):
        return self._events

    async def get_scoreboard(self, start, end):
        self.scoreboard_windows.append((start, end))
        return self._scoreboard


def make_event(event_id, home_team_id, away_team_id, season=2026, season_type=2, week=1, date=None):
    """Builds an Event the way the core API delivers one - ids reachable only
    through $ref URLs."""
    base = "http://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
    return Event.model_validate(
        {
            "id": event_id,
            "date": (date or timezone.now() + datetime.timedelta(days=1)).isoformat(),
            "name": f"{away_team_id} at {home_team_id}",
            "seasonType": {"$ref": f"{base}/seasons/{season}/types/{season_type}?lang=en"},
            "week": {"$ref": f"{base}/seasons/{season}/types/{season_type}/weeks/{week}?lang=en"},
            "competitions": [
                {
                    "id": event_id,
                    "competitors": [
                        {
                            "id": home_team_id,
                            "homeAway": "home",
                            "team": {"$ref": f"{base}/seasons/{season}/teams/{home_team_id}"},
                        },
                        {
                            "id": away_team_id,
                            "homeAway": "away",
                            "team": {"$ref": f"{base}/seasons/{season}/teams/{away_team_id}"},
                        },
                    ],
                }
            ],
        }
    )


def make_scoreboard_event(event_id, home_id, away_id, status, home_score=None, away_score=None):
    return ScoreboardEvent.model_validate(
        {
            "id": event_id,
            "competitionId": event_id,
            "status": status,
            "competitors": [
                {"id": home_id, "homeAway": "home", "score": home_score if home_score is not None else ""},
                {"id": away_id, "homeAway": "away", "score": away_score if away_score is not None else ""},
            ],
        }
    )


class IngestEspnNflInfrastructureTests(TestCase):
    """Covers ingest_espn_nfl_infrastructure (sports/services/ingestion.py):
    the NFL competition, its season, and the five scoreable rounds."""

    def setUp(self):
        patcher = patch("sports.signals.redis_client")
        self.addCleanup(patcher.stop)
        patcher.start()

        self.season_info = SeasonInfo.model_validate(
            {"year": 2026, "startDate": "2026-08-06T07:00Z", "endDate": "2027-02-16T07:59Z"}
        )

    async def test_creates_competition_season_and_five_rounds(self):
        with patch("sports.services.ingestion.EspnClient", return_value=FakeEspnClient(season=self.season_info)):
            await ingest_espn_nfl_infrastructure(2026)

        competition = await Competition.objects.aget(sport=Sport.AMERICAN_FOOTBALL)
        self.assertEqual(competition.name, "NFL")
        self.assertTrue(competition.is_featured)
        self.assertTrue(
            await CompetitionMapping.objects.filter(
                provider=SportsProvider.ESPN, external_id=ESPN_NFL_COMPETITION_EXTERNAL_ID
            ).aexists()
        )

        season = await Season.objects.aget(competition=competition)
        self.assertEqual(season.year, 2026)

        self.assertEqual(await Stage.objects.filter(season=season).acount(), 5)
        stages = [s async for s in Stage.objects.filter(season=season).order_by("level")]
        self.assertEqual([s.name for s in stages], NFL_STAGE_BLUEPRINTS_NAMES)
        # The regular season is the only round where a Draw answer is offered.
        self.assertEqual(stages[0].stage_type, StageType.LEAGUE)
        self.assertTrue(all(s.stage_type == StageType.KNOCK_OUT for s in stages[1:]))

        self.assertTrue(
            await StageMapping.objects.filter(
                provider=SportsProvider.ESPN, external_id=nfl_stage_external_id(2026, "SB")
            ).aexists()
        )

    async def test_is_idempotent(self):
        client = FakeEspnClient(season=self.season_info)
        with patch("sports.services.ingestion.EspnClient", return_value=client):
            await ingest_espn_nfl_infrastructure(2026)
            await ingest_espn_nfl_infrastructure(2026)

        self.assertEqual(await Competition.objects.acount(), 1)
        self.assertEqual(await Season.objects.acount(), 1)
        self.assertEqual(await Stage.objects.acount(), 5)
        self.assertEqual(await StageMapping.objects.acount(), 5)

    async def test_season_is_active_inside_the_league_year(self):
        with patch("sports.services.ingestion.EspnClient", return_value=FakeEspnClient(season=self.season_info)):
            await ingest_espn_nfl_infrastructure(2026)

        season = await Season.objects.aget(year=2026)
        # "now" in the test run sits inside 2026-08-06 .. 2027-02-16 only if
        # the clock says so; assert against the bounds rather than a literal.
        expected = self.season_info.start_date <= timezone.now() <= self.season_info.end_date
        self.assertEqual(season.is_active, expected)


class IngestEspnNflMatchesTests(TestCase):
    """Covers ingest_espn_nfl_matches (sports/services/ingestion.py): the
    schedule upsert, and what it refuses to ingest."""

    def setUp(self):
        patcher = patch("sports.signals.redis_client")
        self.addCleanup(patcher.stop)
        patcher.start()

        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        CompetitionMapping.objects.create(
            provider=SportsProvider.ESPN,
            external_id=ESPN_NFL_COMPETITION_EXTERNAL_ID,
            competition=self.competition,
        )
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        SeasonMapping.objects.create(provider=SportsProvider.ESPN, external_id="2026", season=self.season)

        self.stages = {}
        for blueprint in NFL_STAGE_BLUEPRINTS:
            stage = Stage.objects.create(
                season=self.season,
                name=blueprint.name,
                level=blueprint.level,
                stage_type=blueprint.stage_type,
            )
            StageMapping.objects.create(
                provider=SportsProvider.ESPN,
                external_id=nfl_stage_external_id(2026, blueprint.key),
                stage=stage,
            )
            self.stages[blueprint.key] = stage

        self.home = Team.objects.create(name="Los Angeles Rams", sport=Sport.AMERICAN_FOOTBALL)
        self.away = Team.objects.create(name="San Francisco 49ers", sport=Sport.AMERICAN_FOOTBALL)
        TeamMapping.objects.create(provider=SportsProvider.ESPN, external_id="14", team=self.home)
        TeamMapping.objects.create(provider=SportsProvider.ESPN, external_id="25", team=self.away)

    async def test_creates_match_and_mapping_for_a_regular_season_game(self):
        kickoff = timezone.now() + datetime.timedelta(days=3)
        event = make_event("401872657", "14", "25", date=kickoff)

        with patch("sports.services.ingestion.EspnClient", return_value=FakeEspnClient(events=[event])):
            await ingest_espn_nfl_matches()

        match = await Match.objects.aget()
        self.assertEqual(match.home_team_id, self.home.id)
        self.assertEqual(match.away_team_id, self.away.id)
        self.assertEqual(match.stage_id, self.stages["REG"].id)
        self.assertEqual(match.status, MatchStatus.SCHEDULED)
        self.assertTrue(
            await MatchMapping.objects.filter(
                provider=SportsProvider.ESPN, external_id="401872657", match=match
            ).aexists()
        )

    async def test_routes_postseason_games_to_their_round(self):
        event = make_event("SB1", "14", "25", season_type=3, week=5)

        with patch("sports.services.ingestion.EspnClient", return_value=FakeEspnClient(events=[event])):
            await ingest_espn_nfl_matches()

        match = await Match.objects.aget()
        self.assertEqual(match.stage_id, self.stages["SB"].id)

    async def test_skips_preseason_and_pro_bowl(self):
        events = [
            make_event("PRE1", "14", "25", season_type=1, week=1),
            make_event("PB1", "14", "25", season_type=3, week=4),
        ]

        with patch("sports.services.ingestion.EspnClient", return_value=FakeEspnClient(events=events)):
            await ingest_espn_nfl_matches()

        self.assertEqual(await Match.objects.acount(), 0)

    async def test_reruns_update_in_place_without_duplicating(self):
        first = timezone.now() + datetime.timedelta(days=3)
        moved = first + datetime.timedelta(hours=2)

        with patch(
            "sports.services.ingestion.EspnClient",
            return_value=FakeEspnClient(events=[make_event("E1", "14", "25", date=first)]),
        ):
            await ingest_espn_nfl_matches()
        with patch(
            "sports.services.ingestion.EspnClient",
            return_value=FakeEspnClient(events=[make_event("E1", "14", "25", date=moved)]),
        ):
            await ingest_espn_nfl_matches()

        self.assertEqual(await Match.objects.acount(), 1)
        self.assertEqual(await MatchMapping.objects.acount(), 1)
        match = await Match.objects.aget()
        self.assertEqual(match.kickoff.replace(microsecond=0), moved.replace(microsecond=0))

    async def test_rerun_does_not_reset_a_finished_match(self):
        """The schedule sync must not walk status/scores backwards - those are
        owned by the live sync, which is the only thing that fires scoring."""
        event = make_event("E1", "14", "25")
        with patch("sports.services.ingestion.EspnClient", return_value=FakeEspnClient(events=[event])):
            await ingest_espn_nfl_matches()

        await Match.objects.aupdate(status=MatchStatus.FINISHED, home_score=24, away_score=17)

        with patch("sports.services.ingestion.EspnClient", return_value=FakeEspnClient(events=[event])):
            await ingest_espn_nfl_matches()

        match = await Match.objects.aget()
        self.assertEqual(match.status, MatchStatus.FINISHED)
        self.assertEqual((match.home_score, match.away_score), (24, 17))

    async def test_skips_events_whose_teams_are_not_mapped(self):
        event = make_event("E1", "14", "999")

        with patch("sports.services.ingestion.EspnClient", return_value=FakeEspnClient(events=[event])):
            await ingest_espn_nfl_matches()

        self.assertEqual(await Match.objects.acount(), 0)


class IngestEspnNflLiveMatchesTests(TestCase):
    """Covers ingest_espn_nfl_live_matches (sports/services/ingestion.py):
    the status/score path, which is what drives scoring."""

    def setUp(self):
        patcher = patch("sports.signals.redis_client")
        self.addCleanup(patcher.stop)
        patcher.start()

        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Regular Season", stage_type=StageType.LEAGUE)
        self.home = Team.objects.create(name="Cleveland Browns", sport=Sport.AMERICAN_FOOTBALL)
        self.away = Team.objects.create(name="Minnesota Vikings", sport=Sport.AMERICAN_FOOTBALL)
        TeamMapping.objects.create(provider=SportsProvider.ESPN, external_id="5", team=self.home)
        TeamMapping.objects.create(provider=SportsProvider.ESPN, external_id="16", team=self.away)

    async def test_does_not_call_the_api_when_nothing_is_due(self):
        await Match.objects.acreate(
            stage=self.stage,
            home_team=self.home,
            away_team=self.away,
            kickoff=timezone.now() + datetime.timedelta(days=3),
        )

        with patch("sports.services.ingestion.EspnClient") as mock_client_cls:
            await ingest_espn_nfl_live_matches()

        mock_client_cls.assert_not_called()

    async def test_finalizes_a_finished_game(self):
        match = await self.amake_match("401772633")
        scoreboard = [make_scoreboard_event("401772633", "5", "16", "post", home_score=17, away_score=21)]

        with patch("sports.services.ingestion.EspnClient", return_value=FakeEspnClient(scoreboard=scoreboard)):
            await ingest_espn_nfl_live_matches()

        await match.arefresh_from_db()
        self.assertEqual(match.status, MatchStatus.FINISHED)
        self.assertEqual((match.home_score, match.away_score), (17, 21))
        self.assertEqual(match.outcome, MatchOutcome.AWAY_WIN)

    async def test_marks_an_in_progress_game_live(self):
        match = await self.amake_match("E1")
        scoreboard = [make_scoreboard_event("E1", "5", "16", "in", home_score=7, away_score=3)]

        with patch("sports.services.ingestion.EspnClient", return_value=FakeEspnClient(scoreboard=scoreboard)):
            await ingest_espn_nfl_live_matches()

        await match.arefresh_from_db()
        self.assertEqual(match.status, MatchStatus.LIVE)
        self.assertIsNone(match.outcome)

    async def test_refuses_to_write_scores_when_the_teams_do_not_match(self):
        """A payload whose sides disagree with the stored match would
        otherwise award points to the wrong team."""
        match = await self.amake_match("E1")
        # Home/away swapped relative to what the DB has.
        scoreboard = [make_scoreboard_event("E1", "16", "5", "post", home_score=21, away_score=17)]

        with patch("sports.services.ingestion.EspnClient", return_value=FakeEspnClient(scoreboard=scoreboard)):
            await ingest_espn_nfl_live_matches()

        await match.arefresh_from_db()
        self.assertEqual(match.status, MatchStatus.SCHEDULED)
        self.assertIsNone(match.home_score)

    async def test_picks_up_a_match_left_behind_while_the_worker_was_down(self):
        """A match whose kickoff passed long ago must still be finalized -
        otherwise a weekend of downtime strands it in SCHEDULED forever and
        its predictions never score."""
        match = await self.amake_match("OLD1", kickoff=timezone.now() - datetime.timedelta(days=4))
        scoreboard = [make_scoreboard_event("OLD1", "5", "16", "post", home_score=10, away_score=13)]

        client = FakeEspnClient(scoreboard=scoreboard)
        with patch("sports.services.ingestion.EspnClient", return_value=client):
            await ingest_espn_nfl_live_matches()

        await match.arefresh_from_db()
        self.assertEqual(match.status, MatchStatus.FINISHED)
        # The requested window has to reach back far enough to cover it.
        start, end = client.scoreboard_windows[0]
        self.assertLessEqual(start, match.kickoff.date())

    async def test_ignores_matches_belonging_to_another_provider(self):
        match = await self.amake_match("SHARED", provider=SportsProvider.FIFA)

        with patch("sports.services.ingestion.EspnClient") as mock_client_cls:
            await ingest_espn_nfl_live_matches()

        mock_client_cls.assert_not_called()
        await match.arefresh_from_db()
        self.assertEqual(match.status, MatchStatus.SCHEDULED)

    async def amake_match(self, external_id, provider=SportsProvider.ESPN, **kwargs):
        defaults = {
            "stage_id": self.stage.id,
            "home_team_id": self.home.id,
            "away_team_id": self.away.id,
            "kickoff": timezone.now() - datetime.timedelta(hours=2),
            "status": MatchStatus.SCHEDULED,
        }
        defaults.update(kwargs)
        match = await Match.objects.acreate(**defaults)
        await MatchMapping.objects.acreate(provider=provider, external_id=external_id, match=match)
        return match
