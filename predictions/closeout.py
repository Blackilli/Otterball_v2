"""Closing a pool at the end of its season.

A season ends quietly: the last match finishes, and everything else keeps
running. The bot goes on creating polls for a competition with no fixtures left,
the leaderboard loop goes on editing a message nobody is playing for, and every
ActiveMatchMessage stays in the ticker's query forever. None of that is loud
enough to notice, which is why closing a pool is a deliberate step rather than
something that happens on its own.

Split the way `readiness.py` is: the work lives here so the admin page and
anything else that wants it cannot disagree about what "closed" means.
"""

import dataclasses

from django.db import transaction

from discord_bot.models import ActiveMatchMessage, DiscordGuildPool
from predictions.models import PoolStageRule, Prediction, PredictionPool
from sports.models import Match, MatchStatus


@dataclasses.dataclass(frozen=True)
class CloseoutPlan:
    """What closing this pool would touch, for the confirmation page."""

    unscored_predictions: int
    open_messages: int
    active_bindings: int
    unplayed_matches: int
    pool_is_active: bool

    @property
    def is_worth_doing(self) -> bool:
        return bool(self.unscored_predictions or self.open_messages or self.active_bindings or self.pool_is_active)


@dataclasses.dataclass(frozen=True)
class CloseoutResult:
    """What closing it actually changed."""

    scored_predictions: int
    retired_messages: int
    deactivated_bindings: int
    pool_deactivated: bool


def _unscored(pool: PredictionPool):
    """Predictions on finished matches that never got their points.

    Scoring is a signal on the match, so a match that finished while the worker
    was down leaves these behind - and the leaderboard the season is judged on
    reads points. Closing the pool is the last chance to notice.
    """
    return Prediction.objects.filter(
        pool=pool,
        is_processed=False,
        match__status=MatchStatus.FINISHED,
    ).select_related("match")


def plan_closeout(pool: PredictionPool) -> CloseoutPlan:
    return CloseoutPlan(
        unscored_predictions=_unscored(pool).count(),
        # Same predicate the update below uses, so the number on the
        # confirmation page is the number of rows that change.
        open_messages=ActiveMatchMessage.objects.filter(pool=pool)
        .exclude(is_poll_finalized=True, is_ticker_finalized=True)
        .count(),
        active_bindings=DiscordGuildPool.objects.filter(pool=pool, is_active=True).count(),
        unplayed_matches=Match.objects.filter(stage__season_id=pool.season_id)
        .exclude(status__in=(MatchStatus.FINISHED, MatchStatus.CANCELLED))
        .count(),
        pool_is_active=pool.is_active,
    )


@transaction.atomic
def close_out_pool(pool: PredictionPool) -> CloseoutResult:
    """Score what is left, stop every loop, and deactivate the pool.

    Order matters: the points are computed while the pool is still the live
    one, so the leaderboard the bot last rendered and the one the website shows
    afterwards agree. Nothing here talks to Discord - it cannot, this runs in
    the `web` container - so the polls and the pinned leaderboard stay exactly
    as they are in the channel. The bot simply stops revisiting them.
    """
    scored = _score_remaining(pool)

    retired = (
        ActiveMatchMessage.objects.filter(pool=pool)
        .exclude(is_poll_finalized=True, is_ticker_finalized=True)
        .update(is_poll_finalized=True, is_ticker_finalized=True)
    )

    deactivated = DiscordGuildPool.objects.filter(pool=pool, is_active=True).update(is_active=False)

    was_active = pool.is_active
    if was_active:
        pool.is_active = False
        pool.save(update_fields=["is_active"])

    return CloseoutResult(
        scored_predictions=scored,
        retired_messages=retired,
        deactivated_bindings=deactivated,
        pool_deactivated=was_active,
    )


def _score_remaining(pool: PredictionPool) -> int:
    """The sync twin of `manage.py update_points`, scoped to one pool.

    Same rules cache and the same stage → pool-wide → hardcoded-3 fallback
    chain, because `Prediction.update_points` would otherwise run that lookup
    per row.
    """
    rules_cache: dict[tuple[int, int | None], int] = {
        (rule.pool_id, rule.stage_id): rule.points_per_correct for rule in PoolStageRule.objects.filter(pool=pool)
    }

    scored = 0
    for prediction in _unscored(pool).iterator():
        points = rules_cache.get((prediction.pool_id, prediction.match.stage_id))
        if points is None:
            points = rules_cache.get((prediction.pool_id, None), 3)

        prediction.update_points(
            cached_points=points,
            cached_outcome=prediction.match.outcome,
            cached_match=prediction.match,
        )
        scored += 1

    return scored
