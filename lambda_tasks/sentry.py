"""Sentry scope helpers for task execution.

This library does not initialise Sentry itself — the host application owns
``sentry_sdk.init(...)``. These helpers set the Sentry transaction name and
tags to the running task so that exceptions raised inside a task are
attributed to that task rather than to the Lambda function name (set by
``AwsLambdaIntegration``) or to ``"--"`` (on AWS Batch, where no request or
Lambda integration is active).

Everything here degrades to a no-op when ``sentry_sdk`` is not installed or
no client is active, so the library has no hard dependency on Sentry.
"""

import contextlib
import os
from collections.abc import Iterator

try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None  # ty: ignore[invalid-assignment]


def _runtime_environment() -> str | None:
    """Return the name of the environment the task is executing in.

    On AWS Lambda this is ``AWS_LAMBDA_FUNCTION_NAME``; on AWS Batch it is
    ``AWS_BATCH_JQ_NAME`` (the Batch job queue the job was submitted to). Both
    are set by AWS in their respective runtimes. Returns ``None`` when neither
    is present (e.g. local or eager execution), in which case no runtime tag
    is recorded.
    """
    return os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get(
        "AWS_BATCH_JQ_NAME"
    )


@contextlib.contextmanager
def task_scope(*, task_name: str, message_id: str, n_retries: int) -> Iterator[None]:
    """Attribute Sentry events raised inside the block to *task_name*.

    Sets the transaction name to the fully-qualified task name and records the
    message id and retry count as tags. The scope is isolated to the block via
    ``new_scope``, so records processed one after another in the same Lambda
    invocation do not leak their task name into one another.

    A ``lambda_task.runtime`` tag records the physical execution environment —
    the Lambda function name on Lambda, or the Batch job queue name on Batch —
    when it can be determined from the environment.

    A no-op when ``sentry_sdk`` is unavailable or uninitialised.
    """
    if sentry_sdk is None or not sentry_sdk.is_initialized():
        yield
        return

    with sentry_sdk.new_scope() as scope:
        scope.set_transaction_name(task_name)
        scope.set_tag("lambda_task", task_name)
        scope.set_tag("lambda_task.message_id", message_id)
        scope.set_tag("lambda_task.n_retries", n_retries)

        runtime = _runtime_environment()
        if runtime is not None:
            scope.set_tag("lambda_task.runtime", runtime)

        yield
