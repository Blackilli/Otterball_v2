import asyncio
import datetime
import io
import logging

from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from PIL.ImageFile import ImageFile

from sports.constants import FIFA_GENDER_MAP, FIFA_STAGE_TYPE_MAP, FIFA_STATUS_MAP
from sports.integrations.fifa import (
    FifaClient,
    PictureFormat,
    PictureSize,
    TeamType,
)
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

                metadata_chaned = db_team.name != team_name or db_team.gender != target_gender
                logo_changed = is_new or db_team.logo_url != logo_url

                if not (is_new or metadata_chaned or logo_changed):
                    logger.debug(f"Team {db_team.name} has no data modifications, skipping.")
                    continue

                if is_new:
                    logger.info(f"Seeding brand-new team entry: {team_name}")
                else:
                    logger.info(f"Data drift detected. Updating team metadata: {db_team.name} -> {team_name}")

                db_team.name = team_name
                db_team.logo_url = logo_url
                db_team.gender = target_gender

                if logo_url and logo_url:
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
                db_match.home_score = match.home_team_score
                db_match.away_score = match.away_team_score

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
