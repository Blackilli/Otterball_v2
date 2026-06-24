import logging
from typing import TYPE_CHECKING, Any, AsyncGenerator

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone

from sports.models import Match, MatchOutcome, Season, Stage

logger = logging.getLogger(__name__)

User = get_user_model()

if TYPE_CHECKING:
    from sports.models import Match
    from users.models import User


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
        return f"{self.name} ({self.season})"

    async def aget_user_with_points(self) -> AsyncGenerator[tuple[User, int], Any]:
        async for user in (
            User.objects.annotate(total_points=Sum("predictions__points_awarded", filter=Q(predictions__pool=self)))
            .order_by("-total_points")
            .aiterator()
        ):
            yield user, (user.total_points or 0)


class PoolStageRule(models.Model):
    pool = models.ForeignKey(
        PredictionPool,
        on_delete=models.CASCADE,
        related_name="stage_rules",
    )
    stage = models.ForeignKey(Stage, null=True, on_delete=models.CASCADE, related_name="stage_rules")
    level = models.IntegerField()
    points_per_correct = models.IntegerField(default=3)

    def __str__(self):
        return f"{self.pool} - {self.stage} - {self.level} - {self.points_per_correct} Points"


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
        unique_together = ("pool", "match", "user")

        indexes = [
            models.Index(fields=["pool", "is_processed"]),
            models.Index(fields=["pool", "points_awarded"]),
        ]

    def __str__(self):
        return f"{self.user.username} -> {self.match} ({self.get_predicted_outcome_display()})"

    async def aupdate_points(self, force: bool = False):
        if not force and self.is_processed:
            return
        logger.info(f"Updating points for prediction {self.id}")
        if self.is_correct:
            self.points_awarded = (await self.pool.stage_rules.aget(stage=self.match.stage)).points_per_correct
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
