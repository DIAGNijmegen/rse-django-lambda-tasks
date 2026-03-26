"""
Tests for lambda_tasks.secret_loader.

boto3 is never called for real — the secretsmanager client is patched at the
module level so no AWS credentials are required.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

import lambda_tasks.secret_loader as secret_loader
from lambda_tasks.secret_loader import (
    _parse_reference,
    resolve_secrets_into_env,
)

ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:my-secret"
ARN2 = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:other-secret"

# A valid full reference: <arn>:<json-key>:<version-stage>:<version-id>
# ARN = 7 segments, + 3 suffix = 10 total. version-id may be empty string.
VALID_REF = f"{ARN}:MY_KEY:AWSCURRENT:AWSCURRENT"
VALID_REF2 = f"{ARN2}:OTHER_KEY:AWSCURRENT:AWSCURRENT"


@pytest.fixture(autouse=True)
def clear_cache():
    """Reset the module-level secret cache between every test."""
    secret_loader._secret_cache.clear()
    yield
    secret_loader._secret_cache.clear()


# ---------------------------------------------------------------------------
# _parse_reference — valid inputs
# ---------------------------------------------------------------------------


class TestParseReferenceValid:
    def test_returns_named_tuple_with_arn_and_key(self):
        ref = _parse_reference(env_var="AWS_SECRETS_MANAGER_X", value=VALID_REF)
        assert ref.arn == ARN
        assert ref.json_key == "MY_KEY"
        assert ref.version_stage == "AWSCURRENT"
        assert ref.version_id == "AWSCURRENT"

    def test_version_id_populated(self):
        value = f"{ARN}:DJANGO_ADMIN_URL:AWSCURRENT:abc123"
        ref = _parse_reference(env_var="AWS_SECRETS_MANAGER_X", value=value)
        assert ref.arn == ARN
        assert ref.json_key == "DJANGO_ADMIN_URL"
        assert ref.version_stage == "AWSCURRENT"
        assert ref.version_id == "abc123"


# ---------------------------------------------------------------------------
# _parse_reference — invalid inputs raise ValueError at parse time
# ---------------------------------------------------------------------------


class TestParseReferenceInvalid:
    def test_plain_arn_rejected(self):
        with pytest.raises(ValueError, match="10 colon-separated segments"):
            _parse_reference(env_var="AWS_SECRETS_MANAGER_X", value=ARN)

    def test_empty_json_key_rejected(self):
        # 10 segments but key is empty
        value = f"{ARN}::AWSCURRENT:abc123"
        with pytest.raises(ValueError, match="missing the json-key"):
            _parse_reference(env_var="AWS_SECRETS_MANAGER_X", value=value)

    def test_empty_version_stage_rejected(self):
        value = f"{ARN}:MY_KEY::abc123"
        with pytest.raises(ValueError, match="missing the version-stage"):
            _parse_reference(env_var="AWS_SECRETS_MANAGER_X", value=value)

    def test_empty_version_id_rejected(self):
        value = f"{ARN}:MY_KEY:AWSCURRENT:"
        with pytest.raises(ValueError, match="missing the version-id"):
            _parse_reference(env_var="AWS_SECRETS_MANAGER_X", value=value)

    def test_too_few_segments_rejected(self):
        with pytest.raises(ValueError, match="10 colon-separated segments"):
            _parse_reference(env_var="AWS_SECRETS_MANAGER_X", value=f"{ARN}:KEY:")

    def test_too_many_segments_rejected(self):
        with pytest.raises(ValueError, match="10 colon-separated segments"):
            _parse_reference(
                env_var="AWS_SECRETS_MANAGER_X",
                value=f"{ARN}:KEY:STAGE:VER:EXTRA",
            )

    def test_error_message_includes_env_var_name(self):
        with pytest.raises(ValueError, match="AWS_SECRETS_MANAGER_MY_VAR"):
            _parse_reference(env_var="AWS_SECRETS_MANAGER_MY_VAR", value=ARN)

    @given(st.text(min_size=1).filter(lambda s: s.count(":") != 9))
    def test_any_non_10_segment_value_rejected(self, value: str):
        with pytest.raises(ValueError):
            _parse_reference(env_var="AWS_SECRETS_MANAGER_X", value=value)


# ---------------------------------------------------------------------------
# resolve_secrets_into_env — no references present
# ---------------------------------------------------------------------------


class TestNoReferences:
    def test_no_prefixed_vars_makes_no_boto3_call(self, monkeypatch):
        monkeypatch.delenv("AWS_SECRETS_MANAGER_ANYTHING", raising=False)
        with patch("lambda_tasks.secret_loader.boto3") as mock_boto3:
            resolve_secrets_into_env()
        mock_boto3.client.assert_not_called()

    def test_no_prefixed_vars_leaves_env_unchanged(self, monkeypatch):
        monkeypatch.delenv("AWS_SECRETS_MANAGER_ANYTHING", raising=False)
        before = dict(os.environ)
        resolve_secrets_into_env()
        assert dict(os.environ) == before


# ---------------------------------------------------------------------------
# resolve_secrets_into_env — malformed reference fails at cold start
# ---------------------------------------------------------------------------


class TestMalformedReference:
    def test_plain_arn_raises_before_any_boto3_call(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRETS_MANAGER_MY_VAR", ARN)
        with patch("lambda_tasks.secret_loader.boto3") as mock_boto3:
            with pytest.raises(ValueError, match="AWS_SECRETS_MANAGER_MY_VAR"):
                resolve_secrets_into_env()
        mock_boto3.client.assert_not_called()

    def test_empty_key_raises_before_any_boto3_call(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRETS_MANAGER_MY_VAR", f"{ARN}::AWSCURRENT:abc123")
        with patch("lambda_tasks.secret_loader.boto3") as mock_boto3:
            with pytest.raises(ValueError, match="missing the json-key"):
                resolve_secrets_into_env()
        mock_boto3.client.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_secrets_into_env — conflict detection
# ---------------------------------------------------------------------------


class TestConflict:
    def test_raises_when_target_already_set(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRETS_MANAGER_MY_VAR", VALID_REF)
        monkeypatch.setenv("MY_VAR", "already-set")

        with patch("lambda_tasks.secret_loader.boto3"):
            with pytest.raises(ValueError, match="MY_VAR"):
                resolve_secrets_into_env()

    def test_error_message_lists_all_conflicts(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRETS_MANAGER_VAR_A", VALID_REF)
        monkeypatch.setenv("AWS_SECRETS_MANAGER_VAR_B", VALID_REF2)
        monkeypatch.setenv("VAR_A", "x")
        monkeypatch.setenv("VAR_B", "y")

        with patch("lambda_tasks.secret_loader.boto3"):
            with pytest.raises(ValueError) as exc_info:
                resolve_secrets_into_env()

        msg = str(exc_info.value)
        assert "VAR_A" in msg
        assert "VAR_B" in msg

    def test_conflict_raises_before_any_boto3_call(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRETS_MANAGER_MY_VAR", VALID_REF)
        monkeypatch.setenv("MY_VAR", "already-set")

        with patch("lambda_tasks.secret_loader.boto3") as mock_boto3:
            with pytest.raises(ValueError):
                resolve_secrets_into_env()
        mock_boto3.client.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_secrets_into_env — happy path
# ---------------------------------------------------------------------------


class TestResolution:
    def test_sets_target_env_var(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRETS_MANAGER_MY_VAR", VALID_REF)
        monkeypatch.delenv("MY_VAR", raising=False)

        payload = json.dumps({"MY_KEY": "supersecret"})
        client = MagicMock()
        client.get_secret_value.return_value = {"SecretString": payload}

        with patch("lambda_tasks.secret_loader.boto3") as mock_boto3:
            mock_boto3.client.return_value = client
            resolve_secrets_into_env()

        assert os.environ["MY_VAR"] == "supersecret"

    def test_missing_key_in_secret_raises(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRETS_MANAGER_SOME_VAR", VALID_REF)
        monkeypatch.delenv("SOME_VAR", raising=False)

        client = MagicMock()
        client.get_secret_value.return_value = {
            "SecretString": json.dumps({"OTHER": "x"})
        }

        with patch("lambda_tasks.secret_loader.boto3") as mock_boto3:
            mock_boto3.client.return_value = client
            with pytest.raises(KeyError, match="MY_KEY"):
                resolve_secrets_into_env()

    def test_invalid_json_in_secret_raises(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRETS_MANAGER_SOME_VAR", VALID_REF)
        monkeypatch.delenv("SOME_VAR", raising=False)

        client = MagicMock()
        client.get_secret_value.return_value = {"SecretString": "not-json"}

        with patch("lambda_tasks.secret_loader.boto3") as mock_boto3:
            mock_boto3.client.return_value = client
            with pytest.raises(ValueError, match="MY_KEY"):
                resolve_secrets_into_env()


# ---------------------------------------------------------------------------
# Batching — multiple vars from the same secret ARN
# ---------------------------------------------------------------------------


class TestBatching:
    def test_single_api_call_for_same_arn(self, monkeypatch):
        ref_a = f"{ARN}:KEY_A:AWSCURRENT:AWSCURRENT"
        ref_b = f"{ARN}:KEY_B:AWSCURRENT:AWSCURRENT"
        monkeypatch.setenv("AWS_SECRETS_MANAGER_VAR_A", ref_a)
        monkeypatch.setenv("AWS_SECRETS_MANAGER_VAR_B", ref_b)
        monkeypatch.delenv("VAR_A", raising=False)
        monkeypatch.delenv("VAR_B", raising=False)

        payload = json.dumps({"KEY_A": "val-a", "KEY_B": "val-b"})
        client = MagicMock()
        client.get_secret_value.return_value = {"SecretString": payload}

        with patch("lambda_tasks.secret_loader.boto3") as mock_boto3:
            mock_boto3.client.return_value = client
            resolve_secrets_into_env()

        client.get_secret_value.assert_called_once_with(
            SecretId=ARN,
            VersionStage="AWSCURRENT",
            VersionId="AWSCURRENT",
        )
        assert os.environ["VAR_A"] == "val-a"
        assert os.environ["VAR_B"] == "val-b"

    def test_separate_api_calls_for_different_arns(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRETS_MANAGER_VAR_A", VALID_REF)
        monkeypatch.setenv("AWS_SECRETS_MANAGER_VAR_B", VALID_REF2)
        monkeypatch.delenv("VAR_A", raising=False)
        monkeypatch.delenv("VAR_B", raising=False)

        client = MagicMock()
        client.get_secret_value.side_effect = [
            {"SecretString": json.dumps({"MY_KEY": "secret-a"})},
            {"SecretString": json.dumps({"OTHER_KEY": "secret-b"})},
        ]

        with patch("lambda_tasks.secret_loader.boto3") as mock_boto3:
            mock_boto3.client.return_value = client
            resolve_secrets_into_env()

        assert client.get_secret_value.call_count == 2
        assert os.environ["VAR_A"] == "secret-a"
        assert os.environ["VAR_B"] == "secret-b"


# ---------------------------------------------------------------------------
# Caching — second call reuses cached values
# ---------------------------------------------------------------------------


class TestCaching:
    def test_second_call_makes_no_api_call(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRETS_MANAGER_MY_VAR", VALID_REF)
        monkeypatch.delenv("MY_VAR", raising=False)

        payload = json.dumps({"MY_KEY": "cached-value"})
        client = MagicMock()
        client.get_secret_value.return_value = {"SecretString": payload}

        with patch("lambda_tasks.secret_loader.boto3") as mock_boto3:
            mock_boto3.client.return_value = client
            resolve_secrets_into_env()  # first call — fetches
            monkeypatch.delenv("MY_VAR")  # reset so second call tries again
            resolve_secrets_into_env()  # second call — should use cache

        client.get_secret_value.assert_called_once()

    def test_no_boto3_client_created_when_all_cached(self, monkeypatch):
        secret_loader._secret_cache[(ARN, "AWSCURRENT", "AWSCURRENT")] = {
            "MY_KEY": "pre-cached"
        }
        monkeypatch.setenv("AWS_SECRETS_MANAGER_MY_VAR", VALID_REF)
        monkeypatch.delenv("MY_VAR", raising=False)

        with patch("lambda_tasks.secret_loader.boto3") as mock_boto3:
            resolve_secrets_into_env()

        mock_boto3.client.assert_not_called()
        assert os.environ["MY_VAR"] == "pre-cached"
