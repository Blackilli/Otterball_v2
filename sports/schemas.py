from pydantic import BaseModel

from sports.models import MatchStatus


class MatchUpdatePayload(BaseModel):
    match_id: int
    status: MatchStatus
    home_score: int | None
    away_score: int | None
