from django.apps import AppConfig


class PredictionsConfig(AppConfig):
    name = "predictions"

    def ready(self):
        import predictions.signals
