import datetime
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from discord_bot.models import DiscordChannel, DiscordGuild, DiscordGuildPool, DiscordGuildRole
from predictions.models import DayOfWeek, PoolConfiguration, PoolStageRule, PredictionPool, sync_pool_stage_rules
from sports.models import Season, Sport

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Creates a prediction pool, its configuration, its per-stage scoring rules "
        "and (optionally) its Discord binding, in one idempotent pass. "
        "Re-running with the same --name updates the existing pool rather than duplicating it."
    )

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Pool name, e.g. 'NFL 2026'")

        season = parser.add_argument_group("season (pick one)")
        season.add_argument("--season", type=int, default=None, help="Season id (see /admin/sports/season/)")
        season.add_argument(
            "--sport",
            choices=[s.value for s in Sport],
            default=None,
            help="Resolve the season by sport instead of id",
        )
        season.add_argument("--year", type=int, default=None, help="Season year, used with --sport")

        config = parser.add_argument_group("poll scheduling")
        config.add_argument(
            "--weekdays",
            default=None,
            help="Comma-separated weekdays to post polls on, 0=Monday (e.g. '2' for Wednesday)",
        )
        config.add_argument("--time", default=None, help="Poll creation time, HH:MM (24h, project timezone)")
        config.add_argument("--lookahead", type=int, default=None, help="Days of matches per batch (1-7)")

        scoring = parser.add_argument_group("scoring")
        scoring.add_argument(
            "--points",
            default=None,
            help=(
                "Points per correct pick, per stage: 'Regular Season=1,Wild Card=2,Super Bowl=5'. "
                "Stages left out keep their current value."
            ),
        )

        discord = parser.add_argument_group("discord binding (the bot must have connected once)")
        discord.add_argument("--guild", type=int, default=None, help="Discord guild (server) id")
        discord.add_argument("--channel", type=int, default=None, help="Discord channel id for polls + leaderboard")
        discord.add_argument("--notification-role", type=int, default=None, help="Role id to ping on new polls")

    # -- resolution helpers ------------------------------------------------

    def resolve_season(self, options) -> Season:
        if options["season"]:
            try:
                return Season.objects.select_related("competition").get(id=options["season"])
            except Season.DoesNotExist:
                raise CommandError(f"No season with id {options['season']}. See /admin/sports/season/.")

        if not options["sport"]:
            raise CommandError("Pass --season <id>, or --sport with --year.")

        query = Season.objects.select_related("competition").filter(competition__sport=options["sport"])
        if options["year"]:
            query = query.filter(year=options["year"])

        matches = list(query.order_by("-year")[:5])
        if not matches:
            raise CommandError(
                f"No season found for sport={options['sport']} year={options['year']}. "
                "Run the ingestion command for that sport first (e.g. `manage.py sync_nfl_infra`)."
            )
        if len(matches) > 1 and not options["year"]:
            listed = ", ".join(f"{s.name} (id={s.id}, year={s.year})" for s in matches)
            raise CommandError(f"Several seasons match; pass --year or --season. Candidates: {listed}")
        return matches[0]

    def parse_weekdays(self, raw: str) -> list[int]:
        days = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                day = int(part)
            except ValueError:
                raise CommandError(f"--weekdays takes integers 0-6, got {part!r}")
            if day not in DayOfWeek.values:
                raise CommandError(f"--weekdays takes integers 0-6 (0=Monday), got {day}")
            days.append(day)
        if not days:
            raise CommandError("--weekdays needs at least one day, or polls never post.")
        return sorted(set(days))

    def parse_points(self, raw: str) -> dict[str, int]:
        points = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                raise CommandError(f"--points takes 'Stage Name=points' pairs, got {pair!r}")
            name, _, value = pair.partition("=")
            try:
                points[name.strip()] = int(value)
            except ValueError:
                raise CommandError(f"Points must be a whole number, got {value!r} for {name.strip()!r}")
        return points

    # -- the work ----------------------------------------------------------

    @transaction.atomic
    def handle(self, *args, **options):
        season = self.resolve_season(options)

        pool, created = PredictionPool.objects.get_or_create(
            name=options["name"],
            season=season,
            defaults={"is_active": True},
        )
        verb = "Created" if created else "Updating existing"
        self.stdout.write(self.style.SUCCESS(f"{verb} pool '{pool.name}' (id={pool.id}) on season {season.name}"))

        # A pool created before its season had stages, or one whose season has
        # since gained playoff rounds, is topped up here.
        seeded = sync_pool_stage_rules(pool)
        if seeded:
            self.stdout.write(f"  seeded {len(seeded)} stage rule(s)")

        self.apply_configuration(pool, options)
        self.apply_points(pool, options)
        self.apply_discord_binding(pool, options)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Pool #{pool.id} ready. Verify with:"))
        self.stdout.write(f"  uv run python manage.py check_pool --pool {pool.id}")

    def apply_configuration(self, pool: PredictionPool, options):
        config, _ = PoolConfiguration.objects.get_or_create(pool=pool)
        changed = []

        if options["weekdays"] is not None:
            config.poll_creation_weekdays = self.parse_weekdays(options["weekdays"])
            changed.append(f"weekdays={config.poll_creation_weekdays}")

        if options["time"] is not None:
            try:
                hour, _, minute = options["time"].partition(":")
                config.poll_creation_time = datetime.time(int(hour), int(minute or 0))
            except ValueError:
                raise CommandError(f"--time takes HH:MM, got {options['time']!r}")
            changed.append(f"time={config.poll_creation_time:%H:%M}")

        if options["lookahead"] is not None:
            if not 1 <= options["lookahead"] <= 7:
                raise CommandError("--lookahead must be between 1 and 7 days.")
            config.poll_creation_lookahead_days = options["lookahead"]
            changed.append(f"lookahead={config.poll_creation_lookahead_days}d")

        if changed:
            config.save()
            self.stdout.write(f"  configuration: {', '.join(changed)}")

    def apply_points(self, pool: PredictionPool, options):
        if options["points"] is None:
            return

        wanted = self.parse_points(options["points"])
        rules = {r.stage.name: r for r in pool.stage_rules.select_related("stage") if r.stage}

        unknown = set(wanted) - set(rules)
        if unknown:
            raise CommandError(
                f"No stage named {sorted(unknown)} on season '{pool.season.name}'. Available: {sorted(rules)}"
            )

        for name, value in wanted.items():
            rules[name].points_per_correct = value
        PoolStageRule.objects.bulk_update(list(rules.values()), ["points_per_correct"])

        for name in sorted(rules, key=lambda n: rules[n].level):
            self.stdout.write(f"  {name:28} {rules[name].points_per_correct} pts")

    def apply_discord_binding(self, pool: PredictionPool, options):
        if not options["guild"]:
            return

        try:
            guild = DiscordGuild.objects.get(id=options["guild"])
        except DiscordGuild.DoesNotExist:
            raise CommandError(
                f"No Discord guild with id {options['guild']}. The bot populates these on startup - "
                "start it once, then re-run this."
            )

        channel = None
        if options["channel"]:
            try:
                channel = DiscordChannel.objects.get(id=options["channel"], guild=guild)
            except DiscordChannel.DoesNotExist:
                raise CommandError(f"No channel {options['channel']} in guild {guild.name}.")

        role = None
        if options["notification_role"]:
            try:
                role = DiscordGuildRole.objects.get(id=options["notification_role"], guild=guild)
            except DiscordGuildRole.DoesNotExist:
                raise CommandError(f"No role {options['notification_role']} in guild {guild.name}.")

        # Only write what was actually asked for. Passing channel/role
        # unconditionally would null them out on a partial re-run - rotating
        # just the ping role would silently unset the channel, and the pool
        # would stop posting entirely. Same guard shape as apply_configuration.
        defaults: dict = {"is_active": True}
        if options["channel"]:
            defaults["channel"] = channel
        if options["notification_role"]:
            defaults["notification_role"] = role

        guild_pool, created = DiscordGuildPool.objects.update_or_create(
            guild=guild,
            pool=pool,
            defaults=defaults,
        )
        guild_pool.refresh_from_db()
        verb = "Bound" if created else "Rebound"
        where = f"#{guild_pool.channel.name}" if guild_pool.channel else "no channel yet"
        self.stdout.write(f"  {verb} to {guild.name} ({where})")

        if not guild_pool.channel:
            self.stdout.write(self.style.WARNING("  no channel set: polls and the leaderboard have nowhere to post"))
