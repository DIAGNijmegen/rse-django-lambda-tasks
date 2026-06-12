"""Tests for the submit_batch_job built-in task and batch execution path."""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from lambda_tasks.decorators import batch_task
from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage, TaskRecord
from lambda_tasks.settings import TaskBackend
from lambda_tasks.tasks import _sanitize_job_name, submit_batch_job

BATCH_QUEUES = {
    "default": {
        "job_queue": "arn:aws:batch:eu-west-1:123456789:job-queue/default",
        "job_definition": "arn:aws:batch:eu-west-1:123456789:job-definition/default:1",
    },
}

QUEUES = {"default": "https://sqs.us-east-1.amazonaws.com/000000000000/default"}


# ---------------------------------------------------------------------------
# Module-level batch tasks for import_string resolution
# ---------------------------------------------------------------------------


@batch_task
def _batch_task_succeeds(*, value: int) -> str:
    return f"done-{value}"


@batch_task(retry_on=(ValueError,))
def _batch_task_retries(*, n: int) -> None:
    raise ValueError("fail")


@batch_task(retry_on=(ConnectionError,))
def _batch_task_connection_error(*, x: int) -> None:
    raise ConnectionError("timeout")


# ---------------------------------------------------------------------------
# Tests: _sanitize_job_name
# ---------------------------------------------------------------------------


class TestSanitizeJobName:
    def test_dots_replaced_with_underscores(self):
        assert (
            _sanitize_job_name(task_name="myapp.tasks.my_task") == "myapp_tasks_my_task"
        )

    def test_special_chars_replaced(self):
        assert _sanitize_job_name(task_name="my@task!name") == "my_task_name"

    def test_truncated_to_128_chars(self):
        long_name = "a" * 200
        assert len(_sanitize_job_name(task_name=long_name)) == 128

    def test_hyphens_preserved(self):
        assert _sanitize_job_name(task_name="my-task-name") == "my-task-name"

    def test_underscores_preserved(self):
        assert _sanitize_job_name(task_name="my_task_name") == "my_task_name"


# ---------------------------------------------------------------------------
# Tests: submit_batch_job
# ---------------------------------------------------------------------------


class TestSubmitBatchJob:
    def test_calls_submit_job_with_correct_params(self, settings):
        settings.LAMBDA_TASKS_BATCH_QUEUES = BATCH_QUEUES
        settings.LAMBDA_TASKS_QUEUES = QUEUES

        message = SQSLambdaTaskMessage(
            task_name="myapp.tasks.process_file", kwargs={"file_id": 1}
        )
        message_json = message.model_dump_json()

        mock_client = MagicMock()
        mock_client.submit_job.return_value = {"jobId": "batch-job-123"}

        with patch("lambda_tasks.tasks.boto3.client", return_value=mock_client):
            result = submit_batch_job(message_json=message_json, batch_queue="default")

        assert result == "batch-job-123"
        mock_client.submit_job.assert_called_once_with(
            jobName="myapp_tasks_process_file",
            jobQueue=BATCH_QUEUES["default"]["job_queue"],
            jobDefinition=BATCH_QUEUES["default"]["job_definition"],
            containerOverrides={
                "environment": [
                    {"name": "LAMBDA_TASKS_MESSAGE", "value": message_json},
                ],
            },
        )

    def test_unknown_queue_raises(self, settings):
        settings.LAMBDA_TASKS_BATCH_QUEUES = BATCH_QUEUES
        settings.LAMBDA_TASKS_QUEUES = QUEUES

        message_json = SQSLambdaTaskMessage(
            task_name="myapp.tasks.x", kwargs={}
        ).model_dump_json()

        with pytest.raises(ImproperlyConfigured, match="not defined"):
            submit_batch_job(message_json=message_json, batch_queue="nonexistent")


# ---------------------------------------------------------------------------
# Tests: SQSLambdaTask with batch backend
# ---------------------------------------------------------------------------


class TestSQSLambdaTaskBatchBackend:
    def test_batch_backend_enqueues_submit_batch_job(self, settings):
        settings.LAMBDA_TASKS_QUEUES = QUEUES
        settings.LAMBDA_TASKS_BATCH_QUEUES = BATCH_QUEUES

        message = SQSLambdaTaskMessage(
            task_name="myapp.tasks.heavy_task", kwargs={"n": 1}
        )
        task = SQSLambdaTask(
            message=message, delay=0, queue="default", backend=TaskBackend.BATCH
        )

        with patch("lambda_tasks.tasks.submit_batch_job.execute_on_commit") as mock_eoc:
            task._execute()

        mock_eoc.assert_called_once_with(
            message_json=message.model_dump_json(),
            batch_queue="default",
        )

    @pytest.mark.django_db
    def test_eager_mode_executes_immediately_for_batch(self, settings):
        settings.LAMBDA_TASKS_EAGER = True
        settings.LAMBDA_TASKS_QUEUES = QUEUES

        task_name = f"{_batch_task_succeeds.__module__}.{_batch_task_succeeds.__wrapped__.__qualname__}"
        message = SQSLambdaTaskMessage(task_name=task_name, kwargs={"value": 42})
        task = SQSLambdaTask(
            message=message, delay=0, queue="default", backend=TaskBackend.BATCH
        )

        task._execute()

        record = TaskRecord.objects.get(task_name=task_name)
        assert record.status == TaskRecord.TaskStatus.SUCCEEDED
        assert record.result == "done-42"


# ---------------------------------------------------------------------------
# Tests: Batch task retry
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBatchTaskRetry:
    def test_retryable_exception_sets_retried_status(self, settings):
        settings.LAMBDA_TASKS_EAGER = True
        settings.LAMBDA_TASKS_QUEUES = QUEUES
        settings.LAMBDA_TASKS_BATCH_QUEUES = BATCH_QUEUES

        task_name = f"{_batch_task_connection_error.__module__}.{_batch_task_connection_error.__wrapped__.__qualname__}"
        message = SQSLambdaTaskMessage(task_name=task_name, kwargs={"x": 1})
        message_id = str(uuid.uuid4())

        with patch.object(SQSLambdaTask, "execute_on_commit"):
            message.execute_immediately(message_id=message_id)

        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.RETRIED

    def test_retry_uses_batch_backend(self, settings):
        settings.LAMBDA_TASKS_EAGER = True
        settings.LAMBDA_TASKS_QUEUES = QUEUES
        settings.LAMBDA_TASKS_BATCH_QUEUES = BATCH_QUEUES

        task_name = f"{_batch_task_retries.__module__}.{_batch_task_retries.__wrapped__.__qualname__}"
        message = SQSLambdaTaskMessage(task_name=task_name, kwargs={"n": 1})
        message_id = str(uuid.uuid4())

        retry_tasks: list[SQSLambdaTask] = []

        def capture_retry(self):
            retry_tasks.append(self)

        with patch.object(SQSLambdaTask, "execute_on_commit", capture_retry):
            message.execute_immediately(message_id=message_id)

        assert len(retry_tasks) == 1
        assert retry_tasks[0].backend == TaskBackend.BATCH
        assert retry_tasks[0].message.n_retries == 1
