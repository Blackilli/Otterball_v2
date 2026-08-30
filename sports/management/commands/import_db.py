import shutil
import tarfile
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from sports.management.commands.export_db import BUNDLE_MEDIA_DIR


class Command(BaseCommand):
    help = (
        "Restores the database - and the media files, when the input is a bundle - from an "
        "export produced by export_db. Destructive by nature: reloading into a database that "
        "already has rows will hit primary key conflicts unless --flush is used to wipe "
        "existing data first."
    )

    def add_arguments(self, parser):
        parser.add_argument("input", help="Path to a bundle or fixture produced by export_db")
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Wipe all existing data before loading, for a truly clean restore",
        )
        parser.add_argument(
            "--no-media",
            action="store_true",
            help="Restore the database only, leaving the existing media files untouched",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt",
        )

    def handle(self, *args, **options):
        input_path = Path(options["input"])
        if not input_path.exists():
            raise CommandError(f"No such file: {input_path}")

        # A .json.gz fixture is gzip too, so sniff the actual tar header rather than the name -
        # that also lets a bundle be restored under any filename.
        is_bundle = tarfile.is_tarfile(input_path)
        restore_media = is_bundle and not options["no_media"]

        if not options["yes"]:
            warning = " This will WIPE ALL EXISTING DATA before loading." if options["flush"] else ""
            if restore_media:
                warning += f" Media files in {settings.MEDIA_ROOT} will be overwritten."
            confirm = input(f"About to load {input_path} into the database.{warning} Continue? [y/N] ")
            if confirm.strip().lower() not in ("y", "yes"):
                self.stdout.write("Aborted.")
                return

        if not is_bundle:
            self._load(input_path, flush=options["flush"])
            self.stdout.write(self.style.SUCCESS(f"Restored database from {input_path} (no media in this export)"))
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            extracted = Path(tmpdir)
            with tarfile.open(input_path) as tar:
                # filter="data" refuses absolute paths, ".." traversal and special files.
                tar.extractall(extracted, filter="data")

            fixtures = sorted(extracted.glob("data.json*"))
            if not fixtures:
                raise CommandError(f"{input_path} is a tarball but contains no data.json - not an export_db bundle.")

            self._load(fixtures[0], flush=options["flush"])

            restored = self._restore_media(extracted / BUNDLE_MEDIA_DIR) if restore_media else 0

        media_note = f" and {restored} media file(s)" if restore_media else " (media left untouched)"
        self.stdout.write(self.style.SUCCESS(f"Restored database{media_note} from {input_path}"))

    @staticmethod
    def _load(fixture_path: Path, *, flush: bool) -> None:
        if flush:
            call_command("flush", interactive=False)
        call_command("loaddata", str(fixture_path))

    @staticmethod
    def _restore_media(source: Path) -> int:
        """Copies the bundled media over MEDIA_ROOT. Additive on purpose: files already there
        that the bundle does not mention are left alone rather than deleted, so a restore can
        never be the thing that loses a logo. `manage.py prune_team_logos` clears real orphans."""
        if not source.is_dir():
            return 0

        media_root = Path(settings.MEDIA_ROOT)
        count = 0
        for media_file in source.rglob("*"):
            if not media_file.is_file():
                continue
            destination = media_root / media_file.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(media_file, destination)
            count += 1
        return count
