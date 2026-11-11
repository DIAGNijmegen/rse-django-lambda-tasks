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
    if sentry_sdk and sentry_sdk.is_initialized():
        with sentry_sdk.new_scope() as scope:
            scope.set_transaction_name(task_name)
            scope.set_tag("lambda_task", task_name)
            scope.set_tag("lambda_task.message_id", message_id)
            scope.set_tag("lambda_task.n_retries", n_retries)

            runtime = os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get(
                "AWS_BATCH_JQ_NAME"
            )
            if runtime:
                scope.set_tag("lambda_task.runtime", runtime)

            yield
    else:
        yield
