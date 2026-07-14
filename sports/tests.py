from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.utils import timezone
from PIL import Image

from sports.integrations.fifa import LocaleDescription
from sports.models import Competition, Match, MatchOutcome, MatchStatus, Season, Stage, StageType, Team
from sports.schemas import MatchUpdatePayload
from sports.services.ingestion import _process_and_format_image, extract_name


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
