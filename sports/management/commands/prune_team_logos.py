from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from sports.models import Team

LOGO_DIR = "team_logos"


class Command(BaseCommand):
    help = (
        "Reports, and optionally deletes, team logo files no Team row points at. "
        "Django never overwrites on re-save: it appends a random suffix, so a logo re-downloaded "
        "for an unchanged team left the previous file behind. Ingestion no longer does that, but "
        "the files it already wrote are still on disk."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Actually remove the orphans. Without this the command only reports.",
        )

    def handle(self, *args, **options):
        referenced = {name for name in Team.objects.exclude(logo="").values_list("logo", flat=True) if name}

        try:
            _, filenames = default_storage.listdir(LOGO_DIR)
        except FileNotFoundError:
            self.stdout.write(self.style.WARNING(f"No {LOGO_DIR}/ directory yet - nothing to prune."))
            return

        on_disk = {f"{LOGO_DIR}/{name}" for name in filenames}

        orphans = sorted(on_disk - referenced)
        # A row pointing at a file that is gone is the opposite problem, and a
        # far worse one: that team renders with no badge. Worth surfacing here
        # rather than leaving it to be noticed in Discord.
        missing = sorted(referenced - on_disk)

        self.stdout.write(f"{len(on_disk)} file(s) in {LOGO_DIR}/, {len(referenced)} referenced by a team")

        if missing:
            self.stdout.write(self.style.ERROR(f"{len(missing)} team(s) point at a file that is gone:"))
            for name in missing[:10]:
                self.stdout.write(f"  {name}")
            if len(missing) > 10:
                self.stdout.write(f"  ... and {len(missing) - 10} more")
            self.stdout.write("  Re-run the relevant sync to fetch them again.")

        if not orphans:
            self.stdout.write(self.style.SUCCESS("No orphaned logo files."))
            return

        total_bytes = 0
        for name in orphans:
            try:
                total_bytes += default_storage.size(name)
            except OSError:
                pass

        self.stdout.write(self.style.WARNING(f"{len(orphans)} orphaned file(s), {total_bytes / 1024 / 1024:.1f} MiB"))

        if not options["delete"]:
            for name in orphans[:10]:
                self.stdout.write(f"  {name}")
            if len(orphans) > 10:
                self.stdout.write(f"  ... and {len(orphans) - 10} more")
            self.stdout.write("")
            self.stdout.write("Nothing deleted. Re-run with --delete to remove them.")
            return

        deleted = 0
        for name in orphans:
            try:
                default_storage.delete(name)
                deleted += 1
            except OSError as e:
                self.stdout.write(self.style.ERROR(f"Could not delete {name}: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} orphaned logo file(s)."))
