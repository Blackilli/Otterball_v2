import datetime
import logging
from typing import TYPE_CHECKING, Any, AsyncGenerator

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from sports.models import Match, MatchOutcome, Season, Stage

logger = logging.getLogger(__name__)

User = get_user_model()

if TYPE_CHECKING:
    from sports.models import Match
    from users.models import User


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
        async for user in (
            User.objects.annotate(
                total_points=Sum(
                    "predictions__points_awarded",
                    filter=Q(predictions__pool=self),
                )
            )
            .order_by("-total_points")
            .aiterator()
        ):
            yield user, (user.total_points or 0)

    async def aget_leaderboard(self) -> AsyncGenerator[tuple[int, User, int], Any]:
        current_rank = 1
        tied_count = 0
        previous_points = None

        async for user, points in self.aget_user_with_points():
            if previous_points is not None and points < previous_points:
                current_rank += tied_count
                tied_count = 1
            else:
                tied_count += 1

            previous_points = points
            yield current_rank, user, points


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
        default=7,
        validators=[MinValueValidator(1), MaxValueValidator(7)],
        help_text="Number of days to look ahead for matches (Strictly 1 to 7 days)",
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
    level = models.IntegerField()
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

    async def aupdate_points(self, force: bool = False, cached_points: int | None = None):
        if not force and self.is_processed:
            return

        logger.info(f"Updating points for prediction {self.id}")

        match_obj = await Match.objects.aget(id=self.match_id)

        if self.predicted_outcome == match_obj.outcome:
            if cached_points is not None:
                self.points_awarded = cached_points
            else:
                try:
                    stage_rule = await self.pool.stage_rules.aget(stage_id=match_obj.stage_id)
                    self.points_awarded = stage_rule.points_per_correct
                except PoolStageRule.DoesNotExist:
                    try:
                        fallback_rule = await self.pool.stage_rules.aget(stage=None)
                        self.points_awarded = fallback_rule.points_per_correct
                    except PoolStageRule.DoesNotExist:
                        self.points_awarded = 3
        else:
            self.points_awarded = 0

        self.is_processed = True
        await self.asave()

    def update_points(self, force: bool = False):
        if not force and self.is_processed:
            return
        logger.info(f"Updating points for prediction {self.id}")
        if self.is_correct:
            self.points_awarded = self.pool.stage_rules.get(stage=self.match.stage).points_per_correct
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


@receiver(post_save, sender=PredictionPool)
def create_pool_configuration(sender, instance, created, **kwargs):
    if created:
        PoolConfiguration.objects.create(
            pool=instance,
            poll_creation_weekdays=[
                DayOfWeek.SUNDAY,
            ],
        )
