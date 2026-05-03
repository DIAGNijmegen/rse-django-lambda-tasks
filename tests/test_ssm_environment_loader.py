"""
Tests for lambda_tasks.ssm_environment_loader.

boto3 is never called for real — the SSM client is patched at the module level
so no AWS credentials are required.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import lambda_tasks.ssm_environment_loader as ssm_environment_loader
from lambda_tasks.ssm_environment_loader import (
    _validate_and_parse,
    resolve_ssm_environment,
)


@pytest.fixture(autouse=True)
def reset_loaded():
    """Reset the module-level _loaded sentinel between every test."""
    ssm_environment_loader._loaded = False
    yield
    ssm_environment_loader._loaded = False


@pytest.fixture()
def mock_ssm_client():
    """Patch boto3 so no real AWS calls are made; yields the mock SSM client."""
    with patch("lambda_tasks.ssm_environment_loader.boto3") as mock_boto3:
        client = MagicMock()
        mock_boto3.client.return_value = client
        yield client


@pytest.fixture()
def set_ssm_env(monkeypatch):
    """Helper to set the LAMBDA_TASKS_SSM_ENVIRONMENT env var."""

    def _set(*, value: str) -> None:
        monkeypatch.setenv("LAMBDA_TASKS_SSM_ENVIRONMENT", value)

    return _set


@pytest.fixture()
def unset_ssm_env(monkeypatch):
    """Helper to ensure LAMBDA_TASKS_SSM_ENVIRONMENT is not set."""
    monkeypatch.delenv("LAMBDA_TASKS_SSM_ENVIRONMENT", raising=False)


# ---------------------------------------------------------------------------
# Smoke tests — module importable and public API callable
# ---------------------------------------------------------------------------


class TestSmoke:
    """Verify the module is importable and resolve_ssm_environment is callable."""

    def test_module_is_importable(self):
        """Requirement 4.2: module resides in lambda_tasks package."""
        import lambda_tasks.ssm_environment_loader  # noqa: F401

    def test_resolve_ssm_environment_is_callable_with_no_args(self, unset_ssm_env):
        """Requirement 4.1: public function takes no arguments."""
        resolve_ssm_environment()


# ---------------------------------------------------------------------------
# Unit tests for _validate_and_parse
# ---------------------------------------------------------------------------


class TestValidateAndParse:
    """Unit tests for _validate_and_parse covering validation and happy path."""

    def test_invalid_json_raises_value_error_with_parameter_name(self) -> None:
        """Requirement 2.1: invalid JSON raises ValueError with parameter name."""
        from lambda_tasks.ssm_environment_loader import _validate_and_parse

        with pytest.raises(ValueError, match="my-param"):
            _validate_and_parse(raw_value="not valid json{", parameter_name="my-param")

    def test_non_string_values_raises_value_error_listing_offending_keys(self) -> None:
        """Requirement 2.2: non-string values raises ValueError listing offending keys."""
        from lambda_tasks.ssm_environment_loader import _validate_and_parse

        raw_value = '{"good": "value", "bad_int": 42, "bad_list": [1, 2]}'
        with pytest.raises(ValueError, match="bad_int") as exc_info:
            _validate_and_parse(raw_value=raw_value, parameter_name="test-param")
        assert "bad_list" in str(exc_info.value)

    def test_empty_string_key_raises_value_error(self) -> None:
        """Requirement 2.3: empty string key raises ValueError."""
        from lambda_tasks.ssm_environment_loader import _validate_and_parse

        raw_value = '{"": "some_value", "valid_key": "ok"}'
        with pytest.raises(ValueError):
            _validate_and_parse(raw_value=raw_value, parameter_name="test-param")

    def test_valid_flat_json_returns_dict(self) -> None:
        """Requirements 2.1, 2.2, 2.3: valid flat JSON returns dict[str, str]."""
        from lambda_tasks.ssm_environment_loader import _validate_and_parse

        raw_value = '{"DB_HOST": "localhost", "DB_PORT": "5432"}'
        result = _validate_and_parse(raw_value=raw_value, parameter_name="test-param")
        assert result == {"DB_HOST": "localhost", "DB_PORT": "5432"}


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
    """Property 2: Invalid JSON rejection.

    For any string that is not valid JSON, _validate_and_parse raises
    a ValueError whose message contains the parameter name.

    # Feature: ssm-environment-loader, Property 2: Invalid JSON rejection
    """

    # **Validates: Requirements 2.1**

    @given(raw_value=st.text().filter(lambda s: not _is_valid_json_module_level(s)))
    @settings(max_examples=100)
    def test_invalid_json_raises_value_error_with_parameter_name(
        self, raw_value: str
    ) -> None:
        """Any non-JSON string causes ValueError mentioning the parameter name."""
        with pytest.raises(ValueError, match="test-param"):
            _validate_and_parse(raw_value=raw_value, parameter_name="test-param")


class TestPropertyNonFlatJsonRejection:
    """Property 3: Non-flat JSON rejection with key identification.

    For any valid JSON object containing at least one non-string value,
    _validate_and_parse raises a ValueError whose message contains the
    names of all offending keys.

    # Feature: ssm-environment-loader, Property 3: Non-flat JSON rejection with key identification
    """

    # **Validates: Requirements 2.2**

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
            _validate_and_parse(raw_value=raw_json, parameter_name="test-param")

        error_message = str(exc_info.value)
        for key in non_string_entries:
            # The error message uses Python list repr, so check for repr(key)
            assert (
                repr(key) in error_message
            ), f"Expected offending key {key!r} in error message: {error_message}"


# ---------------------------------------------------------------------------
# Unit tests for resolve_ssm_environment no-op behaviour
# ---------------------------------------------------------------------------


class TestResolveNoOp:
    """Verify resolve_ssm_environment is a no-op when env var is not set.

    Requirements: 1.2
    """

    def test_no_boto3_client_created_when_env_var_not_set(
        self, unset_ssm_env: None
    ) -> None:
        """When LAMBDA_TASKS_SSM_ENVIRONMENT is not set, no boto3 client is created."""
        with patch("lambda_tasks.ssm_environment_loader.boto3") as mock_boto3:
            resolve_ssm_environment()
            mock_boto3.client.assert_not_called()

    def test_os_environ_unchanged_when_env_var_not_set(
        self, unset_ssm_env: None
    ) -> None:
        """When LAMBDA_TASKS_SSM_ENVIRONMENT is not set, os.environ is unchanged."""
        import os

        env_before = os.environ.copy()

        with patch("lambda_tasks.ssm_environment_loader.boto3"):
            resolve_ssm_environment()

        env_after = os.environ.copy()
        assert env_before == env_after


# ---------------------------------------------------------------------------
# Unit tests for resolve_ssm_environment happy path
# ---------------------------------------------------------------------------


class TestResolveHappyPath:
    """Verify resolve_ssm_environment loads SSM parameter content into os.environ.

    Requirements: 1.1, 1.3
    """

    def test_valid_flat_json_sets_all_keys_in_os_environ(
        self,
        set_ssm_env,
        mock_ssm_client: MagicMock,
        monkeypatch,
    ) -> None:
        """Requirement 1.3: valid flat JSON sets all key-value pairs in os.environ."""
        import os

        set_ssm_env(value="/my/param")
        mock_ssm_client.get_parameter.return_value = {
            "Parameter": {
                "Value": json.dumps({"DB_HOST": "localhost", "DB_PORT": "5432"})
            }
        }

        resolve_ssm_environment()

        assert os.environ["DB_HOST"] == "localhost"
        assert os.environ["DB_PORT"] == "5432"

        # Cleanup
        monkeypatch.delenv("DB_HOST", raising=False)
        monkeypatch.delenv("DB_PORT", raising=False)

    def test_ssm_keys_override_existing_env_vars(
        self,
        set_ssm_env,
        mock_ssm_client: MagicMock,
        monkeypatch,
    ) -> None:
        """Requirement 1.3: SSM keys override existing env vars (no conflict detection)."""
        import os

        monkeypatch.setenv("EXISTING_VAR", "old_value")
        set_ssm_env(value="/my/param")
        mock_ssm_client.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps({"EXISTING_VAR": "new_value"})}
        }

        resolve_ssm_environment()

        assert os.environ["EXISTING_VAR"] == "new_value"

    def test_fetch_parameter_calls_get_parameter_with_decryption(
        self,
        set_ssm_env,
        mock_ssm_client: MagicMock,
        monkeypatch,
    ) -> None:
        """Requirement 1.1: _fetch_parameter calls ssm.get_parameter with WithDecryption=True."""
        set_ssm_env(value="/my/param")
        mock_ssm_client.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps({"KEY": "value"})}
        }

        resolve_ssm_environment()

        mock_ssm_client.get_parameter.assert_called_once_with(
            Name="/my/param", WithDecryption=True
        )

        # Cleanup
        monkeypatch.delenv("KEY", raising=False)


# ---------------------------------------------------------------------------
# Property 1: Parameter content round-trip into environment
# ---------------------------------------------------------------------------


class TestPropertyContentRoundTrip:
    """Property 1: Parameter content round-trip into environment.

    For any valid flat JSON object (where all keys are non-empty strings and
    all values are strings), when the SSM parameter returns that JSON, calling
    resolve_ssm_environment() SHALL result in every key-value pair from the
    JSON being present in os.environ with the correct value.

    # Feature: ssm-environment-loader, Property 1: Parameter content round-trip into environment
    """

    # **Validates: Requirements 1.3**

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
        ssm_environment_loader._loaded = False

        # Set the trigger env var
        os.environ["LAMBDA_TASKS_SSM_ENVIRONMENT"] = "/test/param"

        try:
            with patch("lambda_tasks.ssm_environment_loader.boto3") as mock_boto3:
                mock_client = MagicMock()
                mock_boto3.client.return_value = mock_client
                mock_client.get_parameter.return_value = {
                    "Parameter": {"Value": json.dumps(env_dict)}
                }

                resolve_ssm_environment()

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
            os.environ.pop("LAMBDA_TASKS_SSM_ENVIRONMENT", None)
            ssm_environment_loader._loaded = False


# ---------------------------------------------------------------------------
# Unit tests for idempotent execution
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Verify resolve_ssm_environment is idempotent — only one API call regardless of call count.

    Requirements: 3.1, 3.2
    """

    def test_calling_twice_results_in_only_one_api_call(
        self,
        set_ssm_env,
        mock_ssm_client: MagicMock,
        monkeypatch,
    ) -> None:
        """Requirement 3.1: second call skips the AWS API call entirely."""
        import os

        set_ssm_env(value="/my/param")
        mock_ssm_client.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps({"KEY": "value"})}
        }

        resolve_ssm_environment()
        resolve_ssm_environment()

        mock_ssm_client.get_parameter.assert_called_once()

        # Cleanup
        monkeypatch.delenv("KEY", raising=False)

    def test_no_boto3_client_created_when_already_loaded(
        self,
        set_ssm_env,
    ) -> None:
        """Requirement 3.2: no boto3 client created on second call when _loaded is True."""
        ssm_environment_loader._loaded = True
        set_ssm_env(value="/my/param")

        with patch("lambda_tasks.ssm_environment_loader.boto3") as mock_boto3:
            resolve_ssm_environment()
            mock_boto3.client.assert_not_called()


# ---------------------------------------------------------------------------
# Property 4: Idempotent execution
# ---------------------------------------------------------------------------


class TestPropertyIdempotentExecution:
    """Property 4: Idempotent execution.

    For any valid SSM parameter content, calling resolve_ssm_environment() N
    times (where N >= 2) SHALL result in exactly one SSM API call, with all
    subsequent calls returning immediately without contacting AWS.

    # Feature: ssm-environment-loader, Property 4: Idempotent execution
    """

    # **Validates: Requirements 3.1, 3.2**

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
    def test_get_parameter_called_exactly_once_regardless_of_call_count(
        self, env_dict: dict[str, str], call_count: int
    ) -> None:
        """boto3 get_parameter is called exactly once no matter how many times we invoke."""
        import os

        # Reset module-level cache so each hypothesis example starts fresh
        ssm_environment_loader._loaded = False

        os.environ["LAMBDA_TASKS_SSM_ENVIRONMENT"] = "/test/param"

        try:
            with patch("lambda_tasks.ssm_environment_loader.boto3") as mock_boto3:
                mock_client = MagicMock()
                mock_boto3.client.return_value = mock_client
                mock_client.get_parameter.return_value = {
                    "Parameter": {"Value": json.dumps(env_dict)}
                }

                for _ in range(call_count):
                    resolve_ssm_environment()

                mock_client.get_parameter.assert_called_once()
        finally:
            # Clean up: remove all keys we set and the trigger env var
            for key in env_dict:
                os.environ.pop(key, None)
            os.environ.pop("LAMBDA_TASKS_SSM_ENVIRONMENT", None)
            ssm_environment_loader._loaded = False


# ---------------------------------------------------------------------------
# Integration tests for handler cold-start ordering
# ---------------------------------------------------------------------------


class TestHandlerColdStartOrdering:
    """Verify handler.py cold-start calls SSM loader, then secrets, then conditionally Django.

    Requirements: 1.4, 1.5, 1.6
    """

    def test_resolve_ssm_environment_called_before_resolve_secrets_into_env(
        self,
        monkeypatch,
    ) -> None:
        """Requirement 1.5: resolve_ssm_environment() runs before resolve_secrets_into_env()."""
        import sys

        call_order: list[str] = []

        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

        with (
            patch(
                "lambda_tasks.ssm_environment_loader.resolve_ssm_environment",
                side_effect=lambda: call_order.append("resolve_ssm_environment"),
            ) as mock_ssm,
            patch(
                "lambda_tasks.secret_loader.resolve_secrets_into_env",
                side_effect=lambda: call_order.append("resolve_secrets_into_env"),
            ) as mock_secrets,
            patch("django.setup"),
            patch("django.apps.apps.ready", new=False),
        ):
            # Remove cached handler module so reload triggers cold-start code
            sys.modules.pop("lambda_tasks.handler", None)
            import lambda_tasks.handler  # noqa: F401

        assert call_order.index("resolve_ssm_environment") < call_order.index(
            "resolve_secrets_into_env"
        ), (
            f"Expected resolve_ssm_environment before resolve_secrets_into_env, "
            f"got order: {call_order}"
        )

    def test_both_loaders_run_unconditionally_before_django_settings_check(
        self,
        monkeypatch,
    ) -> None:
        """Requirements 1.4, 1.5: both loaders run even when DJANGO_SETTINGS_MODULE is unset."""
        import sys

        call_order: list[str] = []

        # Ensure DJANGO_SETTINGS_MODULE is NOT set — loaders must still run
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

        with (
            patch(
                "lambda_tasks.ssm_environment_loader.resolve_ssm_environment",
                side_effect=lambda: call_order.append("resolve_ssm_environment"),
            ) as mock_ssm,
            patch(
                "lambda_tasks.secret_loader.resolve_secrets_into_env",
                side_effect=lambda: call_order.append("resolve_secrets_into_env"),
            ) as mock_secrets,
            patch("django.setup") as mock_django_setup,
            patch("django.apps.apps.ready", new=False),
        ):
            sys.modules.pop("lambda_tasks.handler", None)
            import lambda_tasks.handler  # noqa: F401

        assert (
            "resolve_ssm_environment" in call_order
        ), "resolve_ssm_environment was not called when DJANGO_SETTINGS_MODULE is unset"
        assert (
            "resolve_secrets_into_env" in call_order
        ), "resolve_secrets_into_env was not called when DJANGO_SETTINGS_MODULE is unset"
        # django.setup() should NOT have been called since DJANGO_SETTINGS_MODULE is unset
        mock_django_setup.assert_not_called()

    def test_django_setup_called_when_settings_module_set_and_apps_not_ready(
        self,
        monkeypatch,
    ) -> None:
        """Requirement 1.6: django.setup() called when DJANGO_SETTINGS_MODULE is set and apps.ready is False."""
        import sys

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.settings")

        with (
            patch(
                "lambda_tasks.ssm_environment_loader.resolve_ssm_environment",
            ) as mock_ssm,
            patch(
                "lambda_tasks.secret_loader.resolve_secrets_into_env",
            ) as mock_secrets,
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
        """Requirement 1.6: django.setup() is NOT called when apps.ready is True."""
        import sys

        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.settings")

        with (
            patch(
                "lambda_tasks.ssm_environment_loader.resolve_ssm_environment",
            ) as mock_ssm,
            patch(
                "lambda_tasks.secret_loader.resolve_secrets_into_env",
            ) as mock_secrets,
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
        """Requirement 1.6: django.setup() is NOT called when DJANGO_SETTINGS_MODULE is unset."""
        import sys

        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

        with (
            patch(
                "lambda_tasks.ssm_environment_loader.resolve_ssm_environment",
            ) as mock_ssm,
            patch(
                "lambda_tasks.secret_loader.resolve_secrets_into_env",
            ) as mock_secrets,
            patch("django.setup") as mock_django_setup,
            patch("django.apps.apps.ready", new=False),
        ):
            sys.modules.pop("lambda_tasks.handler", None)
            import lambda_tasks.handler  # noqa: F401

        mock_django_setup.assert_not_called()
