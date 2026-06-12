"""Built-in maintenance tasks for django-lambda-tasks."""

import re

import boto3
from django.core.exceptions import ImproperlyConfigured
from django.utils.timezone import now, timedelta

from lambda_tasks.decorators import lambda_task
from lambda_tasks.models import SQSLambdaTaskMessage, TaskRecord
from lambda_tasks.settings import BatchQueueConfig, LambdaTasksSettings


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


def _sanitize_job_name(*, task_name: str) -> str:
    """Sanitize a task name for use as an AWS Batch job name.

    Batch job names allow alphanumeric, hyphens, and underscores, max 128 chars.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", task_name)
    return sanitized[:128]


@lambda_task
def submit_batch_job(*, message_json: str, batch_queue: str) -> str:
    """Submit a batch task to AWS Batch.

    Reads the job queue and job definition from LAMBDA_TASKS_QUEUES
    settings, then calls batch.submit_job() with the task message passed
    as an environment variable override.

    Returns the Batch job ID.
    """
    conf = LambdaTasksSettings()
    queues = conf.QUEUES

    if batch_queue not in queues:
        raise ImproperlyConfigured(
            f"Queue '{batch_queue}' is not defined in LAMBDA_TASKS_QUEUES."
        )

    queue_config = queues[batch_queue]

    if not isinstance(queue_config, BatchQueueConfig):
        raise ImproperlyConfigured(f"Queue '{batch_queue}' is not a Batch queue.")

    message = SQSLambdaTaskMessage.model_validate_json(message_json)
    job_name = _sanitize_job_name(task_name=message.task_name)

    client = boto3.client("batch")
    response = client.submit_job(
        jobName=job_name,
        jobQueue=queue_config.job_queue_arn,
        jobDefinition=queue_config.job_definition_arn,
        containerOverrides={
            "environment": [
                {"name": "LAMBDA_TASKS_MESSAGE", "value": message_json},
            ],
        },
    )

    return response["jobId"]
