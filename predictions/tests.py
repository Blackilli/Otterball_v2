from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from predictions.models import PoolStageRule, Prediction, PredictionPool
from predictions.signals import process_match_update
from sports.models import Competition, Match, MatchOutcome, MatchStatus, Season, Stage, StageType, Team

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


class LeaderboardTests(TestCase):
    """Covers PredictionPool.aget_leaderboard's Standard Competition Ranking
    (1-2-2-4: ties share a rank, the next rank skips accordingly)."""

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

    async def test_users_with_no_predictions_in_the_pool_rank_last_with_zero_points(self):
        await self.award(self.alice, 10)

        leaderboard = [entry async for entry in self.pool.aget_leaderboard()]
        points_by_user = {user.username: points for _rank, user, points in leaderboard}

        self.assertEqual(points_by_user["alice"], 10)
        self.assertEqual(points_by_user["bob"], 0)
        self.assertEqual(points_by_user["carol"], 0)
        self.assertEqual(points_by_user["dave"], 0)
