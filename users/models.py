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
    def is_discord_linked(self) -> bool:
        try:
            return self.discord_profile is not None
        except ObjectDoesNotExist:
            return False

    def __str__(self):
        return self.username
