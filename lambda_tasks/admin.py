"""Admin registration for TaskRecord."""

from django.contrib import admin
from django.db.models import DurationField, ExpressionWrapper, F, QuerySet
from django.http import HttpRequest
from django.utils.module_loading import import_string

from lambda_tasks.models import (
    SQSLambdaTask,
    SQSLambdaTaskMessage,
    TaskRecord,
)


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
    readonly_fields = (
        "pk",
        "task_name",
        "kwargs",
        "n_retries",
        "status",
        "start_time",
        "end_time",
        "result",
        "traceback",
    )
    actions = ["replay_tasks"]

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

    @admin.action(description="Replay selected tasks", permissions=("change",))
    def replay_tasks(self, request: HttpRequest, queryset: QuerySet) -> None:
        for record in queryset:
            queue = import_string(record.task_name).queue
            task = SQSLambdaTask(
                message=SQSLambdaTaskMessage(
                    task_name=record.task_name,
                    kwargs=record.kwargs,
                    n_retries=0,
                ),
                delay=0,
                queue=queue,
            )
            task.execute_on_commit()
