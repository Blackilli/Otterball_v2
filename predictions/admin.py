from django.contrib import admin

from predictions.models import PoolStageRule, Prediction, PredictionPool

# Register your models here.


@admin.register(Prediction)
class PredictionAdmin(admin.ModelAdmin):
    list_filter = ("pool", "match__stage")
    readonly_fields = (
        "created_at",
        "updated_at",
        "match",
        "pool",
        "user",
        "points_awarded",
        "is_processed",
        "predicted_outcome",
    )


@admin.register(PredictionPool)
class PredictionPoolAdmin(admin.ModelAdmin):
    pass


@admin.register(PoolStageRule)
class PoolStageRuleAdmin(admin.ModelAdmin):
    pass
