"""
Resolves environment variables that reference AWS Secrets Manager ARNs.

Any env var prefixed with ``AWS_SECRETS_MANAGER_`` is treated as a pointer
to a secret value.  The unprefixed name is the target env var to populate.

Required value format
---------------------
Every reference must follow the full dynamic reference syntax::

    AWS_SECRETS_MANAGER_DJANGO_ADMIN_URL=arn:aws:secretsmanager:eu-west-1:123:secret:my-secret:DJANGO_ADMIN_URL:AWSCURRENT:v1

That is: ``<arn>:<json-key>:<version-stage>:<version-id>``

All four suffix segments must be present and non-empty.
A malformed reference raises ``ValueError`` immediately so the Lambda
container fails at cold start rather than silently misconfiguring Django.

It is a configuration error to set both ``AWS_SECRETS_MANAGER_FOO`` and
``FOO`` — use one or the other.  Having both raises ``ValueError`` at cold
start so the misconfiguration is caught immediately.

Calls are batched by unique (ARN, version-stage, version-id) combination —
one ``GetSecretValue`` call per unique combination, regardless of how many
env vars reference it.  Results are cached in-process so repeated calls to
``resolve_secrets_into_env`` are free after the first.
"""

import json
import logging
import os
from typing import NamedTuple

import boto3

logger = logging.getLogger(__name__)

_PREFIX = "AWS_SECRETS_MANAGER_"

# Module-level cache: (arn, version_stage, version_id) → raw secret string.
# Populated on first call; reused for the lifetime of the Lambda container.
_secret_cache: dict[tuple[str, str, str], dict[str, str]] = {}


class _SecretReference(NamedTuple):
    arn: str
    json_key: str
    version_stage: str
    version_id: str


def _parse_reference(*, env_var: str, value: str) -> _SecretReference:
    """Parse and validate a Secrets Manager reference.

    Expected format::

        <arn>:<json-key>:<version-stage>:<version-id>

    The ARN itself is 7 colon-separated segments, plus 3 suffix segments
    (json-key, version-stage, version-id) = 10 total.
    All four suffix fields must be non-empty.

    Raises ``ValueError`` if the format is invalid — this is intentional so
    the Lambda container fails at cold start rather than starting with a
    misconfigured environment.
    """
    # ARN has exactly 7 colon-separated parts:
    # arn : aws : secretsmanager : region : account : secret : name
    # Plus 3 suffix parts: json-key : version-stage : version-id  → 10 total
    parts = value.split(":")

    if len(parts) != 10:
        raise ValueError(
            f"{env_var} has an invalid Secrets Manager reference format. "
            "Expected <arn>:<json-key>:<version-stage>:<version-id> "
            f"(10 colon-separated segments), got {len(parts)}: {value!r}"
        )

    arn = ":".join(parts[:7])
    json_key, version_stage, version_id = parts[7], parts[8], parts[9]

    for field, field_value in (
        ("json-key", json_key),
        ("version-stage", version_stage),
        ("version-id", version_id),
    ):
        if not field_value:
            raise ValueError(
                f"{env_var} is missing the {field} segment in its Secrets Manager "
                f"reference: {value!r}"
            )

    return _SecretReference(
        arn=arn,
        json_key=json_key,
        version_stage=version_stage,
        version_id=version_id,
    )


def _fetch_secret(*, client: object, ref: _SecretReference) -> dict[str, str]:
    """Fetch a secret string from Secrets Manager, using the module cache.

    The cache key is ``(arn, version_stage, version_id)`` so that references
    to different versions of the same secret are fetched independently.
    """
    cache_key = (ref.arn, ref.version_stage, ref.version_id)

    if cache_key in _secret_cache:
        return _secret_cache[cache_key]

    logger.debug(f"Fetching secret {ref}")

    response = client.get_secret_value(  # type: ignore[union-attr]
        SecretId=ref.arn,
        VersionStage=ref.version_stage,
        VersionId=ref.version_id,
    )

    try:
        secret = json.loads(response["SecretString"])
    except json.JSONDecodeError as error:
        raise ValueError(f"Could not decode secret as json for {ref}")

    _secret_cache[cache_key] = secret

    return secret


def resolve_secrets_into_env() -> None:
    """Scan env vars for ``AWS_SECRETS_MANAGER_*`` references and resolve them.

    For each matching env var the resolved value is written back into
    ``os.environ`` under the unprefixed name.

    Raises ``ValueError`` at cold start if:
    - A reference is malformed (wrong segment count, any empty field)
    - The target env var is already set — use ``AWS_SECRETS_MANAGER_FOO`` or
      ``FOO``, not both

    This function is idempotent — calling it multiple times is safe and cheap
    because resolved secrets are cached after the first fetch.
    """
    references: dict[str, _SecretReference] = {}  # target_name → _SecretReference

    for key, value in os.environ.items():
        if not key.startswith(_PREFIX):
            continue
        target = key[len(_PREFIX) :]
        references[target] = _parse_reference(env_var=key, value=value)

    if not references:
        return

    # Fail fast if any target is already set in the environment.
    conflicts = [target for target in references if target in os.environ]
    if conflicts:
        raise ValueError(
            "The following environment variables are set both directly and via "
            f"AWS_SECRETS_MANAGER_*: {', '.join(sorted(conflicts))}. "
            "Use one or the other, not both."
        )

    # Unique (arn, version_stage, version_id) combinations to minimise API calls.
    unique_cache_keys = {
        (ref.arn, ref.version_stage, ref.version_id) for ref in references.values()
    }
    uncached = unique_cache_keys - _secret_cache.keys()
    logger.info(
        f"Resolving {len(references)} secret reference(s) from"
        f" {len(unique_cache_keys)} unique secret version(s)"
        f" ({len(unique_cache_keys) - len(uncached)} already cached)"
    )

    client = boto3.client("secretsmanager") if uncached else None

    for target, ref in references.items():
        secret = _fetch_secret(client=client, ref=ref)
        secret_value = secret[ref.json_key]

        os.environ[target] = secret_value

        logger.info(f"Resolved {target} from secret {ref}")
