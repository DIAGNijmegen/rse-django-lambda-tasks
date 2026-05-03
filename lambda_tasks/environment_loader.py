"""
Resolves environment variables from an AWS Secrets Manager secret.

When the environment variable ``LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN``
is set, this module fetches the named secret, parses its value as a flat JSON
object (all keys and values must be strings), and sets each key-value pair in
``os.environ``.

Required value format::

    LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN=arn:aws:secretsmanager:eu-west-1:123:secret:my-env:AWSCURRENT:v1

That is: ``<arn>:<version-stage>:<version-id>`` (9 colon-separated segments).
The ARN is 7 segments, plus version-stage and version-id.

This runs at Lambda cold start — before ``resolve_secrets_into_env()`` and
before ``django.setup()`` — so that environment variables loaded from the
secret are available to both the secret loader and Django configuration.

The result is cached at module level via a ``_loaded`` sentinel so that
subsequent calls (warm invocations) are free no-ops.
"""

import json
import logging
import os
from typing import NamedTuple

import boto3

logger = logging.getLogger(__name__)

_loaded: bool = False


class _EnvironmentSecretReference(NamedTuple):
    arn: str
    version_stage: str
    version_id: str


def resolve_environment() -> None:
    """Load secret content into os.environ.

    Reads the secret identified by LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN,
    parses it as a flat JSON object, and sets each key-value pair
    as an environment variable. Idempotent — cached after first call.

    Raises:
        ValueError: If the env var format is invalid, the secret content
                    is not valid JSON, not a flat string→string mapping,
                    or contains an empty string key.
    """
    global _loaded

    raw_reference = os.environ.get("LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN")
    if not raw_reference:
        return

    if _loaded:
        return

    ref = _parse_reference(value=raw_reference)
    raw_value = _fetch_secret(ref=ref)
    parsed = _validate_and_parse(raw_value=raw_value, secret_arn=ref.arn)

    for key, value in parsed.items():
        os.environ[key] = value

    _loaded = True


def _parse_reference(*, value: str) -> _EnvironmentSecretReference:
    """Parse and validate the environment secret reference.

    Expected format::

        <arn>:<version-stage>:<version-id>

    The ARN itself is 7 colon-separated segments, plus 2 suffix segments
    (version-stage, version-id) = 9 total.
    Both suffix fields must be non-empty.

    Raises ``ValueError`` if the format is invalid.
    """
    parts = value.split(":")

    if len(parts) != 9:
        raise ValueError(
            "LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN has an invalid format. "
            "Expected <arn>:<version-stage>:<version-id> "
            f"(9 colon-separated segments), got {len(parts)}: {value!r}"
        )

    arn = ":".join(parts[:7])
    version_stage = parts[7]
    version_id = parts[8]

    for field, field_value in (
        ("version-stage", version_stage),
        ("version-id", version_id),
    ):
        if not field_value:
            raise ValueError(
                f"LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN is missing the "
                f"{field} segment: {value!r}"
            )

    return _EnvironmentSecretReference(
        arn=arn,
        version_stage=version_stage,
        version_id=version_id,
    )


def _fetch_secret(*, ref: _EnvironmentSecretReference) -> str:
    """Fetch a secret string from Secrets Manager using boto3."""
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(
        SecretId=ref.arn,
        VersionStage=ref.version_stage,
        VersionId=ref.version_id,
    )
    return response["SecretString"]


def _validate_and_parse(*, raw_value: str, secret_arn: str) -> dict[str, str]:
    """Parse JSON and validate it is a flat str→str mapping.

    Raises ValueError with descriptive messages on failure.
    """
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Secret {secret_arn} does not contain valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            f"Secret {secret_arn} must be a JSON object, got {type(parsed).__name__}"
        )

    non_string_keys = [
        key for key, value in parsed.items() if not isinstance(value, str)
    ]
    if non_string_keys:
        raise ValueError(
            f"Secret {secret_arn} contains non-string values for keys: {non_string_keys}"
        )

    if "" in parsed:
        raise ValueError(f"Secret {secret_arn} contains an empty string key")

    return parsed
