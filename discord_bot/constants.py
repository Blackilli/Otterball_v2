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

# A stage type that is absent here has no poll layout, and poll_creation skips
# its matches outright - so every stage type a pool can actually use must be
# listed. LEAGUE covers the NFL regular season, where a tie is possible (rare,
# but it happens) and therefore gets the three-answer drawable ordering; the
# NFL playoff rounds are KNOCK_OUT and cannot tie. OTHER is deliberately left
# out: an unclassified stage should fail loudly rather than guess.
DISCORD_POLL_ANSWER_ORDER_MAP = {
    StageType.GROUP: DISCORD_DRAWABLE_POLL_ANSWER_ORDER,
    StageType.LEAGUE: DISCORD_DRAWABLE_POLL_ANSWER_ORDER,
    StageType.KNOCK_OUT: DISCORD_KO_POLL_ANSWER_ORDER,
}
