"""Tests for lambda_tasks.batch_worker entry point."""

import uuid

import pytest

from lambda_tasks.decorators import lambda_task
from lambda_tasks.handler import main
from lambda_tasks.models import SQSLambdaTaskMessage, TaskRecord

QUEUES = {
    "default": {"queue_url": "https://sqs.us-east-1.amazonaws.com/000000000000/default"}
}


# Module-level tasks for import_string resolution


@lambda_task
def _worker_task(*, value: int) -> str:
    return "ok"


def test_missing_env_var_returns_1(monkeypatch):
    monkeypatch.delenv("LAMBDA_TASKS_MESSAGE", raising=False)
    assert main() == 1


@pytest.mark.django_db
def test_valid_message_executes_task(monkeypatch, settings):
    settings.LAMBDA_TASKS_EAGER = True
    settings.LAMBDA_TASKS_QUEUES = QUEUES

    task_name = f"{_worker_task.__module__}.{_worker_task.__wrapped__.__qualname__}"
    message = SQSLambdaTaskMessage(task_name=task_name, kwargs={"value": 1})
    job_id = str(uuid.uuid4())

    monkeypatch.setenv("LAMBDA_TASKS_MESSAGE", message.model_dump_json())
    monkeypatch.setenv("AWS_BATCH_JOB_ID", job_id)

    result = main()

    assert result == 0
    record = TaskRecord.objects.get(pk=job_id)
    assert record.status == TaskRecord.TaskStatus.SUCCEEDED


def test_invalid_json_returns_1(monkeypatch):
    monkeypatch.setenv("LAMBDA_TASKS_MESSAGE", "not-valid-json")
    monkeypatch.delenv("AWS_BATCH_JOB_ID", raising=False)
    assert main() == 1


@pytest.mark.django_db
def test_uses_uuid_when_no_batch_job_id(monkeypatch, settings):
    settings.LAMBDA_TASKS_EAGER = True
    settings.LAMBDA_TASKS_QUEUES = QUEUES

    task_name = f"{_worker_task.__module__}.{_worker_task.__wrapped__.__qualname__}"
    message = SQSLambdaTaskMessage(task_name=task_name, kwargs={"value": 5})

    monkeypatch.setenv("LAMBDA_TASKS_MESSAGE", message.model_dump_json())
    monkeypatch.delenv("AWS_BATCH_JOB_ID", raising=False)

    result = main()

    assert result == 0
    assert TaskRecord.objects.count() == 1
