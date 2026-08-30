import csv
import datetime
import json
import tempfile
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import sync_to_async
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask
from django_celery_results.models import TaskResult

from discord_bot.models import (
    ActiveMatchMessage,
    DiscordChannel,
    DiscordGuild,
    DiscordGuildPool,
    DiscordGuildRole,
    DiscordTeamEmoji,
    MessagePreviewRequest,
    PreviewMessageKind,
    PreviewStatus,
)
from predictions.admin import PredictionPoolAdmin
from predictions.closeout import close_out_pool, plan_closeout
from predictions.history import (
    build_consensus,
    build_contrarians,
    build_crowd_standing,
    build_outcome_mix,
    build_rank_history,
    build_streaks,
    final_order,
)
from predictions.models import (
    MAX_POLL_LOOKAHEAD_DAYS,
    MAX_REMINDER_LEAD_MINUTES,
    PoolStageRule,
    Prediction,
    PredictionPool,
    hit_rate_percent,
    sync_pool_stage_rules,
)
from predictions.readiness import FAIL, OK, WARN, check_environment, check_pool, worst
from predictions.signals import process_match_update
from sports.models import Competition, Match, MatchOutcome, MatchStatus, Season, Sport, Stage, StageType, Team

User = get_user_model()


class PredictionScoringTestCase(TestCase):
    """Shared fixtures for a single finished match with a known outcome."""

    def setUp(self):
        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="2026 World Cup", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP, level=1)
        self.other_stage = Stage.objects.create(
            season=self.season, name="Final", stage_type=StageType.KNOCK_OUT, level=99
        )
        self.home_team = Team.objects.create(name="Germany")
        self.away_team = Team.objects.create(name="Brazil")
        self.match = Match.objects.create(
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=timezone.now(),
            status=MatchStatus.FINISHED,
            home_score=2,
            away_score=1,
        )
        self.pool = PredictionPool.objects.create(name="Test Pool", season=self.season)
        self.user = User.objects.create_user(username="alice")

    def make_prediction(self, predicted_outcome=MatchOutcome.HOME_WIN, **kwargs):
        return Prediction.objects.create(
            pool=self.pool,
            match=self.match,
            user=self.user,
            predicted_outcome=predicted_outcome,
            **kwargs,
        )

    async def amake_prediction(self, predicted_outcome=MatchOutcome.HOME_WIN, **kwargs):
        return await Prediction.objects.acreate(
            pool=self.pool,
            match=self.match,
            user=self.user,
            predicted_outcome=predicted_outcome,
            **kwargs,
        )


class UpdatePointsTests(PredictionScoringTestCase):
    """Covers the sync `update_points` path (predictions/models.py)."""

    def test_incorrect_prediction_awards_zero_points(self):
        prediction = self.make_prediction(predicted_outcome=MatchOutcome.AWAY_WIN)

        prediction.update_points()

        self.assertEqual(prediction.points_awarded, 0)
        self.assertTrue(prediction.is_processed)

    def test_correct_prediction_uses_cached_points(self):
        prediction = self.make_prediction(predicted_outcome=MatchOutcome.HOME_WIN)

        prediction.update_points(cached_points=7, cached_outcome=MatchOutcome.HOME_WIN)

        self.assertEqual(prediction.points_awarded, 7)
        self.assertTrue(prediction.is_processed)

    def test_correct_prediction_uses_stage_specific_rule_over_pool_wide_fallback(self):
        PoolStageRule.objects.create(pool=self.pool, stage=self.stage, level=1, points_per_correct=5)
        PoolStageRule.objects.create(pool=self.pool, stage=None, level=0, points_per_correct=1)
        prediction = self.make_prediction(predicted_outcome=MatchOutcome.HOME_WIN)

        prediction.update_points()

        self.assertEqual(prediction.points_awarded, 5)

    def test_correct_prediction_falls_back_to_pool_wide_rule_when_no_stage_rule(self):
        PoolStageRule.objects.create(pool=self.pool, stage=self.other_stage, level=1, points_per_correct=5)
        PoolStageRule.objects.create(pool=self.pool, stage=None, level=0, points_per_correct=2)
        prediction = self.make_prediction(predicted_outcome=MatchOutcome.HOME_WIN)

        prediction.update_points()

        self.assertEqual(prediction.points_awarded, 2)

    def test_correct_prediction_falls_back_to_hardcoded_default_when_no_rules_exist(self):
        prediction = self.make_prediction(predicted_outcome=MatchOutcome.HOME_WIN)

        prediction.update_points()

        self.assertEqual(prediction.points_awarded, 3)

    def test_already_processed_prediction_is_not_recomputed_without_force(self):
        prediction = self.make_prediction(
            predicted_outcome=MatchOutcome.HOME_WIN,
            is_processed=True,
            points_awarded=99,
        )

        prediction.update_points()

        self.assertEqual(prediction.points_awarded, 99)

    def test_force_recomputes_an_already_processed_prediction(self):
        prediction = self.make_prediction(
            predicted_outcome=MatchOutcome.HOME_WIN,
            is_processed=True,
            points_awarded=99,
        )

        prediction.update_points(force=True, cached_points=4, cached_outcome=MatchOutcome.HOME_WIN)

        self.assertEqual(prediction.points_awarded, 4)

    def test_update_points_persists_to_the_database(self):
        prediction = self.make_prediction(predicted_outcome=MatchOutcome.HOME_WIN)

        prediction.update_points()

        refreshed = Prediction.objects.get(id=prediction.id)
        self.assertEqual(refreshed.points_awarded, 3)
        self.assertTrue(refreshed.is_processed)


class AupdatePointsTests(PredictionScoringTestCase):
    """Mirrors UpdatePointsTests for the async `aupdate_points` path, which is
    kept manually in sync with the sync path (see CLAUDE.md) — these tests
    exist to catch the two implementations drifting apart."""

    async def test_incorrect_prediction_awards_zero_points(self):
        prediction = await self.amake_prediction(predicted_outcome=MatchOutcome.AWAY_WIN)

        await prediction.aupdate_points()

        self.assertEqual(prediction.points_awarded, 0)
        self.assertTrue(prediction.is_processed)

    async def test_correct_prediction_uses_cached_points(self):
        prediction = await self.amake_prediction(predicted_outcome=MatchOutcome.HOME_WIN)

        await prediction.aupdate_points(cached_points=7, cached_outcome=MatchOutcome.HOME_WIN)

        self.assertEqual(prediction.points_awarded, 7)
        self.assertTrue(prediction.is_processed)

    async def test_correct_prediction_uses_stage_specific_rule_over_pool_wide_fallback(self):
        await PoolStageRule.objects.acreate(pool=self.pool, stage=self.stage, level=1, points_per_correct=5)
        await PoolStageRule.objects.acreate(pool=self.pool, stage=None, level=0, points_per_correct=1)
        prediction = await self.amake_prediction(predicted_outcome=MatchOutcome.HOME_WIN)

        await prediction.aupdate_points()

        self.assertEqual(prediction.points_awarded, 5)

    async def test_correct_prediction_falls_back_to_pool_wide_rule_when_no_stage_rule(self):
        await PoolStageRule.objects.acreate(pool=self.pool, stage=self.other_stage, level=1, points_per_correct=5)
        await PoolStageRule.objects.acreate(pool=self.pool, stage=None, level=0, points_per_correct=2)
        prediction = await self.amake_prediction(predicted_outcome=MatchOutcome.HOME_WIN)

        await prediction.aupdate_points()

        self.assertEqual(prediction.points_awarded, 2)

    async def test_correct_prediction_falls_back_to_hardcoded_default_when_no_rules_exist(self):
        prediction = await self.amake_prediction(predicted_outcome=MatchOutcome.HOME_WIN)

        await prediction.aupdate_points()

        self.assertEqual(prediction.points_awarded, 3)

    async def test_already_processed_prediction_is_not_recomputed_without_force(self):
        prediction = await self.amake_prediction(
            predicted_outcome=MatchOutcome.HOME_WIN,
            is_processed=True,
            points_awarded=99,
        )

        await prediction.aupdate_points()

        self.assertEqual(prediction.points_awarded, 99)

    async def test_force_recomputes_an_already_processed_prediction(self):
        prediction = await self.amake_prediction(
            predicted_outcome=MatchOutcome.HOME_WIN,
            is_processed=True,
            points_awarded=99,
        )

        await prediction.aupdate_points(force=True, cached_points=4, cached_outcome=MatchOutcome.HOME_WIN)

        self.assertEqual(prediction.points_awarded, 4)


class ProcessMatchUpdateTests(PredictionScoringTestCase):
    """Covers predictions/signals.py: process_match_update (the batch scorer)
    and the receive_match_update post_save hook that wires it to Match saves."""

    def setUp(self):
        super().setUp()
        # Match.save() also fires sports.signals.notify_match_update, which
        # publishes to Redis on commit. captureOnCommitCallbacks(execute=True)
        # below runs that callback for real, so the client needs mocking.
        patcher = patch("sports.signals.redis_client")
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_scores_every_prediction_on_the_match(self):
        other_user = User.objects.create_user(username="bob")
        correct = self.make_prediction(predicted_outcome=MatchOutcome.HOME_WIN)
        incorrect = Prediction.objects.create(
            pool=self.pool,
            match=self.match,
            user=other_user,
            predicted_outcome=MatchOutcome.AWAY_WIN,
        )

        process_match_update(self.match)

        correct.refresh_from_db()
        incorrect.refresh_from_db()
        self.assertEqual(correct.points_awarded, 3)
        self.assertTrue(correct.is_processed)
        self.assertEqual(incorrect.points_awarded, 0)
        self.assertTrue(incorrect.is_processed)

    def test_uses_stage_specific_rule_over_pool_wide_fallback(self):
        PoolStageRule.objects.create(pool=self.pool, stage=self.stage, level=1, points_per_correct=10)
        PoolStageRule.objects.create(pool=self.pool, stage=None, level=0, points_per_correct=1)
        prediction = self.make_prediction(predicted_outcome=MatchOutcome.HOME_WIN)

        process_match_update(self.match)

        prediction.refresh_from_db()
        self.assertEqual(prediction.points_awarded, 10)

    def test_reprocesses_already_processed_predictions(self):
        prediction = self.make_prediction(
            predicted_outcome=MatchOutcome.HOME_WIN,
            is_processed=True,
            points_awarded=0,
        )

        process_match_update(self.match)

        prediction.refresh_from_db()
        self.assertEqual(prediction.points_awarded, 3)

    def test_match_transition_to_finished_triggers_recalculation_on_commit(self):
        scheduled_match = Match.objects.create(
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=timezone.now(),
            status=MatchStatus.SCHEDULED,
        )
        prediction = Prediction.objects.create(
            pool=self.pool,
            match=scheduled_match,
            user=self.user,
            predicted_outcome=MatchOutcome.HOME_WIN,
        )

        with self.captureOnCommitCallbacks(execute=True):
            scheduled_match.status = MatchStatus.FINISHED
            scheduled_match.home_score = 2
            scheduled_match.away_score = 0
            scheduled_match.save()

        prediction.refresh_from_db()
        self.assertEqual(prediction.points_awarded, 3)
        self.assertTrue(prediction.is_processed)

    def test_non_finishing_match_update_does_not_trigger_recalculation(self):
        scheduled_match = Match.objects.create(
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=timezone.now(),
            status=MatchStatus.SCHEDULED,
        )
        prediction = Prediction.objects.create(
            pool=self.pool,
            match=scheduled_match,
            user=self.user,
            predicted_outcome=MatchOutcome.HOME_WIN,
        )

        with self.captureOnCommitCallbacks(execute=True):
            scheduled_match.status = MatchStatus.LIVE
            scheduled_match.save()

        prediction.refresh_from_db()
        self.assertFalse(prediction.is_processed)
        self.assertEqual(prediction.points_awarded, 0)

    def test_match_created_already_finished_does_not_trigger_recalculation(self):
        # Guards the `created` check in receive_match_update: a match that is
        # inserted directly with status=FINISHED (e.g. a backfill/fixture)
        # should not fire scoring off of its creation save.
        prediction = self.make_prediction(predicted_outcome=MatchOutcome.HOME_WIN)
        prediction.points_awarded = 0
        prediction.is_processed = False
        prediction.save()

        with self.captureOnCommitCallbacks(execute=True):
            Match.objects.create(
                stage=self.stage,
                home_team=self.home_team,
                away_team=self.away_team,
                kickoff=timezone.now(),
                status=MatchStatus.FINISHED,
                home_score=1,
                away_score=0,
            )

        prediction.refresh_from_db()
        self.assertFalse(prediction.is_processed)

    def test_fixture_load_does_not_trigger_recalculation(self):
        # Guards the `raw` check in receive_match_update: import_db/loaddata replays a dump
        # over rows that already exist, so a finished match arrives as an update rather than a
        # creation. The points in the dump are already final, so re-scoring every restored
        # match is pure churn - and it would overwrite the restored values with recomputed ones.
        prediction = self.make_prediction(predicted_outcome=MatchOutcome.HOME_WIN)
        prediction.points_awarded = 0
        prediction.is_processed = False
        prediction.save()

        fixture = [
            {
                "model": "sports.match",
                "pk": self.match.pk,
                "fields": {
                    "stage": self.stage.pk,
                    "home_team": self.home_team.pk,
                    "away_team": self.away_team.pk,
                    "kickoff": self.match.kickoff.isoformat(),
                    "status": MatchStatus.FINISHED,
                    "home_score": 2,
                    "away_score": 1,
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_path = f"{tmpdir}/match.json"
            with open(fixture_path, "w") as fh:
                json.dump(fixture, fh)

            with self.captureOnCommitCallbacks(execute=True):
                call_command("loaddata", fixture_path, verbosity=0)

        prediction.refresh_from_db()
        self.assertFalse(prediction.is_processed)
        self.assertEqual(prediction.points_awarded, 0)


class HitRatePercentTests(TestCase):
    """Covers hit_rate_percent, the one place accuracy is rounded for display."""

    def test_rounds_to_a_whole_percent(self):
        self.assertEqual(hit_rate_percent(1, 3), 33)
        self.assertEqual(hit_rate_percent(2, 3), 67)
        self.assertEqual(hit_rate_percent(3, 4), 75)

    def test_no_picks_is_zero_rather_than_a_crash(self):
        """No listed player has zero picks, but a hand-built row might."""
        self.assertEqual(hit_rate_percent(0, 0), 0)


class LeaderboardTests(TestCase):
    """Covers PredictionPool.aget_leaderboard's Standard Competition Ranking
    (1-2-2-4: ties share a rank, the next rank skips accordingly), now ordered
    by points and then accuracy."""

    def setUp(self):
        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="2026 World Cup", competition=self.competition, year=2026)
        self.pool = PredictionPool.objects.create(name="Test Pool", season=self.season)
        self.alice = User.objects.create_user(username="alice")
        self.bob = User.objects.create_user(username="bob")
        self.carol = User.objects.create_user(username="carol")
        self.dave = User.objects.create_user(username="dave")
        self.stage = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP, level=1)
        self.home_team = Team.objects.create(name="Germany")
        self.away_team = Team.objects.create(name="Brazil")

    async def award(self, user, points):
        match = await Match.objects.acreate(
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=timezone.now(),
            status=MatchStatus.FINISHED,
            home_score=1,
            away_score=0,
        )
        await Prediction.objects.acreate(
            pool=self.pool,
            match=match,
            user=user,
            predicted_outcome=MatchOutcome.HOME_WIN,
            points_awarded=points,
            is_processed=True,
        )

    async def award_many(self, user, *point_values):
        """One prediction per value, so a 0 is a pick that was made and missed."""
        for points in point_values:
            await self.award(user, points)

    async def ranked(self):
        return {user.username: rank async for rank, user, _points in self.pool.aget_leaderboard()}

    async def test_accuracy_breaks_a_tie_on_points(self):
        """Both on 8, but alice got both her picks right and bob missed two."""
        await self.award_many(self.alice, 4, 4)
        await self.award_many(self.bob, 8, 0, 0)

        self.assertEqual(await self.ranked(), {"alice": 1, "bob": 2})

    async def test_level_on_points_and_accuracy_still_shares_a_rank(self):
        await self.award_many(self.alice, 4, 4)
        await self.award_many(self.bob, 4, 4)
        await self.award_many(self.carol, 1)

        self.assertEqual(await self.ranked(), {"alice": 1, "bob": 1, "carol": 3})

    async def test_accuracy_never_outweighs_points(self):
        """It is a tiebreaker, not a second currency: 20 points beats a perfect 8."""
        await self.award_many(self.alice, 4, 4)
        await self.award_many(self.bob, 20, 0, 0, 0)

        self.assertEqual(await self.ranked(), {"bob": 1, "alice": 2})

    async def test_accuracy_counts_only_this_pool(self):
        other_pool = await PredictionPool.objects.acreate(name="Other", season=self.season)
        await self.award_many(self.alice, 4, 4)
        await self.award_many(self.bob, 4, 4)
        # Misses in another pool must not drag bob's accuracy down here.
        match = await Match.objects.acreate(
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=timezone.now(),
            status=MatchStatus.FINISHED,
            home_score=1,
            away_score=0,
        )
        await Prediction.objects.acreate(
            pool=other_pool,
            match=match,
            user=self.bob,
            predicted_outcome=MatchOutcome.AWAY_WIN,
            points_awarded=0,
            is_processed=True,
        )

        self.assertEqual(await self.ranked(), {"alice": 1, "bob": 1})

    async def test_picks_and_correct_ride_along_on_the_user(self):
        """Both surfaces read these off the annotation instead of re-querying."""
        await self.award_many(self.alice, 4, 0, 4)

        leaderboard = [entry async for entry in self.pool.aget_leaderboard()]
        _rank, user, points = leaderboard[0]

        self.assertEqual((points, user.pool_prediction_count, user.pool_correct_count), (8, 3, 2))
        self.assertAlmostEqual(user.pool_hit_rate, 2 / 3)

    async def test_tied_scores_share_a_rank_and_next_rank_skips(self):
        await self.award(self.alice, 10)
        await self.award(self.bob, 8)
        await self.award(self.carol, 8)
        await self.award(self.dave, 5)

        leaderboard = [entry async for entry in self.pool.aget_leaderboard()]
        ranked_by_user = {user.username: rank for rank, user, _points in leaderboard}

        self.assertEqual(ranked_by_user["alice"], 1)
        self.assertEqual(ranked_by_user["bob"], 2)
        self.assertEqual(ranked_by_user["carol"], 2)
        self.assertEqual(ranked_by_user["dave"], 4)

    async def test_users_with_no_predictions_in_the_pool_are_not_listed(self):
        """Non-participants are excluded outright rather than listed on zero.

        They used to be annotated along with everyone else, which is
        backend-dependent and wrong either way: SQLite sorts the resulting
        NULLs last, Postgres sorts them first, so in production an unrelated
        pool's members took the top ranks. See LeaderboardScopingTests."""
        await self.award(self.alice, 10)

        leaderboard = [entry async for entry in self.pool.aget_leaderboard()]
        points_by_user = {user.username: points for _rank, user, points in leaderboard}

        self.assertEqual(points_by_user, {"alice": 10})


class ExportPointHistoryTests(TestCase):
    """Covers the export_point_history management command: one CSV row per
    finished match in kickoff order, one cumulative points column per user."""

    def setUp(self):
        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="2026 World Cup", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP, level=1)
        self.home_team = Team.objects.create(name="Germany")
        self.away_team = Team.objects.create(name="Brazil")
        self.pool = PredictionPool.objects.create(name="Test Pool", season=self.season)
        self.alice = User.objects.create_user(username="alice")
        self.bob = User.objects.create_user(username="bob")

    def make_match(self, kickoff, status=MatchStatus.FINISHED):
        return Match.objects.create(
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=kickoff,
            status=status,
            home_score=1,
            away_score=0,
        )

    def award(self, user, match, points):
        Prediction.objects.create(
            pool=self.pool,
            match=match,
            user=user,
            predicted_outcome=MatchOutcome.HOME_WIN,
            points_awarded=points,
            is_processed=True,
        )

    def run_command(self, *args):
        out = StringIO()
        call_command("export_point_history", self.pool.id, *args, stdout=out)
        return list(csv.reader(StringIO(out.getvalue())))

    def test_rows_accumulate_points_per_user_in_kickoff_order(self):
        first = self.make_match(datetime.datetime(2026, 6, 11, 18, tzinfo=datetime.timezone.utc))
        second = self.make_match(datetime.datetime(2026, 6, 12, 18, tzinfo=datetime.timezone.utc))
        self.award(self.alice, first, 3)
        self.award(self.bob, first, 0)
        self.award(self.alice, second, 5)
        self.award(self.bob, second, 5)

        rows = self.run_command()

        self.assertEqual(rows[0], ["kickoff", "match", "alice", "bob"])
        self.assertEqual(rows[1], ["2026-06-11T18:00:00+00:00", "Germany vs. Brazil", "3", "0"])
        self.assertEqual(rows[2], ["2026-06-12T18:00:00+00:00", "Germany vs. Brazil", "8", "5"])

    def test_user_without_prediction_on_a_match_keeps_previous_total(self):
        first = self.make_match(datetime.datetime(2026, 6, 11, 18, tzinfo=datetime.timezone.utc))
        second = self.make_match(datetime.datetime(2026, 6, 12, 18, tzinfo=datetime.timezone.utc))
        self.award(self.alice, first, 3)
        self.award(self.bob, first, 3)
        self.award(self.bob, second, 5)

        rows = self.run_command()

        self.assertEqual(rows[1][2:], ["3", "3"])
        self.assertEqual(rows[2][2:], ["3", "8"])

    def test_unfinished_matches_are_excluded(self):
        finished = self.make_match(datetime.datetime(2026, 6, 11, 18, tzinfo=datetime.timezone.utc))
        scheduled = self.make_match(
            datetime.datetime(2026, 6, 12, 18, tzinfo=datetime.timezone.utc),
            status=MatchStatus.SCHEDULED,
        )
        self.award(self.alice, finished, 3)
        self.award(self.alice, scheduled, 0)

        rows = self.run_command()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][2:], ["3"])

    def test_predictions_from_other_pools_are_ignored(self):
        match = self.make_match(datetime.datetime(2026, 6, 11, 18, tzinfo=datetime.timezone.utc))
        other_pool = PredictionPool.objects.create(name="Other Pool", season=self.season)
        Prediction.objects.create(
            pool=other_pool,
            match=match,
            user=self.bob,
            predicted_outcome=MatchOutcome.HOME_WIN,
            points_awarded=7,
            is_processed=True,
        )
        self.award(self.alice, match, 3)

        rows = self.run_command()

        self.assertEqual(rows[0], ["kickoff", "match", "alice"])
        self.assertEqual(rows[1][2:], ["3"])

    def test_writes_to_file_when_output_path_given(self):
        match = self.make_match(datetime.datetime(2026, 6, 11, 18, tzinfo=datetime.timezone.utc))
        self.award(self.alice, match, 3)

        with tempfile.NamedTemporaryFile(mode="r", suffix=".csv") as f:
            self.run_command(f.name)
            rows = list(csv.reader(f))

        self.assertEqual(rows[0], ["kickoff", "match", "alice"])
        self.assertEqual(rows[1][2:], ["3"])


class PredictionPoolAdminSeedingTests(TestCase):
    """Covers PredictionPoolAdmin.save_related, which is where a pool saved
    through /admin/ gets its stage rules topped up.

    Seeding hangs off save_related rather than save_model so that inline rows
    the user added are written first - see PredictionPoolAdminInlineTests for
    the collision that ordering prevents."""

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        for name, level in (("Regular Season", 0), ("Super Bowl", 1)):
            Stage.objects.create(season=self.season, name=name, level=level)

        self.admin = PredictionPoolAdmin(PredictionPool, AdminSite())
        self.request = RequestFactory().post("/admin/predictions/predictionpool/add/")
        self.request.user = User.objects.create_superuser(username="admin", password="x")
        # message_user needs a message store on the request.
        setattr(self.request, "session", "session")
        setattr(self.request, "_messages", FallbackStorage(self.request))

    def save(self, pool, change=False):
        # ModelAdmin.save_related calls form.save_m2m() before delegating to
        # the inline formsets; a no-op stands in for it here.
        form = SimpleNamespace(instance=pool, save_m2m=lambda: None)
        self.admin.save_related(self.request, form, formsets=[], change=change)

    def test_saving_a_pool_in_the_admin_seeds_its_stage_rules(self):
        pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)

        self.save(pool)

        self.assertEqual(
            sorted(pool.stage_rules.values_list("stage__name", flat=True)),
            ["Regular Season", "Super Bowl"],
        )

    def test_saving_again_tops_up_without_disturbing_edited_points(self):
        pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)
        self.save(pool)
        rule = pool.stage_rules.get(stage__name="Super Bowl")
        rule.points_per_correct = 5
        rule.save()
        Stage.objects.create(season=self.season, name="Wild Card", level=2)

        self.save(pool, change=True)

        self.assertEqual(pool.stage_rules.count(), 3)
        rule.refresh_from_db()
        self.assertEqual(rule.points_per_correct, 5)


class LeaderboardScopingTests(TestCase):
    """Covers PredictionPool.aget_user_with_points / aget_leaderboard
    (predictions/models.py).

    The ranking must be scoped to the pool being ranked. Before this was
    enforced, every User row in the database got annotated: non-participants
    came back with total_points = NULL, and Postgres sorts NULLs first on a
    DESC order, so members of an unrelated pool silently occupied the top
    ranks and pushed the real players down. The leaderboard cog hides them
    from the rendered embed but not from the rank numbers, so the visible
    effect was a leaderboard that started at rank 40 instead of rank 1."""

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Regular Season", stage_type=StageType.LEAGUE)
        self.home_team = Team.objects.create(name="Cleveland Browns")
        self.away_team = Team.objects.create(name="Minnesota Vikings")
        self.match = Match.objects.create(
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=timezone.now(),
            status=MatchStatus.FINISHED,
            home_score=17,
            away_score=21,
        )
        self.pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)
        self.other_pool = PredictionPool.objects.create(name="World Cup Pool", season=self.season)

    def make_prediction(self, user, pool, points, outcome=MatchOutcome.AWAY_WIN):
        return Prediction.objects.create(
            pool=pool,
            match=self.match,
            user=user,
            predicted_outcome=outcome,
            points_awarded=points,
            is_processed=True,
        )

    async def collect(self, pool):
        return [(rank, user.username, points) async for rank, user, points in pool.aget_leaderboard()]

    async def test_users_from_another_pool_are_not_ranked(self):
        player = await User.objects.acreate_user(username="nfl_player")
        outsider = await User.objects.acreate_user(username="worldcup_only")
        await User.objects.acreate_user(username="admin_who_never_plays")

        await Prediction.objects.acreate(
            pool=self.pool, match=self.match, user=player, predicted_outcome=MatchOutcome.AWAY_WIN, points_awarded=3
        )
        await Prediction.objects.acreate(
            pool=self.other_pool,
            match=self.match,
            user=outsider,
            predicted_outcome=MatchOutcome.AWAY_WIN,
            points_awarded=99,
        )

        leaderboard = await self.collect(self.pool)

        self.assertEqual(leaderboard, [(1, "nfl_player", 3)])

    async def test_points_are_summed_per_pool_not_across_pools(self):
        player = await User.objects.acreate_user(username="dual_player")
        await Prediction.objects.acreate(
            pool=self.pool, match=self.match, user=player, predicted_outcome=MatchOutcome.AWAY_WIN, points_awarded=3
        )
        await Prediction.objects.acreate(
            pool=self.other_pool,
            match=self.match,
            user=player,
            predicted_outcome=MatchOutcome.AWAY_WIN,
            points_awarded=50,
        )

        self.assertEqual(await self.collect(self.pool), [(1, "dual_player", 3)])
        self.assertEqual(await self.collect(self.other_pool), [(1, "dual_player", 50)])

    async def test_order_is_stable_across_calls(self):
        """The leaderboard cog diffs a fingerprint of this list to decide
        whether to edit its Discord message; an unstable order among tied
        users would make it rewrite the message forever."""
        for name in ("zoe", "adam", "mia"):
            user = await User.objects.acreate_user(username=name)
            await Prediction.objects.acreate(
                pool=self.pool,
                match=self.match,
                user=user,
                predicted_outcome=MatchOutcome.AWAY_WIN,
                points_awarded=7,
            )

        self.assertEqual(await self.collect(self.pool), await self.collect(self.pool))

    async def test_empty_pool_yields_nothing(self):
        await User.objects.acreate_user(username="bystander")
        self.assertEqual(await self.collect(self.pool), [])


class SyncPoolStageRulesTests(TestCase):
    """Covers sync_pool_stage_rules (predictions/models.py), called explicitly
    by the admin and by `manage.py create_pool`.

    Without rules, predictions/signals.py falls back to a hardcoded 3 points
    per correct pick - so a pool meant to scale points per round scores every
    round identically, with nothing logged. Seeding the rows makes that
    visible and editable."""

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stages = [
            Stage.objects.create(season=self.season, name=name, level=level, stage_type=stage_type)
            for name, level, stage_type in (
                ("Regular Season", 0, StageType.LEAGUE),
                ("Wild Card", 1, StageType.KNOCK_OUT),
                ("Super Bowl", 2, StageType.KNOCK_OUT),
            )
        ]

    def test_seeds_one_rule_per_stage(self):
        pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)

        sync_pool_stage_rules(pool)

        rules = pool.stage_rules.order_by("level")
        self.assertEqual([r.stage.name for r in rules], ["Regular Season", "Wild Card", "Super Bowl"])
        self.assertEqual([r.level for r in rules], [0, 1, 2])

    def test_seeded_rules_do_not_change_scoring(self):
        """Seeding is about visibility, not behaviour: the value must match
        the fallback that predictions/signals.py already applied."""
        pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)

        sync_pool_stage_rules(pool)

        self.assertTrue(all(r.points_per_correct == 3 for r in pool.stage_rules.all()))

    def test_is_idempotent_and_preserves_edited_points(self):
        pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)
        sync_pool_stage_rules(pool)
        rule = pool.stage_rules.get(stage=self.stages[2])
        rule.points_per_correct = 5
        rule.save()

        created = sync_pool_stage_rules(pool)

        self.assertEqual(created, [])
        self.assertEqual(pool.stage_rules.count(), 3)
        rule.refresh_from_db()
        self.assertEqual(rule.points_per_correct, 5)

    def test_tops_up_rules_when_the_season_gains_a_stage(self):
        """NFL playoff rounds only exist once the bracket is known, so a pool
        created in September must pick them up later."""
        pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)
        sync_pool_stage_rules(pool)
        new_stage = Stage.objects.create(season=self.season, name="Divisional", level=3)

        created = sync_pool_stage_rules(pool)

        self.assertEqual([r.stage_id for r in created], [new_stage.id])
        self.assertEqual(pool.stage_rules.count(), 4)

    def test_a_pool_on_a_season_without_stages_seeds_nothing(self):
        empty_season = Season.objects.create(name="Empty", competition=self.competition, year=2099)

        pool = PredictionPool.objects.create(name="Empty Pool", season=empty_season)

        self.assertEqual(sync_pool_stage_rules(pool), [])
        self.assertEqual(pool.stage_rules.count(), 0)

    def test_rules_from_another_season_are_untouched(self):
        other_season = Season.objects.create(name="Other", competition=self.competition, year=2027)
        Stage.objects.create(season=other_season, name="Regular Season", level=0)

        pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)
        other_pool = PredictionPool.objects.create(name="Other Pool", season=other_season)
        sync_pool_stage_rules(pool)
        sync_pool_stage_rules(other_pool)

        self.assertEqual(pool.stage_rules.count(), 3)
        self.assertEqual(other_pool.stage_rules.count(), 1)


class CreatePoolCommandTests(TestCase):
    """Covers the create_pool management command."""

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        for name, level in (("Regular Season", 0), ("Wild Card", 1), ("Super Bowl", 2)):
            Stage.objects.create(season=self.season, name=name, level=level)

    def call(self, **kwargs):
        out = StringIO()
        call_command("create_pool", stdout=out, **kwargs)
        return out.getvalue()

    def test_creates_pool_configuration_and_rules(self):
        self.call(name="NFL Pool", season=self.season.id, weekdays="2", time="18:00", lookahead=7)

        pool = PredictionPool.objects.get(name="NFL Pool")
        self.assertEqual(pool.configuration.poll_creation_weekdays, [2])
        self.assertEqual(pool.configuration.poll_creation_time, datetime.time(18, 0))
        self.assertEqual(pool.configuration.poll_creation_lookahead_days, 7)
        self.assertEqual(pool.stage_rules.count(), 3)

    def test_sets_points_per_stage_by_name(self):
        self.call(name="NFL Pool", season=self.season.id, points="Regular Season=1,Super Bowl=5")

        pool = PredictionPool.objects.get(name="NFL Pool")
        by_name = {r.stage.name: r.points_per_correct for r in pool.stage_rules.select_related("stage")}
        self.assertEqual(by_name["Regular Season"], 1)
        self.assertEqual(by_name["Super Bowl"], 5)
        # Unmentioned stages keep the seeded default.
        self.assertEqual(by_name["Wild Card"], 3)

    def test_rerunning_updates_rather_than_duplicating(self):
        self.call(name="NFL Pool", season=self.season.id, points="Super Bowl=5")
        self.call(name="NFL Pool", season=self.season.id, points="Super Bowl=8")

        self.assertEqual(PredictionPool.objects.filter(name="NFL Pool").count(), 1)
        pool = PredictionPool.objects.get(name="NFL Pool")
        self.assertEqual(pool.stage_rules.get(stage__name="Super Bowl").points_per_correct, 8)

    def test_resolves_the_season_by_sport_and_year(self):
        self.call(name="NFL Pool", sport=Sport.AMERICAN_FOOTBALL, year=2026)

        self.assertEqual(PredictionPool.objects.get(name="NFL Pool").season_id, self.season.id)

    def test_rejects_an_unknown_stage_name(self):
        with self.assertRaises(CommandError) as ctx:
            self.call(name="NFL Pool", season=self.season.id, points="Divisional=3")
        self.assertIn("Divisional", str(ctx.exception))

    def test_rejects_an_out_of_range_lookahead(self):
        with self.assertRaises(CommandError):
            self.call(name="NFL Pool", season=self.season.id, lookahead=MAX_POLL_LOOKAHEAD_DAYS + 1)

    def test_the_cap_is_discords_maximum_poll_duration(self):
        """32 is spelled out rather than taken from the constant on purpose.

        The cap is not a project preference: a poll runs from creation until
        its match kicks off, so a batch may reach exactly as far ahead as
        Discord will keep a poll open, documented as "up to 32 days". Asserting
        MAX_POLL_LOOKAHEAD_DAYS against itself would pass at any value.
        """
        self.assertEqual(MAX_POLL_LOOKAHEAD_DAYS, 32)

    def test_accepts_a_lookahead_at_the_cap(self):
        self.call(name="NFL Pool", season=self.season.id, lookahead=32)

        pool = PredictionPool.objects.get(name="NFL Pool")
        self.assertEqual(pool.configuration.poll_creation_lookahead_days, 32)

    def test_rejects_empty_weekdays(self):
        """No weekdays means polls never post, so it must not be accepted."""
        with self.assertRaises(CommandError):
            self.call(name="NFL Pool", season=self.season.id, weekdays=",")

    def test_requires_a_season(self):
        with self.assertRaises(CommandError):
            self.call(name="NFL Pool")

    def test_binding_before_the_bot_has_connected_is_a_clear_error(self):
        with self.assertRaises(CommandError) as ctx:
            self.call(name="NFL Pool", season=self.season.id, guild=123456)
        self.assertIn("start it once", str(ctx.exception))

    def test_binds_to_a_guild_channel_and_role(self):
        guild = DiscordGuild.objects.create(id=1, name="Otter Server")
        channel = DiscordChannel.objects.create(id=2, guild=guild, name="nfl-picks", channel_type="text")
        role = DiscordGuildRole.objects.create(id=3, guild=guild, name="Pickers")

        self.call(
            name="NFL Pool",
            season=self.season.id,
            guild=guild.id,
            channel=channel.id,
            notification_role=role.id,
        )

        binding = DiscordGuildPool.objects.get(pool__name="NFL Pool")
        self.assertEqual((binding.guild_id, binding.channel_id, binding.notification_role_id), (1, 2, 3))
        self.assertTrue(binding.is_active)


class PoolConfigurationValidationTests(TestCase):
    """Covers the field validator, which is the path the admin goes through.

    create_pool checks the bounds itself, so without this the admin could still
    save a lookahead Discord will not accept.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL")
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.pool = PredictionPool.objects.create(name="NFL 2026", season=self.season)

    def test_a_lookahead_at_the_cap_validates(self):
        config = self.pool.configuration
        config.poll_creation_lookahead_days = 32
        config.full_clean()

    def test_a_lookahead_past_the_cap_is_rejected(self):
        config = self.pool.configuration
        config.poll_creation_lookahead_days = 33
        with self.assertRaises(ValidationError):
            config.full_clean()

    def test_a_reminder_lead_past_a_week_is_rejected(self):
        config = self.pool.configuration
        config.reminder_lead_minutes = MAX_REMINDER_LEAD_MINUTES + 1
        with self.assertRaises(ValidationError):
            config.full_clean()


class CheckPoolCommandTests(TestCase):
    """Covers the check_pool management command, which exists because nearly
    every pool misconfiguration is otherwise silent."""

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(
            season=self.season, name="Regular Season", level=0, stage_type=StageType.LEAGUE
        )
        self.home = Team.objects.create(name="Browns")
        self.away = Team.objects.create(name="Vikings")
        Match.objects.create(
            stage=self.stage,
            home_team=self.home,
            away_team=self.away,
            kickoff=timezone.now() + datetime.timedelta(days=2),
        )
        self.pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)
        sync_pool_stage_rules(self.pool)

    def run_check(self, **kwargs):
        out = StringIO()
        try:
            call_command("check_pool", pool=self.pool.id, stdout=out, **kwargs)
            exit_code = 0
        except SystemExit as e:
            exit_code = e.code
        return out.getvalue(), exit_code

    def test_reports_a_missing_discord_binding_as_a_failure(self):
        report, exit_code = self.run_check()

        self.assertIn("no Discord binding", report)
        self.assertIn("FAIL", report)
        self.assertEqual(exit_code, 1)

    def test_passes_once_the_pool_is_fully_configured(self):
        guild = DiscordGuild.objects.create(id=1, name="Otter Server")
        channel = DiscordChannel.objects.create(id=2, guild=guild, name="picks", channel_type="text")
        DiscordGuildPool.objects.create(pool=self.pool, guild=guild, channel=channel)
        DiscordTeamEmoji.objects.create(id=10, team=self.home, name="browns")
        DiscordTeamEmoji.objects.create(id=11, team=self.away, name="vikings")
        self.pool.stage_rules.update(points_per_correct=1)
        # Configured now includes "something keeps the sport data fresh" - an
        # unscheduled ingestion is a FAIL, and having run recently is what
        # keeps it off the WARN list too.
        call_command("ensure_schedule", stdout=StringIO())
        PeriodicTask.objects.update(last_run_at=timezone.now())

        report, exit_code = self.run_check()

        self.assertNotIn("FAIL", report)
        self.assertNotIn("WARN", report)
        self.assertEqual(exit_code, 0)

    def test_flags_a_stage_type_with_no_poll_layout(self):
        """An unmapped stage type makes poll creation skip every match in it."""
        self.stage.stage_type = StageType.OTHER
        self.stage.save()

        report, _ = self.run_check()

        self.assertIn("no poll layout", report)
        self.assertIn("FAIL", report)

    def test_flags_missing_stage_rules(self):
        self.pool.stage_rules.all().delete()

        report, _ = self.run_check()

        self.assertIn("no scoring rule", report)
        self.assertIn("fallback of 3", report)

    def test_flags_a_binding_with_no_channel(self):
        guild = DiscordGuild.objects.create(id=1, name="Otter Server")
        DiscordGuildPool.objects.create(pool=self.pool, guild=guild, channel=None)

        report, _ = self.run_check()

        self.assertIn("no channel", report)

    def test_flags_an_empty_poll_schedule(self):
        self.pool.configuration.poll_creation_weekdays = []
        self.pool.configuration.save()

        report, _ = self.run_check()

        self.assertIn("no poll creation weekdays", report)

    def test_reports_the_points_distribution(self):
        rule = self.pool.stage_rules.get(stage=self.stage)
        rule.points_per_correct = 7
        rule.save()

        report, _ = self.run_check()

        self.assertIn("Regular Season=7", report)


class PredictionPoolAdminInlineTests(TestCase):
    """Regression tests for seeding vs. inline stage-rule rows.

    Django saves inline formsets in save_related, which runs *after*
    save_model. Seeding in save_model inserted a rule for the very stage the
    user had just added an inline row for, and their row then collided with
    the unique (pool, stage) constraint - a 500 in exactly the workflow the
    inline exists to support. These drive a real admin POST rather than
    calling the hook directly, because calling save_model alone never builds a
    formset and so cannot catch this."""

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stages = {
            name: Stage.objects.create(season=self.season, name=name, level=level)
            for name, level in (("Regular Season", 0), ("Wild Card", 1), ("Super Bowl", 2))
        }
        self.client.force_login(User.objects.create_superuser(username="admin", password="pw"))

    def post_add(self, **rule):
        """POST the pool add-form, optionally with one inline stage rule."""
        data = {
            "name": "NFL Pool",
            "season": self.season.id,
            "is_active": "on",
            # PoolConfiguration inline (one-to-one, primary key on pool)
            "configuration-TOTAL_FORMS": "1",
            "configuration-INITIAL_FORMS": "0",
            "configuration-MIN_NUM_FORMS": "0",
            "configuration-MAX_NUM_FORMS": "1",
            "configuration-0-poll_creation_weekdays": ["2"],
            "configuration-0-poll_creation_time": "18:00:00",
            "configuration-0-poll_creation_lookahead_days": "7",
            "configuration-0-reminder_lead_minutes": "60",
            # PoolStageRule inline
            "stage_rules-TOTAL_FORMS": "1" if rule else "0",
            "stage_rules-INITIAL_FORMS": "0",
            "stage_rules-MIN_NUM_FORMS": "0",
            "stage_rules-MAX_NUM_FORMS": "1000",
        }
        if rule:
            data["stage_rules-0-stage"] = rule["stage"].id
            data["stage_rules-0-level"] = rule["level"]
            data["stage_rules-0-points_per_correct"] = rule["points"]
        return self.client.post("/admin/predictions/predictionpool/add/", data)

    def test_adding_a_pool_with_an_inline_rule_does_not_collide(self):
        response = self.post_add(stage=self.stages["Wild Card"], level=1, points=10)

        self.assertEqual(response.status_code, 302, "admin add should redirect, not error")
        pool = PredictionPool.objects.get(name="NFL Pool")
        by_name = {r.stage.name: r.points_per_correct for r in pool.stage_rules.select_related("stage")}
        # The user's own row survives...
        self.assertEqual(by_name["Wild Card"], 10)
        # ...and the remaining stages are topped up.
        self.assertEqual(sorted(by_name), ["Regular Season", "Super Bowl", "Wild Card"])

    def test_adding_a_pool_without_inline_rows_still_seeds_every_stage(self):
        response = self.post_add()

        self.assertEqual(response.status_code, 302)
        pool = PredictionPool.objects.get(name="NFL Pool")
        self.assertEqual(pool.stage_rules.count(), 3)


class CreatePoolPartialRebindTests(TestCase):
    """Regression tests for partial re-runs of create_pool's Discord binding.

    The command advertises itself as idempotent and the runbook tells you to
    re-run it to rebind, so a re-run that omits a flag must leave that field
    alone. Passing them unconditionally nulled the channel out, which stops
    the pool posting anything at all."""

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        Stage.objects.create(season=self.season, name="Regular Season", level=0)

        self.guild = DiscordGuild.objects.create(id=1, name="Otter Server")
        self.channel = DiscordChannel.objects.create(id=2, guild=self.guild, name="picks", channel_type="text")
        self.role = DiscordGuildRole.objects.create(id=3, guild=self.guild, name="Pickers")
        self.new_role = DiscordGuildRole.objects.create(id=4, guild=self.guild, name="New Pickers")

        self.call(guild=1, channel=2, notification_role=3)

    def call(self, **kwargs):
        call_command("create_pool", name="NFL Pool", season=self.season.id, stdout=StringIO(), **kwargs)
        return DiscordGuildPool.objects.get(pool__name="NFL Pool")

    def test_rotating_only_the_role_keeps_the_channel(self):
        binding = self.call(guild=1, notification_role=4)

        self.assertEqual(binding.channel_id, 2, "channel must survive a role-only re-run")
        self.assertEqual(binding.notification_role_id, 4)

    def test_changing_only_the_channel_keeps_the_role(self):
        other = DiscordChannel.objects.create(id=5, guild=self.guild, name="picks-2", channel_type="text")

        binding = self.call(guild=1, channel=other.id)

        self.assertEqual(binding.channel_id, 5)
        self.assertEqual(binding.notification_role_id, 3, "role must survive a channel-only re-run")

    def test_a_bare_guild_rerun_changes_nothing_but_reactivates(self):
        DiscordGuildPool.objects.filter(pool__name="NFL Pool").update(is_active=False)

        binding = self.call(guild=1)

        self.assertEqual((binding.channel_id, binding.notification_role_id), (2, 3))
        self.assertTrue(binding.is_active)


class RankHistoryTests(TestCase):
    """Covers predictions/history.py, the walk behind the rank-over-time chart."""

    def setUp(self):
        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="2026 World Cup", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP, level=1)
        self.pool = PredictionPool.objects.create(name="Pool", season=self.season)
        self.home_team = Team.objects.create(name="Germany")
        self.away_team = Team.objects.create(name="Brazil")
        self.alice = User.objects.create_user(username="alice")
        self.bob = User.objects.create_user(username="bob")
        self.kickoff = timezone.now() - datetime.timedelta(days=10)
        self.match_count = 0

    def match(self):
        self.match_count += 1
        return Match.objects.create(
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=self.kickoff + datetime.timedelta(hours=self.match_count),
            status=MatchStatus.FINISHED,
            home_score=1,
            away_score=0,
        )

    def play(self, *rounds):
        """`rounds` is one dict of {user: points} per match, in kickoff order."""
        for awards in rounds:
            match = self.match()
            for user, points in awards.items():
                Prediction.objects.create(
                    pool=self.pool,
                    match=match,
                    user=user,
                    predicted_outcome=MatchOutcome.HOME_WIN,
                    points_awarded=points,
                    is_processed=True,
                )
        return list(
            Prediction.objects.filter(pool=self.pool, match__status=MatchStatus.FINISHED)
            .select_related("match", "user")
            .order_by("match__kickoff", "match_id")
        )

    def test_points_accumulate_across_matches(self):
        history = build_rank_history(self.play({self.alice: 3}, {self.alice: 4}, {self.alice: 0}))

        self.assertEqual([entry.standings[self.alice.id].points for entry in history], [3, 7, 7])
        self.assertEqual([entry.standings[self.alice.id].correct for entry in history], [1, 2, 2])
        self.assertEqual([entry.standings[self.alice.id].picks for entry in history], [1, 2, 3])

    def test_a_player_appears_only_once_they_have_picked(self):
        """Ranking someone on zero picks would show them entering last and
        climbing, which is not what happened - they were not playing yet."""
        history = build_rank_history(self.play({self.alice: 3}, {self.alice: 3, self.bob: 3}))

        self.assertNotIn(self.bob.id, history[0].standings)
        self.assertIn(self.bob.id, history[1].standings)

    def test_ranks_use_points_then_accuracy(self):
        """Same rule as the live leaderboard: level on points, accuracy decides."""
        history = build_rank_history(
            self.play(
                {self.alice: 6, self.bob: 0},
                {self.alice: 0, self.bob: 6},
                {self.bob: 0},
            )
        )
        final = history[-1].standings

        # Both on 6; alice from 2 picks, bob from 3.
        self.assertEqual((final[self.alice.id].points, final[self.bob.id].points), (6, 6))
        self.assertEqual((final[self.alice.id].rank, final[self.bob.id].rank), (1, 2))

    def test_ties_share_a_rank(self):
        history = build_rank_history(self.play({self.alice: 3, self.bob: 3}))
        standings = history[-1].standings

        self.assertEqual({standings[self.alice.id].rank, standings[self.bob.id].rank}, {1})

    def test_history_is_empty_without_scored_matches(self):
        self.assertEqual(build_rank_history([]), [])

    def test_consensus_counts_who_got_each_match_right(self):
        rates = build_consensus(self.play({self.alice: 3, self.bob: 0}, {self.alice: 0, self.bob: 0}))

        self.assertEqual([(rate.picks, rate.correct, rate.percent) for rate in rates], [(2, 1, 50), (2, 0, 0)])

    def test_consensus_has_no_majority_when_the_vote_is_tied(self):
        """A split pool has no consensus; inventing one puts words in its mouth."""
        match = self.match()
        self.vote(match, self.alice, MatchOutcome.HOME_WIN)
        self.vote(match, self.bob, MatchOutcome.AWAY_WIN)

        self.assertIsNone(build_consensus(self.ordered_predictions())[0].majority)

    def test_points_if_right_comes_from_whoever_got_it_right(self):
        """Rather than re-implementing the PoolStageRule lookup: if anyone
        called the match, their award is the stage's rate."""
        rates = build_consensus(self.play({self.alice: 7, self.bob: 0}))

        self.assertEqual(rates[0].points_if_right, 7)

    def vote(self, match, user, outcome, points=0):
        return Prediction.objects.create(
            pool=self.pool,
            match=match,
            user=user,
            predicted_outcome=outcome,
            points_awarded=points,
            is_processed=True,
        )

    def ordered_predictions(self):
        return list(
            Prediction.objects.filter(pool=self.pool)
            .select_related("match", "user")
            .order_by("match__kickoff", "match_id")
        )

    def test_crowd_is_scored_as_a_player_and_placed_among_them(self):
        """Every match here is a 1-0 home win, so an AWAY pick is a miss.

        Both players call match 1 and both miss match 2, which makes the
        crowd's majority right once in two.
        """
        first, second = self.match(), self.match()
        self.vote(first, self.alice, MatchOutcome.HOME_WIN, points=5)
        self.vote(first, self.bob, MatchOutcome.HOME_WIN, points=5)
        self.vote(second, self.alice, MatchOutcome.AWAY_WIN)
        self.vote(second, self.bob, MatchOutcome.AWAY_WIN)
        predictions = self.ordered_predictions()

        crowd = build_crowd_standing(build_consensus(predictions), final_order(build_rank_history(predictions)))

        self.assertEqual((crowd.points, crowd.correct, crowd.picks, crowd.hit_rate), (5, 1, 2, 50))
        # Level with both players, so nobody is ahead of it and nobody behind.
        self.assertEqual((crowd.rank, crowd.beaten, crowd.abstained), (1, 0, 0))

    def test_crowd_is_ranked_below_a_player_who_beat_it(self):
        first, second = self.match(), self.match()
        # alice calls both; bob only the first, so the majority follows bob
        # into the miss on the second.
        self.vote(first, self.alice, MatchOutcome.HOME_WIN, points=5)
        self.vote(first, self.bob, MatchOutcome.HOME_WIN, points=5)
        self.vote(second, self.alice, MatchOutcome.HOME_WIN, points=5)
        self.vote(second, self.bob, MatchOutcome.AWAY_WIN)
        self.vote(second, User.objects.create_user(username="dana"), MatchOutcome.AWAY_WIN)
        predictions = self.ordered_predictions()

        crowd = build_crowd_standing(build_consensus(predictions), final_order(build_rank_history(predictions)))

        self.assertEqual(crowd.points, 5)
        self.assertEqual(crowd.rank, 2, "alice's 10 points are ahead of the crowd's 5")
        # bob is level with it on both points and accuracy, so only dana is behind.
        self.assertEqual(crowd.beaten, 1)

    def test_crowd_abstains_on_a_split_match(self):
        match = self.match()
        self.vote(match, self.alice, MatchOutcome.HOME_WIN)
        self.vote(match, self.bob, MatchOutcome.AWAY_WIN)
        predictions = self.ordered_predictions()

        crowd = build_crowd_standing(build_consensus(predictions), final_order(build_rank_history(predictions)))

        self.assertEqual((crowd.picks, crowd.abstained), (0, 1))

    def test_streaks_count_consecutive_correct_picks(self):
        predictions = self.play(
            {self.alice: 3, self.bob: 0},
            {self.alice: 3, self.bob: 3},
            {self.alice: 0, self.bob: 3},
            {self.alice: 3, self.bob: 3},
        )
        names = {self.alice.id: "alice", self.bob.id: "bob"}

        streaks = build_streaks(predictions, names)

        # alice: 2, break, 1. bob: break, then 3.
        self.assertEqual([(s.name, s.length) for s in streaks], [("bob", 3), ("alice", 2)])

    def test_contrarians_count_picks_against_the_majority(self):
        match = self.match()
        carol = User.objects.create_user(username="carol")
        self.vote(match, self.alice, MatchOutcome.HOME_WIN, points=3)
        self.vote(match, self.bob, MatchOutcome.HOME_WIN, points=3)
        self.vote(match, carol, MatchOutcome.AWAY_WIN)
        predictions = self.ordered_predictions()
        names = {self.alice.id: "alice", self.bob.id: "bob", carol.id: "carol"}

        rebels = build_contrarians(predictions, build_consensus(predictions), names)

        self.assertEqual([(r.name, r.against, r.right) for r in rebels], [("carol", 1, 0)])

    def test_outcome_mix_compares_picks_against_results(self):
        """Two matches, both home wins; the pool picked home once and away once."""
        self.vote(self.match(), self.alice, MatchOutcome.HOME_WIN, points=3)
        self.vote(self.match(), self.alice, MatchOutcome.AWAY_WIN)
        predictions = self.ordered_predictions()

        mix = {entry.label: entry for entry in build_outcome_mix(predictions, build_consensus(predictions))}

        self.assertEqual((mix["Home win"].predicted_percent, mix["Home win"].actual_percent), (50, 100))
        self.assertEqual(mix["Home win"].bias_label, "50 pts under-picked")
        self.assertEqual((mix["Draw"].predicted_percent, mix["Draw"].actual_percent), (0, 0))

    async def test_the_last_entry_is_the_live_leaderboard(self):
        """The chart's right-hand edge and the leaderboard are the same table.

        They are computed by different code - one walks predictions in Python,
        the other aggregates in SQL - so this pins them together.
        """
        predictions = await sync_to_async(self.play)(
            {self.alice: 6, self.bob: 0},
            {self.alice: 0, self.bob: 6},
            {self.bob: 0},
            {self.alice: 3},
        )
        history = build_rank_history(predictions)

        live = {user.id: (rank, points) async for rank, user, points in self.pool.aget_leaderboard()}
        charted = {standing.user_id: (standing.rank, standing.points) for standing in history[-1].standings.values()}

        self.assertEqual(charted, live)


class PoolSetupPageTests(TestCase):
    """Covers the guided setup page in the admin: it has to do everything
    `manage.py create_pool` does, because a PredictionPool row added on its own
    has no schedule, no scoring and nowhere to post - and says so nowhere."""

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(
            season=self.season, name="Regular Season", level=0, stage_type=StageType.LEAGUE
        )
        self.other_stage = Stage.objects.create(
            season=self.season, name="Super Bowl", level=4, stage_type=StageType.KNOCK_OUT
        )
        Match.objects.create(
            stage=self.stage,
            home_team=Team.objects.create(name="Browns"),
            away_team=Team.objects.create(name="Vikings"),
            kickoff=timezone.now() + datetime.timedelta(days=2),
        )

        self.guild = DiscordGuild.objects.create(id=1, name="Otter Raft")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="bets", channel_type="text")
        self.role = DiscordGuildRole.objects.create(id=20, guild=self.guild, name="Otters")

        self.other_guild = DiscordGuild.objects.create(id=2, name="Elsewhere")
        self.other_channel = DiscordChannel.objects.create(
            id=11, guild=self.other_guild, name="general", channel_type="text"
        )

        self.admin = User.objects.create_superuser(username="boss", password="x", email="boss@example.com")
        self.client.force_login(self.admin)
        self.url = reverse("admin:predictions_predictionpool_setup")

    def payload(self, **overrides):
        data = {
            "season": self.season.id,
            "name": "NFL 2026",
            "poll_creation_weekdays": ["2", "6"],
            "poll_creation_time": "18:00",
            "poll_creation_lookahead_days": 7,
            "reminder_lead_minutes": 60,
            "guild": self.guild.id,
            "channel": self.channel.id,
            "notification_role": self.role.id,
        }
        data.update(overrides)
        return {key: value for key, value in data.items() if value is not None}

    # -- access ------------------------------------------------------------

    def test_anonymous_is_sent_to_the_login_page(self):
        self.client.logout()

        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_staff_without_add_permission_is_refused(self):
        staff = User.objects.create_user(username="reader", password="x", is_staff=True)
        self.client.force_login(staff)

        self.assertEqual(self.client.get(self.url).status_code, 403)

    # -- the form ----------------------------------------------------------

    def test_page_reports_the_prerequisites(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([check.status for check in response.context["environment"]], [OK, OK])

    def test_missing_prerequisites_are_reported_rather_than_hidden(self):
        """A fresh install has neither; the page has to say which is missing."""
        DiscordGuild.objects.all().delete()
        Stage.objects.all().delete()

        response = self.client.get(self.url)

        self.assertEqual([check.status for check in response.context["environment"]], [FAIL, FAIL])

    def test_creates_the_pool_its_configuration_rules_and_binding(self):
        response = self.client.post(self.url, self.payload())

        pool = PredictionPool.objects.get(name="NFL 2026")
        self.assertRedirects(response, f"{self.url}?created={pool.pk}")

        self.assertEqual(pool.season, self.season)
        self.assertTrue(pool.is_active)

        config = pool.configuration
        self.assertEqual(config.poll_creation_weekdays, [2, 6])
        self.assertEqual(config.poll_creation_time, datetime.time(18, 0))
        self.assertEqual(config.poll_creation_lookahead_days, 7)
        self.assertEqual(config.reminder_lead_minutes, 60)

        # One rule per stage, or every pick scores the hardcoded fallback.
        self.assertEqual(
            set(pool.stage_rules.values_list("stage_id", flat=True)),
            {self.stage.id, self.other_stage.id},
        )

        binding = DiscordGuildPool.objects.get(pool=pool)
        self.assertEqual(
            (binding.guild, binding.channel, binding.notification_role), (self.guild, self.channel, self.role)
        )
        self.assertTrue(binding.is_active)

    def test_weekdays_are_stored_as_integers(self):
        """The widget hands back strings and the JSON field validator rejects them."""
        self.client.post(self.url, self.payload(poll_creation_weekdays=["0"]))

        config = PredictionPool.objects.get(name="NFL 2026").configuration
        self.assertEqual(config.poll_creation_weekdays, [0])
        config.full_clean()

    def test_binding_is_optional(self):
        self.client.post(self.url, self.payload(guild=None, channel=None, notification_role=None))

        pool = PredictionPool.objects.get(name="NFL 2026")
        self.assertFalse(DiscordGuildPool.objects.filter(pool=pool).exists())

    def test_resubmitting_the_same_name_updates_rather_than_duplicating(self):
        """Same idempotence as the command, so the page is safe to re-run."""
        self.client.post(self.url, self.payload())
        self.client.post(self.url, self.payload(reminder_lead_minutes=15))

        pools = PredictionPool.objects.filter(name="NFL 2026")
        self.assertEqual(pools.count(), 1)
        self.assertEqual(pools.get().configuration.reminder_lead_minutes, 15)
        self.assertEqual(DiscordGuildPool.objects.filter(pool=pools.get()).count(), 1)

    # -- validation --------------------------------------------------------

    def test_a_guild_without_a_channel_is_refused(self):
        """It saves fine and then never posts, which is the most confusing way
        for a new pool to fail."""
        response = self.client.post(self.url, self.payload(channel=None))

        self.assertFormError(
            response.context["form"],
            "channel",
            ["A pool bound to a guild needs a channel, or it has nowhere to post."],
        )
        self.assertFalse(PredictionPool.objects.exists())

    def test_a_channel_from_another_guild_is_refused(self):
        response = self.client.post(self.url, self.payload(channel=self.other_channel.id))

        self.assertFormError(response.context["form"], "channel", ["That channel is in Elsewhere, not Otter Raft."])

    def test_a_channel_without_a_guild_is_refused(self):
        response = self.client.post(self.url, self.payload(guild=None, notification_role=None))

        self.assertFormError(response.context["form"], "guild", ["Pick the guild these belong to."])

    def test_no_weekdays_is_refused(self):
        """Without one, polls never post at all."""
        response = self.client.post(self.url, self.payload(poll_creation_weekdays=[]))

        self.assertTrue(response.context["form"].errors["poll_creation_weekdays"])
        self.assertFalse(PredictionPool.objects.exists())

    def test_lookahead_beyond_discords_maximum_is_refused(self):
        response = self.client.post(self.url, self.payload(poll_creation_lookahead_days=MAX_POLL_LOOKAHEAD_DAYS + 1))

        self.assertTrue(response.context["form"].errors["poll_creation_lookahead_days"])

    # -- the result --------------------------------------------------------

    def test_the_result_page_shows_the_readiness_report(self):
        self.client.post(self.url, self.payload())
        pool = PredictionPool.objects.get(name="NFL 2026")
        # Without a scheduled ingestion the pool is correctly not ready; the
        # deploy installs these, so a set-up pool has them.
        call_command("ensure_schedule", stdout=StringIO())

        response = self.client.get(self.url, {"created": pool.pk})

        self.assertEqual(response.context["created_pool"], pool)
        self.assertTrue(response.context["report"].is_ready)
        self.assertContains(response, "Set the points per round")

    # -- guild-scoped pickers ---------------------------------------------

    def test_channel_and_role_options_carry_their_guild(self):
        """pool_setup.js filters on these; without them it cannot narrow the
        list, and the page falls back to every channel on every server."""
        body = self.client.get(self.url).content.decode()

        self.assertIn(f'value="{self.channel.pk}" data-guild="{self.guild.pk}"', body)
        self.assertIn(f'value="{self.other_channel.pk}" data-guild="{self.other_guild.pk}"', body)
        self.assertIn(f'value="{self.role.pk}" data-guild="{self.guild.pk}"', body)

    def test_the_page_loads_the_picker_script(self):
        self.assertContains(self.client.get(self.url), "predictions/js/pool_setup.js")

    def test_options_are_labelled_with_their_guild(self):
        """The label is what makes the unfiltered list usable with scripting off."""
        body = self.client.get(self.url).content.decode()

        self.assertIn(f"{self.guild.name} · {self.channel.name}", body)

    def test_the_server_still_rejects_a_mismatched_pair(self):
        """The filtering is a convenience; this is what keeps the data right."""
        response = self.client.post(self.url, self.payload(channel=self.other_channel.id))

        self.assertTrue(response.context["form"].errors["channel"])
        self.assertFalse(DiscordGuildPool.objects.exists())

    def test_the_changelist_links_to_the_guided_page(self):
        response = self.client.get(reverse("admin:predictions_predictionpool_changelist"))

        self.assertContains(response, self.url)

    def test_the_changelist_reports_readiness_per_pool(self):
        """A pool with no Discord binding posts nothing and says so nowhere."""
        pool = PredictionPool.objects.create(name="Unbound", season=self.season)
        sync_pool_stage_rules(pool)

        response = self.client.get(reverse("admin:predictions_predictionpool_changelist"))
        body = response.content.decode()

        self.assertIn("no Discord binding", body)
        self.assertIn(f"{self.url}?created={pool.pk}", body)


class ReadinessTests(TestCase):
    """Covers predictions/readiness.py, shared by check_pool and the admin page."""

    def test_worst_wins(self):
        self.assertEqual(worst(OK, WARN, OK), WARN)
        self.assertEqual(worst(WARN, FAIL), FAIL)
        self.assertEqual(worst(), OK)

    def test_environment_reports_a_bare_install_as_not_ready(self):
        self.assertEqual([check.status for check in check_environment()], [FAIL, FAIL])


class IngestionFreshnessTests(TestCase):
    """Covers readiness._check_ingestion. The failure it reports is silent by
    construction: Beat is database-driven, so an unscheduled ingestion task is
    not an error anywhere - the matches just stop arriving."""

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)
        self.interval = IntervalSchedule.objects.create(every=120, period=IntervalSchedule.SECONDS)

    def schedule(self, task, *, enabled=True, last_run_at=None):
        return PeriodicTask.objects.create(
            name=task, task=task, interval=self.interval, enabled=enabled, last_run_at=last_run_at
        )

    def checks(self):
        return {check.label: check for check in check_pool(self.pool).checks}

    def find(self, fragment):
        return next(check for label, check in self.checks().items() if fragment in label)

    def test_an_unscheduled_critical_task_fails(self):
        check = self.find("nothing schedules the NFL infrastructure sync")

        self.assertEqual(check.status, FAIL)
        self.assertIn("ensure_schedule", check.detail)

    def test_an_unscheduled_backstop_only_warns(self):
        """Losing the nflverse cross-check costs a second opinion, not the pool."""
        check = self.find("nothing schedules the nflverse results backstop")

        self.assertEqual(check.status, WARN)

    def test_another_sports_tasks_are_not_reported(self):
        """An NFL pool does not care that the FIFA sync is unscheduled."""
        self.assertNotIn("nothing schedules the FIFA infrastructure sync", self.checks())

    def test_a_disabled_schedule_warns(self):
        self.schedule("sports.tasks.sync_nfl_infrastructure", enabled=False)

        self.assertEqual(self.find("NFL infrastructure sync is disabled").status, WARN)

    def test_a_schedule_that_has_never_run_warns(self):
        self.schedule("sports.tasks.sync_nfl_infrastructure")

        check = self.find("NFL infrastructure sync has never run")
        self.assertEqual(check.status, WARN)
        self.assertIn("beat container", check.detail)

    def test_a_recent_run_passes(self):
        self.schedule(
            "sports.tasks.sync_nfl_infrastructure",
            last_run_at=timezone.now() - datetime.timedelta(hours=2),
        )

        check = self.find("NFL infrastructure sync ran")
        self.assertEqual(check.status, OK)
        self.assertIn("2h ago", check.label)

    def test_a_stale_run_warns(self):
        """Three missed daily cycles is not a hiccup."""
        self.schedule(
            "sports.tasks.sync_nfl_infrastructure",
            last_run_at=timezone.now() - datetime.timedelta(days=5),
        )

        check = self.find("NFL infrastructure sync last ran")
        self.assertEqual(check.status, WARN)
        self.assertIn("5d ago", check.label)

    def test_a_frequent_task_gets_a_grace_floor(self):
        """A two-minute task is not stale after six minutes of a slow worker."""
        self.schedule(
            "sports.tasks.sync_nfl_live_games",
            last_run_at=timezone.now() - datetime.timedelta(minutes=10),
        )

        self.assertEqual(self.find("NFL live match sync ran").status, OK)

    def test_a_failing_task_is_reported_even_though_beat_keeps_dispatching(self):
        self.schedule(
            "sports.tasks.sync_nfl_infrastructure",
            last_run_at=timezone.now() - datetime.timedelta(hours=1),
        )
        TaskResult.objects.create(
            task_id="abc",
            task_name="sports.tasks.sync_nfl_infrastructure",
            status="FAILURE",
            result="ConnectionError: ESPN is unreachable",
        )

        check = self.find("the last recorded NFL infrastructure sync failed")
        self.assertEqual(check.status, WARN)
        self.assertIn("ESPN is unreachable", check.detail)


class PoolCloseoutTests(TestCase):
    """Covers predictions/closeout.py and the admin page over it.

    A season ends quietly - the last match finishes and every loop keeps going -
    so closing a pool is a deliberate step. It has to score first and switch off
    second, or the final leaderboard is missing whatever the scoring signal
    dropped while a worker was down.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(season=self.season, name="Regular Season", level=0)
        self.pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)
        PoolStageRule.objects.update_or_create(pool=self.pool, stage=self.stage, defaults={"points_per_correct": 4})

        self.guild = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="general", channel_type="text")
        self.binding = DiscordGuildPool.objects.create(
            guild=self.guild, channel=self.channel, pool=self.pool, is_active=True
        )
        self.user = User.objects.create_user(username="player")

    def make_match(self, *, offset=0, status=MatchStatus.FINISHED, home_score=2, away_score=1):
        return Match.objects.create(
            stage=self.stage,
            home_team=Team.objects.create(name=f"Home {offset}"),
            away_team=Team.objects.create(name=f"Away {offset}"),
            kickoff=timezone.now() - datetime.timedelta(days=offset + 1),
            status=status,
            home_score=home_score,
            away_score=away_score,
        )

    def make_message(self, match, **kwargs):
        return ActiveMatchMessage.objects.create(
            match=match,
            guild=self.guild,
            pool=self.pool,
            channel=self.channel,
            poll_message_id=match.id * 1000,
            **kwargs,
        )

    def test_it_scores_what_the_signal_left_behind(self):
        """A match that finished while the worker was down leaves predictions
        unprocessed, and the leaderboard the season is judged on reads points."""
        match = self.make_match()
        prediction = Prediction.objects.create(
            user=self.user, match=match, pool=self.pool, predicted_outcome=MatchOutcome.HOME_WIN
        )
        Prediction.objects.filter(pk=prediction.pk).update(is_processed=False, points_awarded=0)

        result = close_out_pool(self.pool)

        prediction.refresh_from_db()
        self.assertEqual(result.scored_predictions, 1)
        self.assertEqual(prediction.points_awarded, 4)
        self.assertTrue(prediction.is_processed)

    def test_it_retires_the_pools_messages_and_bindings_and_the_pool(self):
        open_row = self.make_message(self.make_match(offset=0))
        done_row = self.make_message(self.make_match(offset=1), is_poll_finalized=True, is_ticker_finalized=True)

        result = close_out_pool(self.pool)

        open_row.refresh_from_db()
        done_row.refresh_from_db()
        self.binding.refresh_from_db()
        self.pool.refresh_from_db()
        self.assertTrue(open_row.is_poll_finalized)
        self.assertTrue(open_row.is_ticker_finalized)
        self.assertEqual(result.retired_messages, 1)  # the finished row is left alone
        self.assertFalse(self.binding.is_active)
        self.assertFalse(self.pool.is_active)

    def test_another_pools_rows_are_untouched(self):
        other_pool = PredictionPool.objects.create(name="Other Pool", season=self.season)
        other_binding = DiscordGuildPool.objects.create(
            guild=self.guild,
            channel=DiscordChannel.objects.create(id=11, guild=self.guild, name="other", channel_type="text"),
            pool=other_pool,
            is_active=True,
        )
        match = self.make_match()
        other_row = ActiveMatchMessage.objects.create(
            match=match, guild=self.guild, pool=other_pool, channel=self.channel, poll_message_id=77
        )

        close_out_pool(self.pool)

        other_row.refresh_from_db()
        other_binding.refresh_from_db()
        other_pool.refresh_from_db()
        self.assertFalse(other_row.is_poll_finalized)
        self.assertTrue(other_binding.is_active)
        self.assertTrue(other_pool.is_active)

    def test_closing_twice_changes_nothing_the_second_time(self):
        self.make_message(self.make_match())
        close_out_pool(self.pool)

        result = close_out_pool(self.pool)

        self.assertEqual(result.retired_messages, 0)
        self.assertEqual(result.deactivated_bindings, 0)
        self.assertFalse(result.pool_deactivated)

    def test_the_plan_counts_what_closing_would_change(self):
        self.make_message(self.make_match(offset=0))
        self.make_match(offset=1, status=MatchStatus.SCHEDULED)

        plan = plan_closeout(self.pool)

        self.assertEqual(plan.open_messages, 1)
        self.assertEqual(plan.active_bindings, 1)
        self.assertEqual(plan.unplayed_matches, 1)
        self.assertTrue(plan.pool_is_active)
        self.assertTrue(plan.is_worth_doing)


class PoolCloseoutAdminViewTests(TestCase):
    """The page itself: it must show before it does, and only act on POST."""

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        Stage.objects.create(season=self.season, name="Regular Season", level=0)
        self.pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)

        self.password = "closeout-pass"
        self.admin_user = User.objects.create_superuser(username="closer", password=self.password)
        self.client.force_login(self.admin_user)
        self.url = reverse("admin:predictions_predictionpool_closeout", args=[self.pool.pk])

    def test_a_get_only_reports(self):
        response = self.client.get(self.url)

        self.pool.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Close out")
        self.assertTrue(self.pool.is_active)

    def test_a_post_closes_the_pool_and_redirects(self):
        response = self.client.post(self.url)

        self.pool.refresh_from_db()
        self.assertRedirects(response, reverse("admin:predictions_predictionpool_changelist"))
        self.assertFalse(self.pool.is_active)

    def test_a_reader_cannot_close_a_pool(self):
        reader = User.objects.create_user(username="reader", password=self.password, is_staff=True)
        self.client.force_login(reader)

        response = self.client.post(self.url)

        self.pool.refresh_from_db()
        self.assertIn(response.status_code, (302, 403))
        self.assertTrue(self.pool.is_active)

    def test_the_button_is_on_the_pool_page(self):
        response = self.client.get(reverse("admin:predictions_predictionpool_change", args=[self.pool.pk]))

        self.assertContains(response, self.url)


class MessagePreviewAdminViewTests(TestCase):
    """Queueing a set of test messages for a pool, and taking them back down.

    The page cannot post anything itself - the web container has no Discord
    connection - so everything it does is write a row the bot reads.
    """

    def setUp(self):
        self.competition = Competition.objects.create(name="NFL", sport=Sport.AMERICAN_FOOTBALL)
        self.season = Season.objects.create(name="NFL 2026", competition=self.competition, year=2026)
        self.stage = Stage.objects.create(
            season=self.season, name="Regular Season", stage_type=StageType.LEAGUE, level=0
        )
        self.pool = PredictionPool.objects.create(name="NFL Pool", season=self.season)
        self.guild = DiscordGuild.objects.create(id=1, name="Test Guild")
        self.channel = DiscordChannel.objects.create(id=10, guild=self.guild, name="pool", channel_type="text")
        self.guild_pool = DiscordGuildPool.objects.create(
            guild=self.guild, pool=self.pool, channel=self.channel, is_active=True
        )
        self.played = Match.objects.create(
            stage=self.stage,
            home_team=Team.objects.create(name="Chiefs"),
            away_team=Team.objects.create(name="Eagles"),
            kickoff=timezone.now() - datetime.timedelta(days=2),
            status=MatchStatus.FINISHED,
            home_score=21,
            away_score=17,
        )
        self.upcoming = Match.objects.create(
            stage=self.stage,
            home_team=Team.objects.create(name="Bills"),
            away_team=Team.objects.create(name="Jets"),
            kickoff=timezone.now() + datetime.timedelta(days=3),
        )

        self.admin_user = User.objects.create_superuser(username="previewer", password="preview-pass")
        self.client.force_login(self.admin_user)
        self.url = reverse("admin:predictions_predictionpool_preview", args=[self.pool.pk])

    def test_the_page_offers_this_pool_only(self):
        other_pool = PredictionPool.objects.create(name="Other", season=self.season)
        other_binding = DiscordGuildPool.objects.create(
            guild=self.guild, pool=other_pool, channel=self.channel, is_active=True
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        bindings = response.context["form"].fields["guild_pool"].queryset
        self.assertEqual([binding.pk for binding in bindings], [self.guild_pool.pk])
        self.assertNotIn(other_binding, bindings)

    def test_it_opens_on_the_next_match_to_be_played(self):
        """Same rule the public front page uses: whatever is about to happen is
        what someone is most likely to be checking."""
        response = self.client.get(self.url)

        self.assertEqual(response.context["form"].fields["match"].initial, self.upcoming)

    def test_a_post_queues_the_messages_for_the_bot(self):
        response = self.client.post(
            self.url,
            {
                "guild_pool": self.guild_pool.pk,
                "match": self.played.pk,
                "kinds": [PreviewMessageKind.RESULT_POSTED, PreviewMessageKind.POLL],
            },
        )

        self.assertRedirects(response, self.url)
        preview = MessagePreviewRequest.objects.get()
        self.assertEqual(preview.match, self.played)
        self.assertEqual(preview.guild_pool, self.guild_pool)
        self.assertEqual(preview.requested_by, self.admin_user)
        self.assertEqual(preview.status, PreviewStatus.PENDING)
        # Stored in the order the channel would see them, not the order the
        # checkboxes came back in.
        self.assertEqual(preview.kinds, [PreviewMessageKind.POLL, PreviewMessageKind.RESULT_POSTED])

    def test_queueing_creates_no_active_match_message(self):
        """That row is what makes the poll loop skip a match and the ticker
        adopt a message - a preview must leave both alone."""
        self.client.post(
            self.url,
            {"guild_pool": self.guild_pool.pk, "match": self.upcoming.pk, "kinds": [PreviewMessageKind.POLL]},
        )

        self.assertFalse(ActiveMatchMessage.objects.exists())

    def test_removal_is_queued_rather_than_done_here(self):
        preview = MessagePreviewRequest.objects.create(
            guild_pool=self.guild_pool,
            match=self.played,
            kinds=[PreviewMessageKind.POLL],
            status=PreviewStatus.POSTED,
            posted_message_ids=[7001, 7002],
        )

        response = self.client.post(self.url, {"cleanup": preview.pk})

        preview.refresh_from_db()
        self.assertRedirects(response, self.url)
        self.assertTrue(preview.cleanup_requested)
        # Still recorded: only the bot can delete a Discord message, and it
        # needs the ids to do it.
        self.assertEqual(preview.posted_message_ids, [7001, 7002])

    def test_another_pools_preview_cannot_be_removed_from_here(self):
        other_pool = PredictionPool.objects.create(name="Other", season=self.season)
        other_binding = DiscordGuildPool.objects.create(
            guild=self.guild, pool=other_pool, channel=self.channel, is_active=True
        )
        preview = MessagePreviewRequest.objects.create(
            guild_pool=other_binding, match=self.played, kinds=[PreviewMessageKind.POLL]
        )

        self.client.post(self.url, {"cleanup": preview.pk})

        preview.refresh_from_db()
        self.assertFalse(preview.cleanup_requested)

    def test_a_reader_cannot_queue_anything(self):
        reader = User.objects.create_user(username="reader", password="preview-pass", is_staff=True)
        self.client.force_login(reader)

        response = self.client.post(
            self.url,
            {"guild_pool": self.guild_pool.pk, "match": self.upcoming.pk, "kinds": [PreviewMessageKind.POLL]},
        )

        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(MessagePreviewRequest.objects.exists())

    def test_the_button_is_on_the_pool_page(self):
        response = self.client.get(reverse("admin:predictions_predictionpool_change", args=[self.pool.pk]))

        self.assertContains(response, self.url)
