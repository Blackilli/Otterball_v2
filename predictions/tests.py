import csv
import datetime
import tempfile
from io import StringIO
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase
from django.utils import timezone

from discord_bot.models import (
    DiscordChannel,
    DiscordGuild,
    DiscordGuildPool,
    DiscordGuildRole,
    DiscordTeamEmoji,
)
from predictions.admin import PredictionPoolAdmin
from predictions.models import PoolStageRule, Prediction, PredictionPool, sync_pool_stage_rules
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
            self.call(name="NFL Pool", season=self.season.id, lookahead=30)

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

        report, exit_code = self.run_check()

        self.assertNotIn("FAIL", report)
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


class PredictionPoolAdminSeedingTests(TestCase):
    """Covers PredictionPoolAdmin.save_model, which is where a pool created
    through /admin/ gets its stage rules.

    This is the path the runbook tells you to use, so it is the one that has
    to seed - creating a pool through the admin and forgetting to add rules by
    hand is exactly how a pool ends up scoring every round the same."""

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

    def test_saving_a_pool_in_the_admin_seeds_its_stage_rules(self):
        pool = PredictionPool(name="NFL Pool", season=self.season)

        self.admin.save_model(self.request, pool, form=None, change=False)

        self.assertEqual(
            sorted(pool.stage_rules.values_list("stage__name", flat=True)),
            ["Regular Season", "Super Bowl"],
        )

    def test_saving_again_tops_up_without_disturbing_edited_points(self):
        pool = PredictionPool(name="NFL Pool", season=self.season)
        self.admin.save_model(self.request, pool, form=None, change=False)
        rule = pool.stage_rules.get(stage__name="Super Bowl")
        rule.points_per_correct = 5
        rule.save()
        Stage.objects.create(season=self.season, name="Wild Card", level=2)

        self.admin.save_model(self.request, pool, form=None, change=True)

        self.assertEqual(pool.stage_rules.count(), 3)
        rule.refresh_from_db()
        self.assertEqual(rule.points_per_correct, 5)
