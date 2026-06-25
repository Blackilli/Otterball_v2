from django.apps import AppConfig


class SportsConfig(AppConfig):
    name = "sports"

    def ready(self):
        import sports.signals
