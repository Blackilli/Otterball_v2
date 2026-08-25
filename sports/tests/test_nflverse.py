import datetime
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from sports.constants import (
    ESPN_TO_NFLVERSE_TEAM_ABBREVIATIONS,
    NFL_STAGE_BLUEPRINTS,
    NFLVERSE_TO_ESPN_TEAM_ABBREVIATIONS,
    nfl_stage_external_id,
    nflverse_stage_key,
)
from sports.integrations.espn import Team as EspnTeam
from sports.integrations.nflverse import Game, NflverseClient
from sports.models import (
    Competition,
    Match,
    MatchMapping,
    MatchOutcome,
    MatchStatus,
    Season,
    Sport,
    SportsProvider,
    Stage,
    StageMapping,
    Team,
    TeamMapping,
)
from sports.services.ingestion import ingest_nflverse_nfl_matches, ingest_nflverse_team_mappings

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Required columns. nflverse ships ~46; these are the ones ingestion reads,
# and the ones whose disappearance would break it.
REQUIRED_COLUMNS = {
    "game_id",
    "season",
    "game_type",
    "week",
    "gameday",
    "gametime",
    "away_team",
    "home_team",
    "away_score",
    "home_score",
    "result",
    "espn",
}


def load_csv() -> str:
    return (FIXTURES_DIR / "nflverse_games.csv").read_text(encoding="utf-8")


class RealNflverseCsvTests(TestCase):
    """Guards sports/integrations/nflverse.py against drift in the real
    dataset. The fixture is a genuine excerpt of
    `https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv`
    - the full header, with rows hand-picked to cover each shape that matters:
    a finished regular-season game, the Super Bowl, a real tie, an unplayed
    future game, and both teams whose abbreviation disagrees with ESPN's.

    Refresh it by re-cutting rows from the live file; the release asset is
    rebuilt several times a day during the season."""

    def setUp(self):
        self.games = {g.game_id: g for g in NflverseClient.parse_games(load_csv())}

    def test_required_columns_are_present(self):
        header = load_csv().splitlines()[0].split(",")
        missing = REQUIRED_COLUMNS - set(header)
        self.assertEqual(missing, set(), f"nflverse no longer ships {missing}")

    def test_parses_a_finished_regular_season_game(self):
        game = self.games["2025_05_MIN_CLE"]

        self.assertEqual(game.game_type, "REG")
        self.assertEqual((game.away_team, game.home_team), ("MIN", "CLE"))
        self.assertEqual((game.away_score, game.home_score), (21, 17))
        self.assertTrue(game.is_final)

    def test_kickoff_is_converted_from_eastern_to_utc(self):
        """gameday/gametime are wall-clock Eastern. This game kicked off at
        09:30 ET (it was played in London), i.e. 13:30 UTC - which is exactly
        what ESPN reports for event 401772633."""
        game = self.games["2025_05_MIN_CLE"]

        self.assertEqual(
            game.kickoff,
            datetime.datetime(2025, 10, 5, 13, 30, tzinfo=datetime.timezone.utc),
        )

    def test_carries_the_espn_event_id(self):
        """The whole reason this provider is worth having: a free, precomputed
        cross-reference to ESPN, with no fuzzy matching."""
        self.assertEqual(self.games["2025_05_MIN_CLE"].espn, "401772633")
        self.assertTrue(all(g.espn for g in self.games.values()))

    def test_unplayed_games_have_no_scores(self):
        """Blank cells must become None, not 0 - a 0-0 game is a valid tie."""
        game = self.games["2026_01_NE_SEA"]

        self.assertIsNone(game.home_score)
        self.assertIsNone(game.away_score)
        self.assertIsNone(game.result)
        self.assertFalse(game.is_final)

    def test_a_tie_is_final_even_though_result_is_zero(self):
        """result == 0 is falsy; is_final must test against None or every tie
        looks unplayed."""
        game = self.games["2025_04_GB_DAL"]

        self.assertEqual(game.result, 0)
        self.assertEqual(game.home_score, game.away_score)
        self.assertTrue(game.is_final)

    def test_covers_the_two_teams_espn_names_differently(self):
        abbreviations = {g.home_team for g in self.games.values()} | {g.away_team for g in self.games.values()}

        self.assertIn("LA", abbreviations)
        self.assertIn("WAS", abbreviations)

    def test_game_types_match_the_stage_blueprint_keys(self):
        """nflverse's game_type column already speaks REG/WC/DIV/CON/SB, so no
        translation table is needed - but only while that stays true."""
        blueprint_keys = {bp.key for bp in NFL_STAGE_BLUEPRINTS}

        for game in self.games.values():
            self.assertIn(game.game_type, blueprint_keys)

    def test_season_filter_narrows_the_parse(self):
        only_2026 = NflverseClient.parse_games(load_csv(), seasons={2026})

        self.assertTrue(only_2026)
        self.assertTrue(all(g.season == 2026 for g in only_2026))


class NflverseStageKeyTests(TestCase):
    """Covers nflverse_stage_key (sports/constants.py)."""

    def test_known_rounds_pass_through(self):
        for key in ("REG", "WC", "DIV", "CON", "SB"):
            self.assertEqual(nflverse_stage_key(key), key)

    def test_unknown_or_missing_types_are_not_scoreable(self):
        # The current file only ships the five scoreable types, but an added
        # one (a preseason or exhibition row) must be skipped, not guessed at.
        self.assertIsNone(nflverse_stage_key("PRE"))
        self.assertIsNone(nflverse_stage_key("PRO"))
        self.assertIsNone(nflverse_stage_key(""))
        self.assertIsNone(nflverse_stage_key(None))


class TeamAbbreviationAliasTests(TestCase):
    """Covers the nflverse<->ESPN abbreviation bridge (sports/constants.py).

    Verified against both live APIs: 30 of 32 abbreviations agree, and these
    two do not. Without the bridge the Rams and the Commanders never resolve,
    quietly dropping about two games a week."""

    def test_the_two_known_disagreements(self):
        self.assertEqual(NFLVERSE_TO_ESPN_TEAM_ABBREVIATIONS["LA"], "LAR")
        self.assertEqual(NFLVERSE_TO_ESPN_TEAM_ABBREVIATIONS["WAS"], "WSH")

    def test_the_inverse_map_round_trips(self):
        for nflverse, espn in NFLVERSE_TO_ESPN_TEAM_ABBREVIATIONS.items():
            self.assertEqual(ESPN_TO_NFLVERSE_TEAM_ABBREVIATIONS[espn], nflverse)

    def test_unaliased_abbreviations_pass_through_unchanged(self):
        for abbreviation in ("KC", "SEA", "CLE", "MIN", "LAC", "LV"):
            self.assertEqual(ESPN_TO_NFLVERSE_TEAM_ABBREVIATIONS.get(abbreviation, abbreviation), abbreviation)


class FakeNflverseClient:
    """Stands in for NflverseClient inside `async with NflverseClient()`."""

    def __init__(self, games=None):
        self._games = games or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get_games(self, seasons=None):
        if seasons is None:
            return self._games
        return [g for g in self._games if g.season in seasons]


class IngestNflverseMatchesTests(TestCase):
    """Covers ingest_nflverse_nfl_matches (sports/services/ingestion.py):
    the schedule/finals upsert, its adoption of ESPN-ingested matches, and
    the ESPN ids it attaches on the way through."""

    def setUp(self):
        patcher = patch("sports.signals.redis_client")
        self.addCleanup(patcher.stop)
        patcher.start()

        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2025", competition=self.competition, year=2025)

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
                external_id=nfl_stage_external_id(2025, blueprint.key),
                stage=stage,
            )
            self.stages[blueprint.key] = stage

        self.teams = {}
        for abbreviation, name in (
            ("CLE", "Cleveland Browns"),
            ("MIN", "Minnesota Vikings"),
            ("LA", "Los Angeles Rams"),
            ("WAS", "Washington Commanders"),
        ):
            team = Team.objects.create(name=name, sport=Sport.AMERICAN_FOOTBALL)
            TeamMapping.objects.create(provider=SportsProvider.NFLVERSE, external_id=abbreviation, team=team)
            self.teams[abbreviation] = team

        self.fixture_games = {g.game_id: g for g in NflverseClient.parse_games(load_csv())}

    def game(self, game_id):
        return self.fixture_games[game_id]

    async def run_ingest(self, games):
        with patch("sports.services.ingestion.NflverseClient", return_value=FakeNflverseClient(games)):
            await ingest_nflverse_nfl_matches({2025})

    async def test_creates_a_match_with_both_provider_mappings(self):
        await self.run_ingest([self.game("2025_05_MIN_CLE")])

        match = await Match.objects.aget()
        self.assertEqual(match.home_team_id, self.teams["CLE"].id)
        self.assertEqual(match.away_team_id, self.teams["MIN"].id)
        self.assertEqual(match.stage_id, self.stages["REG"].id)

        mappings = {m.provider: m.external_id async for m in match.mappings.all()}
        self.assertEqual(
            mappings,
            {SportsProvider.NFLVERSE: "2025_05_MIN_CLE", SportsProvider.ESPN: "401772633"},
        )

    async def test_finalizes_a_played_game(self):
        await self.run_ingest([self.game("2025_05_MIN_CLE")])

        match = await Match.objects.aget()
        self.assertEqual(match.status, MatchStatus.FINISHED)
        self.assertEqual((match.home_score, match.away_score), (17, 21))
        self.assertEqual(match.outcome, MatchOutcome.AWAY_WIN)

    async def test_a_tie_finalizes_as_a_draw(self):
        """The case that justifies offering a Draw answer on regular-season
        polls: without it nobody could have picked this correctly."""
        tie = self.game("2025_04_GB_DAL")
        for abbreviation in (tie.home_team, tie.away_team):
            team = await Team.objects.acreate(name=f"Team {abbreviation}", sport=Sport.AMERICAN_FOOTBALL)
            await TeamMapping.objects.acreate(provider=SportsProvider.NFLVERSE, external_id=abbreviation, team=team)

        await self.run_ingest([tie])

        match = await Match.objects.aget()
        self.assertEqual(match.status, MatchStatus.FINISHED)
        self.assertEqual(match.home_score, match.away_score)
        self.assertEqual(match.outcome, MatchOutcome.DRAW)

    async def test_adopts_an_existing_espn_match_instead_of_duplicating_it(self):
        """The same fixture ingested from ESPN first must end up as one row
        carrying both ids - not two rows competing for the same poll."""
        existing = await Match.objects.acreate(
            stage_id=self.stages["REG"].id,
            home_team_id=self.teams["CLE"].id,
            away_team_id=self.teams["MIN"].id,
            kickoff=timezone.now(),
            status=MatchStatus.SCHEDULED,
        )
        await MatchMapping.objects.acreate(provider=SportsProvider.ESPN, external_id="401772633", match=existing)

        await self.run_ingest([self.game("2025_05_MIN_CLE")])

        self.assertEqual(await Match.objects.acount(), 1)
        self.assertEqual(await MatchMapping.objects.acount(), 2)
        await existing.arefresh_from_db()
        self.assertEqual(existing.status, MatchStatus.FINISHED)

    async def test_does_not_refinalize_a_match_espn_already_settled(self):
        """Re-running must not rewrite a FINISHED match, or every pass would
        fire the scoring signal again."""
        existing = await Match.objects.acreate(
            stage_id=self.stages["REG"].id,
            home_team_id=self.teams["CLE"].id,
            away_team_id=self.teams["MIN"].id,
            kickoff=timezone.now(),
            status=MatchStatus.FINISHED,
            home_score=17,
            away_score=21,
        )
        await MatchMapping.objects.acreate(
            provider=SportsProvider.NFLVERSE, external_id="2025_05_MIN_CLE", match=existing
        )
        with patch("sports.services.ingestion.logger") as mock_logger:
            await self.run_ingest([self.game("2025_05_MIN_CLE")])

        self.assertEqual(await Match.objects.acount(), 1)
        finalized_logs = [c for c in mock_logger.info.call_args_list if "Finalized match" in str(c)]
        self.assertEqual(finalized_logs, [])

    async def test_leaves_an_unplayed_game_scheduled(self):
        upcoming = self.game("2026_01_NE_SEA")
        for abbreviation in (upcoming.home_team, upcoming.away_team):
            team = await Team.objects.acreate(name=f"Team {abbreviation}", sport=Sport.AMERICAN_FOOTBALL)
            await TeamMapping.objects.acreate(provider=SportsProvider.NFLVERSE, external_id=abbreviation, team=team)
        season_2026 = await Season.objects.acreate(name="NFL 2026", competition=self.competition, year=2026)
        stage = await Stage.objects.acreate(season=season_2026, name="Regular Season")
        await StageMapping.objects.acreate(
            provider=SportsProvider.ESPN, external_id=nfl_stage_external_id(2026, "REG"), stage=stage
        )

        with patch("sports.services.ingestion.NflverseClient", return_value=FakeNflverseClient([upcoming])):
            await ingest_nflverse_nfl_matches({2026})

        match = await Match.objects.aget()
        self.assertEqual(match.status, MatchStatus.SCHEDULED)
        self.assertIsNone(match.home_score)

    async def test_resolves_the_aliased_team_abbreviations(self):
        """LA and WAS are nflverse's names for the teams ESPN calls LAR and
        WSH; both must land rather than being skipped as unmapped."""
        for game_id, abbreviation in (("2025_01_HOU_LA", "LA"), ("2025_01_NYG_WAS", "WAS")):
            game = self.game(game_id)
            other = game.away_team if game.home_team == abbreviation else game.home_team
            if not await TeamMapping.objects.filter(provider=SportsProvider.NFLVERSE, external_id=other).aexists():
                team = await Team.objects.acreate(name=f"Team {other}", sport=Sport.AMERICAN_FOOTBALL)
                await TeamMapping.objects.acreate(provider=SportsProvider.NFLVERSE, external_id=other, team=team)

        await self.run_ingest([self.game("2025_01_HOU_LA"), self.game("2025_01_NYG_WAS")])

        self.assertEqual(await Match.objects.acount(), 2)
        rams_games = await Match.objects.filter(home_team=self.teams["LA"]).acount()
        commanders_games = await Match.objects.filter(home_team=self.teams["WAS"]).acount()
        self.assertEqual(rams_games, 1)
        self.assertEqual(commanders_games, 1)

    async def test_skips_games_whose_teams_are_unmapped(self):
        await TeamMapping.objects.filter(provider=SportsProvider.NFLVERSE, external_id="MIN").adelete()

        await self.run_ingest([self.game("2025_05_MIN_CLE")])

        self.assertEqual(await Match.objects.acount(), 0)

    async def test_skips_non_scoreable_game_types(self):
        preseason = Game.model_validate(
            {
                "game_id": "2025_00_MIN_CLE",
                "season": "2025",
                "game_type": "PRE",
                "week": "0",
                "gameday": "2025-08-10",
                "gametime": "13:00",
                "away_team": "MIN",
                "home_team": "CLE",
                "away_score": "10",
                "home_score": "7",
                "result": "-3",
                "espn": "999",
            }
        )

        await self.run_ingest([preseason])

        self.assertEqual(await Match.objects.acount(), 0)

    async def test_rerun_is_idempotent(self):
        await self.run_ingest([self.game("2025_05_MIN_CLE")])
        await self.run_ingest([self.game("2025_05_MIN_CLE")])

        self.assertEqual(await Match.objects.acount(), 1)
        self.assertEqual(await MatchMapping.objects.acount(), 2)

    async def test_routes_the_super_bowl_to_its_own_stage(self):
        sb = self.game("2025_22_SEA_NE")
        for abbreviation in (sb.home_team, sb.away_team):
            team = await Team.objects.acreate(name=f"Team {abbreviation}", sport=Sport.AMERICAN_FOOTBALL)
            await TeamMapping.objects.acreate(provider=SportsProvider.NFLVERSE, external_id=abbreviation, team=team)

        await self.run_ingest([sb])

        match = await Match.objects.aget()
        self.assertEqual(match.stage_id, self.stages["SB"].id)


class UnmappedTeamGuardTests(TestCase):
    """Covers the guard added around the nflverse abbreviation bridge.

    An unmapped abbreviation costs that team every one of its ~17 games, and
    the only visible symptom is a team that never appears in a poll. Before
    the guard that produced ~17 near-identical per-game error lines; now the
    distinct cause is named once, so a provider renaming an abbreviation
    surfaces on the first sync instead of in week 6."""

    def setUp(self):
        patcher = patch("sports.signals.redis_client")
        self.addCleanup(patcher.stop)
        patcher.start()

        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2025", competition=self.competition, year=2025)
        stage = Stage.objects.create(season=self.season, name="Regular Season")
        StageMapping.objects.create(
            provider=SportsProvider.ESPN, external_id=nfl_stage_external_id(2025, "REG"), stage=stage
        )
        self.stage = stage

        # Only the home side is mapped, so MIN is the unmapped abbreviation.
        team = Team.objects.create(name="Cleveland Browns", sport=Sport.AMERICAN_FOOTBALL)
        TeamMapping.objects.create(provider=SportsProvider.NFLVERSE, external_id="CLE", team=team)

        self.games = {g.game_id: g for g in NflverseClient.parse_games(load_csv())}

    async def test_names_the_unmapped_abbreviation_once_with_a_game_count(self):
        game = self.games["2025_05_MIN_CLE"]

        with patch("sports.services.ingestion.logger") as mock_logger:
            with patch("sports.services.ingestion.NflverseClient", return_value=FakeNflverseClient([game])):
                await ingest_nflverse_nfl_matches({2025})

        errors = [str(c) for c in mock_logger.error.call_args_list]
        unmapped = [e for e in errors if "Unmapped nflverse team abbreviations" in e]

        self.assertEqual(len(unmapped), 1, f"expected exactly one summary, got {errors}")
        self.assertIn("MIN (1 games)", unmapped[0])
        # The mapped side must not be reported.
        self.assertNotIn("CLE", unmapped[0])
        self.assertEqual(await Match.objects.acount(), 0)

    async def test_reports_a_missing_stage_separately_from_a_missing_team(self):
        """The two failures need different fixes - run the team sync vs run
        the infrastructure sync - so they must not be conflated."""
        await StageMapping.objects.filter(external_id=nfl_stage_external_id(2025, "REG")).adelete()

        with patch("sports.services.ingestion.logger") as mock_logger:
            with patch(
                "sports.services.ingestion.NflverseClient",
                return_value=FakeNflverseClient([self.games["2025_05_MIN_CLE"]]),
            ):
                await ingest_nflverse_nfl_matches({2025})

        errors = [str(c) for c in mock_logger.error.call_args_list]
        self.assertTrue(any("No stage mapping for" in e and "2025:REG" in e for e in errors))

    async def test_stays_quiet_when_everything_resolves(self):
        team = await Team.objects.acreate(name="Minnesota Vikings", sport=Sport.AMERICAN_FOOTBALL)
        await TeamMapping.objects.acreate(provider=SportsProvider.NFLVERSE, external_id="MIN", team=team)

        with patch("sports.services.ingestion.logger") as mock_logger:
            with patch(
                "sports.services.ingestion.NflverseClient",
                return_value=FakeNflverseClient([self.games["2025_05_MIN_CLE"]]),
            ):
                await ingest_nflverse_nfl_matches({2025})

        errors = [str(c) for c in mock_logger.error.call_args_list]
        self.assertEqual(errors, [])
        self.assertEqual(await Match.objects.acount(), 1)


class TeamMappingSeedGuardTests(TestCase):
    """Covers the shortfall check in ingest_nflverse_team_mappings: ending up
    with fewer than 32 mappings means a team is invisible all season."""

    def setUp(self):
        patcher = patch("sports.signals.redis_client")
        self.addCleanup(patcher.stop)
        patcher.start()

    async def test_errors_when_fewer_than_all_teams_are_bridged(self):
        team = await Team.objects.acreate(name="Los Angeles Rams", sport=Sport.AMERICAN_FOOTBALL)
        await TeamMapping.objects.acreate(provider=SportsProvider.ESPN, external_id="14", team=team)

        espn_team = EspnTeam.model_validate({"id": "14", "abbreviation": "LAR", "displayName": "Los Angeles Rams"})

        with patch("sports.services.ingestion.logger") as mock_logger:
            with patch(
                "sports.services.ingestion.EspnClient",
                return_value=FakeEspnTeamsClient([espn_team]),
            ):
                await ingest_nflverse_team_mappings(2025)

        # The one team present is bridged under nflverse's spelling...
        self.assertTrue(
            await TeamMapping.objects.filter(provider=SportsProvider.NFLVERSE, external_id="LA").aexists()
        )
        # ...but 1 of 32 must be reported.
        errors = [str(c) for c in mock_logger.error.call_args_list]
        self.assertTrue(any("but have 1" in e for e in errors), errors)

    async def test_refuses_to_run_before_the_espn_team_sync(self):
        with patch("sports.services.ingestion.logger") as mock_logger:
            with patch("sports.services.ingestion.EspnClient") as mock_client_cls:
                await ingest_nflverse_team_mappings(2025)

        mock_client_cls.assert_not_called()
        errors = [str(c) for c in mock_logger.error.call_args_list]
        self.assertTrue(any("No ESPN team mappings" in e for e in errors))


class FakeEspnTeamsClient:
    """Stands in for EspnClient when only get_teams is exercised."""

    def __init__(self, teams):
        self._teams = teams

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get_teams(self, season_year):
        return self._teams
