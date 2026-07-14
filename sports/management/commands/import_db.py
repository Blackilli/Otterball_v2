from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Restores the database from a fixture produced by export_db. Destructive by nature: "
        "reloading into a database that already has rows will hit primary key conflicts unless "
        "--flush is used to wipe existing data first."
    )

    def add_arguments(self, parser):
        parser.add_argument("input", help="Path to a fixture produced by export_db")
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Wipe all existing data before loading, for a truly clean restore",
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

        if not options["yes"]:
            warning = " This will WIPE ALL EXISTING DATA before loading." if options["flush"] else ""
            confirm = input(f"About to load {input_path} into the database.{warning} Continue? [y/N] ")
            if confirm.strip().lower() not in ("y", "yes"):
                self.stdout.write("Aborted.")
                return

        if options["flush"]:
            call_command("flush", interactive=False)

        call_command("loaddata", str(input_path))
        self.stdout.write(self.style.SUCCESS(f"Restored database from {input_path}"))
