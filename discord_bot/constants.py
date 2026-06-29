from sports.models import MatchOutcome, StageType

DISCORD_DRAWABLE_POLL_ANSWER_ORDER = [
    None,
    MatchOutcome.HOME_WIN,
    MatchOutcome.DRAW,
    MatchOutcome.AWAY_WIN,
]

DISCORD_KO_POLL_ANSWER_ORDER = [
    None,
    MatchOutcome.HOME_WIN,
    MatchOutcome.AWAY_WIN,
]

DISCORD_POLL_ANSWER_ORDER_MAP = {
    StageType.GROUP: DISCORD_DRAWABLE_POLL_ANSWER_ORDER,
    StageType.KNOCK_OUT: DISCORD_KO_POLL_ANSWER_ORDER,
}
