"""Sentry tracing/scope helpers for task execution.

This library does not initialise Sentry itself — the host application owns
``sentry_sdk.init(...)``. These helpers open a per-task trace so that both
errors and performance data are attributed to the running task rather than to
the Lambda function name (set by ``AwsLambdaIntegration``) or to ``"--"`` (on
AWS Batch, where no request or Lambda integration is active).

Each task runs inside its own root span/transaction named after the task, so
with ``traces_sample_rate`` set you get one trace per task on both Lambda and
Batch — not one invocation-level transaction shared across a batch of records.

Future-proofing across the SDK 2.x → 3.0 boundary
--------------------------------------------------
The tracing API changed in SDK 3.0 (stream mode):

* 2.x — the root is a *transaction*. Start it with ``start_transaction`` and
  attach metadata with ``set_tag``/``set_data``. A bare ``start_span`` with no
  active transaction is dropped, and ``Span.set_attribute`` does not exist.
* 3.0 — transaction mode is gone. ``start_span`` is the root (stream mode) and
  metadata is attached via ``set_attribute``; ``start_transaction`` and
  ``set_tag`` on spans no longer exist.

We detect which API is present at runtime and use the correct one, so this
module keeps working unchanged after an SDK upgrade. Metadata is attached with
whichever setter the running span exposes (``set_attribute`` when available,
otherwise ``set_tag``).

Everything here degrades to a no-op when ``sentry_sdk`` is not installed or no
client is active, so the library has no hard dependency on Sentry.
"""

import contextlib
import os
from collections.abc import Iterator
from typing import Any

try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None  # ty: ignore[invalid-assignment]


def _runtime_environment() -> str | None:
    """Return the environment the task is executing in, or ``None``.

    ``AWS_LAMBDA_FUNCTION_NAME`` on Lambda, ``AWS_BATCH_JQ_NAME`` (the Batch job
    queue the job was submitted to) on Batch. ``None`` when neither is present
    (e.g. local or eager execution).
    """
    return os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get(
        "AWS_BATCH_JQ_NAME"
    )


def _set_metadata(span: Any, *, key: str, value: Any) -> None:
    """Attach *key*/*value* to *span* using whichever setter the SDK exposes.

    SDK 3.0 (stream mode) spans use ``set_attribute``; SDK 2.x transactions use
    ``set_tag``. Preferring ``set_attribute`` means the richer, future API is
    used whenever it is available.
    """
    if hasattr(span, "set_attribute"):
        span.set_attribute(key, value)
    else:
        span.set_tag(key, value)


def _start_task_root(*, task_name: str) -> Any:
    """Start and return the per-task root span/transaction context manager.

    The tracing API differs across the SDK 2.x → 3.0 boundary:

    * 2.x — ``start_transaction(name=, op=)`` creates the root transaction
      (a bare ``start_span`` with no active transaction is dropped).
    * 3.0 — transactions are gone; ``start_span(name=)`` is the root in stream
      mode. ``start_span`` no longer accepts ``op`` (it is an attribute now),
      so only ``name`` is passed and ``op`` is recorded via ``_set_metadata``.
    """
    if hasattr(sentry_sdk, "start_transaction"):
        return sentry_sdk.start_transaction(name=task_name, op="task")
    else:
        return sentry_sdk.start_span(name=task_name)


def capture_task_exception() -> None:
    """Report the exception currently being handled to Sentry.

    The executor catches task exceptions (recording a ``FAILED`` TaskRecord)
    rather than re-raising, so the exception never propagates to an integration
    that would report it. This matters most on SDK 3.0 (stream mode), where a
    caught exception produces no error event at all — on 2.x the exception
    happened to still be reported as it passed through the active span. Calling
    this on the terminal failure path gives consistent error reporting on both
    SDK versions.

    Must be called from within an ``except`` block while the task's
    :func:`task_scope` is still active, so the error carries the task name and
    tags. A no-op when ``sentry_sdk`` is unavailable or uninitialised.
    """
    if sentry_sdk and sentry_sdk.is_initialized():
        sentry_sdk.capture_exception()


@contextlib.contextmanager
def task_scope(*, task_name: str, message_id: str, n_retries: int) -> Iterator[None]:
    """Run the enclosed task body inside a per-task Sentry trace.

    Opens an isolated scope and a root span/transaction named after the
    fully-qualified task name, so both error events and performance traces are
    attributed to the task. Records ``lambda_task``, ``lambda_task.message_id``,
    ``lambda_task.n_retries`` and (when derivable) ``lambda_task.runtime`` as
    span metadata and scope tags.

    The scope is isolated via ``new_scope`` so records processed one after
    another in the same Lambda invocation do not leak their task name into one
    another.

    A no-op when ``sentry_sdk`` is unavailable or uninitialised.
    """
    if not (sentry_sdk and sentry_sdk.is_initialized()):
        yield
        return

    runtime = _runtime_environment()

    with sentry_sdk.new_scope() as scope:
        # Scope tags ensure error events carry the attribution even if the
        # span is not sampled for performance.
        scope.set_tag("lambda_task", task_name)
        scope.set_tag("lambda_task.message_id", message_id)
        scope.set_tag("lambda_task.n_retries", n_retries)
        if runtime:
            scope.set_tag("lambda_task.runtime", runtime)

        with _start_task_root(task_name=task_name) as span:
            _set_metadata(span, key="lambda_task", value=task_name)
            _set_metadata(span, key="lambda_task.message_id", value=message_id)
            _set_metadata(span, key="lambda_task.n_retries", value=n_retries)
            if runtime:
                _set_metadata(span, key="lambda_task.runtime", value=runtime)

            yield
