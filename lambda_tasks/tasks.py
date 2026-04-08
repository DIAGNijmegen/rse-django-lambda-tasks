"""Built-in maintenance tasks for django-lambda-tasks."""

from django.utils.timezone import now, timedelta

from lambda_tasks.decorators import lambda_task
from lambda_tasks.models import TaskRecord


@lambda_task
def cleanup_task_records(*, retention_days: int = 7) -> int:
    """Delete TaskRecord rows older than ``retention_days``.

    Returns the number of deleted records.
    """
    cutoff = now() - timedelta(days=retention_days)
    deleted_count, _ = (
        TaskRecord.objects.filter(  # ty: ignore[unresolved-attribute]
            start_time__lt=cutoff,
        )
        .only("pk")
        .delete()
    )
    return deleted_count
