"""Admin registration for TaskRecord."""

from django.contrib import admin
from django.db.models import DurationField, ExpressionWrapper, F, QuerySet
from django.http import HttpRequest

from lambda_tasks.models import TaskRecord


@admin.register(TaskRecord)
class TaskRecordAdmin(admin.ModelAdmin):
    list_display = (
        "pk",
        "task_name",
        "status",
        "start_time",
        "end_time",
        "n_retries",
        "duration",
        "result",
    )
    list_filter = ("status", "task_name")
    date_hierarchy = "start_time"
    search_fields = ("pk", "kwargs")

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return (
            super()
            .get_queryset(request)
            .annotate(
                computed_duration=ExpressionWrapper(
                    F("end_time") - F("start_time"), output_field=DurationField()
                )
            )
        )

    @admin.display(description="Duration", ordering="computed_duration")
    def duration(self, obj: TaskRecord) -> str | None:
        d = getattr(obj, "computed_duration", None)
        if d is None:
            return None
        total_seconds = d.total_seconds()
        return f"{total_seconds:.3f}s"
