import datetime
import io
import logging
from enum import IntEnum, StrEnum
from typing import Any, AsyncGenerator

import httpx2
from aiolimiter import AsyncLimiter
from httpx2 import Response
from PIL import Image
from PIL.ImageFile import ImageFile
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

logger = logging.getLogger(__name__)


class RateLimitedAsyncTransport(httpx2.AsyncHTTPTransport):
    def __init__(self, requests_per_second: float, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.limiter = AsyncLimiter(max_rate=requests_per_second, time_period=1.0)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        async with self.limiter:
            return await super().handle_async_request(request)


class Gender(IntEnum):
    MALE = 1
    FEMALE = 2
    UNKNOWN = 9999


class TeamType(IntEnum):
    CLUB = 0
    NATIONAL = 1
    UNKNOWN = 2
    OTHER = 3


class AgeType(IntEnum):
    MAIN = 7
    OLYMPIC = 5
    YOUTH_OLYMPIC = 6
    UNDER_13 = 8
    UNDER_14 = 9
    UNDER_15 = 10
    UNDER_16 = 11
    UNDER_17 = 1
    UNDER_18 = 2
    UNDER_19 = 3
    UNDER_20 = 4
    UNDER_21 = 12
    UNDER_22 = 13
    UNDER_23 = 14
    UNKNOWN = 0


class FootballType(IntEnum):
    FOOTBALL = 0
    FUTSAL = 1
    BEACH = 2
    ESPORT = 3
    UNKNOWN = 99


class CompetitionType(IntEnum):
    INTERNATIONAL = 1
    NATIONAL = 2
    FIFA = 3
    UNKNOWN = 4


class ActiveStatus(IntEnum):
    ACTIVE = 0
    INACTIVE = 1
    UNKNOWN = 2


class TurfType(IntEnum):
    ARTIFICIAL = 0
    GRASS = 1


class MediaContentType(IntEnum):
    CARD_PHOTO = 0
    PROFILE_PHOTO = 1
    REGULAR_PHOTO = 2
    REGULAR_VIDEO = 3
    BIOGRAPHY = 4
    SHOP = 5


class ConfederationRole(IntEnum):
    PRESIDENT = 0
    ACTING_PRESIDENT = 1
    NORMALISATION_COMMITTEE = 2
    SENIOR_VICE_PRESIDENT = 3
    VICE_PRESIDENT = 4
    GENERAL_SECRETARY = 5
    ACTING_GENERAL_SECRETARY = 6
    TREASURER = 7
    MEDIA_AND_COMMUNICATION_MANAGER = 8
    TECHNICAL_DIRECTOR = 9
    NATIONAL_COACH_MAN = 10
    NATIONAL_COACH_WOMAN = 11
    OTHER = 12
    REFEREE_COMMITTEE = 13
    REFEREE_DEPARTMENT = 14
    REFEREE_COORDINATOR = 15
    FUTSAL_COORDINATOR = 16
    ACTING_TECHNICAL_DIRECTOR = 17


class OfficialType(IntEnum):
    UNKNOWN = 0
    MAIN = 1
    LINEMAN1 = 2
    LINEMAN2 = 3
    FOURTH_OFFICIAL = 4
    VIDEO_ASSISTANT_REFEREE = 5
    RESERVE_REFEREE = 6
    OFFSITE_VIDEO_ASSISTANT_REFEREE = 7
    ASSISTANT_VIDEO_ASSISTANT_REFEREE = 8
    SUPPORT_VIDEO_ASSISTANT_REFEREE = 9
    RESERVE_ASSISTANT_REFEREE = 10
    SECOND_REFEREE = 11
    THIRD_REFEREE = 12
    TIMEKEEPER = 13


class WeatherType(IntEnum):
    CLOUDY = 0
    FOGGY = 1
    HAIL = 2
    LAMP = 3
    PARTLY_CLOUDY = 4
    RAIN = 5
    SNOW = 6
    SUNNY = 7
    CLOUDY_NIGHT = 8
    PARTLY_CLOUDY_NIGHT = 9
    CLEAR_NIGHT = 10
    WINDY = 11
    STORMY = 12
    PARTY_CLOUDY_RAIN = 13
    INDOOR = 14


class MatchStatus(IntEnum):
    PLAYED = 0
    TO_BE_PLAYED = 1
    LIVE = 3
    ABANDONED = 4
    POSTPONED = 7
    CANCELLED = 8
    FORFEITED = 9
    DELAYED = 10
    INTERRUPTION = 11
    LINE_UPS = 12
    RESCHEDULED = 13
    SUSPENDED = 99


class ResultType(IntEnum):
    UNKNOWN = 0
    NORMAL_RESULT = 1
    PENALTY_SHOOTOUT = 2
    EXTRA_TIME = 3
    AGGREGATED = 4
    AGGREGATED_EXTRA_TIME = 5
    AWAY_GOAL = 6
    AWAY_GOAL_EXTRA_TIME = 7
    GOLDEN_GOAL = 8
    SILVER_GOAL = 9
    TOSS_OF_COIN = 10
    FORFEIT = 11
    AWARDED = 12


class Owner(StrEnum):
    CAF = "CAF"
    OFC = "OFC"
    CONCACAF = "CONCACAF"
    AFC = "AFC"
    UEFA = "UEFA"
    CONMEBOL = "CONMEBOL"
    FIFA = "FIFA"
    UNKNOWN = "UNKNOWN"


class StaffRole(IntEnum):
    COOK = 0
    EQUIPMENT_MANAGER = 1
    GOALKEEPER_COACH = 2
    HEAD_OF_ADMINISTRATION = 3
    HEAD_OF_DELEGATION = 4
    INTERPRETER = 5
    KINESIOLOGIST = 6
    LIAISON_OFFICER = 7
    MASSEUR = 8
    MEMBER_OF_DELEGATION = 9
    OTHER_FUNCTION = 10
    TEAM_MEDIA_OFFICER = 11
    PHYSICAL_TRAINER = 12
    TEAM_ADMINISTRATOR = 13
    TECHNICAL_ASSISTANT = 14
    TEAM_DOCTOR = 15
    TEAM_MANAGER = 16
    TEAM_OFFICIAL = 17
    TEAM_TICKETING_MANAGER = 18
    TEAM_PROTOCOL_OFFICER = 19
    TRANSLATOR = 20
    UNKNOWN = 9999


class Period(IntEnum):
    UNKNOWN = 0
    SCHEDULED = 1
    PRE_MATCH = 2
    FIRST_HALF = 3
    HALF_TIME = 4
    SECOND_HALF = 5
    EXTRA_TIME = 6
    EXTRA_FIRST_HALF = 7
    EXTRA_HALF_TIME = 8
    EXTRA_SECOND_HALF = 9
    FULL_TIME = 10
    PENALTY_SHOOTOUT = 11
    POST_MATCH = 12
    ABANDONED = 13
    THIRD_HALF = 14
    BREAK_SECOND_HALF = 15
    PRE_PENALTY_SHOOTOUT = 16
    PRE_EXTRA_TIME = 17


class SubstitutionReason(IntEnum):
    UNKNOWN = 0
    INJURY = 1
    TACTICAL = 2


class Position(IntEnum):
    GK = 0
    D = 1
    M = 2
    F = 3
    UNKNOWN = 4
    W = 5
    P = 6
    VAR = 7
    SUBSTITUTE = 8


class GoalType(IntEnum):
    UNKNOWN = 0
    PENALTY = 1
    GOAL = 2
    OWN = 3
    SECOND_PENALTY = 4


class CardType(IntEnum):
    UNKNOWN = 0
    YELLOW = 1
    RED = 2
    DOUBLE_YELLOW = 3
    ALL = 4


class PlayerSpecialStatus(IntEnum):
    UNKNOWN = 0
    CAPTAIN = 1
    FIELDED = 2
    BOOKED = 4
    INJURED = 8
    ABSENT = 16
    NOT_IN_LINEUP = 32
    SUSPENDED = 64
    SUSPENDED_AFTER_RED_CARD = 128
    SUSPENDED_AFTER_DOUBLE_YELLOW = 256


class PlayerFieldStatus(IntEnum):
    UNKNOWN = 0
    IN = 1
    OUT = 2


class LiveMatchPlyerStatus(IntEnum):
    UNKNOWN = 0
    START = 1
    SUB = 2


class LiveMatchCoachRole(IntEnum):
    MANAGER = 0
    ASSISTANT_MANAGER = 1
    UNKNOWN = 9999


class OfficialityStatus(IntEnum):
    UNCONFIRMED = 0
    MAIN_EVENTS = 1
    CONFIRMED = 2


class CoverageLevel(IntEnum):
    UNKNOWN = 0
    POST_MATCH_SCORES = 1
    POST_MATCH_SCORES_AND_SCORERS = 2
    POST_MATCH_FULL = 3
    LIVE_SCORES = 4
    LIVE_SCORES_AND_SCORERS = 5
    HYBRID_BASIC = 6
    HYBRID_SCORERS = 7
    LIVA_BASIC = 8
    LIVE_PREMIUM_EVENTS = 9
    HYBRID_STATS = 10
    LIVE_FULLL_BASIC = 11
    POST_MATCH_FULL_STATS = 12
    LIVE_FULL_STATS = 13
    POST_MATCH_COMPLETE = 14
    LIVE_COMPLETE = 15


class PictureFormat(StrEnum):
    SQUARE = "sq"


class PictureSize(IntEnum):
    W500 = 5
    W250 = 4
    W150 = 3
    W70 = 2
    W42 = 1


class StageType(IntEnum):
    KNOCK_OUT = 0
    GROUP = 1
    LEAGUE = 2
    UNKNOWN = 3


class LocaleDescription(BaseModel):
    locale: str = Field(..., alias="Locale")
    description: str | None = Field(None, alias="Description")


class RelatedContent(BaseModel):
    id_content: str | None = Field(None, alias="IdContent")
    locale: str = Field(..., alias="Locale")
    description: str = Field(..., alias="Description")
    date: datetime.datetime | None = Field(None, alias="Date")
    related_url: str = Field(..., alias="RelatedUrl")


class MediaContent(BaseModel):
    id_media_content: str | None = Field(None, alias="IdMediaContent")
    type: int | None = Field(None, alias="Type")
    image_url: str = Field(..., alias="ImageUrl")
    thumbnail_url: str = Field(..., alias="ThumbnailUrl")
    image_link_url: str | None = Field(None, alias="ImageLinkUrl")
    related_content: list[RelatedContent] | None = Field(None, alias="RelatedContent")


class Stadium(BaseModel):
    id_stadium: str = Field(..., alias="IdStadium")
    name: list[LocaleDescription] = Field(..., alias="Name")
    capacity: int | None = Field(None, alias="Capacity")
    web_address: str | None = Field(None, alias="WebAddress")
    built: str | None = Field(None, alias="Built")
    roof: bool | None = Field(None, alias="Roof")
    turf: int | None = Field(None, alias="Turf")
    id_city: str | None = Field(None, alias="IdCity")
    city_name: list[LocaleDescription] | None = Field(None, alias="CityName")
    id_country: str | None = Field(None, alias="IdCountry")
    postal_code: str | None = Field(None, alias="PostalCode")
    street: str | None = Field(None, alias="Street")
    email: str | None = Field(None, alias="Email")
    fax: str | None = Field(None, alias="Fax")
    phone: str | None = Field(None, alias="Phone")
    affiliation_country: str | None = Field(None, alias="AffiliationCountry")
    affiliation_region: str | None = Field(None, alias="AffiliationRegion")
    latitude: float | None = Field(None, alias="Latitude")
    longitude: float | None = Field(None, alias="Longitude")
    length: str | None = Field(None, alias="Length")
    width: str | None = Field(None, alias="Width")
    properties: dict | None = Field(None, alias="Properties")
    is_updatable: bool | None = Field(None, alias="IsUpdateable")


class Team(BaseModel):
    id: str = Field(..., alias="IdTeam")
    id_confederation: str | None = Field(None, alias="IdConfederation")
    active_status: ActiveStatus | None = Field(None, alias="ActiveStatus")
    type: TeamType | None = Field(None, alias="Type")
    age_type: AgeType | None = Field(None, alias="AgeType")
    football_type: FootballType | None = Field(None, alias="FootballType")
    gender: Gender | None = Field(None, alias="Gender")
    name: list[LocaleDescription] = Field(..., alias="Name")
    id_association: str | None = Field(None, alias="IdAssociation")
    id_city: str | None = Field(None, alias="IdCity")
    headquarters: str | None = Field(None, alias="Headquarters")
    training_center: str | None = Field(None, alias="TrainingCentre")
    official_site: str | None = Field(None, alias="OfficialSite")
    city: str | None = Field(None, alias="City")
    id_country: str | None = Field(None, alias="IdCountry")
    postal_code: str | None = Field(None, alias="PostalCode")
    region_name: str | None = Field(None, alias="RegionName")
    short_club_name: str | None = Field(None, alias="ShortClubName")
    abbreviation: str | None = Field(None, alias="Abbreviation")
    street: str | None = Field(None, alias="Street")
    foundation_year: int | None = Field(None, alias="FoundationYear")
    stadium: Stadium | None = Field(None, alias="Stadium")
    picture_url: str | None = Field(None, alias="PictureUrl")
    thumbnail_url: str | None = Field(None, alias="ThumbnailUrl")
    display_name: list[LocaleDescription] | None = Field(None, alias="DisplayName")
    content: list[MediaContent] | None = Field(None, alias="Content")
    properties: dict | None = Field(None, alias="Properties")
    is_updateable: bool | None = Field(None, alias="IsUpdateable")


class Language(BaseModel):
    language_code: str | None = Field(..., alias="LanguageCode")
    three_letter_language_code: str | None = Field(..., alias="ThreeLetterLanguageCode")
    descriptions: list[LocaleDescription] | None = Field(None, alias="Descriptions")
    enabled: bool | None = Field(None, alias="Enabled")
    can_be_used_to_send_push_notifications: bool | None = Field(None, alias="CanBeUsedToSendPushNotifications")
    can_be_used_as_web_api_language: bool | None = Field(None, alias="CanBeUsedAsWebApiLanguage")
    can_be_used_for_general_purposes: bool | None = Field(None, alias="CanBeUsedForGeneralPurposes")
    ca_be_used_for_newsletters: bool | None = Field(None, alias="CanBeUsedForNewsletters")
    web_api_culture_to_use: str | None = Field(None, alias="WebApiCultureToUse")
    properties: dict | None = Field(None, alias="Properties")
    is_updateable: bool | None = Field(None, alias="IsUpdateable")


class Country(BaseModel):
    id_country: str = Field(..., alias="IdCountry")
    name: str = Field(..., alias="Name")
    iso_3166_alpha_2: str | None = Field(None, alias="Iso3166Alpha2")
    iso_3166_alpha_3: str | None = Field(None, alias="Iso3166Alpha3")
    alias: list[LocaleDescription] | None = Field(None, alias="Alias")
    gracenote_code: str | None = Field(None, alias="GracenoteCode")
    stats_perform_id: str | None = Field(None, alias="StatsPerformId")
    properties: dict | None = Field(None, alias="Properties")
    is_updateable: bool | None = Field(None, alias="IsUpdateable")


class Competition(BaseModel):
    id_competition: str = Field(..., alias="IdCompetition")
    name: list[LocaleDescription] = Field(..., alias="Name")
    id_confederation: list[str] | None = Field(None, alias="IdConfederation")
    id_member_association: list[str] | None = Field(None, alias="IdMemberAssociation")
    id_owner: list[str] | None = Field(None, alias="IdOwner")
    gender: Gender | None = Field(None, alias="Gender")
    football_type: FootballType | None = Field(None, alias="FootballType")
    team_type: TeamType | None = Field(None, alias="TeamType")
    competition_type: CompetitionType | None = Field(None, alias="CompetitionType")
    age_type: AgeType | None = Field(None, alias="AgeType")
    display_order: int | None = Field(None, alias="DisplayOrder")
    properties: dict | None = Field(None, alias="Properties")
    is_updateable: bool | None = Field(None, alias="IsUpdateable")

    @field_validator("id_owner", mode="before")
    @classmethod
    def parse_comma_separated_string(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


class OrganizationMember(BaseModel):
    id_organization_member: str | None = Field(None, alias="IdOrganizationMember")
    id_confederation: str | None = Field(None, alias="IdConfederation")
    id_association: str | None = Field(None, alias="IdAssociation")
    display_name: list[LocaleDescription] | None = Field(None, alias="DisplayName")
    first_name: str | None = Field(None, alias="FirstName")
    last_name: str | None = Field(None, alias="LastName")
    date_of_birth: datetime.date | None = Field(None, alias="DateOfBirth")
    country: str | None = Field(None, alias="Country")
    role: ConfederationRole | None = Field(None, alias="Role")
    role_type_description: list[LocaleDescription] | None = Field(None, alias="RoleTypeDescription")
    role_begin_date: datetime.datetime | None = Field(None, alias="RoleBeginDate")
    role_end_date: datetime.datetime | None = Field(None, alias="RoleEndDate")
    organization_begin_date: datetime.datetime | None = Field(None, alias="OrganizationBeginDate")


class Confederation(BaseModel):
    id_confederation: str = Field(..., alias="IdConfederation")
    name: list[LocaleDescription] | None = Field(None, alias="Name")
    confederation_acronym: list[LocaleDescription] | None = Field(None, alias="ConfederationAcronym")
    picture_url: str | None = Field(None, alias="PictureUrl")
    dark_picture_url: str | None = Field(None, alias="DarkPictureUrl")
    thumbnail_picture_url: str | None = Field(None, alias="ThumbnailPictureUrl")
    description: list[LocaleDescription] | None = Field(None, alias="Description")
    web_site: str | None = Field(None, alias="WebSite")
    address: str | None = Field(None, alias="Address")
    address_localized: list[LocaleDescription] | None = Field(None, alias="AddressLocalized")
    city_name: str | None = Field(None, alias="CityName")
    city_name_localized: list[LocaleDescription] | None = Field(None, alias="CityNameLocalized")
    country: str | None = Field(None, alias="Country")
    contact_business_phone: str | None = Field(None, alias="ContactBusinessPhone")
    contact_email: str | None = Field(None, alias="ContactEmail")
    contact_fax: str | None = Field(None, alias="ContactFax")
    contact_media_communications_phone: str | None = Field(None, alias="ContactMediaCommunicationsPhone")
    location_picture: str | None = Field(None, alias="LocationPicture")
    order_position: int | None = Field(None, alias="OrderPosition")
    organization: list[OrganizationMember] | None = Field(None, alias="Organization")
    number_of_associations: int | None = Field(None, alias="NumberOfAssociations")
    properties: dict | None = Field(None, alias="Properties")
    is_updateable: bool | None = Field(None, alias="IsUpdateable")


class SeasonHostTeam(BaseModel):
    id_team: str | None = Field(None, alias="IdTeam")


class Season(BaseModel):
    id_season: str | None = Field(None, alias="IdSeason")
    name: list[LocaleDescription] = Field(..., alias="Name")
    short_name: list[LocaleDescription] | None = Field(None, alias="ShortName")
    abbreviation: str | None = Field(None, alias="Abbreviation")
    id_member_association: list[str] | None = Field(None, alias="IdMemberAssociation")
    id_confederation: list[str] | None = Field(None, alias="IdConfederation")
    id_competition: str = Field(..., alias="IdCompetition")
    start_date: datetime.datetime | None = Field(None, alias="StartDate")
    end_date: datetime.datetime | None = Field(None, alias="EndDate")
    picture_url: str | None = Field(None, alias="PictureUrl")
    mascot_picture_url: str | None = Field(None, alias="MascotPictureUrl")
    match_ball_picture_url: str | None = Field(None, alias="MatchBallPictureUrl")
    host_teams: list[SeasonHostTeam] | None = Field(None, alias="HostTeams")
    sport_type: FootballType | None = Field(None, alias="SportType")
    properties: dict | None = Field(None, alias="Properties")
    is_updateable: bool | None = Field(None, alias="IsUpdateable")


class WhereToWatchSource(BaseModel):
    id_channel: str | None = Field(None, alias="IdChannel")
    name: str | None = Field(None, alias="Name")
    logo: str | None = Field(None, alias="Logo")
    tv_channel_url: str | None = Field(None, alias="TvChannelUrl")
    i_os_url: str | None = Field(None, alias="IOsUrl")
    android_url: str | None = Field(None, alias="AndroidUrl")
    url: str | None = Field(None, alias="Url")
    language: str | None = Field(None, alias="Language")


class WhereToWatchMatchBasic(BaseModel):
    id_match: str | None = Field(None, alias="IdMatch")
    date: datetime.datetime | None = Field(None, alias="Date")
    sources: list[WhereToWatchSource] | None = Field(None, alias="Sources")


class WhereToWatchMatch(BaseModel):
    id_season: str | None = Field(None, alias="IdSeason")
    id_competition: str | None = Field(None, alias="IdCompetition")
    id_country: str | None = Field(None, alias="IdCountry")
    id_country_iso_3166_alpha_2: str | None = Field(None, alias="IdCountryIso3166Alpha2")
    country_name: list[LocaleDescription] | None = Field(None, alias="CountryName")
    matches: list[WhereToWatchMatchBasic] | None = Field(None, alias="Matches")


class CompetitionSeasonStatistics(BaseModel):
    id_season: str | None = Field(None, alias="IdSeason")
    name: list[LocaleDescription] | None = Field(None, alias="Name")
    start_date: datetime.datetime | None = Field(None, alias="StartDate")
    end_date: datetime.datetime | None = Field(None, alias="EndDate")
    picture_url: str | None = Field(None, alias="PictureUrl")
    matches_played: int | None = Field(None, alias="MatchesPlayed")
    qualified_teams: int | None = Field(None, alias="QualifiedTeams")
    goals_scored: int | None = Field(None, alias="GoalsScored")
    average_total_attempts: float | None = Field(None, alias="AverageTotalAttempts")
    yellow_cards_per_match: float | None = Field(None, alias="YellowCardsPerMatch")
    red_cards_per_match: float | None = Field(None, alias="RedCardsPerMatch")
    goals_per_match: float | None = Field(None, alias="GoalsPerMatch")
    average_attendance: int | None = Field(None, alias="AverageAttendance")


class CompetitionStatistics(BaseModel):
    id_competition: str | None = Field(None, alias="IdCompetition")
    season_stats: list[CompetitionSeasonStatistics] | None = Field(None, alias="SeasonStats")


class PossessionLastX(BaseModel):
    last_minutes: int | None = Field(None, alias="LastMinutes")
    away: str | None = Field(None, alias="Away")
    home: str | None = Field(None, alias="Home")


class PossessionInterval(BaseModel):
    length: int | None = Field(None, alias="Length")
    range: str = Field(..., alias="Range")
    away: str | None = Field(None, alias="Away")
    home: str | None = Field(None, alias="Home")
    middle: str | None = Field(None, alias="Middle")


class CompetitionMatchLegInfo(BaseModel):
    related_media: str | None = Field(None, alias="RelatedMedia")


class MatchOfficial(BaseModel):
    id_country: str | None = Field(None, alias="IdCountry")
    official_id: str | None = Field(None, alias="OfficialId")
    name_short: list[LocaleDescription] | None = Field(None, alias="NameShort")
    name: list[LocaleDescription] | None = Field(None, alias="Name")
    official_type: OfficialType | None = Field(None, alias="OfficialType")
    type_localized: list[LocaleDescription] | None = Field(None, alias="TypeLocalized")


class Possession(BaseModel):
    intervals: list[PossessionInterval] | None = Field(None, alias="Intervals")
    last_x: list[PossessionLastX] | None = Field(None, alias="LastX")
    overall_home: float | None = Field(None, alias="OverallHome")
    overall_away: float | None = Field(None, alias="OverallAway")


class MatchTeam(BaseModel):
    score: int | None = Field(None, alias="Score")
    side: str | None = Field(None, alias="Side")
    id_team: str | None = Field(None, alias="IdTeam")
    picture_url: str | None = Field(None, alias="PictureUrl")
    id_country: str | None = Field(None, alias="IdCountry")
    tactics: str | None = Field(None, alias="Tactics")
    team_type: TeamType | None = Field(None, alias="TeamType")
    age_type: AgeType | None = Field(None, alias="AgeType")
    team_name: list[LocaleDescription] | None = Field(None, alias="TeamName")
    abbreviation: str | None = Field(None, alias="Abbreviation")
    short_club_name: str | None = Field(None, alias="ShortClubName")
    football_type: FootballType | None = Field(None, alias="FootballType")
    gender: Gender | None = Field(None, alias="Gender")
    id_association: str | None = Field(None, alias="IdAssociation")


class WeatherConditions(BaseModel):
    humidity: str | None = Field(None, alias="Humidity")
    temperature: str | None = Field(None, alias="Temperature")
    wind_speed: str | None = Field(None, alias="WindSpeed")
    type: WeatherType | None = Field(None, alias="Type")
    type_localized: list[LocaleDescription] | None = Field(None, alias="TypeLocalized")


class CompetitionMatch(BaseModel):
    id_competition: str = Field(..., alias="IdCompetition")
    id_season: str = Field(..., alias="IdSeason")
    id_stage: str = Field(..., alias="IdStage")
    id_group: str | None = Field(None, alias="IdGroup")
    weather: WeatherConditions | None = Field(None, alias="Weather")
    attendance: str | None = Field(None, alias="Attendance")
    id_match: str | None = Field(None, alias="IdMatch")
    match_day: str | None = Field(None, alias="MatchDay")
    stage_name: list[LocaleDescription] | None = Field(None, alias="StageName")
    group_name: list[LocaleDescription] | None = Field(None, alias="GroupName")
    competition_name: list[LocaleDescription] | None = Field(None, alias="CompetitionName")
    season_name: list[LocaleDescription] | None = Field(None, alias="SeasonName")
    season_short_name: list[LocaleDescription] | None = Field(None, alias="SeasonShortName")
    date: datetime.datetime | None = Field(None, alias="Date")
    local_date: datetime.datetime | None = Field(None, alias="LocalDate")
    home: MatchTeam | None = Field(None, alias="Home")
    away: MatchTeam | None = Field(None, alias="Away")
    home_team_score: int | None = Field(None, alias="HomeTeamScore")
    away_team_score: int | None = Field(None, alias="AwayTeamScore")
    aggregate_home_team_score: int | None = Field(None, alias="AggregateHomeTeamScore")
    aggregate_away_team_score: int | None = Field(None, alias="AggregateAwayTeamScore")
    home_team_penalty_score: int | None = Field(None, alias="HomeTeamPenaltyScore")
    away_team_penalty_score: int | None = Field(None, alias="AwayTeamPenaltyScore")
    last_period_update: datetime.datetime | None = Field(None, alias="LastPeriodUpdate")
    leg: str | None = Field(None, alias="Leg")
    is_home_match: bool | None = Field(None, alias="IsHomeMatch")
    stadium: Stadium | None = Field(None, alias="Stadium")
    is_ticket_sales_allowed: bool | None = Field(None, alias="IsTicketSalesAllowed")
    match_time: str | None = Field(None, alias="MatchTime")
    second_half_time: int | None = Field(None, alias="SecondHalfTime")
    first_half_time: int | None = Field(None, alias="FirstHalfTime")
    first_half_extra_time: int | None = Field(None, alias="FirstHalfExtraTime")
    second_half_extra_time: int | None = Field(None, alias="SecondHalfExtraTime")
    winner: str | None = Field(None, alias="Winner")
    match_report_url: str | None = Field(None, alias="MatchReportUrl")
    place_holder_a: str | None = Field(None, alias="PlaceHolderA")
    place_holder_b: str | None = Field(None, alias="PlaceHolderB")
    ball_possession: Possession | None = Field(None, alias="BallPossession")
    officials: list[MatchOfficial] | None = Field(None, alias="Officials")
    match_status: MatchStatus | None = Field(None, alias="MatchStatus")
    result_type: ResultType | None = Field(None, alias="ResultType")
    match_number: int | None = Field(None, alias="MatchNumber")
    time_defined: bool | None = Field(None, alias="TimeDefined")
    officiality_status: int | None = Field(None, alias="OfficialityStatus")
    match_leg_info: CompetitionMatchLegInfo | None = Field(None, alias="MatchLegInfo")
    properties: dict | None = Field(None, alias="Properties")
    is_updateable: bool | None = Field(None, alias="IsUpdateable")


class PictureInfo(BaseModel):
    id: str | None = Field(None, alias="Id")
    picture_url: str | None = Field(None, alias="PictureUrl")


class LiveMatchStaff(BaseModel):
    id_staff: str = Field(..., alias="IdStaff")
    id_country: str | None = Field(None, alias="IdCountry")
    role: StaffRole | None = Field(None, alias="Role")
    picture_url: str | None = Field(None, alias="PictureUrl")
    name: list[LocaleDescription] | None = Field(None, alias="Name")
    alias: list[LocaleDescription] | None = Field(None, alias="Alias")


class Substitution(BaseModel):
    id_event: str | None = Field(None, alias="IdEvent")
    period: Period | None = Field(None, alias="Period")
    reason: SubstitutionReason | None = Field(None, alias="Reason")
    substitution_position: Position | None = Field(None, alias="SubstitutePosition")
    id_player_off: str = Field(..., alias="IdPlayerOff")
    id_player_on: str = Field(..., alias="IdPlayerOn")
    player_off_name: list[LocaleDescription] | None = Field(None, alias="PlayerOffName")
    player_on_name: list[LocaleDescription] | None = Field(None, alias="PlayerOnName")
    minute: str | None = Field(None, alias="Minute")
    id_team: str | None = Field(None, alias="IdTeam")


class Goal(BaseModel):
    type: GoalType | None = Field(None, alias="Type")
    id_player: str | None = Field(None, alias="IdPlayer")
    id_assist_player: str | None = Field(None, alias="IdAssistPlayer")
    minute: str | None = Field(None, alias="Minute")
    period: Period | None = Field(None, alias="Period")
    id_goal: str | None = Field(None, alias="IdGoal")
    id_team: str | None = Field(None, alias="IdTeam")


class Booking(BaseModel):
    card: CardType = Field(..., alias="Card")
    period: Period | None = Field(None, alias="Period")
    id_event: str | None = Field(None, alias="IdEvent")
    event_number: str | None = Field(None, alias="EventNumber")
    id_player: str | None = Field(None, alias="IdPlayer")
    id_coach: str | None = Field(None, alias="IdCoach")
    id_staff: str | None = Field(None, alias="IdStaff")
    id_team: str | None = Field(None, alias="IdTeam")
    minute: str | None = Field(None, alias="Minute")
    reason: str | None = Field(None, alias="Reason")


class LiveMatchPlayer(BaseModel):
    id_player: str | None = Field(None, alias="IdPlayer")
    id_team: str | None = Field(None, alias="IdTeam")
    shirt_number: int | None = Field(None, alias="ShirtNumber")
    status: LiveMatchPlyerStatus | None = Field(None, alias="Status")
    special_status: PlayerSpecialStatus | None = Field(None, alias="SpecialStatus")
    captain: bool | None = Field(None, alias="Captain")
    player_name: list[LocaleDescription] | None = Field(None, alias="PlayerName")
    short_name: list[LocaleDescription] | None = Field(None, alias="ShortName")
    position: Position | None = Field(None, alias="Position")
    player_picture: PictureInfo | None = Field(None, alias="PlayerPicture")
    field_status: PlayerFieldStatus | None = Field(None, alias="FieldStatus")
    lineup_x: float | None = Field(None, alias="LineupX")
    lineup_y: float | None = Field(None, alias="LineupY")


class LiveMatchCoach(BaseModel):
    id_coach: str | None = Field(None, alias="IdCoach")
    id_country: str | None = Field(None, alias="IdCountry")
    pricture_url: str | None = Field(None, alias="PictureUrl")
    name: list[LocaleDescription] | None = Field(None, alias="Name")
    alias: list[LocaleDescription] | None = Field(None, alias="Alias")
    role: LiveMatchCoachRole | None = Field(None, alias="Role")
    special_status: PlayerSpecialStatus | None = Field(None, alias="SpecialStatus")


class LiveMatchTeam(BaseModel):
    score: int | None = Field(None, alias="Score")
    side: str | None = Field(None, alias="Side")
    id_team: str | None = Field(None, alias="IdTeam")
    picture_url: str | None = Field(None, alias="PictureUrl")
    id_country: str | None = Field(None, alias="IdCountry")
    team_type: TeamType | None = Field(None, alias="TeamType")
    age_type: AgeType | None = Field(None, alias="AgeType")
    tactics: str | None = Field(None, alias="Tactics")
    team_name: list[LocaleDescription] | None = Field(None, alias="TeamName")
    abbreviation: str | None = Field(None, alias="Abbreviation")
    coaches: list[LiveMatchCoach] | None = Field(None, alias="Coaches")
    players: list[LiveMatchPlayer] | None = Field(None, alias="Players")
    bookings: list[Booking] | None = Field(None, alias="Bookings")
    goals: list[Goal] | None = Field(None, alias="Goals")
    substitutions: list[Substitution] | None = Field(None, alias="Substitutions")
    staff: list[LiveMatchStaff] | None = Field(None, alias="Staffs")
    football_type: FootballType | None = Field(None, alias="FootballType")
    gender: Gender | None = Field(None, alias="Gender")
    id_association: str | None = Field(None, alias="IdAssociation")
    short_club_name: str | None = Field(None, alias="ShortClubName")


class LiveMatch(BaseModel):
    id_match: str = Field(..., alias="IdMatch")
    id_stage: str = Field(..., alias="IdStage")
    id_group: str | None = Field(None, alias="IdGroup")
    id_season: str = Field(..., alias="IdSeason")
    coverage_level: CoverageLevel | None = Field(None, alias="CoverageLevel")
    id_competition: str = Field(..., alias="IdCompetition")
    competition_name: list[LocaleDescription] | None = Field(None, alias="CompetitionName")
    season_name: list[LocaleDescription] | None = Field(None, alias="SeasonName")
    season_short_name: list[LocaleDescription] | None = Field(None, alias="SeasonShortName")
    stadium: Stadium | None = Field(None, alias="Stadium")
    result_type: ResultType | None = Field(None, alias="ResultType")
    match_day: str | None = Field(None, alias="MatchDay")
    match_number: int | None = Field(None, alias="MatchNumber")
    home_team_penalty_score: int | None = Field(None, alias="HomeTeamPenaltyScore")
    away_team_penalty_score: int | None = Field(None, alias="AwayTeamPenaltyScore")
    aggregate_home_team_score: int | None = Field(None, alias="AggregateHomeTeamScore")
    aggregate_away_team_score: int | None = Field(None, alias="AggregateAwayTeamScore")
    weather: WeatherConditions | None = Field(None, alias="Weather")
    attendance: str | None = Field(None, alias="Attendance")
    date: datetime.datetime | None = Field(None, alias="Date")
    local_date: datetime.datetime | None = Field(None, alias="LocalDate")
    match_time: str | None = Field(None, alias="MatchTime")
    second_half_time: int | None = Field(None, alias="SecondHalfTime")
    first_half_time: int | None = Field(None, alias="FirstHalfTime")
    first_half_extra_time: int | None = Field(None, alias="FirstHalfExtraTime")
    second_half_extra_time: int | None = Field(None, alias="SecondHalfExtraTime")
    winner: str | None = Field(None, alias="Winner")
    period: Period | None = Field(None, alias="Period")
    home_team: LiveMatchTeam | None = Field(None, alias="HomeTeam")
    away_team: LiveMatchTeam | None = Field(None, alias="AwayTeam")
    ball_possession: Possession | None = Field(None, alias="BallPossession")
    territorial_possession: Possession | None = Field(None, alias="TerritorialPossesion")
    territorial_third_possession: Possession | None = Field(None, alias="TerritorialThirdPossesion")
    officials: list[MatchOfficial] | None = Field(None, alias="Officials")
    match_status: MatchStatus | None = Field(None, alias="MatchStatus")
    group_name: list[LocaleDescription] | None = Field(None, alias="GroupName")
    stage_name: list[LocaleDescription] | None = Field(None, alias="StageName")
    officiality_status: OfficialityStatus | None = Field(None, alias="OfficialityStatus")
    time_defined: bool | None = Field(None, alias="TimeDefined")
    properties: dict | None = Field(None, alias="Properties")
    is_updateable: bool | None = Field(None, alias="IsUpdateable")


class Stage(BaseModel):
    id_stage: str | None = Field(None, alias="IdStage")
    name: list[LocaleDescription] | None = Field(None, alias="Name")
    id_season: str | None = Field(None, alias="IdSeason")
    season_name: list[LocaleDescription] | None = Field(None, alias="SeasonName")
    id_competition: str | None = Field(None, alias="IdCompetition")
    stage_level: int | None = Field(None, alias="StageLevel")
    start_date: datetime.datetime | None = Field(None, alias="StartDate")
    end_date: datetime.datetime | None = Field(None, alias="EndDate")
    type: StageType | None = Field(None, alias="Type")
    sequence_order: int | None = Field(None, alias="SequenceOrder")
    properties: dict | None = Field(None, alias="Properties")
    is_updateable: bool | None = Field(None, alias="IsUpdateable")


class IApiMultipleResultsPaged[T](BaseModel):
    continuation_token: str | None = Field(None, alias="ContinuationToken")
    continuation_hash: str | None = Field(None, alias="ContinuationHash")
    results: list[T] | None = Field(None, alias="Results")


class FifaClient:
    def __init__(self, base_url: str = "https://api.fifa.com/", timeout: float = 10.0):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout
        self._client: httpx2.AsyncClient | None = None

    async def __aenter__(self):
        throttled_transport = RateLimitedAsyncTransport(requests_per_second=4.0)
        self._client = httpx2.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=throttled_transport,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx2.AsyncClient:
        """Prüft den Zustand und gibt den Client zurück."""
        if self._client is None:
            raise RuntimeError("Der Client muss innerhalb eines 'async with'-Blocks genutzt werden!")
        return self._client

    async def _get_paginated_results[T](
        self,
        url: str,
        adapter: TypeAdapter,
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
        **kwargs,
    ) -> AsyncGenerator[T]:
        all_results: list[T] = list()
        if params is None:
            params = {}
        if headers is None:
            headers = {}
        continuation_hash: str | None = None
        continuation_token: str | None = None

        while True:
            if continuation_hash:
                params["continuationhash"] = continuation_hash
            else:
                params.pop("continuationhash", None)

            if continuation_token:
                headers["x-mdp-continuation-token"] = continuation_token
            else:
                headers.pop("x-mdp-continuation-token", None)

            response: Response = await self.client.get(url=url, params=params, headers=headers, **kwargs)
            response.raise_for_status()
            page = adapter.validate_python(response.json())
            if not page.results:
                break
            for result in page.results:
                yield result
            # all_results.extend(page.results)

            if page.continuation_token or page.continuation_hash:
                continuation_token = page.continuation_token
                continuation_hash = page.continuation_hash
            else:
                break
        # return all_results

    async def get_all_teams(
        self,
        gender: Gender | None = Gender.MALE,
        team_type: TeamType | None = TeamType.NATIONAL,
        age_type: AgeType | None = AgeType.MAIN,
        football_type: FootballType | None = FootballType.FOOTBALL,
        count: int | None = None,
        language: str | None = "en-US",
        include_inheritance: bool | None = None,
    ) -> AsyncGenerator[Team]:
        headers = {}
        params: dict[str, str | int | bool] = {}
        if gender:
            params["gender"] = gender.value
        if team_type:
            params["teamType"] = team_type.value
        if age_type:
            params["ageType"] = age_type.value
        if football_type:
            params["footballType"] = football_type.value
        if count:
            params["count"] = count
        if language:
            params["language"] = language
        if include_inheritance is not None:
            params["includeInheritance"] = "true" if include_inheritance else "false"

        adapter = TypeAdapter(IApiMultipleResultsPaged[Team])

        async for team in self._get_paginated_results(
            url="api/v3/teams/all", adapter=adapter, params=params, headers=headers
        ):
            yield team

    async def get_languages(
        self,
        language: str | None = None,
    ) -> AsyncGenerator[Language]:
        headers = {}
        params: dict[str, str | int | bool] = {}
        if language:
            params["language"] = language

        adapter = TypeAdapter(IApiMultipleResultsPaged[Language])

        async for language in self._get_paginated_results(
            url="api/v3/languages",
            adapter=adapter,
            params=params,
            headers=headers,
        ):
            yield language

    async def get_countries(
        self,
        id_client: str | None = None,
        count: int | None = None,
        language: str | None = None,
    ) -> AsyncGenerator[Country]:
        headers = {}
        params: dict[str, str | int | bool] = {}
        if id_client:
            params["idClient"] = id_client
        if count:
            params["count"] = count
        if language:
            params["language"] = language

        adapter = TypeAdapter(IApiMultipleResultsPaged[Country])

        async for country in self._get_paginated_results(
            url="api/v3/countries",
            adapter=adapter,
            params=params,
            headers=headers,
        ):
            yield country

    async def get_competitions(
        self,
        country_id: str | None = None,
        id_client: str | None = None,
        count: int | None = None,
        language: str | None = None,
    ) -> AsyncGenerator[Competition]:
        headers = {}
        params: dict[str, str | int | bool] = {}
        if country_id:
            params["countryId"] = country_id
        if id_client:
            params["idClient"] = id_client
        if count:
            params["count"] = count
        if language:
            params["language"] = language

        adapter = TypeAdapter(IApiMultipleResultsPaged[Competition])

        async for competition in self._get_paginated_results(
            url="api/v3/competitions",
            adapter=adapter,
            params=params,
            headers=headers,
        ):
            yield competition

    async def get_seasons_by_team(
        self,
        id_team: str,
        id_client: str | None = None,
        count: int | None = None,
        language: str | None = None,
    ) -> AsyncGenerator[Season]:
        headers = {}
        params: dict[str, str | int | bool] = {}
        if id_client:
            params["idClient"] = id_client
        if count:
            params["count"] = count
        if language:
            params["language"] = language

        adapter = TypeAdapter(IApiMultipleResultsPaged[Season])

        async for season in self._get_paginated_results(
            url=f"api/v3/teams/seasons/{id_team}",
            adapter=adapter,
            params=params,
            headers=headers,
        ):
            yield season

    async def search_competitions(
        self,
        name: str,
        id_client: str | None = None,
        count: int | None = None,
        language: str | None = None,
    ) -> AsyncGenerator[Competition]:
        headers = {}
        params: dict[str, str | int | bool] = {"name": name}

        if id_client:
            params["idClient"] = id_client
        if count:
            params["count"] = count
        if language:
            params["language"] = language

        adapter = TypeAdapter(IApiMultipleResultsPaged[Competition])

        async for competition in self._get_paginated_results(
            url="api/v3/competitions/search",
            adapter=adapter,
            params=params,
            headers=headers,
        ):
            yield competition

    async def get_competitions_all(
        self,
        owner: Owner | None = None,
        football_type: FootballType | None = None,
        gender: Gender | None = None,
        competition_type: CompetitionType | None = None,
        age_type: AgeType | None = None,
        id_client: str | None = None,
        count: int | None = None,
        language: str | None = None,
    ) -> AsyncGenerator[Competition]:
        headers = {}
        params: dict[str, str | int | bool] = {}

        if owner:
            params["owner"] = owner
        if football_type:
            params["footballType"] = football_type.value
        if gender:
            params["gender"] = gender.value
        if competition_type:
            params["type"] = competition_type.value
        if age_type:
            params["ageType"] = age_type.value
        if id_client:
            params["idClient"] = id_client
        if count:
            params["count"] = count
        if language:
            params["language"] = language

        adapter = TypeAdapter(IApiMultipleResultsPaged[Competition])

        async for competition in self._get_paginated_results(
            url="api/v3/competitions/all",
            adapter=adapter,
            params=params,
            headers=headers,
        ):
            yield competition

    async def get_competition_by_id(
        self,
        id_competition: str,
        id_client: str | None = None,
        language: str | None = None,
    ) -> Competition:
        headers = {}
        params: dict[str, str | int | bool] = {}
        if id_client:
            params["idClient"] = id_client
        if language:
            params["language"] = language

        adapter = TypeAdapter(Competition)

        response: Response = await self.client.get(
            url=f"api/v3/competitions/{id_competition}", params=params, headers=headers
        )
        response.raise_for_status()

        return adapter.validate_python(response.json())

    async def get_confederations(
        self,
        id_client: str | None = None,
        language: str | None = None,
    ) -> AsyncGenerator[Confederation]:
        headers = {}
        params: dict[str, str | int | bool] = {}
        if id_client:
            params["idClient"] = id_client
        if language:
            params["language"] = language

        adapter = TypeAdapter(IApiMultipleResultsPaged[Confederation])

        async for confederation in self._get_paginated_results(
            url="api/v3/confederations",
            adapter=adapter,
            params=params,
            headers=headers,
        ):
            yield confederation

    async def get_confederation_by_id(
        self,
        confederation_id: str,
        id_client: str | None = None,
        language: str | None = None,
    ) -> Confederation:
        headers = {}
        params: dict[str, str | int | bool] = {}
        if id_client:
            params["idClient"] = id_client
        if language:
            params["language"] = language

        adapter = TypeAdapter(Confederation)

        response: Response = await self.client.get(
            url=f"api/v3/confederations/{confederation_id}",
            params=params,
            headers=headers,
        )
        response.raise_for_status()

        return adapter.validate_python(response.json())

    async def search_season(
        self,
        name: str,
        count: int | None = None,
        language: str | None = None,
    ) -> AsyncGenerator[Season]:
        headers = {}
        params: dict[str, str | int | bool] = {"name": name}

        if count:
            params["count"] = count
        if language:
            params["language"] = language

        adapter = TypeAdapter(IApiMultipleResultsPaged[Season])

        async for season in self._get_paginated_results(
            url="api/v3/seasons/search",
            adapter=adapter,
            params=params,
            headers=headers,
        ):
            yield season

    async def get_season_by_id(
        self,
        season_id: str,
        id_client: str | None = None,
        language: str | None = None,
    ) -> Season | None:
        headers = {}
        params: dict[str, str | int | bool] = {}
        if id_client:
            params["idClient"] = id_client
        if language:
            params["language"] = language

        adapter = TypeAdapter(Season)

        response: Response = await self.client.get(
            url=f"api/v3/seasons/{season_id}",
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        try:
            return adapter.validate_python(response.json())
        except ValidationError as e:
            logger.exception(
                f"Error validating response: {e}\nRaw Response: {response.json()}",
                exc_info=True,
            )

    async def get_picture_by_url(
        self,
        url: str,
        format: PictureFormat = PictureFormat.SQUARE,
        size: PictureSize = PictureSize.W500,
    ) -> ImageFile | None:
        url = url.format(format=format.value, size=size.value)
        try:
            response: Response = await self.client.get(url)
            response.raise_for_status()

            return Image.open(io.BytesIO(response.content))
        except Exception as e:
            logger.error(f"Error fetching picture from URL {url}: {e}")

    async def get_pictures_by_urls(
        self,
        urls: list[str],
        format: PictureFormat = PictureFormat.SQUARE,
        size: PictureSize = PictureSize.W500,
    ) -> AsyncGenerator[ImageFile]:
        for url in urls:
            try:
                response: Response = await self.client.get(url.format(format=format.value, size=size.value))
                response.raise_for_status()

                yield Image.open(io.BytesIO(response.content))
            except Exception as e:
                logger.error(f"Error fetching picture from URL {url}: {e}")

    async def get_competitionstatistics_by_competition_id(
        self,
        competition_id: str,
        id_client: str | None = None,
        language: str | None = None,
    ) -> CompetitionStatistics:
        headers = {}
        params: dict[str, str | int | bool] = {}
        if id_client:
            params["idClient"] = id_client
        if language:
            params["language"] = language

        adapter = TypeAdapter(CompetitionStatistics)

        response: Response = await self.client.get(
            url=f"api/v3/competitionstatistics/competition/{competition_id}",
            params=params,
            headers=headers,
        )
        response.raise_for_status()

        return adapter.validate_python(response.json())

    async def get_competition_seasons(self, competition_id: str) -> AsyncGenerator[Season]:
        comp_stats = await self.get_competitionstatistics_by_competition_id(competition_id)
        if not comp_stats or not comp_stats.season_stats:
            return
        for season_stat in comp_stats.season_stats:
            if not season_stat.id_season:
                continue
            season = await self.get_season_by_id(season_stat.id_season)
            if not season:
                continue
            yield season

    async def get_matches(
        self,
        id_season: str | None = None,
        id_competition: str | None = None,
        id_stage: str | None = None,
        id_stadium: str | None = None,
        id_team: str | None = None,
        match_day: datetime.date | None = None,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
        id_client: str | None = None,
        language: str | None = None,
        count: int | None = None,
    ) -> AsyncGenerator[CompetitionMatch]:
        headers = {}
        params: dict[str, str | int | bool] = {}

        if id_season:
            params["idSeason"] = id_season
        if id_competition:
            params["idCompetition"] = id_competition
        if id_stage:
            params["idStage"] = id_stage
        if id_stadium:
            params["idStadium"] = id_stadium
        if id_team:
            params["idTeam"] = id_team
        if match_day:
            params["matchDay"] = match_day.strftime("%d.%m.%Y")
        if start:
            params["from"] = start.strftime("%Y-%m-%d")
        if end:
            params["to"] = end.strftime("%Y-%m-%d")
        if id_client:
            params["idClient"] = id_client
        if language:
            params["language"] = language
        if count:
            params["count"] = count
        if language:
            params["language"] = language

        adapter = TypeAdapter(IApiMultipleResultsPaged[CompetitionMatch])

        async for match in self._get_paginated_results(
            url="api/v3/calendar/matches",
            adapter=adapter,
            params=params,
            headers=headers,
        ):
            yield match

    async def get_stages(
        self,
        id_season: str | None = None,
        id_competition: str | None = None,
        id_client: str | None = None,
        language: str | None = None,
        count: int | None = None,
    ) -> AsyncGenerator[Stage]:
        headers = {}
        params: dict[str, str | int | bool] = {}

        if id_season:
            params["idSeason"] = id_season
        if id_competition:
            params["idCompetition"] = id_competition
        if id_client:
            params["idClient"] = id_client
        if language:
            params["language"] = language
        if count:
            params["count"] = count
        if language:
            params["language"] = language

        adapter = TypeAdapter(IApiMultipleResultsPaged[Stage])

        async for stage in self._get_paginated_results(
            url="api/v3/stages",
            adapter=adapter,
            params=params,
            headers=headers,
        ):
            yield stage

    async def get_live_match_by_id(
        self,
        match_id: str,
        id_client: str | None = None,
        language: str | None = "en-US",
    ) -> LiveMatch | None:
        headers = {}
        params: dict[str, str | int | bool] = {}
        if id_client:
            params["idClient"] = id_client
        if language:
            params["language"] = language

        adapter = TypeAdapter(LiveMatch)

        response: Response = await self.client.get(
            url=f"api/v3/live/football/{match_id}",
            params=params,
            headers=headers,
        )

        try:
            response.raise_for_status()
            return adapter.validate_python(response.json())
        except Exception as e:
            logger.error(f"Error fetching live match by ID {match_id}: {e}")
