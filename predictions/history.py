"""How a pool's standings got to where they are, match by match.

The leaderboard answers "who is winning"; this answers "who was winning, and
when". Both have to agree at the final match - so the ranking here goes through
the same `CompetitionRanker` and the same points-then-accuracy comparison
`PredictionPool.aget_leaderboard` uses, rather than a second rule that would
quietly disagree with the table it is drawn above.

Deliberately free of database access: callers hand in an ordered list of
`Prediction` rows, which lets the async views and the sync management command
share one implementation of the walk.
"""

from collections import Counter
from dataclasses import dataclass
from itertools import groupby
from typing import Iterable, Sequence

from predictions.models import CompetitionRanker, Prediction, hit_rate_percent
from sports.models import Match, MatchOutcome


@dataclass(frozen=True)
class PlayerStanding:
    """One player's position at one point in the season."""

    user_id: int
    points: int
    picks: int
    correct: int
    rank: int

    @property
    def hit_rate(self) -> int:
        return hit_rate_percent(self.correct, self.picks)


@dataclass(frozen=True)
class HistoryEntry:
    """The whole table as it stood once one match had been scored."""

    #: 1-based position in the sequence of scored matches - the chart's x axis.
    #: Matches are spaced evenly rather than by date: a World Cup group stage
    #: puts four matches on one day and then rests, and a time axis would
    #: squash those four into a single unreadable column.
    index: int
    match: Match
    standings: dict[int, PlayerStanding]

    def ordered(self) -> list[PlayerStanding]:
        return sorted(self.standings.values(), key=lambda s: s.rank)


def build_rank_history(predictions: Iterable[Prediction]) -> list[HistoryEntry]:
    """Walk `predictions` in kickoff order, ranking the table after each match.

    `predictions` must already be ordered by (match kickoff, match id) and
    limited to finished matches - a match still to be played has no points to
    add, and would flatten the line for everyone.

    A player's entry only appears once they have made their first pick: ranking
    someone on zero picks before they have joined would show them entering last
    and climbing, which is not what happened.
    """
    points: dict[int, int] = {}
    picks: dict[int, int] = {}
    correct: dict[int, int] = {}

    history: list[HistoryEntry] = []
    for index, (_match_id, group) in enumerate(groupby(predictions, key=lambda p: p.match_id), start=1):
        match_predictions = list(group)

        for prediction in match_predictions:
            user_id = prediction.user_id
            points[user_id] = points.get(user_id, 0) + prediction.points_awarded
            picks[user_id] = picks.get(user_id, 0) + 1
            correct[user_id] = correct.get(user_id, 0) + (1 if prediction.points_awarded > 0 else 0)

        # Same comparison as the live leaderboard: points first, then the
        # unrounded share of picks that scored.
        ordered = sorted(
            points,
            key=lambda user_id: (points[user_id], correct[user_id] / picks[user_id], -user_id),
            reverse=True,
        )

        ranker = CompetitionRanker()
        standings = {}
        for user_id in ordered:
            standings[user_id] = PlayerStanding(
                user_id=user_id,
                points=points[user_id],
                picks=picks[user_id],
                correct=correct[user_id],
                rank=ranker.rank((points[user_id], correct[user_id] / picks[user_id])),
            )

        history.append(HistoryEntry(index=index, match=match_predictions[0].match, standings=standings))

    return history


def final_order(history: Sequence[HistoryEntry]) -> list[PlayerStanding]:
    """The last entry's table, best first. Empty when nothing has been scored."""
    return history[-1].ordered() if history else []


@dataclass(frozen=True)
class MatchConsensus:
    """What the pool thought of one match, and what actually happened."""

    match: Match
    picks: int
    correct: int
    #: Votes per predicted outcome.
    counts: dict[str, int]
    #: The outcome most of the pool picked, or None when the vote was tied -
    #: a split pool has no consensus, and inventing one would put words in its
    #: mouth.
    majority: str | None
    actual: MatchOutcome | None
    #: What a correct pick on this match was worth. Taken from the highest
    #: points_awarded on the match rather than from PoolStageRule: if anyone
    #: got it right, their award *is* the stage's rate, which keeps this from
    #: re-implementing the rule lookup in Prediction.update_points. Zero when
    #: nobody called it, which is exactly when it is never used.
    points_if_right: int

    @property
    def percent(self) -> int:
        return hit_rate_percent(self.correct, self.picks)


def build_consensus(predictions: Iterable[Prediction]) -> list[MatchConsensus]:
    """One entry per scored match, in the order given.

    Same input as `build_rank_history`, so the caller walks the database once
    and gets both. Everything the pool-wide stats need comes off this: the
    matches almost nobody called, what the crowd would have scored, and who
    was betting against it.
    """
    consensus = []
    for _match_id, group in groupby(predictions, key=lambda p: p.match_id):
        match_predictions = list(group)
        counts = Counter(prediction.predicted_outcome for prediction in match_predictions)
        ranked = counts.most_common()
        tied = len(ranked) > 1 and ranked[0][1] == ranked[1][1]

        consensus.append(
            MatchConsensus(
                match=match_predictions[0].match,
                picks=len(match_predictions),
                correct=sum(1 for prediction in match_predictions if prediction.points_awarded > 0),
                counts=dict(counts),
                majority=None if tied else ranked[0][0],
                actual=match_predictions[0].match.outcome,
                points_if_right=max(prediction.points_awarded for prediction in match_predictions),
            )
        )
    return consensus


@dataclass(frozen=True)
class CrowdStanding:
    """The pool's own consensus, scored as if it were a player.

    The question it answers is the one every pool argues about: could you just
    have voted with everyone else and won?
    """

    points: int
    picks: int
    correct: int
    #: Where the crowd would have placed among the real players.
    rank: int
    #: How many players it would have finished ahead of.
    beaten: int
    #: Matches the pool was split down the middle on, where the crowd had no
    #: pick to copy.
    abstained: int

    @property
    def hit_rate(self) -> int:
        return hit_rate_percent(self.correct, self.picks)


def build_crowd_standing(
    consensus: Sequence[MatchConsensus],
    final: Sequence[PlayerStanding],
) -> CrowdStanding | None:
    """Score the majority pick on every match against the finished table."""
    if not consensus or not final:
        return None

    picks = [entry for entry in consensus if entry.majority is not None]
    hits = [entry for entry in picks if entry.majority == entry.actual]
    points = sum(entry.points_if_right for entry in hits)
    rate = len(hits) / len(picks) if picks else 0

    # Compared on (points, accuracy), the same pair the leaderboard ranks by,
    # so the crowd is placed by the pool's own rules rather than on points
    # alone.
    def key(standing: PlayerStanding) -> tuple[int, float]:
        return (standing.points, standing.correct / standing.picks if standing.picks else 0)

    ahead = sum(1 for standing in final if key(standing) > (points, rate))
    behind = sum(1 for standing in final if key(standing) < (points, rate))

    return CrowdStanding(
        points=points,
        picks=len(picks),
        correct=len(hits),
        rank=ahead + 1,
        beaten=behind,
        abstained=len(consensus) - len(picks),
    )


@dataclass(frozen=True)
class PlayerStreak:
    name: str
    length: int


def build_streaks(predictions: Iterable[Prediction], names: dict[int, str]) -> list[PlayerStreak]:
    """Each player's longest run of consecutive correct picks, longest first.

    Sorted by user id only - the sort is stable and the input is already in
    kickoff order, so each player's picks stay chronological, which is the
    whole point of a streak.
    """
    longest: dict[int, int] = {}
    for user_id, group in groupby(sorted(predictions, key=lambda p: p.user_id), key=lambda p: p.user_id):
        run = best = 0
        for prediction in group:
            run = run + 1 if prediction.points_awarded > 0 else 0
            best = max(best, run)
        longest[user_id] = best

    return sorted(
        (PlayerStreak(name=names.get(user_id, "?"), length=length) for user_id, length in longest.items()),
        key=lambda streak: (-streak.length, streak.name),
    )


@dataclass(frozen=True)
class Contrarian:
    name: str
    against: int
    right: int

    @property
    def percent(self) -> int:
        return hit_rate_percent(self.right, self.against)


def build_contrarians(
    predictions: Iterable[Prediction],
    consensus: Sequence[MatchConsensus],
    names: dict[int, str],
) -> list[Contrarian]:
    """Who picked against the majority, and how often it paid off.

    Ordered by how often they did it rather than by how well it went: sorting
    by success rate puts whoever went against the crowd twice and got both
    right at the top, which says nothing.
    """
    majority = {entry.match.id: entry.majority for entry in consensus}
    against: Counter[int] = Counter()
    right: Counter[int] = Counter()

    for prediction in predictions:
        pick = majority.get(prediction.match_id)
        if pick is None or prediction.predicted_outcome == pick:
            continue
        against[prediction.user_id] += 1
        if prediction.points_awarded > 0:
            right[prediction.user_id] += 1

    return [
        Contrarian(name=names.get(user_id, "?"), against=count, right=right[user_id])
        for user_id, count in against.most_common()
    ]


@dataclass(frozen=True)
class OutcomeMix:
    """How often the pool picked an outcome, against how often it happened."""

    outcome: MatchOutcome
    label: str
    predicted: int
    predicted_percent: int
    actual: int
    actual_percent: int

    @property
    def css_class(self) -> str:
        return f"vote-{self.outcome.lower()}"

    @property
    def bias(self) -> int:
        """Percentage points of over- or under-picking."""
        return self.predicted_percent - self.actual_percent

    @property
    def bias_label(self) -> str:
        if abs(self.bias) < 2:
            return "about right"
        return f"{abs(self.bias)} pts {'over' if self.bias > 0 else 'under'}-picked"


def build_outcome_mix(
    predictions: Iterable[Prediction],
    consensus: Sequence[MatchConsensus],
) -> list[OutcomeMix]:
    """The pool's taste in results, against reality.

    Reveals the bias a leaderboard cannot: a pool that almost never picks the
    draw is leaving points on the table every time one happens.
    """
    picked = Counter(prediction.predicted_outcome for prediction in predictions)
    happened = Counter(entry.actual for entry in consensus if entry.actual is not None)
    total_picks = sum(picked.values())
    total_matches = sum(happened.values())
    if not total_picks or not total_matches:
        return []

    labels = {
        MatchOutcome.HOME_WIN: "Home win",
        MatchOutcome.DRAW: "Draw",
        MatchOutcome.AWAY_WIN: "Away win",
    }
    return [
        OutcomeMix(
            outcome=outcome,
            label=label,
            predicted=picked.get(outcome, 0),
            predicted_percent=round(picked.get(outcome, 0) * 100 / total_picks),
            actual=happened.get(outcome, 0),
            actual_percent=round(happened.get(outcome, 0) * 100 / total_matches),
        )
        for outcome, label in labels.items()
    ]
