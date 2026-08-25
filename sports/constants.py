from dataclasses import dataclass

from sports import models
from sports.integrations import espn, fifa

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

# ---------------------------------------------------------------------------
# ESPN / NFL
# ---------------------------------------------------------------------------

ESPN_STATUS_MAP: dict[espn.EventStatus | None, models.MatchStatus] = {
    espn.EventStatus.PRE: models.MatchStatus.SCHEDULED,
    espn.EventStatus.IN: models.MatchStatus.LIVE,
    espn.EventStatus.POST: models.MatchStatus.FINISHED,
}

# ESPN has no "league" entity to ingest, so the NFL competition/season are
# pinned to fixed external ids instead of being discovered from the API.
ESPN_NFL_COMPETITION_EXTERNAL_ID = "nfl"
ESPN_NFL_COMPETITION_NAME = "NFL"

# Used as a health check when seeding the nflverse abbreviation bridge: an
# unmapped team loses every one of its games, so a shortfall must be loud.
NFL_TEAM_COUNT = 32


@dataclass(frozen=True)
class NflStageBlueprint:
    """One scoreable NFL round.

    The pool scores against Stage rows (via PoolStageRule), so the rounds are
    modelled one Stage each rather than one per week - that gives exactly the
    five knobs the old bot's GameTypeScaling had, and keeps the leaderboard's
    "Point Distribution" embed to five readable lines.

    `key` is the stable half of StageMapping.external_id ("2026:REG"); it
    matches the old bot's GameType ids so the two are easy to line up.
    """

    key: str
    name: str
    level: int
    stage_type: models.StageType


NFL_STAGE_BLUEPRINTS: tuple[NflStageBlueprint, ...] = (
    # Regular season games can end in a tie, so this stage gets the drawable
    # (3-answer) poll ordering. Playoff games cannot tie.
    NflStageBlueprint("REG", "Regular Season", 0, models.StageType.LEAGUE),
    NflStageBlueprint("WC", "Wild Card", 1, models.StageType.KNOCK_OUT),
    NflStageBlueprint("DIV", "Divisional", 2, models.StageType.KNOCK_OUT),
    NflStageBlueprint("CON", "Conference Championship", 3, models.StageType.KNOCK_OUT),
    NflStageBlueprint("SB", "Super Bowl", 4, models.StageType.KNOCK_OUT),
)

NFL_STAGE_BLUEPRINTS_BY_KEY: dict[str, NflStageBlueprint] = {bp.key: bp for bp in NFL_STAGE_BLUEPRINTS}

# Postseason week number -> round. Week 4 is the Pro Bowl, which is deliberately
# absent: it is an exhibition, not something worth predicting.
NFL_POSTSEASON_WEEK_KEYS: dict[int, str] = {1: "WC", 2: "DIV", 3: "CON", 5: "SB"}


def nfl_stage_key(season_type: int | None, week: int | None) -> str | None:
    """Which round an ESPN event belongs to, or None if it isn't scoreable.

    Returns None for preseason, the offseason, and the Pro Bowl - those are
    filtered out of ingestion rather than turned into polls.
    """
    if season_type == espn.SeasonType.REGULAR:
        return "REG"
    if season_type == espn.SeasonType.POSTSEASON and week is not None:
        return NFL_POSTSEASON_WEEK_KEYS.get(week)
    return None


def nfl_stage_external_id(season_year: int, key: str) -> str:
    return f"{season_year}:{key}"


# ---------------------------------------------------------------------------
# nflverse
# ---------------------------------------------------------------------------


# nflverse's game_type column already uses the same round keys as
# NFL_STAGE_BLUEPRINTS, so no translation table is needed - but validate
# rather than trust, since an unknown value must be skipped, not guessed.
def nflverse_stage_key(game_type: str | None) -> str | None:
    """Which round an nflverse row belongs to, or None if it isn't scoreable.

    Returns None for preseason ("PRE") and anything unrecognised, matching
    nfl_stage_key's contract for the ESPN path.
    """
    if not game_type:
        return None
    return game_type if game_type in NFL_STAGE_BLUEPRINTS_BY_KEY else None


# nflverse and ESPN agree on 30 of the 32 abbreviations. These are the two
# that differ - without them the Rams and the Commanders silently fail to
# resolve, dropping roughly two games from every week.
NFLVERSE_TO_ESPN_TEAM_ABBREVIATIONS: dict[str, str] = {
    "LA": "LAR",  # Los Angeles Rams
    "WAS": "WSH",  # Washington Commanders
}

ESPN_TO_NFLVERSE_TEAM_ABBREVIATIONS: dict[str, str] = {
    espn: nflverse for nflverse, espn in NFLVERSE_TO_ESPN_TEAM_ABBREVIATIONS.items()
}
