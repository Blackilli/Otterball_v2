import datetime
import sys

from django.core.management.base import BaseCommand
from django.utils import timezone

from discord_bot.constants import DISCORD_POLL_ANSWER_ORDER_MAP
from discord_bot.models import DiscordGuildPool, DiscordTeamEmoji
from predictions.models import DayOfWeek, PredictionPool
from sports.models import Match, Stage

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"


class Command(BaseCommand):
    help = (
        "Reports whether a prediction pool is actually ready to run. "
        "Nearly every way a pool can be misconfigured fails silently - no stage rules means "
        "every pick scores the hardcoded fallback, an unmapped stage type makes poll creation "
        "skip matches, and a pool with no Discord binding simply never posts."
    )

    def add_arguments(self, parser):
        parser.add_argument("--pool", type=int, default=None, help="Pool id (default: every active pool)")
        parser.add_argument(
            "--all",
            action="store_true",
            help="Include inactive pools",
        )

    def handle(self, *args, **options):
        pools = PredictionPool.objects.select_related("season", "season__competition")
        if options["pool"]:
            pools = pools.filter(id=options["pool"])
        elif not options["all"]:
            pools = pools.filter(is_active=True)

        pools = list(pools.order_by("id"))
        if not pools:
            self.stdout.write(self.style.WARNING("No matching pools. Create one with `manage.py create_pool`."))
            return

        worst = OK
        for pool in pools:
            worst = self.rank(worst, self.report(pool))

        self.stdout.write("")
        if worst == FAIL:
            self.stdout.write(self.style.ERROR("Not ready - resolve the FAIL items above."))
            # Non-zero so this is usable as a deploy/CI gate.
            sys.exit(1)
        elif worst == WARN:
            self.stdout.write(self.style.WARNING("Usable, but check the WARN items above."))
        else:
            self.stdout.write(self.style.SUCCESS("All checks passed."))

    @staticmethod
    def rank(current: str, new: str) -> str:
        order = {OK: 0, WARN: 1, FAIL: 2}
        return new if order[new] > order[current] else current

    def line(self, status: str, label: str, detail: str = ""):
        style = {OK: self.style.SUCCESS, WARN: self.style.WARNING, FAIL: self.style.ERROR}[status]
        self.stdout.write(f"  {style(status.ljust(4))} {label}" + (f" - {detail}" if detail else ""))

    def report(self, pool: PredictionPool) -> str:
        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO(f"Pool #{pool.id}: {pool.name}"))
        self.stdout.write(f"  season: {pool.season.name} ({pool.season.competition.get_sport_display()})")

        worst = OK
        for check in (
            self.check_active,
            self.check_stages,
            self.check_stage_rules,
            self.check_poll_answer_orders,
            self.check_configuration,
            self.check_matches,
            self.check_discord_binding,
            self.check_emoji,
        ):
            worst = self.rank(worst, check(pool))
        return worst

    # -- individual checks -------------------------------------------------

    def check_active(self, pool) -> str:
        if not pool.is_active:
            self.line(WARN, "pool is inactive", "no polls or leaderboard updates will run")
            return WARN
        if not pool.season.is_active:
            self.line(WARN, "season is inactive", f"'{pool.season.name}' is outside its date range")
            return WARN
        self.line(OK, "pool and season are active")
        return OK

    def check_stages(self, pool) -> str:
        count = Stage.objects.filter(season_id=pool.season_id).count()
        if not count:
            self.line(FAIL, "season has no stages", "run the ingestion command for this sport")
            return FAIL
        self.line(OK, f"season has {count} stage(s)")
        return OK

    def check_stage_rules(self, pool) -> str:
        stages = list(Stage.objects.filter(season_id=pool.season_id).order_by("level"))
        if not stages:
            return OK  # already reported by check_stages

        rules = {r.stage_id: r for r in pool.stage_rules.all()}
        fallback = rules.get(None)
        missing = [s for s in stages if s.id not in rules]

        if missing and not fallback:
            names = ", ".join(s.name for s in missing)
            self.line(
                FAIL,
                f"{len(missing)} stage(s) have no scoring rule",
                f"{names} - picks there score the hardcoded fallback of 3. Run `manage.py create_pool --name "
                f"'{pool.name}' --season {pool.season_id}` to seed them",
            )
            return FAIL

        if missing:
            names = ", ".join(s.name for s in missing)
            self.line(WARN, f"{len(missing)} stage(s) fall back to the pool-wide rule", names)

        distribution = ", ".join(f"{s.name}={rules[s.id].points_per_correct}" for s in stages if s.id in rules)
        self.line(OK, "scoring", distribution or "pool-wide fallback only")

        values = {rules[s.id].points_per_correct for s in stages if s.id in rules}
        if len(values) == 1 and len(stages) > 1:
            self.line(
                WARN,
                "every stage scores the same",
                "intentional for a flat pool; if you meant to scale per round, set --points",
            )
            return WARN
        return WARN if missing else OK

    def check_poll_answer_orders(self, pool) -> str:
        """An unmapped stage_type makes poll_creation log an error and skip
        the match outright, so a whole round can silently never get polls."""
        unmapped = [
            s
            for s in Stage.objects.filter(season_id=pool.season_id)
            if s.stage_type not in DISCORD_POLL_ANSWER_ORDER_MAP
        ]
        if unmapped:
            detail = ", ".join(f"{s.name} ({s.stage_type})" for s in unmapped)
            self.line(
                FAIL,
                f"{len(unmapped)} stage(s) have no poll layout",
                f"{detail} - poll creation skips these matches entirely. "
                "Add the stage type to DISCORD_POLL_ANSWER_ORDER_MAP",
            )
            return FAIL
        self.line(OK, "every stage type has a poll layout")
        return OK

    def check_configuration(self, pool) -> str:
        config = getattr(pool, "configuration", None)
        if config is None:
            self.line(FAIL, "no PoolConfiguration", "polls will never be scheduled")
            return FAIL

        if not config.poll_creation_weekdays:
            self.line(FAIL, "no poll creation weekdays set", "polls will never post")
            return FAIL

        days = ", ".join(DayOfWeek(d).label for d in sorted(config.poll_creation_weekdays))
        self.line(
            OK,
            "poll schedule",
            f"{days} at {config.poll_creation_time:%H:%M}, {config.poll_creation_lookahead_days}d lookahead",
        )
        return OK

    def check_matches(self, pool) -> str:
        config = getattr(pool, "configuration", None)
        lookahead = config.poll_creation_lookahead_days if config else 7
        now = timezone.now()

        upcoming = Match.objects.filter(
            stage__season_id=pool.season_id,
            kickoff__gte=now,
            kickoff__lte=now + datetime.timedelta(days=lookahead),
        ).count()
        total_future = Match.objects.filter(stage__season_id=pool.season_id, kickoff__gte=now).count()

        if not total_future:
            self.line(FAIL, "no upcoming matches on this season", "run the schedule ingestion")
            return FAIL
        if not upcoming:
            next_match = (
                Match.objects.filter(stage__season_id=pool.season_id, kickoff__gte=now).order_by("kickoff").first()
            )
            self.line(
                WARN,
                f"no matches in the next {lookahead}d",
                f"{total_future} later; next is {next_match.kickoff:%Y-%m-%d %H:%M}",
            )
            return WARN
        self.line(OK, f"{upcoming} match(es) in the next {lookahead}d", f"{total_future} upcoming in total")
        return OK

    def check_discord_binding(self, pool) -> str:
        bindings = list(
            DiscordGuildPool.objects.filter(pool=pool).select_related("guild", "channel", "notification_role")
        )
        if not bindings:
            self.line(
                FAIL,
                "no Discord binding",
                "the pool will never post. Create a DiscordGuildPool in /admin/ "
                "(the bot must have connected once to populate guilds/channels)",
            )
            return FAIL

        worst = OK
        for binding in bindings:
            if not binding.is_active:
                self.line(WARN, f"binding to {binding.guild.name} is inactive")
                worst = self.rank(worst, WARN)
                continue
            if not binding.channel:
                self.line(FAIL, f"binding to {binding.guild.name} has no channel", "nowhere to post")
                worst = self.rank(worst, FAIL)
                continue

            role = f", pings @{binding.notification_role.name}" if binding.notification_role else ", no ping role"
            pinned = "leaderboard posted" if binding.leaderboard_msg else "leaderboard not posted yet"
            self.line(OK, f"bound to {binding.guild.name} #{binding.channel.name}", f"{pinned}{role}")
        return worst

    def check_emoji(self, pool) -> str:
        """Missing emoji are cosmetic - polls fall back to a white/black circle."""
        now = timezone.now()
        team_ids = set(
            Match.objects.filter(stage__season_id=pool.season_id, kickoff__gte=now)
            .values_list("home_team_id", flat=True)
            .union(
                Match.objects.filter(stage__season_id=pool.season_id, kickoff__gte=now).values_list(
                    "away_team_id", flat=True
                )
            )
        )
        if not team_ids:
            return OK

        have = set(DiscordTeamEmoji.objects.filter(team_id__in=team_ids).values_list("team_id", flat=True))
        missing = team_ids - have
        if missing:
            self.line(
                WARN,
                f"{len(missing)} of {len(team_ids)} teams have no emoji",
                "cosmetic; the bot registers them on startup",
            )
            return WARN
        self.line(OK, f"all {len(team_ids)} upcoming teams have emoji")
        return OK
