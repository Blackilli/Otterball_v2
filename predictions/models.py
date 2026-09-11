import datetime
import logging
from typing import TYPE_CHECKING, Any, AsyncGenerator

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Case, Count, FloatField, Q, Sum, Value, When
from django.db.models.functions import Cast
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from sports.models import Match, MatchOutcome, MatchStatus, Season, Stage

logger = logging.getLogger(__name__)

User = get_user_model()

if TYPE_CHECKING:
    from sports.models import Match
    from users.models import User


# How long before kickoff MatchTickerCog posts its "last call" reminder, when a
# pool has not been given its own value. Lives here rather than in the cog so
# the field default and the cog's fallback cannot drift apart.
DEFAULT_REMINDER_LEAD_MINUTES = 60

# A week. Past this the reminder would fire before the poll it is reminding
# people about is likely to exist at all.
MAX_REMINDER_LEAD_MINUTES = 7 * 24 * 60

# How far ahead one poll batch may reach. The ceiling is Discord's own maximum
# poll duration - "Number of hours the poll should be open for, up to 32 days"
# (https://docs.discord.com/developers/resources/poll) - because a poll runs
# from creation until its match kicks off, so a lookahead longer than that
# would ask Discord for a poll it refuses to open. It used to be 7 days, which
# was Discord's original limit before they raised it.
MAX_POLL_LOOKAHEAD_DAYS = 32

# Unchanged when the cap went up: a week of matches per batch is the cadence
# pools actually run on, and raising it retroactively would front-load a
# month of polls into channels that expect seven days of them.
DEFAULT_POLL_LOOKAHEAD_DAYS = 7


class CompetitionRanker:
    """Standard Competition Ranking (1-2-2-4) over a run of descending standings.

    Ties share a rank and the next rank skips by the size of the tie, so two
    players level on 40 points and the same accuracy are both 2nd and the next
    is 4th.

    A tiny state machine rather than a loop in each caller, because there are
    two - the live leaderboard both surfaces read, and the rank-over-time
    history behind the stats page - and a ranking rule that disagreed between
    the chart and the table it sits above would be very hard to spot.

    `rank` must be fed standings in descending order, which is what
    `PredictionPool.aget_user_with_points` and `predictions.history` guarantee.
    """

    def __init__(self):
        self._rank = 1
        self._tied = 0
        self._previous = None

    def rank(self, standing) -> int:
        if self._previous is not None and standing < self._previous:
            self._rank += self._tied
            self._tied = 1
        else:
            self._tied += 1

        self._previous = standing
        return self._rank


def hit_rate_percent(correct: int, picks: int) -> int:
    """Correct picks as a whole percentage of picks made.

    The one place the displayed accuracy is rounded, so the website and the
    pinned Discord leaderboard cannot show the same player two different
    numbers. Ranking does *not* use this - it compares the unrounded ratio (see
    PredictionPool.aget_leaderboard), because two players a tenth of a point
    apart should not be tied by a rounding step.
    """
    if not picks:
        return 0
    return round(correct * 100 / picks)


class DayOfWeek(models.IntegerChoices):
    MONDAY = 0, "Monday"
    TUESDAY = 1, "Tuesday"
    WEDNESDAY = 2, "Wednesday"
    THURSDAY = 3, "Thursday"
    FRIDAY = 4, "Friday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


def validate_weekdays_list(value: Any) -> None:
    """
    Enforces structural integrity on the JSONField.
    Ensures the data is a list of unique integers between 0 and 6.
    """
    if not isinstance(value, list):
        raise ValidationError("Weekdays must be structured as a JSON list/array.")

    for item in value:
        # Check type
        if not isinstance(item, int):
            raise ValidationError(f"Value '{item}' is not an integer.")
        # Check bounds (0 to 6)
        if item < 0 or item > 6:
            raise ValidationError(f"Integer '{item}' falls outside the valid DayOfWeek range (0-6).")

    # Check for duplicates
    if len(value) != len(set(value)):
        raise ValidationError("Duplicate weekdays are not allowed inside the configuration matrix.")


# Create your models here.
class PredictionPool(models.Model):
    name = models.CharField(max_length=255)
    season: Season = models.ForeignKey(
        "sports.Season",
        on_delete=models.CASCADE,
        related_name="prediction_pools",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} (Season ID #{self.season_id})"

    async def aget_user_with_points(self) -> AsyncGenerator[tuple[User, int], Any]:
        # Only users who actually play in *this* pool are ranked. Without the
        # prediction_count filter every User row is annotated, non-participants
        # come back with total_points = NULL, and Postgres sorts NULLs first on
        # a DESC order - so members of an unrelated pool would silently occupy
        # the top ranks and push this pool's players down.
        # Order: points, then accuracy, then id. The id is there purely so the
        # order is stable - the leaderboard cog diffs a fingerprint of this
        # list to decide whether to edit its Discord message, and an unstable
        # order would make it edit forever.
        # All three aggregates walk the same `predictions` relation and say so
        # through `filter=`, so they share one join and become FILTER clauses
        # rather than multiplying each other's rows.
        async for user in (
            User.objects.annotate(
                total_points=Sum(
                    "predictions__points_awarded",
                    filter=Q(predictions__pool=self),
                ),
                pool_prediction_count=Count(
                    "predictions",
                    filter=Q(predictions__pool=self),
                ),
                # The hit rate's denominator, and it is *not* every pick: a
                # match still to be played has not been got wrong yet, so
                # counting it drags a player's accuracy down for as long as the
                # fixture is open - worst right after a poll batch, when
                # everyone has just voted on a week nobody has played. Only
                # FINISHED counts, which also drops a postponed or cancelled
                # fixture: its predictions are void, and scoring them as misses
                # would punish people for a match that never happened.
                pool_settled_count=Count(
                    "predictions",
                    filter=Q(predictions__pool=self, predictions__match__status=MatchStatus.FINISHED),
                ),
                # Carries the same FINISHED condition as the denominator, so
                # the pair can never come from different sets of matches - a
                # scored prediction on a fixture later marked postponed would
                # otherwise leave someone correct on more matches than they
                # have settled, and a hit rate over 100%.
                pool_correct_count=Count(
                    "predictions",
                    filter=Q(
                        predictions__pool=self,
                        predictions__points_awarded__gt=0,
                        predictions__match__status=MatchStatus.FINISHED,
                    ),
                ),
            )
            .annotate(
                # The tiebreaker, unrounded. Guarded rather than relying on the
                # HAVING below to spare it: the ratio is computed in the SELECT
                # list, and Postgres raises on division by zero where SQLite
                # quietly returns NULL. Nobody has a rate before the first
                # result, so everyone sits on 0.0 and points alone order them.
                pool_hit_rate=Case(
                    When(pool_settled_count=0, then=Value(0.0)),
                    default=Cast("pool_correct_count", FloatField()) / Cast("pool_settled_count", FloatField()),
                    output_field=FloatField(),
                ),
            )
            # Any pick at all, settled or not: someone who has voted on a week
            # nobody has played yet is playing, and dropping them until the
            # first kickoff would empty the board at the start of a season.
            .filter(pool_prediction_count__gt=0)
            # Every caller renders a name, and the Discord profile is where
            # the name people know each other by lives: one LEFT JOIN on a
            # one-to-one beats a query per player.
            .select_related("discord_profile")
            .order_by("-total_points", "-pool_hit_rate", "id")
            .aiterator()
        ):
            yield user, (user.total_points or 0)

    async def aget_leaderboard(self) -> AsyncGenerator[tuple[int, User, int], Any]:
        """Standard Competition Ranking (1-2-2-4) over points, then accuracy.

        Points alone reward turning up as much as being right: someone who
        votes on every match outranks a sharper player who missed a week, and
        on equal points they used to share a rank. Accuracy separates them, so
        a tie now means level on *both* - same points and the same share of
        picks right.

        The comparison uses `pool_hit_rate`, the unrounded ratio the database
        ordered by, not the percentage the pages display - the two must not
        disagree about which player is ahead.
        """
        ranker = CompetitionRanker()

        async for user, points in self.aget_user_with_points():
            yield ranker.rank((points, user.pool_hit_rate)), user, points


class PoolConfiguration(models.Model):
    pool = models.OneToOneField(
        PredictionPool,
        on_delete=models.CASCADE,
        related_name="configuration",
        primary_key=True,
    )
    poll_creation_weekdays = models.JSONField(
        default=list,
        validators=[validate_weekdays_list],
        help_text="List of weekdays when the polls should be created (e.g. [0, 6])",
    )
    poll_creation_time = models.TimeField(
        default=datetime.time(0, 0),
        help_text="Time when the polls should be created",
    )
    poll_creation_lookahead_days = models.IntegerField(
        default=DEFAULT_POLL_LOOKAHEAD_DAYS,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_POLL_LOOKAHEAD_DAYS)],
        help_text=(
            "Number of days of matches to put in one poll batch "
            "(1 to {max}, capped by Discord's maximum poll duration)"
        ).format(max=MAX_POLL_LOOKAHEAD_DAYS),
    )
    reminder_lead_minutes = models.PositiveIntegerField(
        default=DEFAULT_REMINDER_LEAD_MINUTES,
        validators=[MaxValueValidator(MAX_REMINDER_LEAD_MINUTES)],
        help_text=(
            "How many minutes before kickoff the 'you haven't voted yet' reminder is posted. "
            "0 turns the reminder off for this pool."
        ),
    )

    def __str__(self):
        return f"Configuration for Pool #{self.pool_id}"


class PoolStageRule(models.Model):
    pool = models.ForeignKey(
        PredictionPool,
        on_delete=models.CASCADE,
        related_name="stage_rules",
    )
    stage = models.ForeignKey(Stage, null=True, blank=True, on_delete=models.CASCADE, related_name="stage_rules")
    level = models.IntegerField(default=0)
    points_per_correct = models.IntegerField(default=3)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["pool", "stage"], name="unique_pool_stage_rule"),
        ]

    def __str__(self):
        return f"Pool #{self.pool_id} - Stage #{self.stage_id} - {self.points_per_correct} Points"


class Prediction(models.Model):
    pool: PredictionPool = models.ForeignKey(
        "PredictionPool",
        on_delete=models.CASCADE,
        related_name="predictions",
    )
    match: Match = models.ForeignKey(
        "sports.Match",
        on_delete=models.CASCADE,
        related_name="predictions",
    )

    user: User = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="predictions",
    )

    predicted_outcome = models.CharField(max_length=10, choices=MatchOutcome.choices)

    points_awarded = models.IntegerField(default=0)
    is_processed = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["pool", "match", "user"], name="unique_user_match_prediction"),
        ]

        indexes = [
            models.Index(fields=["pool", "is_processed"]),
            models.Index(fields=["pool", "points_awarded"]),
        ]

    def __str__(self):
        return f"{self.user.username} -> {self.match} ({self.get_predicted_outcome_display()})"

    async def aupdate_points(
        self,
        force: bool = False,
        cached_points: int | None = None,
        cached_outcome: MatchOutcome | None = None,
        cached_match: Match | None = None,
    ):
        if not force and self.is_processed:
            return

        logger.info(f"Updating points asynchronously for prediction {self.id}")

        outcome = cached_outcome
        match_obj = cached_match

        if outcome is None:
            if not match_obj:
                match_obj = await Match.objects.aget(id=self.match_id)
            outcome = match_obj.outcome

        if self.predicted_outcome != outcome:
            self.points_awarded = 0
        elif cached_points is not None:
            self.points_awarded = cached_points
        else:
            if not match_obj:
                match_obj = await Match.objects.aget(id=self.match_id)
            try:
                stage_rule = await self.pool.stage_rules.aget(stage_id=match_obj.stage_id)
                self.points_awarded = stage_rule.points_per_correct
            except PoolStageRule.DoesNotExist:
                try:
                    fallback_rule = await self.pool.stage_rules.aget(stage=None)
                    self.points_awarded = fallback_rule.points_per_correct
                except PoolStageRule.DoesNotExist:
                    self.points_awarded = 3

        self.is_processed = True
        await self.asave()

    def update_points(
        self,
        force: bool = False,
        cached_points: int | None = None,
        cached_outcome: MatchOutcome | None = None,
        cached_match: Match | None = None,
    ):
        if not force and self.is_processed:
            return

        logger.info(f"Updating points for prediction {self.id}")
        outcome = cached_outcome
        match_obj = cached_match

        if outcome is None:
            if not match_obj:
                match_obj = Match.objects.get(id=self.match_id)
            outcome = match_obj.outcome

        if self.predicted_outcome != outcome:
            self.points_awarded = 0
        elif cached_points is not None:
            self.points_awarded = cached_points
        else:
            if not match_obj:
                match_obj = Match.objects.get(id=self.match_id)
            try:
                stage_rule = self.pool.stage_rules.get(stage_id=match_obj.stage_id)
                self.points_awarded = stage_rule.points_per_correct
            except PoolStageRule.DoesNotExist:
                try:
                    fallback_rule = self.pool.stage_rules.get(stage=None)
                    self.points_awarded = fallback_rule.points_per_correct
                except PoolStageRule.DoesNotExist:
                    self.points_awarded = 3

        self.is_processed = True
        self.save()

    @property
    def is_editable(self) -> bool:
        return timezone.now() < self.match.kickoff

    @property
    def is_correct(self) -> bool:
        logger.info(
            f"Comparing prediction {self.id} outcome ({self.predicted_outcome}) to match outcome {self.match.outcome}. Result: {self.predicted_outcome == self.match.outcome}"
        )
        return self.predicted_outcome == self.match.outcome


def sync_pool_stage_rules(pool: PredictionPool) -> list["PoolStageRule"]:
    """Give `pool` a PoolStageRule for every stage of its season.

    Idempotent - only missing rules are created, existing ones are left alone,
    so this is safe to re-run after a season gains a stage (the NFL playoff
    rounds only appear once the bracket is known).

    New rules take the model's default points rather than inventing an
    escalation, so scoring is unchanged from the implicit fallback in
    predictions/signals.py. The point is that the rules become *visible* and
    editable: without them every correct pick silently scores the hardcoded
    fallback, and a pool meant to scale points per round quietly doesn't.

    Deliberately *not* called from the post_save that creates
    PoolConfiguration. That configuration is one-to-one and cannot collide,
    but stage rules are many-per-pool with a unique (pool, stage) constraint -
    seeding them on every save would turn the common
    `create pool, then add a rule` sequence into an IntegrityError, and would
    silently invalidate any code that relies on a stage having no rule so the
    pool-wide fallback applies. Callers ask for it explicitly instead: the
    admin (PredictionPoolAdmin.save_model) and `manage.py create_pool`.
    """
    existing_stage_ids = set(pool.stage_rules.values_list("stage_id", flat=True))

    created = [
        PoolStageRule(pool=pool, stage=stage, level=stage.level)
        for stage in Stage.objects.filter(season_id=pool.season_id).order_by("level")
        if stage.id not in existing_stage_ids
    ]
    if created:
        PoolStageRule.objects.bulk_create(created)
        logger.info(f"Seeded {len(created)} stage rules for pool {pool.id}")

    return created


@receiver(post_save, sender=PredictionPool)
def create_pool_configuration(sender, instance, created, **kwargs):
    if created:
        PoolConfiguration.objects.create(
            pool=instance,
            poll_creation_weekdays=[
                DayOfWeek.SUNDAY,
            ],
        )
