import asyncio
import collections
import datetime
import io
import logging

from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from PIL.ImageFile import ImageFile

from sports.constants import (
    ESPN_NFL_COMPETITION_EXTERNAL_ID,
    ESPN_NFL_COMPETITION_NAME,
    ESPN_STATUS_MAP,
    ESPN_TO_NFLVERSE_TEAM_ABBREVIATIONS,
    FIFA_GENDER_MAP,
    FIFA_STAGE_TYPE_MAP,
    FIFA_STATUS_MAP,
    NFL_STAGE_BLUEPRINTS,
    NFL_TEAM_COUNT,
    nfl_stage_external_id,
    nfl_stage_key,
    nflverse_stage_key,
)
from sports.integrations.espn import EspnClient
from sports.integrations.fifa import (
    FifaClient,
    PictureFormat,
    PictureSize,
    TeamType,
)
from sports.integrations.nflverse import NflverseClient
from sports.models import (
    Competition,
    CompetitionMapping,
    Gender,
    Match,
    MatchMapping,
)
from sports.models import MatchStatus as DjangoMatchStatus
from sports.models import (
    Season,
    SeasonMapping,
    Sport,
    SportsProvider,
    Stage,
    StageMapping,
    StageType,
    Team,
    TeamMapping,
)

logger = logging.getLogger(__name__)


def extract_name(locale_list: list) -> str:
    if not locale_list:
        return "Unknown Team"
    for item in locale_list:
        if item.locale in ["en-GB", "en-US"]:
            return item.description or "Unknown Team"
    return locale_list[0].description or "Unknown Team"


def _process_and_format_image(pil_img, team_id: str, team_name: str) -> ContentFile:
    out_buffer = io.BytesIO()
    pil_img.save(out_buffer, format="PNG")
    return ContentFile(
        out_buffer.getvalue(),
        name=f"{team_id}_{slugify(team_name)}.png",
    )


async def ingest_all_fifa_competitions(sport: Sport = Sport.SOCCER):
    async with FifaClient() as client:
        existing_mappings = {
            ext_id
            async for ext_id in CompetitionMapping.objects.filter(
                provider=SportsProvider.FIFA,
            ).values_list("external_id", flat=True)
        }

        async for api_comp in client.get_competitions_all():
            if not api_comp.id_competition or api_comp.id_competition in existing_mappings:
                continue

            comp_name = extract_name(api_comp.name)

            new_comp = await Competition.objects.acreate(
                name=comp_name,
                sport=sport,
                is_featured=False,
                gender=FIFA_GENDER_MAP.get(api_comp.gender, Gender.OTHER),
            )

            await CompetitionMapping.objects.acreate(
                provider=SportsProvider.FIFA,
                external_id=api_comp.id_competition,
                competition=new_comp,
            )
            existing_mappings.add(api_comp.id_competition)
            logger.info(f"Created competition {new_comp.name} with ID {new_comp.id}")


async def ingest_fifa_national_teams(sport: Sport = Sport.SOCCER):
    async with FifaClient() as client:
        team_mapping_cache = {
            tm.external_id: tm
            async for tm in TeamMapping.objects.filter(provider=SportsProvider.FIFA).select_related("team")
        }

        async for api_team in client.get_all_teams(gender=None, team_type=TeamType.NATIONAL):
            team_name = extract_name(api_team.name)
            if not api_team.id:
                logger.warning(f"Team {team_name} has no ID, skipping.")
                continue

            logo_url = (
                api_team.picture_url.format(
                    format=PictureFormat.SQUARE,
                    size=PictureSize.W500,
                )
                if api_team.picture_url
                else None
            )
            target_gender = FIFA_GENDER_MAP.get(api_team.gender, Gender.OTHER)

            try:
                team_mapping = team_mapping_cache.get(api_team.id)
                is_new = team_mapping is None

                db_team = team_mapping.team if team_mapping else Team(sport=sport)

                metadata_changed = db_team.name != team_name or db_team.gender != target_gender
                logo_changed = is_new or db_team.logo_url != logo_url

                if not (is_new or metadata_changed or logo_changed):
                    logger.debug(f"Team {db_team.name} has no data modifications, skipping.")
                    continue

                if is_new:
                    logger.info(f"Seeding brand-new team entry: {team_name}")
                else:
                    logger.info(f"Data drift detected. Updating team metadata: {db_team.name} -> {team_name}")

                db_team.name = team_name
                db_team.logo_url = logo_url
                db_team.gender = target_gender

                # `logo_changed`, not `logo_url` twice: re-saving an unchanged
                # logo does not overwrite the old file, it writes a new one with
                # a random suffix appended, so every unrelated metadata edit used
                # to leave another orphan behind in team_logos/.
                if logo_url and logo_changed:
                    image_response: ImageFile | None = await client.get_picture_by_url(logo_url)
                    if image_response:
                        try:
                            with image_response as pil_img:
                                logo_file: ContentFile = await asyncio.to_thread(
                                    _process_and_format_image,
                                    pil_img,
                                    api_team.id,
                                    team_name,
                                )
                                await asyncio.to_thread(
                                    db_team.logo.save,
                                    logo_file.name,
                                    logo_file,
                                    save=False,
                                )
                        except Exception as e:
                            logger.error(f"Error processing image for {team_name}: {e}")
                await db_team.asave()

                if is_new:
                    await TeamMapping.objects.acreate(
                        provider=SportsProvider.FIFA,
                        external_id=api_team.id,
                        team=db_team,
                    )
                    logger.info(f"Created team {db_team.name} with ID {db_team.id}")
            except Exception as e:
                logger.error(f"Error creating team {team_name}: {e}")


async def ingest_fifa_seasons():
    season_mapping_cache = {
        sm.external_id: sm
        async for sm in SeasonMapping.objects.filter(provider=SportsProvider.FIFA)
        .select_related("season")
        .aiterator()
    }

    async with FifaClient() as client:
        async for comp_mapping in (
            CompetitionMapping.objects.filter(competition__is_featured=True, provider=SportsProvider.FIFA)
            .select_related("competition")
            .aiterator()
        ):
            async for api_season in client.get_competition_seasons(competition_id=comp_mapping.external_id):
                if not api_season or not api_season.id_season:
                    continue
                season_name = extract_name(api_season.name)
                try:
                    season_mapping = season_mapping_cache.get(api_season.id_season)

                    db_season = season_mapping.season if season_mapping else Season()
                    db_season.name = season_name
                    db_season.year = api_season.start_date.year
                    db_season.competition_id = comp_mapping.competition_id
                    db_season.is_active = api_season.start_date <= timezone.now() <= api_season.end_date

                    await db_season.asave()

                    if not season_mapping:
                        await SeasonMapping.objects.acreate(
                            external_id=api_season.id_season,
                            provider=SportsProvider.FIFA,
                            season=db_season,
                        )
                        logger.info(f"Created season mapping for {season_name}")
                except Exception as e:
                    logger.exception(f"Error creating season {season_name}: {e}")


async def ingest_fifa_stages():
    async with FifaClient() as client:
        async for season_mapping in (
            SeasonMapping.objects.select_related("season").filter(season__is_active=True).aiterator()
        ):
            api_stages = [stage async for stage in client.get_stages(id_season=season_mapping.external_id)]
            if not api_stages:
                continue
            logger.info(f"Ingesting {len(api_stages)} stages for season {season_mapping.season.name}")
            ext_stage_ids = {s.id_stage for s in api_stages if s.id_stage}
            stage_mapping_cache = {
                sm.external_id: sm
                async for sm in StageMapping.objects.filter(
                    external_id__in=ext_stage_ids, provider=SportsProvider.FIFA
                )
                .select_related("stage")
                .aiterator()
            }

            for api_stage in api_stages:
                if not api_stage.id_stage:
                    continue
                logger.info(f"Processing stage {api_stage.name}: {api_stage}")
                stage_mapping = stage_mapping_cache.get(api_stage.id_stage)
                db_stage = stage_mapping.stage if stage_mapping else Stage()
                db_stage.name = extract_name(api_stage.name)
                db_stage.season_id = season_mapping.season_id
                db_stage.level = api_stage.sequence_order
                db_stage.stage_type = FIFA_STAGE_TYPE_MAP.get(api_stage.type, StageType.OTHER)
                await db_stage.asave()

                if not stage_mapping:
                    await StageMapping.objects.acreate(
                        external_id=api_stage.id_stage,
                        provider=SportsProvider.FIFA,
                        stage=db_stage,
                    )
                    logger.info(f"Created stage mapping for {db_stage.name}")


async def ingest_upcoming_matches(
    timedelta: datetime.timedelta = datetime.timedelta(days=14),
):
    competition_cache: dict[int, str] = {}

    async with FifaClient() as client:
        async for season_mapping in (
            SeasonMapping.objects.select_related("season")
            .filter(
                season__competition__is_featured=True,
                season__is_active=True,
                season__competition__sport=Sport.SOCCER,
                provider=SportsProvider.FIFA,
            )
            .aiterator()
        ):
            comp_id = season_mapping.season.competition_id

            if comp_id not in competition_cache:
                comp_map = await CompetitionMapping.objects.filter(
                    competition_id=comp_id,
                    provider=SportsProvider.FIFA,
                ).afirst()
                if not comp_map:
                    logger.error(f"Competition mapping not found for season {comp_id}")
                    continue
                competition_cache[comp_id] = comp_map.external_id

            api_matches = [
                match
                async for match in client.get_matches(
                    id_competition=competition_cache[comp_id],
                    id_season=season_mapping.external_id,
                    # start=timezone.now().date(),
                    end=(timezone.now() + timedelta).date(),
                )
            ]

            if not api_matches:
                logger.error(f"No upcoming matches found for season {comp_id}")
                continue

            valid_matches = [m for m in api_matches if m.id_match and m.home and m.away and m.id_stage]

            ext_team_ids = {m.home.id_team for m in valid_matches} | {m.away.id_team for m in valid_matches}
            ext_stage_ids = {m.id_stage for m in valid_matches}
            ext_match_ids = {m.id_match for m in valid_matches}

            team_cache = {
                tm.external_id: tm.team_id
                async for tm in TeamMapping.objects.filter(external_id__in=ext_team_ids, provider=SportsProvider.FIFA)
            }

            stage_cache = {
                sm.external_id: sm.stage_id
                async for sm in StageMapping.objects.filter(
                    external_id__in=ext_stage_ids, provider=SportsProvider.FIFA
                )
            }

            match_mapping_cache = {
                mm.external_id: mm
                async for mm in MatchMapping.objects.filter(
                    external_id__in=ext_match_ids, provider=SportsProvider.FIFA
                ).select_related("match")
            }

            for match in valid_matches:
                stage_id = stage_cache.get(match.id_stage)
                home_id = team_cache.get(match.home.id_team)
                away_id = team_cache.get(match.away.id_team)

                if not (stage_id and home_id and away_id):
                    logger.error(f"Incomplete infrastructure mappings for batch match {match.id_match}")
                    continue

                match_mapping = match_mapping_cache.get(match.id_match)
                db_match = match_mapping.match if match_mapping else Match()

                db_match.kickoff = match.date
                db_match.status = FIFA_STATUS_MAP.get(
                    match.match_status,
                    DjangoMatchStatus.SCHEDULED,
                )
                db_match.home_score = (
                    match.home_team_penalty_score or match.aggregate_home_team_score or match.home_team_score
                )
                db_match.away_score = (
                    match.away_team_penalty_score or match.aggregate_away_team_score or match.away_team_score
                )

                db_match.stage_id = stage_id
                db_match.home_team_id = home_id
                db_match.away_team_id = away_id

                await db_match.asave()

                if not match_mapping:
                    await MatchMapping.objects.acreate(
                        external_id=match.id_match,
                        provider=SportsProvider.FIFA,
                        match=db_match,
                    )
                    logger.info(f"Successfully created new match mapping link for {db_match.id}")


async def ingest_fifa_live_matches():
    match_mappings = [
        mm
        async for mm in MatchMapping.objects.select_related("match")
        .filter(
            Q(match__status=DjangoMatchStatus.LIVE)
            | (
                Q(match__status=DjangoMatchStatus.SCHEDULED)
                & Q(match__kickoff__lte=timezone.now() + datetime.timedelta(minutes=15))
            )
        )
        .aiterator()
    ]

    if not match_mappings:
        logger.info("No live matches found, skipping ingestion")
        return

    mapping_cache = {mm.external_id: mm for mm in match_mappings}

    async with FifaClient() as client:
        tasks = [client.get_live_match_by_id(mm.external_id) for mm in match_mappings]

        async for future in asyncio.as_completed(tasks):
            try:
                api_match = await future
                if not api_match or not api_match.home_team or not api_match.away_team:
                    continue

                db_match_mapping = mapping_cache.get(api_match.id_match)
                if not db_match_mapping:
                    logger.error(f"Match mapping not found for match {api_match.id_match}")
                    continue

                db_match = db_match_mapping.match

                old_home_score = db_match.home_score
                old_away_score = db_match.away_score
                new_status = FIFA_STATUS_MAP.get(api_match.match_status, DjangoMatchStatus.SCHEDULED)
                new_home_score = (
                    len(api_match.home_team.goals) or api_match.home_team_penalty_score or api_match.home_team.score
                )
                new_away_score = (
                    len(api_match.away_team.goals) or api_match.away_team_penalty_score or api_match.away_team.score
                )

                if (
                    old_home_score == new_home_score
                    and old_away_score == new_away_score
                    and db_match.status == new_status
                ):
                    logger.debug(f"No change detected for match {db_match.id}")
                    continue

                db_match.home_score = new_home_score

                db_match.away_score = new_away_score
                db_match.status = new_status
                await db_match.asave()
                if old_home_score != db_match.home_score or old_away_score != db_match.away_score:
                    logger.info(
                        f"Updated match {db_match.id} with live scores. From {old_home_score}:{old_away_score} to {db_match.home_score}:{db_match.away_score}"
                    )
                else:
                    logger.info(f"Updated match {db_match.id} with live scores.")
            except Exception as e:
                logger.error(f"Anomalie im reaktiven Task-Stream: {e}")


# ---------------------------------------------------------------------------
# ESPN / NFL
#
# Same mapping-table discipline as the FIFA path above: every lookup goes
# through a *Mapping row keyed by the provider's id, never by guessing on
# name/date. ESPN makes this easier than the old Otterball-NFL bot's two-source
# setup did - the schedule endpoint and the scoreboard endpoint share one event
# id, so no fuzzy (team, kickoff +- 24h) reconciliation is needed.
# ---------------------------------------------------------------------------


def current_nfl_season_year(today: datetime.date | None = None) -> int:
    """The NFL "season year" that `today` falls in.

    A season is labelled by the year it kicks off in, but runs into February
    of the next one, so anything before March still belongs to the previous
    season (the 2026 Super Bowl is played in February 2027).
    """
    today = today or timezone.localdate()
    return today.year if today.month >= 3 else today.year - 1


async def ingest_espn_nfl_infrastructure(season_year: int | None = None):
    """Seed the NFL competition, one season, and its five scoreable rounds.

    Unlike the FIFA path there is nothing to discover here - ESPN has no
    league/season entity worth ingesting - so the skeleton is written from
    NFL_STAGE_BLUEPRINTS. Idempotent: safe to re-run every day.
    """
    season_year = season_year or current_nfl_season_year()

    comp_mapping = (
        await CompetitionMapping.objects.select_related("competition")
        .filter(provider=SportsProvider.ESPN, external_id=ESPN_NFL_COMPETITION_EXTERNAL_ID)
        .afirst()
    )
    if comp_mapping:
        competition = comp_mapping.competition
    else:
        competition = await Competition.objects.acreate(
            name=ESPN_NFL_COMPETITION_NAME,
            sport=Sport.AMERICAN_FOOTBALL,
            gender=Gender.MALE,
            is_featured=True,
        )
        await CompetitionMapping.objects.acreate(
            provider=SportsProvider.ESPN,
            external_id=ESPN_NFL_COMPETITION_EXTERNAL_ID,
            competition=competition,
        )
        logger.info(f"Created NFL competition with ID {competition.id}")

    season_external_id = str(season_year)
    season_mapping = (
        await SeasonMapping.objects.select_related("season")
        .filter(provider=SportsProvider.ESPN, external_id=season_external_id)
        .afirst()
    )

    # ESPN knows when the league year opens and closes; use it so is_active
    # flips on its own rather than needing a manual edit. These bounds span
    # preseason through the Super Bowl, so a pool set up in August already
    # counts as active.
    start_date = end_date = None
    try:
        async with EspnClient() as client:
            api_season = await client.get_season(season_year)
        start_date = api_season.start_date
        end_date = api_season.end_date
    except Exception as e:
        logger.error(f"Could not read ESPN season bounds for {season_year}: {e}")

    now = timezone.now()
    is_active = True
    if start_date and end_date:
        is_active = start_date <= now <= end_date

    db_season = season_mapping.season if season_mapping else Season()
    db_season.name = f"NFL {season_year}"
    db_season.year = season_year
    db_season.competition_id = competition.id
    db_season.is_active = is_active
    await db_season.asave()

    if not season_mapping:
        await SeasonMapping.objects.acreate(
            provider=SportsProvider.ESPN,
            external_id=season_external_id,
            season=db_season,
        )
        logger.info(f"Created season mapping for NFL {season_year}")

    stage_external_ids = {nfl_stage_external_id(season_year, bp.key) for bp in NFL_STAGE_BLUEPRINTS}
    stage_mapping_cache = {
        sm.external_id: sm
        async for sm in StageMapping.objects.filter(
            provider=SportsProvider.ESPN,
            external_id__in=stage_external_ids,
        )
        .select_related("stage")
        .aiterator()
    }

    for blueprint in NFL_STAGE_BLUEPRINTS:
        external_id = nfl_stage_external_id(season_year, blueprint.key)
        stage_mapping = stage_mapping_cache.get(external_id)

        db_stage = stage_mapping.stage if stage_mapping else Stage()
        db_stage.name = blueprint.name
        db_stage.season_id = db_season.id
        db_stage.level = blueprint.level
        db_stage.stage_type = blueprint.stage_type
        await db_stage.asave()

        if not stage_mapping:
            await StageMapping.objects.acreate(
                provider=SportsProvider.ESPN,
                external_id=external_id,
                stage=db_stage,
            )
            logger.info(f"Created stage mapping for {db_stage.name} ({external_id})")

    return db_season


async def ingest_espn_nfl_teams(season_year: int | None = None):
    """Upsert the 32 NFL franchises, with logos and their brand colour."""
    season_year = season_year or current_nfl_season_year()

    async with EspnClient() as client:
        team_mapping_cache = {
            tm.external_id: tm
            async for tm in TeamMapping.objects.filter(provider=SportsProvider.ESPN)
            .select_related("team")
            .aiterator()
        }

        api_teams = await client.get_teams(season_year)
        if not api_teams:
            logger.error(f"No NFL teams returned for season {season_year}")
            return

        for api_team in api_teams:
            team_name = api_team.full_name
            try:
                team_mapping = team_mapping_cache.get(api_team.id)
                is_new = team_mapping is None

                db_team = team_mapping.team if team_mapping else Team(sport=Sport.AMERICAN_FOOTBALL)

                logo_url = api_team.logo_url
                target_color = api_team.hex_color or db_team.color

                metadata_changed = db_team.name != team_name or db_team.color != target_color
                logo_changed = is_new or db_team.logo_url != logo_url

                if not (is_new or metadata_changed or logo_changed):
                    logger.debug(f"Team {db_team.name} has no data modifications, skipping.")
                    continue

                if is_new:
                    logger.info(f"Seeding brand-new team entry: {team_name}")
                else:
                    logger.info(f"Data drift detected. Updating team metadata: {db_team.name} -> {team_name}")

                db_team.name = team_name
                db_team.logo_url = logo_url
                db_team.color = target_color
                db_team.sport = Sport.AMERICAN_FOOTBALL

                if logo_url and logo_changed:
                    image_response: ImageFile | None = await client.get_picture_by_url(logo_url)
                    if image_response:
                        try:
                            with image_response as pil_img:
                                logo_file: ContentFile = await asyncio.to_thread(
                                    _process_and_format_image,
                                    pil_img,
                                    api_team.id,
                                    team_name,
                                )
                                await asyncio.to_thread(
                                    db_team.logo.save,
                                    logo_file.name,
                                    logo_file,
                                    save=False,
                                )
                        except Exception as e:
                            logger.error(f"Error processing image for {team_name}: {e}")

                await db_team.asave()

                if is_new:
                    await TeamMapping.objects.acreate(
                        provider=SportsProvider.ESPN,
                        external_id=api_team.id,
                        team=db_team,
                    )
                    logger.info(f"Created team {db_team.name} with ID {db_team.id}")
            except Exception as e:
                logger.error(f"Error creating team {team_name}: {e}")


async def ingest_espn_nfl_matches(
    timedelta: datetime.timedelta = datetime.timedelta(days=14),
    start: datetime.date | None = None,
    end: datetime.date | None = None,
):
    """Upsert the NFL schedule for the next `timedelta`.

    `start`/`end` override that window outright, which is what backfilling an
    already-played stretch of the season needs.

    Only kickoff/teams/stage are written here - status and scores are owned by
    ingest_espn_nfl_live_matches, so a re-run of this never walks a finished
    match back to SCHEDULED.
    """
    start = start or timezone.localdate()
    end = end or (timezone.now() + timedelta).date()

    async with EspnClient() as client:
        api_events = await client.get_events(start=start, end=end)

    if not api_events:
        logger.info(f"No NFL events found between {start} and {end}")
        return

    # Preseason and the Pro Bowl have no stage blueprint and are dropped here.
    scoreable = []
    for event in api_events:
        stage_key = nfl_stage_key(event.season_type_id, event.week_number)
        season_year = event.season_year
        if not stage_key or not season_year:
            logger.debug(f"Skipping non-scoreable NFL event {event.id} ({event.name})")
            continue
        if not (event.home and event.away and event.home.team_id and event.away.team_id):
            logger.warning(f"NFL event {event.id} has no confirmed teams yet, skipping.")
            continue
        scoreable.append((event, nfl_stage_external_id(season_year, stage_key)))

    if not scoreable:
        logger.info(f"No scoreable NFL events between {start} and {end}")
        return

    ext_team_ids = {e.home.team_id for e, _ in scoreable} | {e.away.team_id for e, _ in scoreable}
    ext_stage_ids = {stage_ext_id for _, stage_ext_id in scoreable}
    ext_event_ids = {e.id for e, _ in scoreable}

    team_cache = {
        tm.external_id: tm.team_id
        async for tm in TeamMapping.objects.filter(
            external_id__in=ext_team_ids, provider=SportsProvider.ESPN
        ).aiterator()
    }
    stage_cache = {
        sm.external_id: sm.stage_id
        async for sm in StageMapping.objects.filter(
            external_id__in=ext_stage_ids, provider=SportsProvider.ESPN
        ).aiterator()
    }
    match_mapping_cache = {
        mm.external_id: mm
        async for mm in MatchMapping.objects.filter(external_id__in=ext_event_ids, provider=SportsProvider.ESPN)
        .select_related("match")
        .aiterator()
    }

    for event, stage_ext_id in scoreable:
        stage_id = stage_cache.get(stage_ext_id)
        home_id = team_cache.get(event.home.team_id)
        away_id = team_cache.get(event.away.team_id)

        if not (stage_id and home_id and away_id):
            logger.error(
                f"Incomplete infrastructure mappings for NFL event {event.id} "
                f"(stage={stage_ext_id}, home={event.home.team_id}, away={event.away.team_id}). "
                "Run the NFL infrastructure and team sync first."
            )
            continue

        match_mapping = match_mapping_cache.get(event.id)
        db_match = match_mapping.match if match_mapping else Match()

        db_match.kickoff = event.date
        db_match.stage_id = stage_id
        db_match.home_team_id = home_id
        db_match.away_team_id = away_id

        await db_match.asave()

        if not match_mapping:
            await MatchMapping.objects.acreate(
                external_id=event.id,
                provider=SportsProvider.ESPN,
                match=db_match,
            )
            logger.info(f"Successfully created new match mapping link for {db_match.id}")


async def ingest_espn_nfl_live_matches():
    """Pull status and scores for NFL matches that are live or already due.

    The window intentionally includes anything still unfinished whose kickoff
    has passed, not just the next 15 minutes: if the worker was down over a
    weekend, those matches would otherwise stay SCHEDULED forever and never
    fire the scoring signal.
    """
    match_mappings = [
        mm
        async for mm in MatchMapping.objects.select_related("match")
        .filter(
            Q(provider=SportsProvider.ESPN)
            & ~Q(match__status__in=[DjangoMatchStatus.FINISHED, DjangoMatchStatus.CANCELLED])
            & Q(match__kickoff__lte=timezone.now() + datetime.timedelta(minutes=15))
        )
        .aiterator()
    ]

    if not match_mappings:
        logger.info("No live NFL matches found, skipping ingestion")
        return

    mapping_cache = {mm.external_id: mm for mm in match_mappings}
    kickoffs = [mm.match.kickoff for mm in match_mappings]
    start = (min(kickoffs) - datetime.timedelta(days=1)).date()
    end = (max(kickoffs) + datetime.timedelta(days=1)).date()

    # The scoreboard reports the ESPN team id per side; verifying it against
    # the stored match stops a mismatched payload from writing scores to the
    # wrong team.
    team_cache = {
        tm.external_id: tm.team_id
        async for tm in TeamMapping.objects.filter(provider=SportsProvider.ESPN).aiterator()
    }

    async with EspnClient() as client:
        try:
            api_events = await client.get_scoreboard(start=start, end=end)
        except Exception as e:
            logger.error(f"Error fetching NFL scoreboard for {start}..{end}: {e}")
            return

    for api_event in api_events:
        try:
            db_match_mapping = mapping_cache.get(api_event.event_id)
            if not db_match_mapping:
                continue

            db_match = db_match_mapping.match
            home, away = api_event.home, api_event.away
            if not home or not away:
                logger.error(f"Scoreboard event {api_event.event_id} has no competitors")
                continue

            if team_cache.get(home.id) != db_match.home_team_id or team_cache.get(away.id) != db_match.away_team_id:
                logger.error(
                    f"Team mismatch for match {db_match.id}: scoreboard says "
                    f"{home.abbreviation}/{away.abbreviation}, refusing to write scores."
                )
                continue

            new_status = ESPN_STATUS_MAP.get(api_event.status, DjangoMatchStatus.SCHEDULED)
            old_home_score = db_match.home_score
            old_away_score = db_match.away_score

            if old_home_score == home.score and old_away_score == away.score and db_match.status == new_status:
                logger.debug(f"No change detected for match {db_match.id}")
                continue

            db_match.home_score = home.score
            db_match.away_score = away.score
            db_match.status = new_status
            await db_match.asave()

            logger.info(
                f"Updated match {db_match.id} ({new_status}). "
                f"From {old_home_score}:{old_away_score} to {db_match.home_score}:{db_match.away_score}"
            )
        except Exception as e:
            logger.error(f"Error processing NFL scoreboard event {api_event.event_id}: {e}")


# ---------------------------------------------------------------------------
# nflverse
#
# A second provider for the NFL, covering the schedule and *final* results
# only - nflverse is a batch export and never carries in-progress state, so
# ESPN's scoreboard remains the live source.
#
# Its value is redundancy plus the free ESPN cross-reference: every row
# carries the matching ESPN event id, so ingesting a game here attaches both
# a NFLVERSE and an ESPN MatchMapping. If ESPN's remaining hosts start
# refusing us the way site.api.espn.com already does, the matches are already
# keyed correctly for whatever replaces it.
# ---------------------------------------------------------------------------


async def ingest_nflverse_team_mappings(season_year: int | None = None):
    """Give every NFL team a mapping keyed by its nflverse abbreviation.

    nflverse identifies teams by abbreviation while ESPN uses numeric ids, so
    the two are bridged here once. The abbreviations come from ESPN (which is
    also where the Team rows come from); the two providers disagree on exactly
    two of them, hence ESPN_TO_NFLVERSE_TEAM_ABBREVIATIONS.
    """
    season_year = season_year or current_nfl_season_year()

    espn_team_cache = {
        tm.external_id: tm.team_id
        async for tm in TeamMapping.objects.filter(provider=SportsProvider.ESPN).aiterator()
    }
    if not espn_team_cache:
        logger.error("No ESPN team mappings found - run the NFL team sync first.")
        return

    existing = {
        tm.external_id async for tm in TeamMapping.objects.filter(provider=SportsProvider.NFLVERSE).aiterator()
    }

    async with EspnClient() as client:
        api_teams = await client.get_teams(season_year)

    created = 0
    for api_team in api_teams:
        team_id = espn_team_cache.get(api_team.id)
        if not team_id or not api_team.abbreviation:
            continue

        espn_abbreviation = api_team.abbreviation.upper()
        nflverse_abbreviation = ESPN_TO_NFLVERSE_TEAM_ABBREVIATIONS.get(espn_abbreviation, espn_abbreviation)

        if nflverse_abbreviation in existing:
            continue

        await TeamMapping.objects.acreate(
            provider=SportsProvider.NFLVERSE,
            external_id=nflverse_abbreviation,
            team_id=team_id,
        )
        existing.add(nflverse_abbreviation)
        created += 1

    logger.info(f"Created {created} nflverse team mappings ({len(existing)} total)")

    # A team without a mapping loses every one of its ~17 games silently, so
    # say so loudly here rather than leaving it to be noticed in week 6.
    if len(existing) != NFL_TEAM_COUNT:
        logger.error(
            f"Expected {NFL_TEAM_COUNT} nflverse team mappings but have {len(existing)}. "
            "Every game of an unmapped team will be skipped - check "
            "ESPN_TO_NFLVERSE_TEAM_ABBREVIATIONS for a new provider disagreement."
        )


async def ingest_nflverse_nfl_matches(seasons: set[int] | None = None):
    """Upsert the NFL schedule and final results from nflverse.

    Kickoff, teams and stage are always refreshed. Status and scores are only
    written when nflverse reports the game as played *and* the match is not
    already FINISHED - so this can finalize a match ESPN missed, without
    re-firing the scoring signal for one it already settled.
    """
    seasons = seasons or {current_nfl_season_year()}

    async with NflverseClient() as client:
        try:
            api_games = await client.get_games(seasons=seasons)
        except Exception as e:
            logger.error(f"Error fetching nflverse games for {sorted(seasons)}: {e}")
            return

    if not api_games:
        logger.info(f"No nflverse games found for seasons {sorted(seasons)}")
        return

    scoreable = []
    for game in api_games:
        stage_key = nflverse_stage_key(game.game_type)
        if not stage_key:
            logger.debug(f"Skipping non-scoreable nflverse game {game.game_id} ({game.game_type})")
            continue
        if game.kickoff is None:
            logger.debug(f"Skipping nflverse game {game.game_id} with no scheduled kickoff")
            continue
        scoreable.append((game, nfl_stage_external_id(game.season, stage_key)))

    if not scoreable:
        logger.info(f"No scoreable nflverse games for seasons {sorted(seasons)}")
        return

    team_cache = {
        tm.external_id: tm.team_id
        async for tm in TeamMapping.objects.filter(provider=SportsProvider.NFLVERSE).aiterator()
    }
    stage_cache = {
        sm.external_id: sm.stage_id
        async for sm in StageMapping.objects.filter(
            external_id__in={ext for _, ext in scoreable}, provider=SportsProvider.ESPN
        ).aiterator()
    }
    nflverse_mapping_cache = {
        mm.external_id: mm
        async for mm in MatchMapping.objects.filter(
            external_id__in={g.game_id for g, _ in scoreable}, provider=SportsProvider.NFLVERSE
        )
        .select_related("match")
        .aiterator()
    }
    # The ESPN ids nflverse hands us, so an already-ingested ESPN match is
    # adopted rather than duplicated.
    espn_mapping_cache = {
        mm.external_id: mm
        async for mm in MatchMapping.objects.filter(
            external_id__in={g.espn for g, _ in scoreable if g.espn}, provider=SportsProvider.ESPN
        )
        .select_related("match")
        .aiterator()
    }

    finalized = 0
    skipped = 0
    unmapped_teams: collections.Counter[str] = collections.Counter()
    missing_stages: collections.Counter[str] = collections.Counter()

    for game, stage_ext_id in scoreable:
        try:
            stage_id = stage_cache.get(stage_ext_id)
            home_id = team_cache.get(game.home_team)
            away_id = team_cache.get(game.away_team)

            if not (stage_id and home_id and away_id):
                # Collected rather than logged per game: one unmapped team is
                # ~17 near-identical error lines, which is how a provider
                # renaming an abbreviation goes unnoticed for weeks. The
                # summary after the loop names the distinct causes once.
                if not home_id:
                    unmapped_teams[game.home_team] += 1
                if not away_id:
                    unmapped_teams[game.away_team] += 1
                if not stage_id:
                    missing_stages[stage_ext_id] += 1
                skipped += 1
                logger.debug(
                    f"Skipping nflverse game {game.game_id} "
                    f"(stage={stage_ext_id}, home={game.home_team}, away={game.away_team})"
                )
                continue

            nflverse_mapping = nflverse_mapping_cache.get(game.game_id)
            espn_mapping = espn_mapping_cache.get(game.espn) if game.espn else None

            if nflverse_mapping:
                db_match = nflverse_mapping.match
            elif espn_mapping:
                # Same fixture, already ingested from ESPN - reuse that row.
                db_match = espn_mapping.match
            else:
                db_match = Match()

            db_match.kickoff = game.kickoff
            db_match.stage_id = stage_id
            db_match.home_team_id = home_id
            db_match.away_team_id = away_id

            should_finalize = game.is_final and db_match.status != DjangoMatchStatus.FINISHED
            if should_finalize:
                db_match.home_score = game.home_score
                db_match.away_score = game.away_score
                db_match.status = DjangoMatchStatus.FINISHED

            await db_match.asave()

            if should_finalize:
                finalized += 1
                logger.info(
                    f"Finalized match {db_match.id} from nflverse: "
                    f"{game.away_team} {game.away_score} @ {game.home_team} {game.home_score}"
                )

            if not nflverse_mapping:
                await MatchMapping.objects.acreate(
                    external_id=game.game_id,
                    provider=SportsProvider.NFLVERSE,
                    match=db_match,
                )
                logger.info(f"Linked nflverse game {game.game_id} to match {db_match.id}")

            # The cross-reference: free ESPN id, no fuzzy matching needed.
            if game.espn and not espn_mapping:
                await MatchMapping.objects.aget_or_create(
                    external_id=game.espn,
                    provider=SportsProvider.ESPN,
                    defaults={"match": db_match},
                )
        except Exception as e:
            logger.error(f"Error processing nflverse game {game.game_id}: {e}")

    if unmapped_teams:
        # The abbreviation bridge is the usual culprit: nflverse and ESPN
        # disagree on LA/WAS vs LAR/WSH, and a *new* disagreement would
        # otherwise cost that team its whole season without an obvious signal.
        detail = ", ".join(f"{team} ({count} games)" for team, count in sorted(unmapped_teams.items()))
        logger.error(
            f"Unmapped nflverse team abbreviations: {detail}. "
            "Those games were skipped. Run the NFL team sync, or add the "
            "abbreviation to ESPN_TO_NFLVERSE_TEAM_ABBREVIATIONS if the "
            "providers now disagree on it."
        )

    if missing_stages:
        detail = ", ".join(f"{stage} ({count} games)" for stage, count in sorted(missing_stages.items()))
        logger.error(
            f"No stage mapping for: {detail}. Those games were skipped. "
            "Run the NFL infrastructure sync for the affected season."
        )

    logger.info(f"nflverse ingestion complete. Finalized {finalized} matches, skipped {skipped}.")
