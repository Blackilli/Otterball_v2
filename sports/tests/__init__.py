import datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from django.conf import settings
from django.test import TestCase
from django.utils import timezone
from PIL import Image
from pydantic import TypeAdapter

from sports.integrations.fifa import (
    Booking,
)
from sports.integrations.fifa import Competition as FifaCompetition
from sports.integrations.fifa import CompetitionMatch as FifaCompetitionMatch
from sports.integrations.fifa import CompetitionType, FifaClient
from sports.integrations.fifa import Gender as FifaGender
from sports.integrations.fifa import (
    Goal,
    IApiMultipleResultsPaged,
    LiveMatch,
    LiveMatchCoach,
    LiveMatchPlayer,
    LiveMatchStaff,
    LiveMatchTeam,
    LocaleDescription,
)
from sports.integrations.fifa import MatchStatus as FifaMatchStatus
from sports.integrations.fifa import MatchTeam, ResultType
from sports.integrations.fifa import Season as FifaSeason
from sports.integrations.fifa import Stage as FifaStage
from sports.integrations.fifa import Substitution
from sports.integrations.fifa import Team as FifaTeam
from sports.models import (
    Competition,
    CompetitionMapping,
    Gender,
    Match,
    MatchMapping,
    MatchOutcome,
    MatchStatus,
    Season,
    SportsProvider,
    Stage,
    StageType,
    Team,
)
from sports.schemas import MatchUpdatePayload
from sports.services.ingestion import (
    _process_and_format_image,
    extract_name,
    ingest_all_fifa_competitions,
    ingest_fifa_live_matches,
)


class MatchOutcomeTests(TestCase):
    """Covers Match.outcome (sports/models.py), which derives the winner from
    status/scores rather than storing it - predictions/signals.py scoring
    depends on this being correct."""

    def setUp(self):
        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="2026 World Cup", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP)
        self.home_team = Team.objects.create(name="Germany")
        self.away_team = Team.objects.create(name="Brazil")

    def make_match(self, **kwargs):
        defaults = {
            "stage": self.stage,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": timezone.now(),
        }
        defaults.update(kwargs)
        return Match.objects.create(**defaults)

    def test_higher_home_score_is_a_home_win(self):
        match = self.make_match(status=MatchStatus.FINISHED, home_score=2, away_score=1)
        self.assertEqual(match.outcome, MatchOutcome.HOME_WIN)

    def test_higher_away_score_is_an_away_win(self):
        match = self.make_match(status=MatchStatus.FINISHED, home_score=0, away_score=3)
        self.assertEqual(match.outcome, MatchOutcome.AWAY_WIN)

    def test_equal_scores_is_a_draw(self):
        match = self.make_match(status=MatchStatus.FINISHED, home_score=1, away_score=1)
        self.assertEqual(match.outcome, MatchOutcome.DRAW)

    def test_unfinished_match_has_no_outcome_even_with_scores(self):
        match = self.make_match(status=MatchStatus.LIVE, home_score=1, away_score=0)
        self.assertIsNone(match.outcome)

    def test_finished_match_without_scores_has_no_outcome(self):
        match = self.make_match(status=MatchStatus.FINISHED, home_score=None, away_score=None)
        self.assertIsNone(match.outcome)


class ExtractNameTests(TestCase):
    """Covers extract_name (sports/services/ingestion.py), which picks an
    English display name out of the FIFA API's list of localized names."""

    def test_prefers_en_gb_locale(self):
        names = [
            LocaleDescription(Locale="de-DE", Description="Deutschland"),
            LocaleDescription(Locale="en-GB", Description="Germany"),
        ]
        self.assertEqual(extract_name(names), "Germany")

    def test_falls_back_to_en_us_locale(self):
        names = [
            LocaleDescription(Locale="fr-FR", Description="Allemagne"),
            LocaleDescription(Locale="en-US", Description="Germany"),
        ]
        self.assertEqual(extract_name(names), "Germany")

    def test_falls_back_to_first_entry_when_no_english_locale(self):
        names = [
            LocaleDescription(Locale="de-DE", Description="Deutschland"),
            LocaleDescription(Locale="fr-FR", Description="Allemagne"),
        ]
        self.assertEqual(extract_name(names), "Deutschland")

    def test_empty_list_returns_unknown_team(self):
        self.assertEqual(extract_name([]), "Unknown Team")

    def test_missing_description_on_matched_locale_returns_unknown_team(self):
        names = [LocaleDescription(Locale="en-GB", Description=None)]
        self.assertEqual(extract_name(names), "Unknown Team")


class ProcessAndFormatImageTests(TestCase):
    """Covers _process_and_format_image (sports/services/ingestion.py), which
    turns a downloaded team logo into the ContentFile saved onto Team.logo."""

    def test_returns_a_slugified_png_content_file(self):
        pil_img = Image.new("RGB", (4, 4), color="red")

        content_file = _process_and_format_image(pil_img, team_id="123", team_name="FC Ottersdorf!")

        self.assertEqual(content_file.name, "123_fc-ottersdorf.png")
        reopened = Image.open(content_file)
        self.assertEqual(reopened.format, "PNG")
        self.assertEqual(reopened.size, (4, 4))


class NotifyMatchUpdateSignalTests(TestCase):
    """Covers notify_match_update (sports/signals.py): every Match update (not
    creation) should publish a MatchUpdatePayload to Redis on commit, which is
    what predictions/signals.py's receive_match_update consumes downstream."""

    def setUp(self):
        patcher = patch("sports.signals.redis_client")
        self.addCleanup(patcher.stop)
        self.mock_redis = patcher.start()

        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="2026 World Cup", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP)
        self.home_team = Team.objects.create(name="Germany")
        self.away_team = Team.objects.create(name="Brazil")
        with self.captureOnCommitCallbacks(execute=True):
            self.match = Match.objects.create(
                stage=self.stage,
                home_team=self.home_team,
                away_team=self.away_team,
                kickoff=timezone.now(),
                status=MatchStatus.SCHEDULED,
            )
        self.mock_redis.reset_mock()

    def test_match_update_publishes_payload_on_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.match.status = MatchStatus.LIVE
            self.match.home_score = 1
            self.match.away_score = 0
            self.match.save()

        self.mock_redis.publish.assert_called_once()
        channel, message_json = self.mock_redis.publish.call_args[0]
        self.assertEqual(channel, settings.REDIS_MATCH_UPDATE_TOPIC)

        payload = MatchUpdatePayload.model_validate_json(message_json)
        self.assertEqual(payload.match_id, self.match.id)
        self.assertEqual(payload.status, MatchStatus.LIVE)
        self.assertEqual(payload.home_score, 1)
        self.assertEqual(payload.away_score, 0)

    def test_match_creation_does_not_publish(self):
        with self.captureOnCommitCallbacks(execute=True):
            Match.objects.create(
                stage=self.stage,
                home_team=self.home_team,
                away_team=self.away_team,
                kickoff=timezone.now(),
                status=MatchStatus.SCHEDULED,
            )

        self.mock_redis.publish.assert_not_called()


class FakeFifaClient:
    """Stands in for sports.integrations.fifa.FifaClient inside `async with
    FifaClient() as client:` blocks - only implements what ingestion.py calls."""

    def __init__(self, competitions=None, live_matches_by_external_id=None):
        self._competitions = competitions or []
        self._live_matches = live_matches_by_external_id or {}
        self.requested_live_match_ids = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get_competitions_all(self, *args, **kwargs):
        for competition in self._competitions:
            yield competition

    async def get_live_match_by_id(self, match_id, *args, **kwargs):
        self.requested_live_match_ids.append(match_id)
        return self._live_matches.get(match_id)


class IngestAllFifaCompetitionsTests(TestCase):
    """Covers ingest_all_fifa_competitions (sports/services/ingestion.py): it
    should create a Competition + CompetitionMapping for each unseen FIFA
    competition and skip ones already mapped, using field names verified
    against the real FIFA swagger spec at api.fifa.com/ApiFdcpSwagger/docs/v1."""

    async def test_skips_already_mapped_competitions_and_creates_new_ones(self):
        existing_competition = await Competition.objects.acreate(name="Existing Cup")
        await CompetitionMapping.objects.acreate(
            provider=SportsProvider.FIFA, external_id="EXISTING", competition=existing_competition
        )

        already_mapped = FifaCompetition(
            IdCompetition="EXISTING",
            Name=[{"Locale": "en-GB", "Description": "Existing Cup (renamed)"}],
        )
        brand_new = FifaCompetition(
            IdCompetition="NEW1",
            Name=[{"Locale": "en-GB", "Description": "FIFA World Cup"}],
            Gender=FifaGender.FEMALE.value,
        )
        fake_client = FakeFifaClient(competitions=[already_mapped, brand_new])

        with patch("sports.services.ingestion.FifaClient", return_value=fake_client):
            await ingest_all_fifa_competitions()

        self.assertEqual(await Competition.objects.acount(), 2)
        new_competition = await Competition.objects.aget(name="FIFA World Cup")
        self.assertEqual(new_competition.gender, Gender.FEMALE)
        self.assertTrue(
            await CompetitionMapping.objects.filter(
                provider=SportsProvider.FIFA, external_id="NEW1", competition=new_competition
            ).aexists()
        )
        # The already-mapped competition's name must not have been touched.
        await existing_competition.arefresh_from_db()
        self.assertEqual(existing_competition.name, "Existing Cup")


class IngestFifaLiveMatchesTests(TestCase):
    """Covers ingest_fifa_live_matches (sports/services/ingestion.py): the
    LIVE-or-kicking-off-soon filter, and the score/status update from a
    LiveMatch response using field names verified against the real spec."""

    def setUp(self):
        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="2026 World Cup", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP)
        self.home_team = Team.objects.create(name="Germany")
        self.away_team = Team.objects.create(name="Brazil")

    async def amake_match_with_mapping(self, external_id, **kwargs):
        defaults = {
            "stage": self.stage,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": timezone.now(),
        }
        defaults.update(kwargs)
        match = await Match.objects.acreate(**defaults)
        await MatchMapping.objects.acreate(provider=SportsProvider.FIFA, external_id=external_id, match=match)
        return match

    async def test_no_eligible_matches_never_touches_the_fifa_client(self):
        await self.amake_match_with_mapping("FINISHED1", status=MatchStatus.FINISHED, home_score=1, away_score=0)

        with patch("sports.services.ingestion.FifaClient") as mock_client_cls:
            await ingest_fifa_live_matches()

        mock_client_cls.assert_not_called()

    async def test_queries_only_live_and_soon_matches_and_applies_score_updates(self):
        live_match = await self.amake_match_with_mapping("LIVE1", status=MatchStatus.LIVE, home_score=0, away_score=0)
        soon_match = await self.amake_match_with_mapping(
            "SOON1",
            status=MatchStatus.SCHEDULED,
            kickoff=timezone.now() + datetime.timedelta(minutes=10),
        )
        await self.amake_match_with_mapping(
            "LATER1",
            status=MatchStatus.SCHEDULED,
            kickoff=timezone.now() + datetime.timedelta(minutes=30),
        )
        await self.amake_match_with_mapping("FINISHED1", status=MatchStatus.FINISHED, home_score=1, away_score=0)

        live_update = LiveMatch(
            IdMatch="LIVE1",
            IdStage="S1",
            IdSeason="SE1",
            IdCompetition="C1",
            MatchStatus=FifaMatchStatus.LIVE.value,
            HomeTeam=LiveMatchTeam(Score=2, IdTeam="T1", Goals=[{"IdGoal": "g1"}, {"IdGoal": "g2"}]),
            AwayTeam=LiveMatchTeam(Score=1, IdTeam="T2", Goals=[{"IdGoal": "g3"}]),
        )
        fake_client = FakeFifaClient(live_matches_by_external_id={"LIVE1": live_update, "SOON1": None})

        with patch("sports.services.ingestion.FifaClient", return_value=fake_client):
            await ingest_fifa_live_matches()

        self.assertCountEqual(fake_client.requested_live_match_ids, ["LIVE1", "SOON1"])

        await live_match.arefresh_from_db()
        self.assertEqual(live_match.home_score, 2)
        self.assertEqual(live_match.away_score, 1)
        self.assertEqual(live_match.status, MatchStatus.LIVE)

        await soon_match.arefresh_from_db()
        self.assertIsNone(soon_match.home_score)
        self.assertIsNone(soon_match.away_score)


class FifaClientQueryParamTests(TestCase):
    """Covers FifaClient.get_competitions_all's query-param construction: the
    competition_type filter must be sent as `type`, not `competitionType` -
    verified against the real api.fifa.com/ApiFdcpSwagger/docs/v1 spec, which
    the client previously got wrong (silently, since nothing called it with
    competition_type set)."""

    async def test_competition_type_filter_uses_the_real_query_param_name(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"Results": [], "ContinuationToken": None, "ContinuationHash": None}

        client = FifaClient()
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)

        async for _ in client.get_competitions_all(competition_type=CompetitionType.INTERNATIONAL):
            pass

        _, kwargs = client._client.get.call_args
        self.assertEqual(kwargs["params"].get("type"), CompetitionType.INTERNATIONAL.value)
        self.assertNotIn("competitionType", kwargs["params"])


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name):
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


class RealFifaApiResponseTests(TestCase):
    """Guards sports/integrations/fifa.py's schemas against drift from the
    real API. Each fixture is a genuine response captured from api.fifa.com
    for FIFA World Cup 2026 (competition_id=17, season_id=285023) via
    `curl -A "Mozilla/5.0" "https://api.fifa.com/api/v3/..."`.

    Parsing alone isn't a strong enough check: pydantic's default
    extra="ignore" silently drops any JSON key that doesn't match a field's
    alias instead of raising, which is exactly how the training_center,
    staff, territorial_possession/territorial_third_possession, match_day,
    id_assist_player, and is_updatable(Stadium) bugs went unnoticed. So
    every test here explicitly asserts zero unmapped keys, on top of
    parsing successfully."""

    def assert_all_keys_mapped(self, model_cls, data, label):
        if data is None:
            return
        aliases = {f.alias for f in model_cls.model_fields.values() if f.alias}
        unmapped = set(data.keys()) - aliases
        self.assertEqual(unmapped, set(), f"{label}: unmapped API keys {unmapped}")

    def test_competition_response(self):
        data = load_fixture("competition_17.json")
        self.assert_all_keys_mapped(FifaCompetition, data, "Competition")

        competition = FifaCompetition.model_validate(data)
        self.assertEqual(competition.id_competition, "17")

    def test_season_response(self):
        data = load_fixture("season_285023.json")
        self.assert_all_keys_mapped(FifaSeason, data, "Season")

        season = FifaSeason.model_validate(data)
        self.assertEqual(season.id_season, "285023")
        self.assertEqual(season.id_competition, "17")

    def test_stages_response(self):
        page = load_fixture("stages_285023.json")
        for stage in page["Results"]:
            self.assert_all_keys_mapped(FifaStage, stage, f"Stage {stage['IdStage']}")

        parsed = TypeAdapter(IApiMultipleResultsPaged[FifaStage]).validate_python(page)
        self.assertEqual(len(parsed.results), len(page["Results"]))

    def test_matches_response(self):
        page = load_fixture("matches_285023.json")
        for match in page["Results"]:
            self.assert_all_keys_mapped(FifaCompetitionMatch, match, f"Match {match['IdMatch']}")
            self.assert_all_keys_mapped(MatchTeam, match.get("Home"), f"Match {match['IdMatch']} Home")
            self.assert_all_keys_mapped(MatchTeam, match.get("Away"), f"Match {match['IdMatch']} Away")

        parsed = TypeAdapter(IApiMultipleResultsPaged[FifaCompetitionMatch]).validate_python(page)
        self.assertEqual(len(parsed.results), len(page["Results"]))

    def test_scheduled_matches_response(self):
        """Covers not-yet-played matches, including the World Cup 2026 final,
        which - as of this fixture's capture - has only one finalist decided:
        Home is Spain, Away is null with PlaceHolderA/B ("W101"/"W102")
        pointing at the still-undecided semi-final winner."""
        page = load_fixture("matches_scheduled.json")
        for match in page["Results"]:
            self.assert_all_keys_mapped(FifaCompetitionMatch, match, f"Match {match['IdMatch']}")
            self.assert_all_keys_mapped(MatchTeam, match.get("Home"), f"Match {match['IdMatch']} Home")
            self.assert_all_keys_mapped(MatchTeam, match.get("Away"), f"Match {match['IdMatch']} Away")

        parsed = TypeAdapter(IApiMultipleResultsPaged[FifaCompetitionMatch]).validate_python(page)
        by_id = {m.id_match: m for m in parsed.results}

        semifinal = by_id["400021540"]
        self.assertIsNotNone(semifinal.home)
        self.assertIsNotNone(semifinal.away)

        final = by_id["400021543"]
        self.assertIsNotNone(final.home)
        self.assertIsNone(final.away)
        self.assertEqual(final.place_holder_a, "W101")
        self.assertEqual(final.place_holder_b, "W102")

    def test_finished_matches_by_result_type_response(self):
        """Covers the three ways a knockout match can be decided: regular
        time, extra time, and a penalty shootout (with penalty scores set
        alongside the 90-minute score that ended level)."""
        page = load_fixture("matches_result_types.json")
        for match in page["Results"]:
            self.assert_all_keys_mapped(FifaCompetitionMatch, match, f"Match {match['IdMatch']}")
            self.assert_all_keys_mapped(MatchTeam, match.get("Home"), f"Match {match['IdMatch']} Home")
            self.assert_all_keys_mapped(MatchTeam, match.get("Away"), f"Match {match['IdMatch']} Away")

        parsed = TypeAdapter(IApiMultipleResultsPaged[FifaCompetitionMatch]).validate_python(page)
        by_id = {m.id_match: m for m in parsed.results}

        normal_result = by_id["400021518"]
        self.assertEqual(normal_result.result_type, ResultType.NORMAL_RESULT)
        self.assertIsNone(normal_result.home_team_penalty_score)

        penalty_shootout = by_id["400021513"]
        self.assertEqual(penalty_shootout.result_type, ResultType.PENALTY_SHOOTOUT)
        self.assertEqual(penalty_shootout.home_team_score, penalty_shootout.away_team_score)
        self.assertIsNotNone(penalty_shootout.home_team_penalty_score)
        self.assertIsNotNone(penalty_shootout.away_team_penalty_score)
        self.assertNotEqual(penalty_shootout.home_team_penalty_score, penalty_shootout.away_team_penalty_score)

        extra_time = by_id["400021525"]
        self.assertEqual(extra_time.result_type, ResultType.EXTRA_TIME)
        self.assertIsNone(extra_time.home_team_penalty_score)

    def test_teams_response(self):
        page = load_fixture("teams_national.json")
        for team in page["Results"]:
            self.assert_all_keys_mapped(FifaTeam, team, f"Team {team['IdTeam']}")

        parsed = TypeAdapter(IApiMultipleResultsPaged[FifaTeam]).validate_python(page)
        self.assertEqual(len(parsed.results), len(page["Results"]))

    def test_live_match_response(self):
        data = load_fixture("live_match_400021541.json")
        self.assert_all_keys_mapped(LiveMatch, data, "LiveMatch")

        for side in ("HomeTeam", "AwayTeam"):
            team = data.get(side)
            self.assert_all_keys_mapped(LiveMatchTeam, team, side)
            for player in team.get("Players") or []:
                self.assert_all_keys_mapped(LiveMatchPlayer, player, f"{side} Player")
            for coach in team.get("Coaches") or []:
                self.assert_all_keys_mapped(LiveMatchCoach, coach, f"{side} Coach")
            for staff in team.get("Staffs") or []:
                self.assert_all_keys_mapped(LiveMatchStaff, staff, f"{side} Staff")
            for goal in team.get("Goals") or []:
                self.assert_all_keys_mapped(Goal, goal, f"{side} Goal")
            for booking in team.get("Bookings") or []:
                self.assert_all_keys_mapped(Booking, booking, f"{side} Booking")
            for substitution in team.get("Substitutions") or []:
                self.assert_all_keys_mapped(Substitution, substitution, f"{side} Substitution")

        live_match = LiveMatch.model_validate(data)
        self.assertEqual(live_match.id_match, "400021541")
