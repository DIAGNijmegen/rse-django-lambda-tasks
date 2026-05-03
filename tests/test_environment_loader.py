"""
Tests for lambda_tasks.environment_loader.

boto3 is never called for real — the Secrets Manager client is patched at the
module level so no AWS credentials are required.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import lambda_tasks.environment_loader as environment_loader
from lambda_tasks.environment_loader import (
    _parse_reference,
    _validate_and_parse,
    resolve_environment,
)

# A valid 9-segment reference used throughout tests
_VALID_REF = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:my-env:AWSCURRENT:v1"
_VALID_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:my-env"


@pytest.fixture(autouse=True)
def reset_loaded():
    """Reset the module-level _loaded sentinel between every test."""
    environment_loader._loaded = False
    yield
    environment_loader._loaded = False


@pytest.fixture()
def mock_secretsmanager_client():
    """Patch boto3 so no real AWS calls are made; yields the mock Secrets Manager client."""
    with patch("lambda_tasks.environment_loader.boto3") as mock_boto3:
        client = MagicMock()
        mock_boto3.client.return_value = client
        yield client


@pytest.fixture()
def set_env_arn(monkeypatch):
    """Helper to set the LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN env var."""

    def _set(*, value: str) -> None:
        monkeypatch.setenv("LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN", value)

    return _set


@pytest.fixture()
def unset_env_arn(monkeypatch):
    """Helper to ensure LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN is not set."""
    monkeypatch.delenv("LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN", raising=False)


# ---------------------------------------------------------------------------
# Smoke tests — module importable and public API callable
# ---------------------------------------------------------------------------


class TestSmoke:
    """Verify the module is importable and resolve_environment is callable."""

    def test_module_is_importable(self):
        """Module resides in lambda_tasks package."""
        import lambda_tasks.environment_loader  # noqa: F401

    def test_resolve_environment_is_callable_with_no_args(self, unset_env_arn):
        """Public function takes no arguments."""
        resolve_environment()


# ---------------------------------------------------------------------------
# Unit tests for _parse_reference
# ---------------------------------------------------------------------------


class TestParseReference:
    """Unit tests for _parse_reference covering format validation."""

    def test_valid_reference_returns_named_tuple(self) -> None:
        """A valid 9-segment reference is parsed correctly."""
        ref = _parse_reference(value=_VALID_REF)
        assert ref.arn == _VALID_ARN
        assert ref.version_stage == "AWSCURRENT"
        assert ref.version_id == "v1"

    def test_too_few_segments_raises_value_error(self) -> None:
        """Fewer than 9 segments raises ValueError."""
        with pytest.raises(ValueError, match="9 colon-separated segments"):
            _parse_reference(value="arn:aws:secretsmanager:eu-west-1:123:secret:my-env")

    def test_too_many_segments_raises_value_error(self) -> None:
        """More than 9 segments raises ValueError."""
        with pytest.raises(ValueError, match="9 colon-separated segments"):
            _parse_reference(
                value="arn:aws:secretsmanager:eu-west-1:123:secret:my-env:AWSCURRENT:v1:extra"
            )

    def test_empty_version_stage_raises_value_error(self) -> None:
        """Empty version-stage raises ValueError."""
        with pytest.raises(ValueError, match="version-stage"):
            _parse_reference(
                value="arn:aws:secretsmanager:eu-west-1:123:secret:my-env::v1"
            )

    def test_empty_version_id_raises_value_error(self) -> None:
        """Empty version-id raises ValueError."""
        with pytest.raises(ValueError, match="version-id"):
            _parse_reference(
                value="arn:aws:secretsmanager:eu-west-1:123:secret:my-env:AWSCURRENT:"
            )


# ---------------------------------------------------------------------------
# Unit tests for _validate_and_parse
# ---------------------------------------------------------------------------


class TestValidateAndParse:
    """Unit tests for _validate_and_parse covering validation and happy path."""

    def test_invalid_json_raises_value_error_with_secret_arn(self) -> None:
        """Invalid JSON raises ValueError with secret ARN."""
        with pytest.raises(ValueError, match=_VALID_ARN):
            _validate_and_parse(
                raw_value="not valid json{",
                secret_arn=_VALID_ARN,
            )

    def test_non_string_values_raises_value_error_listing_offending_keys(self) -> None:
        """Non-string values raises ValueError listing offending keys."""
        raw_value = '{"good": "value", "bad_int": 42, "bad_list": [1, 2]}'
        with pytest.raises(ValueError, match="bad_int") as exc_info:
            _validate_and_parse(
                raw_value=raw_value,
                secret_arn=_VALID_ARN,
            )
        assert "bad_list" in str(exc_info.value)

    def test_empty_string_key_raises_value_error(self) -> None:
        """Empty string key raises ValueError."""
        raw_value = '{"": "some_value", "valid_key": "ok"}'
        with pytest.raises(ValueError):
            _validate_and_parse(
                raw_value=raw_value,
                secret_arn=_VALID_ARN,
            )

    def test_valid_flat_json_returns_dict(self) -> None:
        """Valid flat JSON returns dict[str, str]."""
        raw_value = '{"DB_HOST": "localhost", "DB_PORT": "5432"}'
        result = _validate_and_parse(
            raw_value=raw_value,
            secret_arn=_VALID_ARN,
        )
        assert result == {"DB_HOST": "localhost", "DB_PORT": "5432"}

    def test_non_dict_json_raises_value_error(self) -> None:
        """Non-dict JSON (e.g. a list) raises ValueError."""
        raw_value = '["a", "b"]'
        with pytest.raises(ValueError, match="must be a JSON object"):
            _validate_and_parse(
                raw_value=raw_value,
                secret_arn=_VALID_ARN,
            )


# ---------------------------------------------------------------------------
# Property-based tests (Hypothesis)
# ---------------------------------------------------------------------------


def _is_valid_json_module_level(s: str) -> bool:
    """Return True if s is parseable as JSON, False otherwise."""
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


class TestPropertyInvalidJsonRejection:
    """Property: Invalid JSON rejection.

    For any string that is not valid JSON, _validate_and_parse raises
    a ValueError whose message contains the secret ARN.
    """

    @given(raw_value=st.text().filter(lambda s: not _is_valid_json_module_level(s)))
    @settings(max_examples=100)
    def test_invalid_json_raises_value_error_with_secret_arn(
        self, raw_value: str
    ) -> None:
        """Any non-JSON string causes ValueError mentioning the secret ARN."""
        with pytest.raises(ValueError, match="test-arn"):
            _validate_and_parse(raw_value=raw_value, secret_arn="test-arn")


class TestPropertyNonFlatJsonRejection:
    """Property: Non-flat JSON rejection with key identification.

    For any valid JSON object containing at least one non-string value,
    _validate_and_parse raises a ValueError whose message contains the
    names of all offending keys.
    """

    @given(
        data=st.data(),
    )
    @settings(max_examples=100)
    def test_non_flat_json_raises_value_error_listing_all_offending_keys(
        self, data: st.DataObject
    ) -> None:
        """Any JSON object with non-string values raises ValueError naming all bad keys."""
        non_string_value_strategy = st.one_of(
            st.integers(),
            st.floats(allow_nan=False),
            st.lists(elements=st.integers()),
            st.dictionaries(keys=st.text(), values=st.integers()),
            st.booleans(),
            st.none(),
        )

        # Generate at least one non-string entry
        non_string_entries = data.draw(
            st.dictionaries(
                keys=st.text(min_size=1),
                values=non_string_value_strategy,
                min_size=1,
            )
        )

        # Optionally add some valid string entries
        string_entries = data.draw(
            st.dictionaries(
                keys=st.text(min_size=1),
                values=st.text(),
            )
        )

        # Merge: non-string entries override string entries to guarantee at least one bad key
        combined = {**string_entries, **non_string_entries}
        raw_json = json.dumps(combined)

        with pytest.raises(ValueError) as exc_info:
            _validate_and_parse(raw_value=raw_json, secret_arn="test-arn")

        error_message = str(exc_info.value)
        for key in non_string_entries:
            assert (
                repr(key) in error_message
            ), f"Expected offending key {key!r} in error message: {error_message}"


# ---------------------------------------------------------------------------
# Unit tests for resolve_environment no-op behaviour
# ---------------------------------------------------------------------------


class TestResolveNoOp:
    """Verify resolve_environment is a no-op when env var is not set."""

    def test_no_boto3_client_created_when_env_var_not_set(
        self, unset_env_arn: None
    ) -> None:
        """When LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN is not set, no boto3 client is created."""
        with patch("lambda_tasks.environment_loader.boto3") as mock_boto3:
            resolve_environment()
            mock_boto3.client.assert_not_called()

    def test_os_environ_unchanged_when_env_var_not_set(
        self, unset_env_arn: None
    ) -> None:
        """When LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN is not set, os.environ is unchanged."""
        import os

        env_before = os.environ.copy()

        with patch("lambda_tasks.environment_loader.boto3"):
            resolve_environment()

        env_after = os.environ.copy()
        assert env_before == env_after


# ---------------------------------------------------------------------------
# Unit tests for resolve_environment happy path
# ---------------------------------------------------------------------------


class TestResolveHappyPath:
    """Verify resolve_environment loads secret content into os.environ."""

    def test_valid_flat_json_sets_all_keys_in_os_environ(
        self,
        set_env_arn,
        mock_secretsmanager_client: MagicMock,
        monkeypatch,
    ) -> None:
        """Valid flat JSON sets all key-value pairs in os.environ."""
        import os

        set_env_arn(value=_VALID_REF)
        mock_secretsmanager_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"DB_HOST": "localhost", "DB_PORT": "5432"})
        }

        resolve_environment()

        assert os.environ["DB_HOST"] == "localhost"
        assert os.environ["DB_PORT"] == "5432"

        # Cleanup
        monkeypatch.delenv("DB_HOST", raising=False)
        monkeypatch.delenv("DB_PORT", raising=False)

    def test_secret_keys_override_existing_env_vars(
        self,
        set_env_arn,
        mock_secretsmanager_client: MagicMock,
        monkeypatch,
    ) -> None:
        """Secret keys override existing env vars (no conflict detection)."""
        import os

        monkeypatch.setenv("EXISTING_VAR", "old_value")
        set_env_arn(value=_VALID_REF)
        mock_secretsmanager_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"EXISTING_VAR": "new_value"})
        }

        resolve_environment()

        assert os.environ["EXISTING_VAR"] == "new_value"

    def test_fetch_secret_calls_get_secret_value_with_correct_params(
        self,
        set_env_arn,
        mock_secretsmanager_client: MagicMock,
        monkeypatch,
    ) -> None:
        """_fetch_secret calls get_secret_value with ARN, VersionStage, and VersionId."""
        set_env_arn(value=_VALID_REF)
        mock_secretsmanager_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"KEY": "value"})
        }

        resolve_environment()

        mock_secretsmanager_client.get_secret_value.assert_called_once_with(
            SecretId=_VALID_ARN,
            VersionStage="AWSCURRENT",
            VersionId="v1",
        )

        # Cleanup
        monkeypatch.delenv("KEY", raising=False)

    def test_invalid_reference_format_raises_value_error(
        self,
        set_env_arn,
        mock_secretsmanager_client: MagicMock,
    ) -> None:
        """An env var with wrong segment count raises ValueError before any API call."""
        set_env_arn(value="arn:aws:secretsmanager:eu-west-1:123:secret:my-env")

        with pytest.raises(ValueError, match="9 colon-separated segments"):
            resolve_environment()

        mock_secretsmanager_client.get_secret_value.assert_not_called()


# ---------------------------------------------------------------------------
# Property: Secret content round-trip into environment
# ---------------------------------------------------------------------------


class TestPropertyContentRoundTrip:
    """Property: Secret content round-trip into environment.

    For any valid flat JSON object (where all keys are non-empty strings and
    all values are strings), when the secret returns that JSON, calling
    resolve_environment() SHALL result in every key-value pair from the
    JSON being present in os.environ with the correct value.
    """

    @given(
        env_dict=st.dictionaries(
            keys=st.text(
                min_size=1,
                alphabet=st.characters(
                    blacklist_characters="=\0", blacklist_categories=("Cs",)
                ),
            ),
            values=st.text(
                alphabet=st.characters(
                    blacklist_characters="\0", blacklist_categories=("Cs",)
                ),
            ),
            min_size=1,
        ),
    )
    @settings(max_examples=100)
    def test_all_key_value_pairs_present_in_os_environ(
        self, env_dict: dict[str, str]
    ) -> None:
        """Every key-value pair from the generated dict is in os.environ after loading."""
        import os

        # Reset module-level cache so each hypothesis example starts fresh
        environment_loader._loaded = False

        # Set the trigger env var with valid 9-segment format
        os.environ["LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN"] = _VALID_REF

        try:
            with patch("lambda_tasks.environment_loader.boto3") as mock_boto3:
                mock_client = MagicMock()
                mock_boto3.client.return_value = mock_client
                mock_client.get_secret_value.return_value = {
                    "SecretString": json.dumps(env_dict)
                }

                resolve_environment()

            for key, value in env_dict.items():
                assert key in os.environ, f"Key {key!r} not found in os.environ"
                assert os.environ[key] == value, (
                    f"Expected os.environ[{key!r}] == {value!r}, "
                    f"got {os.environ[key]!r}"
                )
        finally:
            # Clean up: remove all keys we set and the trigger env var
            for key in env_dict:
                os.environ.pop(key, None)
            os.environ.pop("LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN", None)
            environment_loader._loaded = False


# ---------------------------------------------------------------------------
# Unit tests for idempotent execution
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Verify resolve_environment is idempotent — only one API call regardless of call count."""

    def test_calling_twice_results_in_only_one_api_call(
        self,
        set_env_arn,
        mock_secretsmanager_client: MagicMock,
        monkeypatch,
    ) -> None:
        """Second call skips the AWS API call entirely."""
        set_env_arn(value=_VALID_REF)
        mock_secretsmanager_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"KEY": "value"})
        }

        resolve_environment()
        resolve_environment()

        mock_secretsmanager_client.get_secret_value.assert_called_once()

        # Cleanup
        monkeypatch.delenv("KEY", raising=False)

    def test_no_boto3_client_created_when_already_loaded(
        self,
        set_env_arn,
    ) -> None:
        """No boto3 client created on second call when _loaded is True."""
        environment_loader._loaded = True
        set_env_arn(value=_VALID_REF)

        with patch("lambda_tasks.environment_loader.boto3") as mock_boto3:
            resolve_environment()
            mock_boto3.client.assert_not_called()


# ---------------------------------------------------------------------------
# Property: Idempotent execution
# ---------------------------------------------------------------------------


class TestPropertyIdempotentExecution:
    """Property: Idempotent execution.

    For any valid secret content, calling resolve_environment() N
    times (where N >= 2) SHALL result in exactly one Secrets Manager API call,
    with all subsequent calls returning immediately without contacting AWS.
    """

    @given(
        env_dict=st.dictionaries(
            keys=st.text(
                min_size=1,
                alphabet=st.characters(
                    blacklist_characters="=\0", blacklist_categories=("Cs",)
                ),
            ),
            values=st.text(
                alphabet=st.characters(
                    blacklist_characters="\0", blacklist_categories=("Cs",)
                ),
            ),
            min_size=1,
        ),
        call_count=st.integers(min_value=2, max_value=10),
    )
    @settings(max_examples=100)
    def test_get_secret_value_called_exactly_once_regardless_of_call_count(
        self, env_dict: dict[str, str], call_count: int
    ) -> None:
        """boto3 get_secret_value is called exactly once no matter how many times we invoke."""
        import os

        # Reset module-level cache so each hypothesis example starts fresh
        environment_loader._loaded = False

        os.environ["LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN"] = _VALID_REF

        try:
            with patch("lambda_tasks.environment_loader.boto3") as mock_boto3:
                mock_client = MagicMock()
                mock_boto3.client.return_value = mock_client
                mock_client.get_secret_value.return_value = {
                    "SecretString": json.dumps(env_dict)
                }

                for _ in range(call_count):
                    resolve_environment()

                mock_client.get_secret_value.assert_called_once()
        finally:
            # Clean up: remove all keys we set and the trigger env var
            for key in env_dict:
                os.environ.pop(key, None)
            os.environ.pop("LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN", None)
            environment_loader._loaded = False


# ---------------------------------------------------------------------------
# Integration tests for handler cold-start ordering
# ---------------------------------------------------------------------------


class TestHandlerColdStartOrdering:
    """Verify handler.py cold-start calls environment loader, then secrets, then conditionally Django."""

    def test_resolve_environment_called_before_resolve_secrets_into_env(
        self,
        monkeypatch,
    ) -> None:
        """resolve_environment() runs before resolve_secrets_into_env()."""
        import sys

        call_order: list[str] = []

        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

        with (
            patch(
                "lambda_tasks.environment_loader.resolve_environment",
                side_effect=lambda: call_order.append("resolve_environment"),
            ),
            patch(
                "lambda_tasks.secret_loader.resolve_secrets_into_env",
                side_effect=lambda: call_order.append("resolve_secrets_into_env"),
            ),
            patch("django.setup"),
            patch("django.apps.apps.ready", new=False),
        ):
            # Remove cached handler module so reload triggers cold-start code
            sys.modules.pop("lambda_tasks.handler", None)
            import lambda_tasks.handler  # noqa: F401

        assert call_order.index("resolve_environment") < call_order.index(
            "resolve_secrets_into_env"
        ), (
            f"Expected resolve_environment before resolve_secrets_into_env, "
            f"got order: {call_order}"
        )

    def test_both_loaders_run_unconditionally_before_django_settings_check(
        self,
        monkeypatch,
    ) -> None:
        """Both loaders run even when DJANGO_SETTINGS_MODULE is unset."""
        import sys

        call_order: list[str] = []

        # Ensure DJANGO_SETTINGS_MODULE is NOT set — loaders must still run
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

        with (
            patch(
                "lambda_tasks.environment_loader.resolve_environment",
                side_effect=lambda: call_order.append("resolve_environment"),
            ),
            patch(
                "lambda_tasks.secret_loader.resolve_secrets_into_env",
                side_effect=lambda: call_order.append("resolve_secrets_into_env"),
            ),
            patch("django.setup") as mock_django_setup,
            patch("django.apps.apps.ready", new=False),
        ):
            sys.modules.pop("lambda_tasks.handler", None)
            import lambda_tasks.handler  # noqa: F401

        assert (
            "resolve_environment" in call_order
        ), "resolve_environment was not called when DJANGO_SETTINGS_MODULE is unset"
        assert (
            "resolve_secrets_into_env" in call_order
        ), "resolve_secrets_into_env was not called when DJANGO_SETTINGS_MODULE is unset"
        # django.setup() should NOT have been called since DJANGO_SETTINGS_MODULE is unset
        mock_django_setup.assert_not_called()

    def test_django_setup_called_when_settings_module_set_and_apps_not_ready(
        self,
        monkeypatch,
    ) -> None:
        """django.setup() called when DJANGO_SETTINGS_MODULE is set and apps.ready is False."""
        import sys

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.settings")

        with (
            patch(
                "lambda_tasks.environment_loader.resolve_environment",
            ),
            patch(
                "lambda_tasks.secret_loader.resolve_secrets_into_env",
            ),
            patch("django.setup") as mock_django_setup,
            patch("django.apps.apps.ready", new=False),
        ):
            sys.modules.pop("lambda_tasks.handler", None)
            import lambda_tasks.handler  # noqa: F401

        mock_django_setup.assert_called_once()

    def test_django_setup_not_called_when_apps_already_ready(
        self,
        monkeypatch,
    ) -> None:
        """django.setup() is NOT called when apps.ready is True."""
        import sys

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.settings")

        with (
            patch(
                "lambda_tasks.environment_loader.resolve_environment",
            ),
            patch(
                "lambda_tasks.secret_loader.resolve_secrets_into_env",
            ),
            patch("django.setup") as mock_django_setup,
            patch("django.apps.apps.ready", new=True),
        ):
            sys.modules.pop("lambda_tasks.handler", None)
            import lambda_tasks.handler  # noqa: F401

        mock_django_setup.assert_not_called()

    def test_django_setup_not_called_when_settings_module_not_set(
        self,
        monkeypatch,
    ) -> None:
        """django.setup() is NOT called when DJANGO_SETTINGS_MODULE is unset."""
        import sys

        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

        with (
            patch(
                "lambda_tasks.environment_loader.resolve_environment",
            ),
            patch(
                "lambda_tasks.secret_loader.resolve_secrets_into_env",
            ),
            patch("django.setup") as mock_django_setup,
            patch("django.apps.apps.ready", new=False),
        ):
            sys.modules.pop("lambda_tasks.handler", None)
            import lambda_tasks.handler  # noqa: F401

        mock_django_setup.assert_not_called()
