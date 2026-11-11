"""Tests for lambda_tasks.sentry and its integration with execute_immediately.

Verifies that task execution opens a per-task Sentry trace (one transaction
named after the task, with runtime/metadata) and attributes error events to the
task name — on both Lambda and Batch — and that everything degrades to a no-op
when Sentry is not initialised.
"""

import threading
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
from lambda_tasks.sentry import handler_error_scope, task_scope
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


def _active_root(name: str):
    """Start an outer root transaction/segment, as AwsLambdaIntegration would.

    Uses ``start_transaction`` on SDK 2.x and ``start_span`` on 3.0 so the test
    simulates an already-active invocation trace on both SDK versions.
    """
    if hasattr(sentry_sdk, "start_transaction"):
        return sentry_sdk.start_transaction(name=name, op="function.aws")
    else:
        return sentry_sdk.start_span(name=name)


class _CapturedTrace:
    """A captured root span/transaction, normalised across SDK 2.x and 3.0.

    ``name`` is the task name (the transaction name on 2.x, the span name on
    3.0). ``meta`` merges every place task metadata may live: scope/transaction
    tags (2.x) and span attributes (3.0). ``is_root`` is True when this is a
    root transaction/segment rather than a child span.
    """

    def __init__(self, *, name: str, meta: dict, is_root: bool = True) -> None:
        self.name = name
        self.meta = meta
        self.is_root = is_root


def _normalise_2x_transaction(event: dict) -> list[_CapturedTrace]:
    """Flatten a 2.x transaction envelope into its root plus child spans.

    On 2.x child spans are bundled inside the transaction payload rather than
    sent separately, so we expand them here to mirror the 3.0 shape.
    """
    trace = event.get("contexts", {}).get("trace", {})
    root_meta = {**event.get("tags", {}), **(trace.get("data") or {})}
    traces = [
        _CapturedTrace(name=event.get("transaction", ""), meta=root_meta, is_root=True)
    ]
    for span in event.get("spans", []):
        meta = {**(span.get("tags") or {}), **(span.get("data") or {})}
        traces.append(
            _CapturedTrace(
                name=span.get("description") or span.get("op") or "",
                meta=meta,
                is_root=False,
            )
        )
    return traces


def _normalise_3x_spans(payload: dict) -> list[_CapturedTrace]:
    traces: list[_CapturedTrace] = []
    for span in payload.get("items", []):
        # 3.0 attributes are {key: {"value": ..., "type": ...}}.
        meta = {k: v.get("value") for k, v in span.get("attributes", {}).items()}
        traces.append(
            _CapturedTrace(
                name=span.get("name", ""),
                meta=meta,
                is_root=bool(span.get("is_segment")),
            )
        )
    return traces


class _CapturingTransport(Transport):
    """In-memory Sentry transport recording error and trace events.

    Works on both SDK 2.x (transactions, ``get_transaction_event``) and 3.0
    (stream-mode spans, envelope items of type ``span``).

    Access is guarded by a lock and reads return snapshots: ``sentry_sdk`` may
    deliver envelopes from a background thread (observed on some 3.x
    pre-releases), so appends and reads must not race.
    """

    def __init__(self, options) -> None:
        super().__init__(options)
        self._lock = threading.Lock()
        self._errors: list[dict] = []
        self._traces: list[_CapturedTrace] = []

    def capture_envelope(self, envelope) -> None:
        for item in envelope.items:
            if item.type == "event":
                error = item.get_event()
                if error is not None:
                    with self._lock:
                        self._errors.append(error)
            elif item.type == "transaction":  # SDK 2.x
                with self._lock:
                    self._traces.extend(_normalise_2x_transaction(item.payload.json))
            elif item.type == "span":  # SDK 3.0 stream mode
                with self._lock:
                    self._traces.extend(_normalise_3x_spans(item.payload.json))

    def snapshot_errors(self) -> list[dict]:
        with self._lock:
            return list(self._errors)

    def snapshot_traces(self) -> list[_CapturedTrace]:
        with self._lock:
            return list(self._traces)


class _Captured:
    """Convenience view over captured error and trace events."""

    def __init__(self, transport: _CapturingTransport) -> None:
        self._transport = transport

    @property
    def errors(self) -> list[dict]:
        return self._transport.snapshot_errors()

    @property
    def traces(self) -> list[_CapturedTrace]:
        sentry_sdk.flush()
        return self._transport.snapshot_traces()

    def trace_named(self, name: str) -> _CapturedTrace:
        """Return the single captured root trace whose name is *name*.

        On SDK 3.0 the Django integration auto-instruments DB/signal spans, so
        the transport sees many spans; select ours by name rather than by
        position. Flushes first because 3.0 stream mode sends spans
        asynchronously in batches.
        """
        sentry_sdk.flush()
        traces = self._transport.snapshot_traces()
        matches = [trace for trace in traces if trace.name == name]
        assert len(matches) == 1, (
            f"expected exactly one trace named {name!r}, "
            f"got {[t.name for t in traces]}"
        )
        return matches[0]

    def root_names(self) -> list[str]:
        """Names of all captured root transactions/segments (not child spans).

        Used to assert that a task does not create its own root when it runs
        inside an already-active trace (the Lambda case).
        """
        sentry_sdk.flush()
        return [t.name for t in self._transport.snapshot_traces() if t.is_root]

    def task_trace(self, name: str) -> _CapturedTrace:
        """Return the single captured trace (root or child span) named *name*."""
        sentry_sdk.flush()
        traces = self._transport.snapshot_traces()
        matches = [t for t in traces if t.name == name]
        assert len(matches) == 1, (
            f"expected exactly one trace named {name!r}, "
            f"got {[t.name for t in traces]}"
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
# Trace nesting / volume (the core of the child-span fix)
# ---------------------------------------------------------------------------


class TestTaskTraceNesting:
    def test_task_is_child_span_when_trace_already_active(self, sentry):
        # Lambda case: AwsLambdaIntegration starts an invocation transaction, so
        # the task must NOT open a second root transaction — it nests as a
        # child span. This is what keeps task tracing from doubling trace
        # volume and defeating traces_sample_rate.
        with _active_root("lambda-invocation"):
            with task_scope(task_name="myapp.tasks.greet", message_id="m", n_retries=0):
                pass

        # The task is a child span (not a root), and it did not create its own
        # root transaction — the only root is the invocation.
        assert sentry.task_trace("myapp.tasks.greet").is_root is False
        assert "myapp.tasks.greet" not in sentry.root_names()
        assert "lambda-invocation" in sentry.root_names()

    def test_task_is_root_when_no_trace_active(self, sentry):
        # Batch case: no Lambda integration, nothing active, so the task opens
        # its own root transaction/segment and is traced on its own.
        with task_scope(task_name="myapp.tasks.greet", message_id="m", n_retries=0):
            pass

        task_trace = sentry.task_trace("myapp.tasks.greet")
        assert task_trace.is_root is True
        assert "myapp.tasks.greet" in sentry.root_names()

    def test_task_child_span_carries_metadata(self, sentry):
        with _active_root("lambda-invocation"):
            with task_scope(
                task_name="myapp.tasks.greet", message_id="mid-9", n_retries=1
            ):
                pass

        meta = sentry.task_trace("myapp.tasks.greet").meta
        assert meta["lambda_task"] == "myapp.tasks.greet"
        assert meta["lambda_task.message_id"] == "mid-9"


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

    def test_failing_task_reports_error_attributed_to_task_name(
        self, sentry_with_logging
    ):
        # The task body is a plain `raise`. The library logs it at ERROR, which
        # the host's LoggingIntegration turns into a Sentry event — attributed
        # to the task via the active task_scope tags. Consistent on 2.x and 3.0.
        sentry = sentry_with_logging
        task_name = _task_name(_sentry_task_raises)
        message = _make_message(task_name=task_name, kwargs={"msg": "boom"})
        message_id = str(uuid.uuid4())

        with patch("lambda_tasks.models.TimeoutContext"):
            message.execute_immediately(message_id=message_id)

        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.FAILED

        errors_with_exc = [e for e in sentry.errors if e.get("exception")]
        assert len(errors_with_exc) == 1
        error = errors_with_exc[0]
        assert error["tags"]["lambda_task"] == task_name
        assert error["tags"]["lambda_task.message_id"] == message_id
        exc_types = [v["type"] for v in error["exception"]["values"]]
        assert "RuntimeError" in exc_types


# ---------------------------------------------------------------------------
# Exception / error-log reporting on the failure paths
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestTaskFailureReporting:
    def test_terminal_failure_reports_exception_event(self, sentry_with_logging):
        # Plain-failure branch: the library logs the exception at ERROR; with the
        # host's LoggingIntegration that becomes a single Sentry event with the
        # task's exception type — on 2.x and 3.0.
        sentry = sentry_with_logging
        task_name = _task_name(_sentry_task_raises)
        message = _make_message(task_name=task_name, kwargs={"msg": "kaboom"})
        message_id = str(uuid.uuid4())

        with patch("lambda_tasks.models.TimeoutContext"):
            message.execute_immediately(message_id=message_id)

        assert TaskRecord.objects.get(pk=message_id).status == (
            TaskRecord.TaskStatus.FAILED
        )
        errors_with_exc = [e for e in sentry.errors if e.get("exception")]
        assert len(errors_with_exc) == 1
        exc_types = [v["type"] for v in errors_with_exc[0]["exception"]["values"]]
        assert exc_types == ["RuntimeError"]

    def test_max_retries_exceeded_reports_exception_event(self, sentry_with_logging):
        # Max-retries terminal branch: the library logs the exception at ERROR
        # before raising MaxRetriesExceededError, so the original failure is
        # reported (via LoggingIntegration).
        sentry = sentry_with_logging
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
        # The reported event is the task's own exception, attributed to the task.
        errors_with_exc = [e for e in sentry.errors if e.get("exception")]
        assert len(errors_with_exc) == 1
        exc_types = [v["type"] for v in errors_with_exc[0]["exception"]["values"]]
        assert "RuntimeError" in exc_types
        assert errors_with_exc[0]["tags"]["lambda_task"] == task_name

    def test_retrying_failure_is_not_reported(self, sentry_with_logging):
        # Non-terminal retry branch: the task will be re-enqueued, so the library
        # logs the retry at WARNING (not ERROR) and does not report it to Sentry
        # — avoiding noise on every retry. Only terminal failures are reported.
        sentry = sentry_with_logging
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


# ---------------------------------------------------------------------------
# Handler-level failure reporting (outside any task scope)
# ---------------------------------------------------------------------------


class TestHandlerErrorScope:
    def test_tags_apply_to_logged_event(self, sentry_with_logging):
        # handler_error_scope tags the scope; an error log inside it (captured
        # by LoggingIntegration) carries the tags.
        import logging

        with handler_error_scope(message_id="msg-123"):
            try:
                raise ValueError("malformed SQS message")
            except ValueError:
                logging.getLogger("lambda_tasks.handler").error("failed", exc_info=True)

        errors = [e for e in sentry_with_logging.errors if e.get("exception")]
        assert len(errors) == 1
        tags = errors[0].get("tags") or {}
        assert tags.get("lambda_task.handler_error") in (True, "True")
        assert tags.get("lambda_task.message_id") == "msg-123"
        exc_types = [v["type"] for v in errors[0]["exception"]["values"]]
        assert "ValueError" in exc_types

    def test_message_id_optional(self, sentry_with_logging):
        import logging

        with handler_error_scope():
            try:
                raise ValueError("no message id")
            except ValueError:
                logging.getLogger("lambda_tasks.handler").error("failed", exc_info=True)

        errors = [e for e in sentry_with_logging.errors if e.get("exception")]
        assert len(errors) == 1
        assert "lambda_task.message_id" not in (errors[0].get("tags") or {})

    def test_noop_when_sentry_uninitialised(self):
        # No sentry_sdk.init() — the context manager must still work.
        with handler_error_scope(message_id="m"):
            pass

    def test_noop_when_sentry_module_missing(self):
        with patch("lambda_tasks.sentry.sentry_sdk", None):
            with handler_error_scope(message_id="m"):
                pass
