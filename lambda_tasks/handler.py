"""
AWS Lambda handler for lambda_tasks.

Processes a batch of SQS records using partial-batch failure reporting.
Each record is processed independently — a failure in one record does not
prevent processing of other records.

Cold-start initialisation (environment loading, secret resolution, Django
setup) runs inside the handler on the first invocation rather than at module
import time. This keeps the Lambda init phase fast and avoids init-duration
timeouts. The sequence is guarded by a module-level sentinel so subsequent
warm invocations skip it.
"""

import logging
import os

import django
from django.apps import apps

from lambda_tasks.environment_loader import resolve_environment
from lambda_tasks.secret_loader import resolve_secrets_into_env

logger = logging.getLogger(__name__)

_cold_start_done: bool = False


def _perform_cold_start() -> None:
    """Run one-time initialisation: env loading, secrets, Django setup.

    Both loaders are idempotent and run unconditionally — the environment
    secret may provide DJANGO_SETTINGS_MODULE, and individual secrets may
    depend on environment-loaded vars.
    """
    global _cold_start_done

    if _cold_start_done:
        return

    resolve_environment()
    resolve_secrets_into_env()

    if os.environ.get("DJANGO_SETTINGS_MODULE") and not apps.ready:
        django.setup()

    _configure_logging()

    _cold_start_done = True


def _configure_logging() -> None:
    """Ensure the lambda_tasks logger hierarchy emits at INFO so task log lines
    appear in CloudWatch.

    The AWS Lambda runtime pre-configures the root logger, but child loggers
    default to WARNING unless explicitly configured. If Django's LOGGING
    dictConfig has already set a level on the ``lambda_tasks`` logger (i.e. the
    user explicitly configured it), we leave it alone. Otherwise we default to
    INFO (or the value of the LAMBDA_TASKS_LOG_LEVEL env var).
    """
    lambda_tasks_logger = logging.getLogger("lambda_tasks")

    # level == NOTSET means nobody (neither dictConfig nor user code) has
    # explicitly configured this logger — safe to apply our default.
    if lambda_tasks_logger.level != logging.NOTSET:
        return

    log_level_name = os.environ.get("LAMBDA_TASKS_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    lambda_tasks_logger.setLevel(log_level)


def handler(event: dict, context: object) -> dict:
    """AWS Lambda entry point. Processes a batch of SQS records.

    Returns a partial-batch failure report so AWS only re-drives failed records.
    Signature is fixed by AWS and uses two args only.
    """
    _perform_cold_start()

    # Local import due to AppRegistryNotReady
    from lambda_tasks.models import SQSLambdaTaskMessage

    batch_item_failures: list[dict] = []

    for record in event["Records"]:
        try:
            SQSLambdaTaskMessage.model_validate_json(
                record["body"]
            ).execute_immediately(message_id=record["messageId"])
        except Exception:
            logger.error(
                "Failed to process SQS record",
                exc_info=True,
            )
            batch_item_failures.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": batch_item_failures}
