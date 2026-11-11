"""Sentry tracing/scope helpers for task execution.

This library does not initialise Sentry itself — the host application owns
``sentry_sdk.init(...)``. These helpers trace each task and attribute errors to
the running task rather than to the Lambda function name (set by
``AwsLambdaIntegration``) or to ``"--"`` (on AWS Batch, where no request or
Lambda integration is active).

Trace shape depends on whether a trace is already active:

* **Lambda** — ``AwsLambdaIntegration`` starts a transaction per invocation, so
  the task is traced as a **child span** of it. One transaction per invocation,
  with the task span nested inside — not a second, competing transaction.
* **Batch** (and anywhere nothing is active) — the task starts its **own root
  transaction**, so it is still traced on its own.

Why the child span matters: starting a nested *transaction* per task produced a
second transaction that inherited (rather than independently re-rolled) the
invocation's sampling decision, roughly doubling transaction volume and
defeating ``traces_sample_rate``. A child span is sent only if its parent
transaction is sampled, so sampling is respected.

Error attribution does not depend on the trace being sampled: the ``lambda_task``
scope tag carries the task name on every error event regardless.

Future-proofing across the SDK 2.x → 3.0 boundary
--------------------------------------------------
The tracing API changed in SDK 3.0 (stream mode):

* 2.x — a root is a *transaction* (``start_transaction``); a child uses
  ``start_span``. Metadata is attached with ``set_tag``; ``Span.set_attribute``
  does not exist, and ``Scope`` has a ``transaction`` attribute.
* 3.0 — transaction mode is gone. ``start_span`` is both root (when nothing is
  active) and child (when something is), metadata is attached via
  ``set_attribute``, and ``Scope`` has no ``transaction`` attribute.

We detect which API is present at runtime (via ``hasattr``) and use the correct
one, so this module keeps working unchanged after an SDK upgrade. Metadata is
attached with whichever setter the running span exposes (``set_attribute`` when
available, otherwise ``set_tag``).

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
    """Start and return the per-task span/transaction context manager.

    Crucially, this starts a **child span** when a trace is already active
    (e.g. ``AwsLambdaIntegration`` starts a transaction per Lambda invocation)
    and only starts a **new transaction** when nothing is active (e.g. AWS
    Batch, where there is no Lambda integration).

    Why this matters for volume: starting a nested *transaction* under the
    invocation transaction produced a *second* transaction per task that
    inherited — rather than independently re-rolled — the parent's sampling
    decision, roughly doubling transaction volume and defeating
    ``traces_sample_rate``. A child span carries no separate sampling decision:
    it is sent only if its parent transaction is sampled, so one invocation
    yields one transaction with the task span nested inside it.

    The tracing API also differs across the SDK 2.x → 3.0 boundary:

    * 2.x — ``start_transaction(name=, op=)`` for a root; ``start_span`` for a
      child of the active transaction.
    * 3.0 — transactions are gone; ``start_span(name=)`` is both the root (when
      nothing is active) and a child (when something is), and no longer accepts
      ``op``.
    """
    if not hasattr(sentry_sdk, "start_transaction"):
        # SDK 3.0: start_span is automatically a root when nothing is active
        # and a child otherwise — no manual active-trace check needed. (Scope
        # has no ``transaction`` attribute on 3.0.)
        return sentry_sdk.start_span(name=task_name)
    else:
        # SDK 2.x: a bare start_span with no active transaction is dropped,
        # so pick child span vs. new transaction based on whether a trace is
        # already active.
        has_active_transaction = sentry_sdk.get_current_scope().transaction is not None

        if has_active_transaction:
            return sentry_sdk.start_span(op="task", name=task_name)
        else:
            return sentry_sdk.start_transaction(name=task_name, op="task")


def handler_error_scope(
    *, message_id: str | None = None
) -> "contextlib.AbstractContextManager[Any]":
    """Scope a handler-level failure's tags so a subsequent error *log* carries them.

    Pre-execution failures in the Lambda/Batch handler (malformed message,
    un-importable task, misconfiguration, cold-start errors) happen outside any
    task scope. The handler logs them at ERROR; with the host's
    ``LoggingIntegration(event_level=ERROR)`` that log becomes the Sentry event.
    Entering this context first tags an isolated scope so the resulting event is
    distinguishable (``lambda_task.handler_error``) and carries the message id.

    Usage::

        with handler_error_scope(message_id=...):
            logger.error("...", exc_info=True)

    A no-op (returns a null context) when ``sentry_sdk`` is unavailable or
    uninitialised.
    """
    if not (sentry_sdk and sentry_sdk.is_initialized()):
        return contextlib.nullcontext()

    @contextlib.contextmanager
    def _scope() -> Iterator[None]:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("lambda_task.handler_error", True)
            if message_id is not None:
                scope.set_tag("lambda_task.message_id", message_id)
            yield

    return _scope()


@contextlib.contextmanager
def task_scope(*, task_name: str, message_id: str, n_retries: int) -> Iterator[None]:
    """Run the enclosed task body inside a per-task Sentry trace.

    Opens an isolated scope and traces the task — as a child span of the active
    invocation transaction on Lambda, or as its own root transaction when
    nothing is active (Batch). Records ``lambda_task``, ``lambda_task.message_id``,
    ``lambda_task.n_retries`` and (when derivable) ``lambda_task.runtime`` as
    both span metadata and scope tags, so errors are attributed to the task even
    when the trace is not sampled.

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
        # Attribute error events and the trace to the task via tags/attributes.
        # We deliberately do NOT call scope.set_transaction_name(task_name):
        # on Lambda the task runs inside the invocation transaction, and
        # renaming it (a) fights across sibling records in a batch and (b) on
        # SDK 3.0 stream mode corrupts the root-segment naming. The
        # ``lambda_task`` tag below is the reliable, version-independent
        # attribution; the task span itself is named after the task.
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
