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

from lambda_tasks.secret_loader import resolve_secrets_into_env

# Cold-start Django setup — runs once per Lambda container.
# Secrets are resolved first so Django settings can reference the populated
# env vars. resolve_secrets_into_env() is idempotent and caches fetched
# secrets in-process, so subsequent invocations pay no extra cost.
if os.environ.get("DJANGO_SETTINGS_MODULE") and not apps.ready:
    resolve_secrets_into_env()
    django.setup()


logger = logging.getLogger(__name__)


def handler(*, event: dict, context: object) -> dict:
    """AWS Lambda entry point. Processes a batch of SQS records.

    Returns a partial-batch failure report so AWS only re-drives failed records.
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
