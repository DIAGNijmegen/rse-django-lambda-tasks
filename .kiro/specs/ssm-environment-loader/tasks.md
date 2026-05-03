# Implementation Plan: SSM Environment Loader

## Overview

Implement a new module `lambda_tasks/ssm_environment_loader.py` that reads an AWS SSM Parameter Store parameter at Lambda cold start and sets its JSON content as environment variables. Follow TDD: write failing tests first, then implement the minimum code to pass. Integrate into `handler.py` so SSM loading runs before secrets and Django setup.

## Tasks

- [x] 1. Create module skeleton and test infrastructure
  - [x] 1.1 Create `lambda_tasks/ssm_environment_loader.py` with module docstring, imports, module-level `_loaded` sentinel, and stub `resolve_ssm_environment()` that does nothing
    - Define `_loaded: bool = False`
    - Import `os`, `json`, `logging`, `boto3`
    - Stub `resolve_ssm_environment() -> None` with `pass` body
    - Stub `_fetch_parameter(*, parameter_name: str) -> str` with `pass` body
    - Stub `_validate_and_parse(*, raw_value: str, parameter_name: str) -> dict[str, str]` with `pass` body
    - All functions use keyword-only args and full type annotations
    - _Requirements: 4.1, 4.2, 4.3_

  - [x] 1.2 Create `tests/test_ssm_environment_loader.py` with test scaffolding
    - Add autouse fixture to reset `_loaded` sentinel between tests
    - Add fixture to patch boto3 SSM client at module level (same pattern as `test_secret_loader.py`)
    - Add monkeypatch-based env var helpers
    - Verify module is importable and `resolve_ssm_environment` is callable with no args
    - _Requirements: 4.1, 4.2_

- [x] 2. Implement validation logic (`_validate_and_parse`)
  - [x] 2.1 Write unit tests for `_validate_and_parse` covering invalid JSON, non-flat objects, and empty keys
    - Test: invalid JSON string raises `ValueError` with parameter name in message
    - Test: JSON with non-string values raises `ValueError` listing offending keys
    - Test: JSON with empty string key raises `ValueError`
    - Test: valid flat JSON returns `dict[str, str]`
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 2.2 Implement `_validate_and_parse` to pass all validation tests
    - Parse with `json.loads`; catch `json.JSONDecodeError` and raise `ValueError` with parameter name
    - Check top-level is a `dict`; raise if not
    - Check all values are `str`; collect offending keys and raise `ValueError` listing them
    - Check no empty string keys; raise `ValueError` if found
    - Return validated `dict[str, str]`
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 2.3 Write property test for invalid JSON rejection
    - **Property 2: Invalid JSON rejection**
    - Generate arbitrary strings that are not valid JSON
    - Assert `_validate_and_parse` raises `ValueError` with parameter name in message
    - **Validates: Requirements 2.1**

  - [x] 2.4 Write property test for non-flat JSON rejection with key identification
    - **Property 3: Non-flat JSON rejection with key identification**
    - Generate JSON objects with at least one non-string value (int, float, list, dict, bool, None)
    - Assert `_validate_and_parse` raises `ValueError` whose message contains all offending key names
    - **Validates: Requirements 2.2**

- [x] 3. Implement core loading logic (`resolve_ssm_environment`)
  - [x] 3.1 Write unit tests for `resolve_ssm_environment` no-op behaviour
    - Test: when `LAMBDA_TASKS_SSM_ENVIRONMENT` is not set, no boto3 client is created and no API call is made
    - Test: when `LAMBDA_TASKS_SSM_ENVIRONMENT` is not set, `os.environ` is unchanged
    - _Requirements: 1.2_

  - [x] 3.2 Write unit tests for `resolve_ssm_environment` happy path
    - Test: when SSM parameter contains valid flat JSON, all key-value pairs are set in `os.environ`
    - Test: SSM keys override existing env vars (no conflict detection)
    - Test: `_fetch_parameter` calls `ssm.get_parameter(Name=param_name, WithDecryption=True)`
    - _Requirements: 1.1, 1.3_

  - [x] 3.3 Implement `_fetch_parameter` and `resolve_ssm_environment`
    - `_fetch_parameter`: create boto3 SSM client, call `get_parameter(Name=parameter_name, WithDecryption=True)`, return `Parameter.Value`
    - `resolve_ssm_environment`: check `LAMBDA_TASKS_SSM_ENVIRONMENT` env var; if not set, return immediately; if `_loaded` is True, return immediately; call `_fetch_parameter`, call `_validate_and_parse`, set each key in `os.environ`, set `_loaded = True`
    - _Requirements: 1.1, 1.2, 1.3_

  - [x] 3.4 Write property test for parameter content round-trip into environment
    - **Property 1: Parameter content round-trip into environment**
    - Generate arbitrary flat `dict[str, str]` (non-empty keys, string values)
    - Mock SSM to return `json.dumps(generated_dict)`
    - Assert every key-value pair from the dict is present in `os.environ` after calling `resolve_ssm_environment()`
    - **Validates: Requirements 1.3**

- [x] 4. Implement idempotency
  - [x] 4.1 Write unit tests for idempotent execution
    - Test: calling `resolve_ssm_environment()` twice results in only one boto3 API call
    - Test: no boto3 client created on second call when `_loaded` is True
    - _Requirements: 3.1, 3.2_

  - [x] 4.2 Verify idempotency implementation passes (already implemented via `_loaded` flag in 3.3)
    - Confirm `_loaded` sentinel prevents re-execution
    - _Requirements: 3.1, 3.2_

  - [x] 4.3 Write property test for idempotent execution
    - **Property 4: Idempotent execution**
    - Generate valid SSM content and a call count N (2 to 10)
    - Call `resolve_ssm_environment()` N times
    - Assert boto3 `get_parameter` was called exactly once
    - **Validates: Requirements 3.1, 3.2**

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Integrate into handler and verify ordering
  - [x] 6.1 Write integration test for handler cold-start ordering
    - Test: `resolve_ssm_environment()` is called before `resolve_secrets_into_env()`
    - Test: both loaders run unconditionally before the `DJANGO_SETTINGS_MODULE` check
    - Test: `django.setup()` is only called when `DJANGO_SETTINGS_MODULE` is set and `apps.ready` is False
    - _Requirements: 1.4, 1.5, 1.6_

  - [x] 6.2 Modify `lambda_tasks/handler.py` to integrate SSM loader
    - Add `from lambda_tasks.ssm_environment_loader import resolve_ssm_environment`
    - Move `resolve_secrets_into_env()` outside the `if` block
    - Add `resolve_ssm_environment()` call before `resolve_secrets_into_env()`
    - Keep `django.setup()` inside the `if os.environ.get("DJANGO_SETTINGS_MODULE") and not apps.ready:` conditional
    - _Requirements: 1.4, 1.5, 1.6_

- [x] 7. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- Follow TDD: write failing tests first (tasks X.1, X.2), then implement (tasks X.3)
- All functions use keyword-only arguments and full type annotations per project conventions
- boto3 is mocked in all tests — no real AWS calls
