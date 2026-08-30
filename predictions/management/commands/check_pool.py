import sys

from django.core.management.base import BaseCommand

from predictions.models import PredictionPool
from predictions.readiness import FAIL, OK, WARN, Readiness, check_pool, worst


class Command(BaseCommand):
    help = (
        "Reports whether a prediction pool is actually ready to run. "
        "Nearly every way a pool can be misconfigured fails silently - no stage rules means "
        "every pick scores the hardcoded fallback, an unmapped stage type makes poll creation "
        "skip matches, and a pool with no Discord binding simply never posts."
    )

    def add_arguments(self, parser):
        parser.add_argument("--pool", type=int, default=None, help="Pool id (default: every active pool)")
        parser.add_argument("--all", action="store_true", help="Include inactive pools")

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

        overall = OK
        for pool in pools:
            report = check_pool(pool)
            self.render(report)
            overall = worst(overall, report.status)

        self.stdout.write("")
        if overall == FAIL:
            self.stdout.write(self.style.ERROR("Not ready - resolve the FAIL items above."))
            # Non-zero so this is usable as a deploy/CI gate.
            sys.exit(1)
        elif overall == WARN:
            self.stdout.write(self.style.WARNING("Usable, but check the WARN items above."))
        else:
            self.stdout.write(self.style.SUCCESS("All checks passed."))

    def render(self, report: Readiness):
        pool = report.pool
        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO(f"Pool #{pool.id}: {pool.name}"))
        self.stdout.write(f"  season: {pool.season.name} ({pool.season.competition.get_sport_display()})")

        styles = {OK: self.style.SUCCESS, WARN: self.style.WARNING, FAIL: self.style.ERROR}
        for check in report.checks:
            line = f"  {styles[check.status](check.status.ljust(4))} {check.label}"
            self.stdout.write(line + (f" - {check.detail}" if check.detail else ""))
