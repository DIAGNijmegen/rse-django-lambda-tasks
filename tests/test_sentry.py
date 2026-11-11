"""Tests for lambda_tasks.sentry and its integration with execute_immediately.

Verifies that task execution opens a per-task Sentry trace (one transaction
named after the task, with runtime/metadata) and attributes error events to the
task name — on both Lambda and Batch — and that everything degrades to a no-op
when Sentry is not initialised.
"""

import uuid
from unittest.mock import patch

import pytest
import sentry_sdk
from sentry_sdk.transport import Transport

from lambda_tasks.decorators import lambda_task
from lambda_tasks.models import SQSLambdaTaskMessage, TaskRecord
from lambda_tasks.sentry import task_scope

# ---------------------------------------------------------------------------
# Tasks (module level for import_string resolution)
# ---------------------------------------------------------------------------


@lambda_task
def _sentry_task_ok(*, x: int) -> int:
    sentry_sdk.capture_message("during task")
    return x * 2


@lambda_task
def _sentry_task_raises(*, msg: str) -> None:
    try:
        raise RuntimeError(msg)
    except RuntimeError:
        sentry_sdk.capture_exception()
        raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_name(wrapper) -> str:
    func = wrapper.__wrapped__
    return f"{func.__module__}.{func.__qualname__}"


def _make_message(*, task_name: str, kwargs: dict) -> SQSLambdaTaskMessage:
    return SQSLambdaTaskMessage(task_name=task_name, kwargs=kwargs)


class _CapturingTransport(Transport):
    """In-memory Sentry transport recording error and transaction events."""

    def __init__(self, options) -> None:
        super().__init__(options)
        self.errors: list[dict] = []
        self.transactions: list[dict] = []

    def capture_envelope(self, envelope) -> None:
        for item in envelope.items:
            error = item.get_event()
            if error is not None:
                self.errors.append(error)
            transaction = item.get_transaction_event()
            if transaction is not None:
                self.transactions.append(transaction)


class _Captured:
    """Convenience view over captured error and transaction events."""

    def __init__(self, transport: _CapturingTransport) -> None:
        self._transport = transport

    @property
    def errors(self) -> list[dict]:
        return self._transport.errors

    @property
    def transactions(self) -> list[dict]:
        return self._transport.transactions


@pytest.fixture
def sentry():
    """Initialise a real Sentry client with an in-memory transport + tracing.

    ``traces_sample_rate=1.0`` ensures the per-task transaction is always
    sampled so it can be asserted on. The client is torn down afterwards so it
    does not leak into other tests.
    """
    transport = _CapturingTransport({})
    sentry_sdk.init(
        dsn="http://public@localhost/1",
        transport=transport,
        traces_sample_rate=1.0,
    )
    try:
        yield _Captured(transport)
    finally:
        sentry_sdk.get_global_scope().set_client(None)


# ---------------------------------------------------------------------------
# task_scope unit behaviour
# ---------------------------------------------------------------------------


class TestTaskScope:
    def test_noop_when_sentry_uninitialised_does_not_raise(self):
        # No sentry_sdk.init() has run, so the scope is a no-op.
        with task_scope(task_name="a.b.c", message_id="mid", n_retries=0):
            pass

    def test_noop_when_sentry_module_missing(self):
        with patch("lambda_tasks.sentry.sentry_sdk", None):
            with task_scope(task_name="a.b.c", message_id="mid", n_retries=0):
                pass

    def test_opens_per_task_transaction(self, sentry):
        with task_scope(task_name="a.b.c", message_id="mid-1", n_retries=2):
            pass

        assert len(sentry.transactions) == 1
        assert sentry.transactions[0]["transaction"] == "a.b.c"

    def test_error_event_attributed_to_task_name(self, sentry):
        with task_scope(task_name="a.b.c", message_id="mid-1", n_retries=2):
            sentry_sdk.capture_message("inside")

        assert len(sentry.errors) == 1
        error = sentry.errors[0]
        assert error["transaction"] == "a.b.c"
        assert error["tags"]["lambda_task"] == "a.b.c"
        assert error["tags"]["lambda_task.message_id"] == "mid-1"
        assert error["tags"]["lambda_task.n_retries"] == 2

    def test_transaction_carries_task_metadata(self, sentry):
        with task_scope(task_name="a.b.c", message_id="mid-1", n_retries=2):
            pass

        # On 2.x metadata lands as tags; on 3.0 as attributes. Accept either so
        # the test survives an SDK upgrade.
        transaction = sentry.transactions[0]
        meta = {
            **transaction.get("tags", {}),
            **(transaction.get("contexts", {}).get("trace", {}).get("data", {})),
        }
        assert meta["lambda_task"] == "a.b.c"
        assert meta["lambda_task.message_id"] == "mid-1"

    def test_scope_is_isolated_to_the_block(self, sentry):
        with task_scope(task_name="a.b.c", message_id="mid", n_retries=0):
            pass
        # Outside the block the transaction name must not leak onto new events.
        sentry_sdk.capture_message("outside")

        assert len(sentry.errors) == 1
        assert sentry.errors[0].get("transaction") != "a.b.c"

    def test_runtime_tag_from_lambda_function_name(self, sentry, monkeypatch):
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-worker-fn")
        monkeypatch.delenv("AWS_BATCH_JQ_NAME", raising=False)

        with task_scope(task_name="a.b.c", message_id="mid", n_retries=0):
            sentry_sdk.capture_message("inside")

        assert sentry.errors[0]["tags"]["lambda_task.runtime"] == "my-worker-fn"

    def test_runtime_tag_from_batch_job_queue_name(self, sentry, monkeypatch):
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
        monkeypatch.setenv("AWS_BATCH_JQ_NAME", "heavy-job-queue")

        with task_scope(task_name="a.b.c", message_id="mid", n_retries=0):
            sentry_sdk.capture_message("inside")

        assert sentry.errors[0]["tags"]["lambda_task.runtime"] == "heavy-job-queue"

    def test_lambda_function_name_takes_precedence_over_batch(
        self, sentry, monkeypatch
    ):
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-worker-fn")
        monkeypatch.setenv("AWS_BATCH_JQ_NAME", "heavy-job-queue")

        with task_scope(task_name="a.b.c", message_id="mid", n_retries=0):
            sentry_sdk.capture_message("inside")

        assert sentry.errors[0]["tags"]["lambda_task.runtime"] == "my-worker-fn"

    def test_no_runtime_tag_when_env_absent(self, sentry, monkeypatch):
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
        monkeypatch.delenv("AWS_BATCH_JQ_NAME", raising=False)

        with task_scope(task_name="a.b.c", message_id="mid", n_retries=0):
            sentry_sdk.capture_message("inside")

        assert "lambda_task.runtime" not in sentry.errors[0]["tags"]


# ---------------------------------------------------------------------------
# Integration with execute_immediately
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestExecuteImmediatelySentry:
    def test_successful_task_opens_transaction_and_tags_errors(self, sentry):
        task_name = _task_name(_sentry_task_ok)
        message = _make_message(task_name=task_name, kwargs={"x": 3})
        message_id = str(uuid.uuid4())

        with patch("lambda_tasks.models.TimeoutContext"):
            message.execute_immediately(message_id=message_id)

        assert TaskRecord.objects.get(pk=message_id).status == (
            TaskRecord.TaskStatus.SUCCEEDED
        )
        assert sentry.transactions[0]["transaction"] == task_name
        assert sentry.errors[0]["transaction"] == task_name
        assert sentry.errors[0]["tags"]["lambda_task.message_id"] == message_id

    def test_failing_task_event_attributed_to_task_name(self, sentry):
        task_name = _task_name(_sentry_task_raises)
        message = _make_message(task_name=task_name, kwargs={"msg": "boom"})
        message_id = str(uuid.uuid4())

        with patch("lambda_tasks.models.TimeoutContext"):
            message.execute_immediately(message_id=message_id)

        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.FAILED
        assert sentry.transactions[0]["transaction"] == task_name
        assert sentry.errors[0]["transaction"] == task_name
        assert sentry.errors[0]["tags"]["lambda_task.message_id"] == message_id
