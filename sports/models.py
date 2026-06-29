from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from django.db.models.fields.related_descriptors import RelatedManager

    from discord_bot.models import ActiveMatchMessage
    from predictions.models import Prediction


# Create your models here.
class Sport(models.TextChoices):
    SOCCER = "SOCCER", "Soccer"
    AMERICAN_FOOTBALL = "AMERICAN_FOOTBALL", "American Football"


class StageType(models.TextChoices):
    GROUP = "GROUP", "Group Stage"
    KNOCK_OUT = "KNOCK_OUT", "Knockout Stage"
    LEAGUE = "LEAGUE", "League"
    OTHER = "OTHER", "Other"


class SportsProvider(models.TextChoices):
    FIFA = "FIFA", "FIFA Official API"
    ESPN = "ESPN", "ESPN Sports Data"


class MatchStatus(models.TextChoices):
    SCHEDULED = "SCHEDULED", "Scheduled"
    LIVE = "LIVE", "Live"
    FINISHED = "FINISHED", "Finished"
    POSTPONED = "POSTPONED", "Postponed"
    CANCELLED = "CANCELLED", "Cancelled"


class MatchOutcome(models.TextChoices):
    HOME_WIN = "HOME", "Home Win"
    AWAY_WIN = "AWAY", "Away Win"
    DRAW = "DRAW", "Draw"


class Gender(models.TextChoices):
    MALE = "MALE", "Male"
    FEMALE = "FEMALE", "Female"
    OTHER = "OTHER", "Other"


# ####################################


class ExternalMappingBase(models.Model):
    # noinspection PyTypeChecker
    provider = models.CharField(
        max_length=50,
        choices=SportsProvider.choices,
        default=SportsProvider.FIFA,
    )
    external_id = models.CharField(max_length=255)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Competition(models.Model):
    name = models.CharField(max_length=255)
    # noinspection PyTypeChecker
    sport = models.CharField(
        max_length=50,
        choices=Sport.choices,
        default=Sport.SOCCER,
    )
    # noinspection PyTypeChecker
    gender = models.CharField(
        max_length=10,
        choices=Gender.choices,
        default=Gender.MALE,
    )
    is_featured = models.BooleanField(default=False)
    mappings: RelatedManager[CompetitionMapping]

    def __str__(self):
        return f"{self.name} ({self.get_sport_display()})"


class CompetitionMapping(ExternalMappingBase):
    competition: Competition = models.ForeignKey(
        "Competition",
        on_delete=models.CASCADE,
        related_name="mappings",
    )

    class Meta:
        unique_together = ("provider", "external_id")

    def __str__(self):
        return f"{self.competition.name} ({self.provider})"


class Season(models.Model):
    name = models.CharField(max_length=255)
    competition: Competition = models.ForeignKey(
        "Competition",
        on_delete=models.CASCADE,
        related_name="seasons",
    )
    year = models.IntegerField()
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("competition", "year")

    def __str__(self):
        return f"{self.name}"


class SeasonMapping(ExternalMappingBase):
    season: Season = models.ForeignKey(
        "Season",
        on_delete=models.CASCADE,
        related_name="mappings",
    )

    class Meta:
        unique_together = ("provider", "external_id")

    def __str__(self):
        return f"{self.season.name} ({self.provider})"


class Stage(models.Model):
    season: Season = models.ForeignKey(
        "Season",
        on_delete=models.CASCADE,
        related_name="stages",
    )
    name = models.CharField(max_length=255)
    # noinspection PyTypeChecker
    stage_type = models.CharField(
        max_length=10,
        choices=StageType.choices,
        default=StageType.GROUP,
    )
    level = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.season} - {self.name}"


class StageMapping(ExternalMappingBase):
    stage: Stage = models.ForeignKey(
        "Stage",
        on_delete=models.CASCADE,
        related_name="mappings",
    )

    class Meta:
        unique_together = ("provider", "external_id")

    def __str__(self):
        return f"{self.stage.season} - {self.stage.name} ({self.provider})"


class Team(models.Model):
    name = models.CharField(max_length=255)
    # noinspection PyTypeChecker
    sport = models.CharField(
        max_length=50,
        choices=Sport.choices,
        default=Sport.SOCCER,
    )
    # noinspection PyTypeChecker
    gender = models.CharField(
        max_length=10,
        choices=Gender.choices,
        default=Gender.MALE,
    )
    logo_url = models.URLField(null=True, blank=True)
    logo = models.ImageField(upload_to="team_logos", null=True, blank=True)
    color = models.CharField(max_length=9, default="#60669c")

    class Meta:
        indexes = [
            models.Index(fields=["name", "sport"]),
        ]

    def __str__(self):
        return f"{self.name}"


class TeamMapping(ExternalMappingBase):
    team: Team = models.ForeignKey("Team", on_delete=models.CASCADE, related_name="mappings")

    class Meta:
        unique_together = ("provider", "external_id")

    def __str__(self):
        return f"{self.team.name} ({self.provider})"


class Match(models.Model):
    stage: Stage = models.ForeignKey(
        "Stage",
        on_delete=models.CASCADE,
        related_name="matches",
    )
    home_team: Team = models.ForeignKey(
        "Team",
        on_delete=models.CASCADE,
        related_name="home_matches",
    )
    away_team: Team = models.ForeignKey(
        "Team",
        on_delete=models.CASCADE,
        related_name="away_matches",
    )

    kickoff = models.DateTimeField()
    # noinspection PyTypeChecker
    status = models.CharField(
        max_length=15,
        choices=MatchStatus.choices,
        default=MatchStatus.SCHEDULED,
    )

    home_score = models.IntegerField(null=True, blank=True)
    away_score = models.IntegerField(null=True, blank=True)

    external_mappings: RelatedManager[MatchMapping]
    predictions: RelatedManager[Prediction]
    active_messages: RelatedManager[ActiveMatchMessage]

    class Meta:
        verbose_name_plural = "Matches"
        indexes = [
            models.Index(fields=["status", "kickoff"]),
        ]

    @property
    def outcome(self) -> MatchOutcome | None:
        if not self.status == MatchStatus.FINISHED or self.home_score is None or self.away_score is None:
            return None

        if self.home_score > self.away_score:
            return MatchOutcome.HOME_WIN
        if self.home_score < self.away_score:
            return MatchOutcome.AWAY_WIN
        return MatchOutcome.DRAW

    def __str__(self):
        return f"{self.kickoff.strftime('%Y-%m-%d')} - {self.home_team} vs. {self.away_team}"


class MatchMapping(ExternalMappingBase):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="mappings")

    class Meta:
        unique_together = ("provider", "external_id")

    def __str__(self):
        return f"{self.match.kickoff.strftime('%Y-%m-%d')} - {self.match.home_team} vs. {self.match.away_team} ({self.provider})"
