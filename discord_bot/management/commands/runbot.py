from django.conf import settings
from django.core.management.base import BaseCommand

from discord_bot.bot import OtterBallBot


class Command(BaseCommand):
    help = "Launches the Discord prediction bot application loop"

    def handle(self, *args, **options):
        bot_token: str | None = getattr(settings, "DISCORD_BOT_TOKEN", None)

        if not bot_token:
            self.stderr.write(self.style.ERROR("DISCORD_BOT_TOKEN is not set in settings.py"))
            return

        self.stdout.write(self.style.SUCCESS("Starting async event loop for discord.py..."))

        bot = OtterBallBot()

        try:
            bot.run(bot_token, log_handler=None)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nBot execution intercepted by user. Shutting down..."))
