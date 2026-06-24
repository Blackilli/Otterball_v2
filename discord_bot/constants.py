from sports.models import MatchOutcome

DISCORD_POLL_MAP = {
    1: MatchOutcome.HOME_WIN,
    2: MatchOutcome.DRAW,
    3: MatchOutcome.AWAY_WIN,
}
