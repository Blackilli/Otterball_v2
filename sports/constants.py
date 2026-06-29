from sports import models
from sports.integrations import fifa

FIFA_STATUS_MAP: dict[fifa.MatchStatus | None, models.MatchStatus] = {
    fifa.MatchStatus.TO_BE_PLAYED: models.MatchStatus.SCHEDULED,
    fifa.MatchStatus.LIVE: models.MatchStatus.LIVE,
    fifa.MatchStatus.PLAYED: models.MatchStatus.FINISHED,
    fifa.MatchStatus.POSTPONED: models.MatchStatus.POSTPONED,
    fifa.MatchStatus.CANCELLED: models.MatchStatus.CANCELLED,
    fifa.MatchStatus.ABANDONED: models.MatchStatus.CANCELLED,
}
FIFA_GENDER_MAP: dict[fifa.Gender | None, models.Gender] = {
    fifa.Gender.MALE: models.Gender.MALE,
    fifa.Gender.FEMALE: models.Gender.FEMALE,
    fifa.Gender.UNKNOWN: models.Gender.OTHER,
}

FIFA_STAGE_TYPE_MAP = {
    fifa.StageType.GROUP: models.StageType.GROUP,
    fifa.StageType.KNOCK_OUT: models.StageType.KNOCK_OUT,
    fifa.StageType.LEAGUE: models.StageType.LEAGUE,
    fifa.StageType.UNKNOWN: models.StageType.OTHER,
}
