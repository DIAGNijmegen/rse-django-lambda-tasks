"""
Resolves environment variables from an AWS SSM Parameter Store parameter.

When the environment variable ``LAMBDA_TASKS_SSM_ENVIRONMENT`` is set, this
module fetches the named SSM parameter, parses its value as a flat JSON object
(all keys and values must be strings), and sets each key-value pair in
``os.environ``.

This runs at Lambda cold start — before ``resolve_secrets_into_env()`` and
before ``django.setup()`` — so that SSM-loaded env vars are available to both
the secret loader and Django configuration.

The result is cached at module level via a ``_loaded`` sentinel so that
subsequent calls (warm invocations) are free no-ops.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

_loaded: bool = False


def resolve_ssm_environment() -> None:
    """Load SSM parameter content into os.environ.

    Reads the parameter named by LAMBDA_TASKS_SSM_ENVIRONMENT,
    parses it as a flat JSON object, and sets each key-value pair
    as an environment variable. Idempotent — cached after first call.

    Raises:
        ValueError: If the parameter content is not valid JSON,
                    not a flat string→string mapping, or contains
                    an empty string key.
    """
    global _loaded

    parameter_name = os.environ.get("LAMBDA_TASKS_SSM_ENVIRONMENT")
    if not parameter_name:
        return

    if _loaded:
        return

    raw_value = _fetch_parameter(parameter_name=parameter_name)
    parsed = _validate_and_parse(raw_value=raw_value, parameter_name=parameter_name)

    for key, value in parsed.items():
        os.environ[key] = value

    _loaded = True


def _fetch_parameter(*, parameter_name: str) -> str:
    """Fetch a single SSM parameter value using boto3."""
    client = boto3.client("ssm")
    response = client.get_parameter(Name=parameter_name, WithDecryption=True)
    return response["Parameter"]["Value"]


def _validate_and_parse(*, raw_value: str, parameter_name: str) -> dict[str, str]:
    """Parse JSON and validate it is a flat str→str mapping.

    Raises ValueError with descriptive messages on failure.
    """
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Parameter {parameter_name} does not contain valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            f"Parameter {parameter_name} must be a JSON object, got {type(parsed).__name__}"
        )

    non_string_keys = [
        key for key, value in parsed.items() if not isinstance(value, str)
    ]
    if non_string_keys:
        raise ValueError(
            f"Parameter {parameter_name} contains non-string values for keys: {non_string_keys}"
        )

    if "" in parsed:
        raise ValueError(f"Parameter {parameter_name} contains an empty string key")

    return parsed
