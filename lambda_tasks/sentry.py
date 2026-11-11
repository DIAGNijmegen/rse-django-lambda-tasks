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

    Uses ``start_transaction`` on SDK 2.x (where a root span must be a
    transaction to be sent) and ``start_span`` on SDK 3.0 (where transactions
    no longer exist and ``start_span`` is the root in stream mode).
    """
    if hasattr(sentry_sdk, "start_transaction"):
        return sentry_sdk.start_transaction(name=task_name, op="task")
    else:
        return sentry_sdk.start_span(name=task_name, op="task")


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
