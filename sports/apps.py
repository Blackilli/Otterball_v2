from django.apps import AppConfig


class SportsConfig(AppConfig):
    name = "sports"

    def ready(self):
        # Connects the worker_ready receiver. Imported here rather than from
        # otterball_v2/celery.py, which is loaded before the app registry is
        # ready; the signal only fires in a worker, so this is inert in the
        # web, bot and beat processes.
        import sports.catchup  # noqa: F401
        import sports.signals  # noqa: F401
