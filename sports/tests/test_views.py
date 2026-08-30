import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from discord_bot.constants import DISCORD_DRAWABLE_POLL_ANSWER_ORDER, DISCORD_POLL_ANSWER_ORDER_MAP
from discord_bot.models import DiscordProfile
from predictions.models import PoolStageRule, Prediction, PredictionPool
from sports.charts import CHART_HIGHLIGHTED, CHART_SERIES, build_rank_chart
from sports.constants import DRAWABLE_STAGE_TYPES
from sports.models import (
    Competition,
    Match,
    MatchOutcome,
    MatchStatus,
    Season,
    Stage,
    StageType,
    Team,
)
from sports.views import (
    LIVE_GRACE,
    MAX_RECENT_RESULTS,
    MAX_UPCOMING_MATCHES,
    BracketView,
    LeaderboardView,
    UpcomingMatchesView,
    group_by_day,
)

User = get_user_model()


class ViewTestCase(TestCase):
    """A season with a group stage and a knockout stage, and teams to play them."""

    def setUp(self):
        self.now = timezone.now()
        self.competition = Competition.objects.create(name="World Cup")
        self.season = Season.objects.create(name="World Cup 2026", competition=self.competition, year=2026)
        self.group = Stage.objects.create(season=self.season, name="Group A", stage_type=StageType.GROUP, level=1)
        self.final = Stage.objects.create(season=self.season, name="Final", stage_type=StageType.KNOCK_OUT, level=9)
        self.home = Team.objects.create(name="Germany")
        self.away = Team.objects.create(name="Brazil")

    def make_match(self, stage=None, offset_hours=1, **kwargs):
        return Match.objects.create(
            stage=stage or self.group,
            home_team=kwargs.pop("home_team", self.home),
            away_team=kwargs.pop("away_team", self.away),
            kickoff=self.now + datetime.timedelta(hours=offset_hours),
            **kwargs,
        )


class UpcomingMatchesViewTests(ViewTestCase):
    def test_lists_scheduled_matches_for_the_season(self):
        match = self.make_match(offset_hours=5)

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([m for day in response.context["match_days"] for m in day.matches], [match])

    def test_finished_and_cancelled_matches_are_not_upcoming(self):
        self.make_match(offset_hours=2, status=MatchStatus.FINISHED, home_score=1, away_score=0)
        self.make_match(offset_hours=3, status=MatchStatus.CANCELLED)
        scheduled = self.make_match(offset_hours=4)

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))

        self.assertEqual([m for day in response.context["match_days"] for m in day.matches], [scheduled])

    def test_match_that_has_just_kicked_off_is_still_listed(self):
        """Status lags kickoff by up to a sync interval, so time alone must not drop it."""
        live = self.make_match(offset_hours=-1, status=MatchStatus.LIVE)
        stale = self.make_match(offset_hours=-1 - LIVE_GRACE.total_seconds() / 3600)

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))

        listed = [m for day in response.context["match_days"] for m in day.matches]
        self.assertIn(live, listed)
        self.assertNotIn(stale, listed)
        self.assertEqual(response.context["live_count"], 1)

    def test_matches_from_another_season_are_excluded(self):
        other_season = Season.objects.create(name="Euro 2028", competition=self.competition, year=2028)
        other_stage = Stage.objects.create(season=other_season, name="Group A", level=1)
        mine = self.make_match(offset_hours=2)
        self.make_match(stage=other_stage, offset_hours=3)

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))

        self.assertEqual([m for day in response.context["match_days"] for m in day.matches], [mine])

    def test_upcoming_list_is_capped(self):
        for hours in range(MAX_UPCOMING_MATCHES + 5):
            self.make_match(offset_hours=hours + 1)

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))

        listed = [m for day in response.context["match_days"] for m in day.matches]
        self.assertEqual(len(listed), MAX_UPCOMING_MATCHES)

    def test_recent_results_are_the_newest_finished_matches(self):
        for hours in range(MAX_RECENT_RESULTS + 3):
            self.make_match(
                offset_hours=-(hours + 24),
                status=MatchStatus.FINISHED,
                home_score=1,
                away_score=0,
            )

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))

        results = response.context["recent_results"]
        self.assertEqual(len(results), MAX_RECENT_RESULTS)
        self.assertEqual(results, sorted(results, key=lambda m: m.kickoff, reverse=True))

    def test_unknown_season_is_a_404(self):
        response = self.client.get(reverse("sports:season-matches", args=[self.season.id + 999]))

        self.assertEqual(response.status_code, 404)

    def test_empty_database_renders_rather_than_erroring(self):
        Match.objects.all().delete()
        Season.objects.all().delete()

        response = self.client.get(reverse("sports:upcoming-matches"))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["season"])


class DefaultSeasonTests(ViewTestCase):
    def test_index_shows_the_season_with_the_next_match(self):
        other_season = Season.objects.create(name="Euro 2028", competition=self.competition, year=2028)
        other_stage = Stage.objects.create(season=other_season, name="Group A", level=1)
        self.make_match(offset_hours=48)
        self.make_match(stage=other_stage, offset_hours=2)

        response = self.client.get(reverse("sports:upcoming-matches"))

        self.assertEqual(response.context["season"], other_season)

    def test_index_falls_back_to_the_most_recent_season_when_nothing_is_upcoming(self):
        """The off-season still has to land somewhere - a blank page is worse."""
        other_season = Season.objects.create(name="Euro 2024", competition=self.competition, year=2024)
        other_stage = Stage.objects.create(season=other_season, name="Group A", level=1)
        self.make_match(offset_hours=-500, status=MatchStatus.FINISHED, home_score=1, away_score=0)
        self.make_match(stage=other_stage, offset_hours=-100, status=MatchStatus.FINISHED, home_score=1, away_score=0)

        response = self.client.get(reverse("sports:upcoming-matches"))

        self.assertEqual(response.context["season"], other_season)

    def test_switcher_only_offers_seasons_that_have_matches(self):
        Season.objects.create(name="Empty 2030", competition=self.competition, year=2030)
        self.make_match(offset_hours=2)

        response = self.client.get(reverse("sports:upcoming-matches"))

        self.assertEqual([s.id for s in response.context["seasons"]], [self.season.id])


class VoteSplitTests(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.pool = PredictionPool.objects.create(name="Pool", season=self.season)

    def predict(self, match, outcome, username):
        return Prediction.objects.create(
            pool=self.pool,
            match=match,
            user=User.objects.create(username=username),
            predicted_outcome=outcome,
        )

    def test_split_counts_and_percentages(self):
        match = self.make_match(offset_hours=2)
        self.predict(match, MatchOutcome.HOME_WIN, "a")
        self.predict(match, MatchOutcome.HOME_WIN, "b")
        self.predict(match, MatchOutcome.AWAY_WIN, "c")
        self.predict(match, MatchOutcome.DRAW, "d")

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))
        listed = response.context["match_days"][0].matches[0]

        self.assertEqual(listed.vote_total, 4)
        self.assertEqual(
            [(share.outcome, share.count, share.percent) for share in listed.vote_split],
            [
                (MatchOutcome.HOME_WIN, 2, 50),
                (MatchOutcome.DRAW, 1, 25),
                (MatchOutcome.AWAY_WIN, 1, 25),
            ],
        )

    def test_knockout_match_has_no_draw_segment(self):
        match = self.make_match(stage=self.final, offset_hours=2)
        self.predict(match, MatchOutcome.HOME_WIN, "a")

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))
        listed = response.context["match_days"][0].matches[0]

        self.assertEqual(
            [share.outcome for share in listed.vote_split],
            [MatchOutcome.HOME_WIN, MatchOutcome.AWAY_WIN],
        )

    def test_predictions_from_another_pools_season_are_not_counted(self):
        other_season = Season.objects.create(name="Euro 2028", competition=self.competition, year=2028)
        other_pool = PredictionPool.objects.create(name="Other", season=other_season)
        match = self.make_match(offset_hours=2)
        Prediction.objects.create(
            pool=other_pool,
            match=match,
            user=User.objects.create(username="outsider"),
            predicted_outcome=MatchOutcome.HOME_WIN,
        )

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))
        listed = response.context["match_days"][0].matches[0]

        self.assertEqual(listed.vote_total, 0)

    def test_finished_matches_carry_a_split_too(self):
        match = self.make_match(
            offset_hours=-26,
            status=MatchStatus.FINISHED,
            home_score=2,
            away_score=1,
        )
        self.predict(match, MatchOutcome.HOME_WIN, "a")
        self.predict(match, MatchOutcome.AWAY_WIN, "b")

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))
        result = response.context["recent_results"][0]

        self.assertEqual(result.vote_total, 2)
        self.assertEqual([share.count for share in result.vote_split], [1, 0, 1])

    def test_the_outcome_that_happened_is_marked_correct(self):
        match = self.make_match(
            offset_hours=-26,
            status=MatchStatus.FINISHED,
            home_score=1,
            away_score=3,
        )
        self.predict(match, MatchOutcome.AWAY_WIN, "a")

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))
        result = response.context["recent_results"][0]

        self.assertEqual(
            [share.outcome for share in result.vote_split if share.is_correct],
            [MatchOutcome.AWAY_WIN],
        )
        self.assertContains(response, "vote-bar has-result")

    def test_nobody_is_marked_correct_before_the_match_is_played(self):
        match = self.make_match(offset_hours=2)
        self.predict(match, MatchOutcome.HOME_WIN, "a")

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))
        listed = response.context["match_days"][0].matches[0]

        self.assertEqual([share.is_correct for share in listed.vote_split], [False, False, False])
        self.assertNotContains(response, "has-result")

    def test_a_correct_outcome_nobody_picked_is_still_marked(self):
        """The answer key is the point - a segment of zero still says what happened."""
        match = self.make_match(
            offset_hours=-26,
            status=MatchStatus.FINISHED,
            home_score=1,
            away_score=1,
        )
        self.predict(match, MatchOutcome.HOME_WIN, "a")

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))
        result = response.context["recent_results"][0]
        correct = [share for share in result.vote_split if share.is_correct]

        self.assertEqual([(share.outcome, share.count) for share in correct], [(MatchOutcome.DRAW, 0)])

    def test_knockout_tie_marks_nothing(self):
        """A level knockout was decided on penalties, which the schema does not store."""
        match = self.make_match(
            stage=self.final,
            offset_hours=-26,
            status=MatchStatus.FINISHED,
            home_score=1,
            away_score=1,
        )
        self.predict(match, MatchOutcome.HOME_WIN, "a")

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))
        result = response.context["recent_results"][0]

        self.assertEqual([share.is_correct for share in result.vote_split], [False, False])

    def test_match_without_predictions_renders_no_bar(self):
        self.make_match(offset_hours=2)

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))

        self.assertEqual(response.context["match_days"][0].matches[0].vote_total, 0)
        self.assertNotContains(response, "vote-bar")


class BracketViewTests(ViewTestCase):
    def test_rounds_are_ordered_by_stage_level(self):
        semi = Stage.objects.create(season=self.season, name="Semi-final", stage_type=StageType.KNOCK_OUT, level=8)
        final_match = self.make_match(stage=self.final, offset_hours=48)
        semi_match = self.make_match(stage=semi, offset_hours=24)
        self.make_match(stage=self.group, offset_hours=2)

        response = self.client.get(reverse("sports:season-bracket", args=[self.season.id]))

        self.assertEqual(
            [(r["stage"], r["matches"]) for r in response.context["rounds"]],
            [(semi, [semi_match]), (self.final, [final_match])],
        )

    def test_group_stages_are_not_part_of_the_tree(self):
        self.make_match(stage=self.group, offset_hours=2)

        response = self.client.get(reverse("sports:season-bracket", args=[self.season.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rounds"], [])

    def test_matches_within_a_round_are_ordered_by_kickoff(self):
        later = self.make_match(stage=self.final, offset_hours=48)
        earlier = self.make_match(stage=self.final, offset_hours=24)

        response = self.client.get(reverse("sports:season-bracket", args=[self.season.id]))

        self.assertEqual(response.context["rounds"][0]["matches"], [earlier, later])

    def test_winner_is_marked_and_loser_dimmed(self):
        self.make_match(
            stage=self.final,
            offset_hours=-2,
            status=MatchStatus.FINISHED,
            home_score=2,
            away_score=1,
        )

        response = self.client.get(reverse("sports:season-bracket", args=[self.season.id]))

        self.assertContains(response, "team-row winner")
        self.assertContains(response, "team-row loser")

    def test_bracket_matches_carry_a_prediction_split(self):
        pool = PredictionPool.objects.create(name="Pool", season=self.season)
        match = self.make_match(
            stage=self.final,
            offset_hours=-2,
            status=MatchStatus.FINISHED,
            home_score=2,
            away_score=1,
        )
        Prediction.objects.create(
            pool=pool,
            match=match,
            user=User.objects.create(username="a"),
            predicted_outcome=MatchOutcome.HOME_WIN,
        )

        response = self.client.get(reverse("sports:season-bracket", args=[self.season.id]))
        listed = response.context["rounds"][0]["matches"][0]

        self.assertEqual(listed.vote_total, 1)
        self.assertEqual([share.is_correct for share in listed.vote_split], [True, False])
        self.assertContains(response, "vote-bar has-result")

    def test_every_round_is_listed_in_the_rail(self):
        """The rail is what says the tree continues past the right edge."""
        semi = Stage.objects.create(season=self.season, name="Semi-final", stage_type=StageType.KNOCK_OUT, level=8)
        self.make_match(stage=semi, offset_hours=24)
        self.make_match(stage=semi, offset_hours=26)
        self.make_match(stage=self.final, offset_hours=48)

        response = self.client.get(reverse("sports:season-bracket", args=[self.season.id]))

        # One rail link per round, each pointing at that round's column.
        self.assertContains(response, f'href="#round-{semi.id}"')
        self.assertContains(response, f'href="#round-{self.final.id}"')
        self.assertContains(response, f'id="round-{semi.id}"')
        self.assertContains(response, f'id="round-{self.final.id}"')

    def test_bracket_without_any_season_is_a_404(self):
        Match.objects.all().delete()
        Season.objects.all().delete()

        response = self.client.get("/seasons/1/bracket/")

        self.assertEqual(response.status_code, 404)


class GroupByDayTests(ViewTestCase):
    def test_late_kickoff_is_filed_under_its_local_date(self):
        """A 01:30 local kickoff is the previous day in UTC; the heading must not be."""
        local = timezone.get_current_timezone()
        late = timezone.make_aware(datetime.datetime(2026, 6, 30, 1, 30), local)
        match = self.make_match(offset_hours=1)
        match.kickoff = late
        match.save()

        days = group_by_day([match])

        self.assertEqual([day.date for day in days], [datetime.date(2026, 6, 30)])

    def test_consecutive_matches_on_one_day_share_a_group(self):
        first = self.make_match(offset_hours=1)
        second = self.make_match(offset_hours=2)
        next_day = self.make_match(offset_hours=48)

        days = group_by_day([first, second, next_day])

        self.assertEqual([len(day.matches) for day in days], [2, 1])


class DrawableStageTypeTests(TestCase):
    def test_poll_answer_order_agrees_with_drawable_stage_types(self):
        """The poll and the vote split have to offer Draw for the same stages.

        They read different constants - the bot builds poll answers from
        DISCORD_POLL_ANSWER_ORDER_MAP, the views size the split from
        DRAWABLE_STAGE_TYPES - so this pins the two together.
        """
        for stage_type, order in DISCORD_POLL_ANSWER_ORDER_MAP.items():
            with self.subTest(stage_type=stage_type):
                self.assertEqual(
                    order == DISCORD_DRAWABLE_POLL_ANSWER_ORDER,
                    stage_type in DRAWABLE_STAGE_TYPES,
                )


class LeaderboardViewTests(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.pool = PredictionPool.objects.create(name="Otterball 2026", season=self.season)
        self.match = self.make_match(
            offset_hours=-4,
            status=MatchStatus.FINISHED,
            home_score=2,
            away_score=1,
        )

    def player(self, username, *, outcome=MatchOutcome.HOME_WIN, points=0, match=None, global_name=None):
        user = User.objects.create(username=username)
        if global_name is not None:
            DiscordProfile.objects.create(
                user=user,
                id=abs(hash(username)) % 10**17,
                username=username,
                global_name=global_name,
            )
        Prediction.objects.create(
            pool=self.pool,
            match=match or self.match,
            user=user,
            predicted_outcome=outcome,
            points_awarded=points,
            is_processed=True,
        )
        return user

    def rows(self):
        response = self.client.get(reverse("sports:season-leaderboard", args=[self.season.id]))
        self.assertEqual(response.status_code, 200)
        return response.context["boards"][0].rows

    def test_players_are_ranked_by_points(self):
        self.player("low", points=1)
        self.player("high", points=9)

        self.assertEqual([(row.rank, row.name, row.points) for row in self.rows()], [(1, "high", 9), (2, "low", 1)])

    def test_ties_share_a_rank_and_the_next_one_skips(self):
        """Standard Competition Ranking, the same 1-2-2-4 the Discord message uses."""
        self.player("first", points=9)
        self.player("joint_a", points=5)
        self.player("joint_b", points=5)
        self.player("last", points=1)

        self.assertEqual([row.rank for row in self.rows()], [1, 2, 2, 4])

    def test_discord_global_name_is_what_is_shown(self):
        self.player("raw_username", global_name="Otter Enjoyer", points=3)

        self.assertEqual([row.name for row in self.rows()], ["Otter Enjoyer"])

    def test_user_without_a_discord_profile_falls_back_to_the_username(self):
        self.player("no_profile", points=3)

        self.assertEqual([row.name for row in self.rows()], ["no_profile"])

    def test_picks_and_correct_counts(self):
        other = self.make_match(offset_hours=-6, status=MatchStatus.FINISHED, home_score=0, away_score=1)
        user = self.player("player", points=3)
        Prediction.objects.create(
            pool=self.pool,
            match=other,
            user=user,
            predicted_outcome=MatchOutcome.HOME_WIN,
            points_awarded=0,
            is_processed=True,
        )

        row = self.rows()[0]

        self.assertEqual((row.picks, row.correct, row.points), (2, 1, 3))

    def test_hit_rate_is_correct_picks_over_picks_made(self):
        user = self.player("player", points=3)
        for offset in (-6, -8, -10):
            Prediction.objects.create(
                pool=self.pool,
                match=self.make_match(offset_hours=offset, status=MatchStatus.FINISHED, home_score=0, away_score=1),
                user=user,
                predicted_outcome=MatchOutcome.HOME_WIN,
                points_awarded=0,
                is_processed=True,
            )

        row = self.rows()[0]

        self.assertEqual((row.correct, row.picks, row.hit_rate), (1, 4, 25))

    def test_hit_rate_is_rendered_as_a_percentage(self):
        self.player("player", points=3)

        response = self.client.get(reverse("sports:season-leaderboard", args=[self.season.id]))

        self.assertContains(response, "100%")

    def test_players_from_another_pool_are_not_listed(self):
        other_pool = PredictionPool.objects.create(name="Other", season=self.season)
        Prediction.objects.create(
            pool=other_pool,
            match=self.match,
            user=User.objects.create(username="outsider"),
            predicted_outcome=MatchOutcome.HOME_WIN,
            points_awarded=99,
        )
        self.player("mine", points=1)

        response = self.client.get(reverse("sports:season-leaderboard", args=[self.season.id]))
        boards = {board.pool.name: [row.name for row in board.rows] for board in response.context["boards"]}

        self.assertEqual(boards, {"Otterball 2026": ["mine"], "Other": ["outsider"]})

    def test_stage_rules_are_shown_with_the_table(self):
        PoolStageRule.objects.create(pool=self.pool, stage=self.final, level=9, points_per_correct=7)
        self.player("player", points=7)

        response = self.client.get(reverse("sports:season-leaderboard", args=[self.season.id]))

        self.assertEqual(
            [(rule.stage.name, rule.points_per_correct) for rule in response.context["boards"][0].rules],
            [("Final", 7)],
        )

    def test_pool_without_predictions_renders_an_empty_table(self):
        response = self.client.get(reverse("sports:season-leaderboard", args=[self.season.id]))

        self.assertEqual(response.context["boards"][0].rows, [])
        self.assertContains(response, "No picks yet.")

    def test_season_without_a_pool_says_so(self):
        self.pool.delete()

        response = self.client.get(reverse("sports:season-leaderboard", args=[self.season.id]))

        self.assertEqual(response.context["boards"], [])
        self.assertContains(response, "No pool plays this season.")

    def test_leaderboard_tab_is_hidden_when_the_season_has_no_pool(self):
        self.pool.delete()
        self.make_match(offset_hours=3)

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))

        self.assertFalse(response.context["has_leaderboard"])
        self.assertNotContains(response, "Leaderboard")


class AsyncViewTests(TestCase):
    def test_pages_are_served_on_the_async_path(self):
        """A sync handler on any of these silently drops them off it."""
        for view in (UpcomingMatchesView, BracketView, LeaderboardView):
            with self.subTest(view=view.__name__):
                self.assertTrue(view.view_is_async)


class StatsViewTests(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.pool = PredictionPool.objects.create(name="Pool", season=self.season)
        self.players = [User.objects.create(username=f"p{i}") for i in range(12)]

    def play(self, rounds):
        """`rounds` is a list of {player index: points} dicts, one per match."""
        for offset, awards in enumerate(rounds):
            match = self.make_match(
                offset_hours=-100 + offset,
                status=MatchStatus.FINISHED,
                home_score=1,
                away_score=0,
            )
            for index, points in awards.items():
                Prediction.objects.create(
                    pool=self.pool,
                    match=match,
                    user=self.players[index],
                    predicted_outcome=MatchOutcome.HOME_WIN,
                    points_awarded=points,
                    is_processed=True,
                )

    def board(self):
        response = self.client.get(reverse("sports:season-stats", args=[self.season.id]))
        self.assertEqual(response.status_code, 200)
        return response.context["boards"][0]

    def test_headline_numbers(self):
        self.play([{0: 3, 1: 0}, {0: 3, 1: 3}])

        board = self.board()

        self.assertEqual((board.matches, board.players, board.picks, board.correct), (2, 2, 4, 3))
        self.assertEqual(board.crowd_hit_rate, 75)

    def test_toughest_calls_are_the_least_called_matches(self):
        self.play([{0: 3, 1: 3}, {0: 0, 1: 0}, {0: 3, 1: 0}])

        calls = self.board().toughest_calls

        self.assertEqual([call.percent for call in calls], [0, 50, 100])

    def test_chart_needs_two_scored_matches(self):
        self.play([{0: 3, 1: 0}])

        board = self.board()

        self.assertEqual(board.matches, 1)
        self.assertIsNone(board.chart)
        self.assertContains(
            self.client.get(reverse("sports:season-stats", args=[self.season.id])),
            "Not enough scored matches",
        )

    def test_chart_names_a_capped_number_and_draws_the_rest(self):
        rounds = [{index: index for index in range(len(self.players))} for _ in range(3)]
        self.play(rounds)

        chart = self.board().chart

        self.assertEqual(len(chart.named), CHART_SERIES)
        self.assertEqual(len(chart.coloured), CHART_HIGHLIGHTED)
        # Everyone is drawn - the unnamed ones as the faint band.
        self.assertEqual(len(chart.series), len(self.players))
        self.assertEqual(len(chart.field), len(self.players) - CHART_SERIES)
        self.assertEqual(chart.total_players, len(self.players))

    def test_something_is_always_drawn_on_rank_one(self):
        """The chart used to draw only the final top ten, so at a match whose
        leader finished outside it nothing sat on rank 1 and the axis had a hole
        a reader cannot tell from a bug. The whole field is drawn now.

        Here p11 leads after match 1 and then finishes last.
        """
        rounds = [{11: 10}]
        rounds += [{index: 5 for index in range(11)} for _ in range(3)]
        self.play(rounds)

        chart = self.board().chart

        self.assertNotIn(self.players[11].id, [series.user_id for series in chart.named])
        ranked_first = {index for series in chart.series for index, rank, _points in series.samples if rank == 1}
        self.assertEqual(ranked_first, set(range(1, len(rounds) + 1)))

    def test_the_leader_of_every_match_is_named_for_the_tooltip(self):
        """Whoever held rank 1, so the tooltip can open on them even when they
        are not one of the named ten."""
        rounds = [{11: 10}]
        rounds += [{index: 5 for index in range(11)} for _ in range(3)]
        self.play(rounds)

        chart = self.board().chart

        self.assertEqual(len(chart.leaders), len(rounds))
        self.assertEqual(chart.leaders[0], [self.players[11].username, 10])

    def test_a_tied_lead_names_both(self):
        self.play([{0: 5, 1: 5}, {0: 5, 1: 5}])

        chart = self.board().chart

        self.assertEqual(chart.leaders[-1][0], f"{self.players[0].username} & {self.players[1].username}")

    def test_chart_series_are_ordered_by_final_rank(self):
        self.play([{0: 1, 1: 5, 2: 3}, {0: 1, 1: 5, 2: 3}])

        chart = self.board().chart

        self.assertEqual([s.final_rank for s in chart.series], [1, 2, 3])
        self.assertEqual([s.slot for s in chart.series], [1, 2, 3])

    def test_chart_records_best_and_worst_rank(self):
        # p0 leads, is overtaken, then leads again.
        self.play([{0: 5, 1: 0}, {0: 0, 1: 9}, {0: 9, 1: 0}])

        chart = self.board().chart
        leader = chart.series[0]

        self.assertEqual((leader.best_rank, leader.worst_rank, leader.final_rank), (1, 2, 1))

    def test_season_without_a_pool_says_so(self):
        self.pool.delete()

        response = self.client.get(reverse("sports:season-stats", args=[self.season.id]))

        self.assertEqual(response.context["boards"], [])
        self.assertContains(response, "No pool plays this season")

    def test_pool_without_scored_matches_says_so(self):
        response = self.client.get(reverse("sports:season-stats", args=[self.season.id]))

        self.assertEqual(self.board().matches, 0)
        self.assertContains(response, "Nothing scored yet")

    def test_chart_dom_id_is_unique_per_pool(self):
        other = PredictionPool.objects.create(name="Other", season=self.season)
        self.play([{0: 3}, {0: 3}])
        for prediction in Prediction.objects.all():
            Prediction.objects.create(
                pool=other,
                match=prediction.match,
                user=self.players[1],
                predicted_outcome=MatchOutcome.HOME_WIN,
                points_awarded=1,
                is_processed=True,
            )

        response = self.client.get(reverse("sports:season-stats", args=[self.season.id]))
        ids = [board.chart.dom_id for board in response.context["boards"]]

        self.assertEqual(len(set(ids)), 2, "two charts on one page cannot share a script id")

    def test_picker_lists_every_player_and_checks_the_named_ones(self):
        """The reader can draw anyone, so the list is the whole field - the
        server's ten are only the starting selection."""
        self.play([{index: index for index in range(len(self.players))} for _ in range(3)])

        html = self.client.get(reverse("sports:season-stats", args=[self.season.id])).content.decode()
        picker = html.split('class="chart-picker"', 1)[1].split("</div>\n\n", 1)[0]

        self.assertEqual(picker.count('type="checkbox"'), len(self.players))
        self.assertEqual(picker.count("checked"), CHART_SERIES)
        self.assertIn("data-select-all", picker)
        self.assertIn("data-select-none", picker)

    def test_picker_ships_hidden_because_it_does_nothing_without_script(self):
        self.play([{0: 3, 1: 0}, {0: 3, 1: 3}])

        response = self.client.get(reverse("sports:season-stats", args=[self.season.id]))

        self.assertContains(response, "data-chart-picker hidden")

    def test_every_line_and_label_is_addressable_by_user(self):
        """Selecting a player means restyling their line, their label and their
        swatches, so all three have to carry the id."""
        self.play([{index: index for index in range(len(self.players))} for _ in range(3)])

        html = self.client.get(reverse("sports:season-stats", args=[self.season.id])).content.decode()
        chart = html.split('class="lines"', 1)[1]
        lines, labels = chart.split('class="line-labels"', 1)
        labels = labels.split("</g>", 1)[0]

        self.assertEqual(lines.count("polyline"), len(self.players) * 2)
        self.assertEqual(lines.count("data-user="), len(self.players))
        # A label per player, with the field's hidden until they are selected.
        self.assertEqual(labels.count("<text"), len(self.players))
        self.assertEqual(labels.count("data-user="), len(self.players))
        self.assertEqual(labels.count(" hidden>"), len(self.players) - CHART_SERIES)

    def test_payload_carries_every_players_samples(self):
        """The picker cannot fetch: a player the reader ticks has to already
        have their history on the page."""
        self.play([{index: index for index in range(len(self.players))} for _ in range(3)])

        payload = self.board().chart.payload

        self.assertEqual(len(payload["series"]), len(self.players))
        self.assertTrue(all(entry["samples"] for entry in payload["series"]))
        self.assertEqual(sum(entry["shown"] for entry in payload["series"]), CHART_SERIES)
        self.assertEqual(payload["highlighted"], CHART_HIGHLIGHTED)

    def test_stats_tab_is_hidden_when_the_season_has_no_pool(self):
        self.pool.delete()
        self.make_match(offset_hours=3)

        response = self.client.get(reverse("sports:season-matches", args=[self.season.id]))

        self.assertNotContains(response, "Stats")


class RankChartLayoutTests(TestCase):
    """Covers sports/charts.py geometry - the parts a screenshot cannot assert."""

    def test_no_chart_below_two_points(self):
        self.assertIsNone(build_rank_chart([], {}))

    def test_end_labels_never_overlap(self):
        """Players tied on the final rank share a y and would print on top of
        one another; the spread pass is what keeps the names readable."""
        from predictions.history import HistoryEntry, PlayerStanding

        history = []
        for index in (1, 2):
            standings = {
                user_id: PlayerStanding(user_id=user_id, points=10, picks=2, correct=1, rank=1)
                for user_id in range(6)
            }
            history.append(HistoryEntry(index=index, match=_FakeMatch(), standings=standings))

        chart = build_rank_chart(history, {user_id: f"player{user_id}" for user_id in range(6)})
        label_ys = sorted(series.label_y for series in chart.series)

        gaps = [b - a for a, b in zip(label_ys, label_ys[1:])]
        self.assertTrue(all(gap >= 12 for gap in gaps), gaps)


class _FakeMatch:
    """Just enough Match for the chart's axis labels."""

    kickoff = timezone.now()

    class home_team:
        name = "Germany"

    class away_team:
        name = "Brazil"


class TemplateSyntaxLeakTests(ViewTestCase):
    """No page may render a template tag as visible text.

    Django's `{# ... #}` comment does **not** span lines - only the first line
    is stripped and the rest is printed to the page. That has escaped review
    three times in these templates, so it gets a test rather than more care.
    """

    def setUp(self):
        super().setUp()
        self.pool = PredictionPool.objects.create(name="Pool", season=self.season)
        for hours, status, scores in ((2, MatchStatus.SCHEDULED, (None, None)), (-4, MatchStatus.FINISHED, (2, 1))):
            match = self.make_match(offset_hours=hours, status=status, home_score=scores[0], away_score=scores[1])
            Prediction.objects.create(
                pool=self.pool,
                match=match,
                user=User.objects.create(username=f"player{hours}"),
                predicted_outcome=MatchOutcome.HOME_WIN,
                points_awarded=3 if status == MatchStatus.FINISHED else 0,
                is_processed=True,
            )
        self.make_match(stage=self.final, offset_hours=-2, status=MatchStatus.FINISHED, home_score=1, away_score=0)

    def test_no_page_leaks_a_template_tag(self):
        pages = [
            reverse("sports:upcoming-matches"),
            reverse("sports:season-matches", args=[self.season.id]),
            reverse("sports:season-bracket", args=[self.season.id]),
            reverse("sports:season-leaderboard", args=[self.season.id]),
            reverse("sports:season-stats", args=[self.season.id]),
        ]
        for page in pages:
            with self.subTest(page=page):
                body = self.client.get(page).content.decode()
                for marker in ("{#", "#}", "{%", "%}"):
                    self.assertNotIn(marker, body, f"{marker} rendered into {page}")
