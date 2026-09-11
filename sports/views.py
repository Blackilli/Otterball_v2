"""Read-only public pages: what is next, how the bracket stands, who is winning.

The web side of Otterball is deliberately thin - the game is played in Discord,
and these pages add no interaction of their own. They exist so a pool has
something to link to: a fixture list nobody has to scroll a channel for, a
tournament tree, which a chat log is a genuinely bad way to show, and a
leaderboard that is readable without opening Discord at all.

The views are **async**, like the rest of the project. Django serves a
class-based view on the async path when every one of its handlers is a
coroutine (`View.view_is_async`), which is why `SeasonPageView` overrides
`get` rather than `get_context_data` - `get_context_data` is not part of that
path, so the pages build their context in `aget_page_context` instead. The
payoff is that these views use the same async ORM the bot and the ingestion
code use, so there is one way to query in this codebase rather than two.

Everything here is anonymous and read-only, and the querysets are written to be
flat: one query for the matches, one for the vote splits, one for the season
switcher. No `Prediction` is looked up per match and no logo is fetched through
a lazy relation inside a loop.
"""

import datetime
from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Count, Prefetch
from django.http import Http404
from django.shortcuts import aget_object_or_404
from django.utils import timezone
from django.views.generic import TemplateView

from predictions.history import (
    build_consensus,
    build_contrarians,
    build_crowd_standing,
    build_outcome_mix,
    build_rank_history,
    build_streaks,
    final_order,
)
from predictions.models import PoolStageRule, Prediction, PredictionPool, hit_rate_percent
from sports.charts import RankChart, build_rank_chart
from sports.constants import DRAWABLE_STAGE_TYPES
from sports.models import Match, MatchOutcome, MatchStatus, Season, StageType

#: How many upcoming matches one page shows. A count rather than a date window:
#: a window is either empty in the off-season or endless during a group stage,
#: whereas "the next two dozen" is roughly a week and a half of NFL or six days
#: of a World Cup group stage, and is never blank while a season has a future.
MAX_UPCOMING_MATCHES = 24

#: How many of the season's least-called matches the stats page lists. Enough
#: to show a pattern, short enough to stay a highlight rather than a log.
MAX_TOUGHEST_CALLS = 5

#: Rows in the streak and contrarian tables. A highlight, not a directory -
#: the leaderboard is where every player is listed.
MAX_STAT_ROWS = 5

#: The results strip under the fixture list. Small on purpose - it is context
#: for the upcoming matches (and the only thing on the page once a season has
#: finished), not a results archive.
MAX_RECENT_RESULTS = 6

#: A match that kicked off this recently still belongs at the top of the page.
#: `status` only becomes LIVE once ingestion notices, which is up to two minutes
#: after kickoff for the NFL and longer for a competition on the daily sync, so
#: filtering on `kickoff >= now` alone would drop a game that is being played.
LIVE_GRACE = datetime.timedelta(hours=3)

#: Statuses that mean the match is over one way or another and does not belong
#: in a fixture list. POSTPONED is deliberately not here: it keeps its old
#: kickoff, so it is worth showing with its badge rather than hiding.
CONCLUDED_STATUSES = (MatchStatus.FINISHED, MatchStatus.CANCELLED)


@dataclass(frozen=True)
class VoteShare:
    """One segment of a match's prediction split."""

    outcome: MatchOutcome
    label: str
    count: int
    #: Rounded for display only; the bar is sized from the same number, so the
    #: three segments can add up to 99 or 101 and that is fine - they are laid
    #: out with flex-grow, not absolute widths.
    percent: int
    #: True when the match is over and this is the outcome it ended on, i.e.
    #: this segment's voters are the ones who scored. False on every segment
    #: of an unplayed match, and on every segment of a knockout tie that went
    #: to penalties - the schema records the level score, not who advanced.
    is_correct: bool = False

    @property
    def css_class(self) -> str:
        return f"vote-{self.outcome.lower()}"


@dataclass(frozen=True)
class MatchDay:
    """The matches of one calendar day, in the site's timezone."""

    date: datetime.date
    matches: list[Match]


@dataclass(frozen=True)
class LeaderboardRow:
    """One player's line on a pool's leaderboard."""

    rank: int
    name: str
    picks: int
    correct: int
    points: int

    @property
    def hit_rate(self) -> int:
        """Correct picks as a whole percentage of picks made.

        Says something the points column cannot: points reward volume as much
        as accuracy, so someone who voted on every match outranks a sharper
        player who missed a week. It is also the tiebreaker the ranking uses -
        though the ranking compares the unrounded ratio, so two rows can show
        the same percentage and still be ordered, with the Correct and Picks
        columns beside them showing why.

        Rounded by `predictions.models.hit_rate_percent`, which the Discord
        leaderboard calls too, so the two cannot disagree.
        """
        return hit_rate_percent(self.correct, self.picks)


@dataclass(frozen=True)
class PoolStats:
    """One pool's season so far: the headline numbers, the chart, the tables."""

    pool: PredictionPool
    players: int
    matches: int
    picks: int
    correct: int
    chart: RankChart | None
    #: Matches the fewest people called, hardest first.
    toughest_calls: list = field(default_factory=list)
    #: The pool's own majority pick, scored as if it were a player.
    crowd: object | None = None
    #: Longest runs of consecutive correct picks, longest first.
    streaks: list = field(default_factory=list)
    #: Who picked against the majority most, and how it went.
    contrarians: list = field(default_factory=list)
    #: How the pool picked, against how matches actually ended.
    outcome_mix: list = field(default_factory=list)

    @property
    def crowd_hit_rate(self) -> int:
        """How often a pick in this pool was right, across everyone."""
        return hit_rate_percent(self.correct, self.picks)


@dataclass(frozen=True)
class Standings:
    """One pool's leaderboard, with the rules that produced the numbers."""

    pool: PredictionPool
    rows: list[LeaderboardRow] = field(default_factory=list)
    rules: list[PoolStageRule] = field(default_factory=list)


def match_queryset():
    """Every match with the rows the templates touch already joined.

    A match card reads both teams (name, logo, colour), its stage and the
    season/competition the stage belongs to. Without this a fixture list of 24
    matches costs about a hundred queries.
    """
    return Match.objects.select_related(
        "home_team",
        "away_team",
        "stage",
        "stage__season",
        "stage__season__competition",
    )


async def aresolve_default_season() -> Season | None:
    """The season `/` shows when the URL does not name one.

    Whichever season owns the next match to be played, so the default follows
    the calendar without anyone having to flag a season as current. Falls back
    to the season of the most recent match, so the site still lands somewhere
    useful in the off-season, and to None on an empty database.
    """
    now = timezone.now()
    season_id = (
        await Match.objects.filter(kickoff__gte=now - LIVE_GRACE)
        .order_by("kickoff")
        .values_list("stage__season_id", flat=True)
        .afirst()
    )
    if season_id is None:
        season_id = await Match.objects.order_by("-kickoff").values_list("stage__season_id", flat=True).afirst()
    if season_id is None:
        return None
    return await Season.objects.select_related("competition").filter(pk=season_id).afirst()


async def aseasons_for_switcher() -> list[Season]:
    """Seasons worth offering in the nav: the ones that have matches."""
    return [
        season
        async for season in Season.objects.annotate(match_count=Count("stages__matches", distinct=True))
        .filter(match_count__gt=0)
        .select_related("competition")
        .order_by("-year", "competition__name", "name")
    ]


async def ahas_bracket(season: Season) -> bool:
    return await Match.objects.filter(stage__season=season, stage__stage_type=StageType.KNOCK_OUT).aexists()


async def ahas_leaderboard(season: Season) -> bool:
    return await season.prediction_pools.aexists()


def outcome_label(match: Match, outcome: MatchOutcome) -> str:
    if outcome == MatchOutcome.HOME_WIN:
        return match.home_team.name
    if outcome == MatchOutcome.AWAY_WIN:
        return match.away_team.name
    return "Draw"


async def aattach_vote_splits(matches: list[Match], season: Season) -> None:
    """Hang each match's prediction breakdown on it as `vote_split`.

    One grouped query for every match passed in. The split is public
    information - Discord shows a running poll's tally to everyone who opens
    it - so there is nothing here that the channel does not already give away.

    Only predictions from pools playing *this* season are counted, so a match
    that somehow appears in two pools' seasons cannot borrow the other's votes.

    Finished matches get one too, with the outcome that actually happened
    flagged - on a result the split stops being a poll and becomes the answer
    key, so the card shows how many people called it.
    """
    if not matches:
        return

    tallies: defaultdict[int, dict[str, int]] = defaultdict(dict)
    rows = (
        Prediction.objects.filter(match__in=matches, pool__season=season)
        .values("match_id", "predicted_outcome")
        .annotate(count=Count("id"))
    )
    async for row in rows:
        tallies[row["match_id"]][row["predicted_outcome"]] = row["count"]

    for match in matches:
        counts = tallies.get(match.id, {})
        total = sum(counts.values())
        # None until the match is FINISHED with both scores in, which is
        # exactly when nothing should be marked correct yet.
        result = match.outcome
        # A knockout match has no Draw answer on its poll, so it gets no Draw
        # segment either - this mirrors DISCORD_POLL_ANSWER_ORDER_MAP, which
        # decides the same thing for the poll itself.
        outcomes = [MatchOutcome.HOME_WIN, MatchOutcome.AWAY_WIN]
        if match.stage.stage_type in DRAWABLE_STAGE_TYPES:
            outcomes.insert(1, MatchOutcome.DRAW)

        match.vote_total = total
        match.vote_split = [
            VoteShare(
                outcome=outcome,
                label=outcome_label(match, outcome),
                count=counts.get(outcome, 0),
                percent=round(counts.get(outcome, 0) * 100 / total) if total else 0,
                is_correct=result is not None and outcome == result,
            )
            for outcome in outcomes
        ]


def group_by_day(matches: list[Match]) -> list[MatchDay]:
    """Split a kickoff-ordered list into calendar days, in the site's timezone.

    `kickoff` is stored in UTC, so a 01:30 CEST kickoff is the previous day in
    the database. Grouping on the raw value would file it under the wrong
    heading; a World Cup night session is exactly that case.
    """
    days: list[MatchDay] = []
    for match in matches:
        local_date = timezone.localtime(match.kickoff).date()
        if not days or days[-1].date != local_date:
            days.append(MatchDay(date=local_date, matches=[]))
        days[-1].matches.append(match)
    return days


class SeasonPageView(TemplateView):
    """Async base for the public pages: resolves the season and the shared nav.

    `TemplateView` supplies the template plumbing only. Because `get` is a
    coroutine here, Django serves the whole class on its async path, and
    `get_context_data` is never called - subclasses add their own context in
    `aget_page_context`.
    """

    #: A page that makes no sense without a season (the bracket, the
    #: leaderboard) sets this and gets a 404 on an empty database instead of
    #: rendering an empty shell.
    season_required = False

    async def aget_season(self) -> Season | None:
        season_id = self.kwargs.get("season_id")
        if season_id is not None:
            return await aget_object_or_404(Season.objects.select_related("competition"), pk=season_id)

        season = await aresolve_default_season()
        if season is None and self.season_required:
            raise Http404("No season to show.")
        return season

    async def aget_page_context(self, season: Season | None) -> dict:
        return {}

    async def get(self, request, *args, **kwargs):
        season = await self.aget_season()
        context = {
            "view": self,
            "season": season,
            "seasons": await aseasons_for_switcher(),
            "has_bracket": season is not None and await ahas_bracket(season),
            "has_leaderboard": season is not None and await ahas_leaderboard(season),
        }
        context.update(await self.aget_page_context(season))
        return self.render_to_response(context)


class UpcomingMatchesView(SeasonPageView):
    """The fixture list: what is being played now, and what is next."""

    template_name = "sports/upcoming_matches.html"

    async def aget_page_context(self, season: Season | None) -> dict:
        if season is None:
            return {"match_days": [], "recent_results": [], "live_count": 0}

        now = timezone.now()
        upcoming = [
            match
            async for match in match_queryset()
            .filter(stage__season=season, kickoff__gte=now - LIVE_GRACE)
            .exclude(status__in=CONCLUDED_STATUSES)
            .order_by("kickoff", "id")[:MAX_UPCOMING_MATCHES]
        ]
        recent = [
            match
            async for match in match_queryset()
            .filter(stage__season=season, status=MatchStatus.FINISHED)
            .order_by("-kickoff", "-id")[:MAX_RECENT_RESULTS]
        ]

        # One call, so both lists come out of a single grouped query.
        await aattach_vote_splits(upcoming + recent, season)
        return {
            "match_days": group_by_day(upcoming),
            "recent_results": recent,
            "live_count": sum(1 for match in upcoming if match.status == MatchStatus.LIVE),
        }


class BracketView(SeasonPageView):
    """The tournament tree: one column per knockout round, earliest first.

    Rounds come from `Stage.level`, which every ingestion path already sets in
    playing order (World Cup: Round of 32 up to Final; NFL: Wild Card up to
    Super Bowl), so nothing here needs to know which sport it is looking at.

    Matches inside a round are ordered by kickoff and *not* joined to the round
    before them: the schema stores no bracket progression ("winner of match 12
    plays here"), so any connecting line between the columns would be a guess.
    Rather than draw one that is wrong for an unplayed round, the columns are
    left as columns.
    """

    template_name = "sports/bracket.html"
    season_required = True

    async def aget_page_context(self, season: Season | None) -> dict:
        stages = (
            season.stages.filter(stage_type=StageType.KNOCK_OUT)
            .prefetch_related(Prefetch("matches", queryset=match_queryset().order_by("kickoff", "id")))
            .order_by("level", "id")
        )
        rounds = []
        async for stage in stages:
            matches = list(stage.matches.all())
            if matches:
                rounds.append({"stage": stage, "matches": matches})

        # The whole tree's splits in one query, same as the fixture list.
        await aattach_vote_splits([match for round in rounds for match in round["matches"]], season)
        return {"rounds": rounds}


class LeaderboardView(SeasonPageView):
    """Standings for every pool playing this season.

    Ranks come from `PredictionPool.aget_leaderboard`, the same generator the
    bot's pinned leaderboard message reads, so the page and the message cannot
    disagree about who is second. Unlike the Discord version there is no
    "Plebs" collapse - a table has no 1024-character field limit to work
    around, so everyone gets a row.

    A season with several pools gets several tables rather than a picker: it is
    the uncommon case, and a pool the visitor cannot see is worse than a longer
    page.
    """

    template_name = "sports/leaderboard.html"
    season_required = True

    async def aget_page_context(self, season: Season | None) -> dict:
        boards = []
        async for pool in season.prediction_pools.order_by("-is_active", "-created_at"):
            # picks and correct ride along on the user: aget_user_with_points
            # annotates both, because the ranking needs the ratio to order by.
            # Picks is the *settled* count - the same denominator the hit rate
            # beside it divides by, so the row cannot read "10 picks, 3 correct,
            # 60%" while a week of fixtures is still open.
            rows = [
                LeaderboardRow(
                    rank=rank,
                    name=user.display_name,
                    picks=user.pool_settled_count,
                    correct=user.pool_correct_count,
                    points=points,
                )
                async for rank, user, points in pool.aget_leaderboard()
            ]
            rules = [rule async for rule in pool.stage_rules.select_related("stage").order_by("level", "id")]
            boards.append(Standings(pool=pool, rows=rows, rules=rules))

        return {"boards": boards}


class StatsView(SeasonPageView):
    """Season stats: how the table moved, and which matches nobody saw coming.

    The rank-over-time chart is the point of the page. It ranks through the
    same `CompetitionRanker` and the same points-then-accuracy comparison the
    live leaderboard uses, so its last column is the leaderboard - a chart that
    disagreed with the table on the next page would be worse than no chart.

    One pass over the pool's scored predictions feeds everything here: the
    history, the call rates and the totals all come off the same list rather
    than a query each.
    """

    template_name = "sports/stats.html"
    season_required = True

    async def aget_page_context(self, season: Season | None) -> dict:
        boards = []
        async for pool in season.prediction_pools.order_by("-is_active", "-created_at"):
            predictions = [
                prediction
                async for prediction in Prediction.objects.filter(pool=pool, match__status=MatchStatus.FINISHED)
                .select_related(
                    "match",
                    "match__home_team",
                    "match__away_team",
                    "user",
                    "user__discord_profile",
                )
                .order_by("match__kickoff", "match_id")
            ]

            history = build_rank_history(predictions)
            consensus = build_consensus(predictions)
            names = {prediction.user_id: prediction.user.display_name for prediction in predictions}

            boards.append(
                PoolStats(
                    pool=pool,
                    players=len(names),
                    matches=len(history),
                    picks=len(predictions),
                    correct=sum(1 for prediction in predictions if prediction.points_awarded > 0),
                    chart=build_rank_chart(history, names, dom_id=f"rank-chart-{pool.id}"),
                    # Ties broken by pick count so the hardest call among
                    # equally-missed matches is the one most people attempted.
                    toughest_calls=sorted(consensus, key=lambda entry: (entry.percent, -entry.picks))[
                        :MAX_TOUGHEST_CALLS
                    ],
                    crowd=build_crowd_standing(consensus, final_order(history)),
                    streaks=build_streaks(predictions, names)[:MAX_STAT_ROWS],
                    contrarians=build_contrarians(predictions, consensus, names)[:MAX_STAT_ROWS],
                    outcome_mix=build_outcome_mix(predictions, consensus),
                )
            )

        return {"boards": boards}
