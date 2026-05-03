"""
AWS Lambda handler for lambda_tasks.

Processes a batch of SQS records using partial-batch failure reporting.
Each record is processed independently — a failure in one record does not
prevent processing of other records.
"""

import logging
import os

import django
from django.apps import apps

from lambda_tasks.environment_loader import resolve_environment
from lambda_tasks.secret_loader import resolve_secrets_into_env

# Both loaders are idempotent and run unconditionally before the
# DJANGO_SETTINGS_MODULE check — the environment secret may provide
# that var, and individual secrets may depend on environment-loaded vars.
resolve_environment()
resolve_secrets_into_env()

if os.environ.get("DJANGO_SETTINGS_MODULE") and not apps.ready:
    django.setup()


logger = logging.getLogger(__name__)


def handler(event: dict, context: object) -> dict:
    """AWS Lambda entry point. Processes a batch of SQS records.

    Returns a partial-batch failure report so AWS only re-drives failed records.
    Signature is fixed by AWS and uses two args only.
    """
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
