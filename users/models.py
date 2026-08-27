import uuid
from typing import TYPE_CHECKING

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ObjectDoesNotExist
from django.db import models

if TYPE_CHECKING:
    from django.db.models.fields.related_descriptors import RelatedManager

    from discord_bot.models import DiscordProfile
    from predictions.models import Prediction


# Create your models here.
class User(AbstractUser):
    email = models.EmailField(
        # unique=True,
        db_index=True,
        # error_messages={"unique": "A user with that email already exists."},
    )

    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )

    predictions: RelatedManager[Prediction]
    discord_profile: DiscordProfile | None

    @property
    def display_name(self) -> str:
        """What to call this user on screen.

        The Discord global name first, because that is the name people know
        each other by in the pool channel and the one the bot's leaderboard
        message uses - a web leaderboard listing different names than the
        pinned one would read as a different scoreboard.

        Reaches the profile through the reverse accessor rather than importing
        discord_bot, which depends on this app and not the other way round.
        Callers rendering more than one user should `select_related`
        ("discord_profile") or this is a query per row.
        """
        try:
            profile = self.discord_profile
        except ObjectDoesNotExist:
            return self.username
        return profile.global_name or profile.username or self.username

    @property
    def is_discord_linked(self) -> bool:
        try:
            return self.discord_profile is not None
        except ObjectDoesNotExist:
            return False

    def __str__(self):
        return self.username
