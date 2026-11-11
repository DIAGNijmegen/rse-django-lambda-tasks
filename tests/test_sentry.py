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
from lambda_tasks.logging import task_logger
from lambda_tasks.models import (
    MaxRetriesExceededError,
    SQSLambdaTask,
    SQSLambdaTaskMessage,
    TaskRecord,
)
from lambda_tasks.sentry import task_scope
from lambda_tasks.settings import LambdaTasksSettings

# ---------------------------------------------------------------------------
# Tasks (module level for import_string resolution)
# ---------------------------------------------------------------------------


@lambda_task
def _sentry_task_ok(*, x: int) -> int:
    sentry_sdk.capture_message("during task")
    return x * 2


@lambda_task
def _sentry_task_raises(*, msg: str) -> None:
    # A plain raise — the library's failure path reports this to Sentry; the
    # task itself does not call capture_exception.
    raise RuntimeError(msg)


@lambda_task
def _sentry_task_logs_error(*, msg: str) -> str:
    # Logs an error via task_logger without raising. With a host-configured
    # LoggingIntegration(event_level=ERROR) this surfaces as a Sentry event on
    # both SDK 2.x and 3.0.
    try:
        raise ValueError(msg)
    except ValueError as error:
        task_logger.error(error, exc_info=True)
    return "logged"


@lambda_task(retry_on=(RuntimeError,))
def _sentry_task_retryable(*, msg: str) -> None:
    # Matches retry_on — on a non-terminal attempt the executor re-enqueues
    # rather than treating it as a terminal failure.
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_name(wrapper) -> str:
    func = wrapper.__wrapped__
    return f"{func.__module__}.{func.__qualname__}"


def _make_message(*, task_name: str, kwargs: dict) -> SQSLambdaTaskMessage:
    return SQSLambdaTaskMessage(task_name=task_name, kwargs=kwargs)


class _CapturedTrace:
    """A captured root span/transaction, normalised across SDK 2.x and 3.0.

    ``name`` is the task name (the transaction name on 2.x, the span name on
    3.0). ``meta`` merges every place task metadata may live: scope/transaction
    tags (2.x) and span attributes (3.0).
    """

    def __init__(self, *, name: str, meta: dict) -> None:
        self.name = name
        self.meta = meta


def _normalise_2x_transaction(event: dict) -> _CapturedTrace:
    trace = event.get("contexts", {}).get("trace", {})
    meta = {**event.get("tags", {}), **(trace.get("data") or {})}
    return _CapturedTrace(name=event.get("transaction", ""), meta=meta)


def _normalise_3x_spans(payload: dict) -> list[_CapturedTrace]:
    traces: list[_CapturedTrace] = []
    for span in payload.get("items", []):
        # 3.0 attributes are {key: {"value": ..., "type": ...}}.
        meta = {k: v.get("value") for k, v in span.get("attributes", {}).items()}
        traces.append(_CapturedTrace(name=span.get("name", ""), meta=meta))
    return traces


class _CapturingTransport(Transport):
    """In-memory Sentry transport recording error and trace events.

    Works on both SDK 2.x (transactions, ``get_transaction_event``) and 3.0
    (stream-mode spans, envelope items of type ``span``).
    """

    def __init__(self, options) -> None:
        super().__init__(options)
        self.errors: list[dict] = []
        self.traces: list[_CapturedTrace] = []

    def capture_envelope(self, envelope) -> None:
        for item in envelope.items:
            if item.type == "event":
                error = item.get_event()
                if error is not None:
                    self.errors.append(error)
            elif item.type == "transaction":  # SDK 2.x
                self.traces.append(_normalise_2x_transaction(item.payload.json))
            elif item.type == "span":  # SDK 3.0 stream mode
                self.traces.extend(_normalise_3x_spans(item.payload.json))


class _Captured:
    """Convenience view over captured error and trace events."""

    def __init__(self, transport: _CapturingTransport) -> None:
        self._transport = transport

    @property
    def errors(self) -> list[dict]:
        return self._transport.errors

    @property
    def traces(self) -> list[_CapturedTrace]:
        sentry_sdk.flush()
        return self._transport.traces

    def trace_named(self, name: str) -> _CapturedTrace:
        """Return the single captured root trace whose name is *name*.

        On SDK 3.0 the Django integration auto-instruments DB/signal spans, so
        the transport sees many spans; select ours by name rather than by
        position. Flushes first because 3.0 stream mode sends spans
        asynchronously in batches.
        """
        sentry_sdk.flush()
        matches = [trace for trace in self._transport.traces if trace.name == name]
        assert len(matches) == 1, (
            f"expected exactly one trace named {name!r}, "
            f"got {[t.name for t in self._transport.traces]}"
        )
        return matches[0]


@pytest.fixture
def sentry():
    """Initialise a real Sentry client with an in-memory transport + tracing.

    ``traces_sample_rate=1.0`` ensures the per-task trace is always sampled so
    it can be asserted on. The client is torn down afterwards so it does not
    leak into other tests.
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


@pytest.fixture
def sentry_with_logging():
    """Like ``sentry`` but with ``LoggingIntegration(event_level=ERROR)``.

    Mirrors a host application that wants ``logger.error(...)`` calls promoted
    to Sentry events. On SDK 2.x this was the default; on 3.0 the default is
    ``event_level=None``, so the integration must be configured explicitly for
    error logs to surface. Testing with it enabled proves ``task_logger.error``
    is reported on both SDK versions.
    """
    import logging

    from sentry_sdk.integrations.logging import LoggingIntegration

    transport = _CapturingTransport({})
    sentry_sdk.init(
        dsn="http://public@localhost/1",
        transport=transport,
        traces_sample_rate=1.0,
        integrations=[LoggingIntegration(event_level=logging.ERROR)],
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

        assert sentry.trace_named("a.b.c").name == "a.b.c"

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

        # Metadata lands as transaction tags on 2.x and span attributes on 3.0;
        # the captured trace normalises both into `meta`.
        meta = sentry.trace_named("a.b.c").meta
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
        assert sentry.trace_named(task_name).name == task_name
        assert sentry.errors[0]["transaction"] == task_name
        assert sentry.errors[0]["tags"]["lambda_task.message_id"] == message_id

    def test_failing_task_reports_error_attributed_to_task_name(self, sentry):
        # The task body is a plain `raise` (no capture_exception). The library's
        # failure path reports it to Sentry — consistently on SDK 2.x and 3.0,
        # where a merely-caught exception would otherwise produce no error.
        task_name = _task_name(_sentry_task_raises)
        message = _make_message(task_name=task_name, kwargs={"msg": "boom"})
        message_id = str(uuid.uuid4())

        with patch("lambda_tasks.models.TimeoutContext"):
            message.execute_immediately(message_id=message_id)

        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.FAILED
        assert sentry.trace_named(task_name).name == task_name

        assert len(sentry.errors) == 1
        error = sentry.errors[0]
        assert error["transaction"] == task_name
        assert error["tags"]["lambda_task"] == task_name
        assert error["tags"]["lambda_task.message_id"] == message_id
        exc_types = [v["type"] for v in error["exception"]["values"]]
        assert "RuntimeError" in exc_types


# ---------------------------------------------------------------------------
# Exception / error-log reporting on the failure paths
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestTaskFailureReporting:
    def test_terminal_failure_captures_exception_event(self, sentry):
        # Plain-failure branch: the library calls capture_exception, producing a
        # single error event with the task's exception type — on 2.x and 3.0.
        task_name = _task_name(_sentry_task_raises)
        message = _make_message(task_name=task_name, kwargs={"msg": "kaboom"})
        message_id = str(uuid.uuid4())

        with patch("lambda_tasks.models.TimeoutContext"):
            message.execute_immediately(message_id=message_id)

        assert TaskRecord.objects.get(pk=message_id).status == (
            TaskRecord.TaskStatus.FAILED
        )
        assert len(sentry.errors) == 1
        exc_types = [v["type"] for v in sentry.errors[0]["exception"]["values"]]
        assert exc_types == ["RuntimeError"]

    def test_max_retries_exceeded_captures_exception_event(self, sentry):
        # Max-retries terminal branch: the library captures the exception before
        # raising MaxRetriesExceededError, so the original failure is reported.
        task_name = _task_name(_sentry_task_retryable)
        message = SQSLambdaTaskMessage(
            task_name=task_name,
            kwargs={"msg": "exhausted"},
            n_retries=LambdaTasksSettings().MAX_RETRIES,
        )
        message_id = str(uuid.uuid4())

        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(SQSLambdaTask, "execute_on_commit") as mock_enqueue:
                with pytest.raises(MaxRetriesExceededError):
                    message.execute_immediately(message_id=message_id)

        # No retry was enqueued — this was terminal.
        mock_enqueue.assert_not_called()
        assert TaskRecord.objects.get(pk=message_id).status == (
            TaskRecord.TaskStatus.FAILED
        )
        # The captured event is the task's own exception, attributed to the task.
        errors_with_exc = [e for e in sentry.errors if e.get("exception")]
        assert len(errors_with_exc) == 1
        exc_types = [v["type"] for v in errors_with_exc[0]["exception"]["values"]]
        assert "RuntimeError" in exc_types
        assert errors_with_exc[0]["transaction"] == task_name

    def test_retrying_failure_does_not_capture_exception_event(self, sentry):
        # Non-terminal retry branch: the task will be re-enqueued, so the library
        # deliberately does NOT report the transient failure to Sentry (avoiding
        # noise on every retry). Only terminal failures are captured.
        task_name = _task_name(_sentry_task_retryable)
        message = SQSLambdaTaskMessage(
            task_name=task_name,
            kwargs={"msg": "transient"},
            n_retries=0,
        )
        message_id = str(uuid.uuid4())

        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(SQSLambdaTask, "execute_on_commit") as mock_enqueue:
                message.execute_immediately(message_id=message_id)

        # A retry was enqueued and the record is RETRIED (non-terminal).
        mock_enqueue.assert_called_once()
        assert TaskRecord.objects.get(pk=message_id).status == (
            TaskRecord.TaskStatus.RETRIED
        )
        # No exception event was reported for the transient failure.
        assert [e for e in sentry.errors if e.get("exception")] == []

    def test_task_logger_error_is_reported_with_logging_integration(
        self, sentry_with_logging
    ):
        # task_logger.error(...) inside a task surfaces as a Sentry event when
        # the host enables LoggingIntegration(event_level=ERROR) — the explicit
        # config needed on 3.0 (and the default on 2.x).
        task_name = _task_name(_sentry_task_logs_error)
        message = _make_message(task_name=task_name, kwargs={"msg": "logged failure"})
        message_id = str(uuid.uuid4())

        with patch("lambda_tasks.models.TimeoutContext"):
            message.execute_immediately(message_id=message_id)

        # The task itself did not raise, so it succeeds overall.
        assert TaskRecord.objects.get(pk=message_id).status == (
            TaskRecord.TaskStatus.SUCCEEDED
        )

        log_events = [
            e
            for e in sentry_with_logging.errors
            if e.get("logger") == "lambda_tasks.task"
        ]
        assert len(log_events) == 1
        assert log_events[0]["level"] == "error"
        # The error log is attributed to the running task.
        assert log_events[0]["transaction"] == task_name
