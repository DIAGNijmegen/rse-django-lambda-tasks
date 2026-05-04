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

    A temporary StreamHandler is attached to the ``lambda_tasks`` logger for
    the duration of the loaders so their log output is visible before Django's
    LOGGING dictConfig has run. It is removed immediately after so that
    Django's configuration is the sole authority on logging from that point on.
    """
    global _cold_start_done

    if _cold_start_done:
        return

    lambda_tasks_logger = logging.getLogger(__package__)
    boot_handler = logging.StreamHandler()
    boot_handler.setLevel(logging.DEBUG)
    lambda_tasks_logger.addHandler(boot_handler)
    lambda_tasks_logger.setLevel(logging.DEBUG)

    try:
        resolve_environment()
        resolve_secrets_into_env()
    finally:
        lambda_tasks_logger.removeHandler(boot_handler)
        lambda_tasks_logger.setLevel(logging.NOTSET)

    if os.environ.get("DJANGO_SETTINGS_MODULE") and not apps.ready:
        django.setup()

    _cold_start_done = True


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
