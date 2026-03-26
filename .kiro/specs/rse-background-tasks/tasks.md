# Implementation Plan: lambda_tasks

## Overview

Implement the `lambda_tasks` Django library as 8 Python modules plus a Django model with migration. Each task builds incrementally toward a fully wired library that can be installed into a Django project.

All implementation follows strict red/green TDD: write a failing test first, then write the minimum code to make it pass, then refactor. Tests are run in parallel with `pytest-xdist` and in random order with `pytest-randomly` to catch ordering dependencies.

## Tasks

- [x] 1. Project scaffolding and package structure
  - Create `lambda_tasks/` package directory with `__init__.py`
  - Create `lambda_tasks/exceptions.py` with `ConfigurationError` and re-export of `ImproperlyConfigured`
  - Create `lambda_tasks/apps.py` with `BackgroundTasksConfig(AppConfig)`
  - Add `hypothesis` as a dev dependency: `uv add --dev hypothesis`
  - Add `pytest-django` as a dev dependency: `uv add --dev pytest-django`
  - Add `pytest-xdist` as a dev dependency: `uv add --dev pytest-xdist`
  - Add `pytest-randomly` as a dev dependency: `uv add --dev pytest-randomly`
  - Create `tests/` directory with `conftest.py` (Django settings fixture)
  - Configure `pytest.ini` or `pyproject.toml` `[tool.pytest.ini_options]` with `-n auto` (xdist) and `--randomly-seed=last` for reproducible reruns
  - _Requirements: 1.1, 8.1_

- [x] 2. Settings module (`lambda_tasks/conf.py`)
  - [x] 2.1 Write failing tests for `LambdaTasksSettings` validation, then implement to make them pass
    - Tests: missing both queue settings → `ImproperlyConfigured`; `LAMBDA_TASKS_QUEUES` without `"default"` key → `ImproperlyConfigured`; `DEFAULT_SOFT_TIMEOUT >= DEFAULT_HARD_TIMEOUT` → `ConfigurationError`; valid settings → correct attribute values exposed
    - Implement lazy `LambdaTasksSettings` class that reads from `django.conf.settings` on first access
    - Expose: `QUEUES`, `SQS_QUEUE_URL`, `DEFAULT_DELAY`, `DEFAULT_SOFT_TIMEOUT`, `DEFAULT_HARD_TIMEOUT`
    - _Requirements: 8.1–8.8, 9.2, 9.3_

  - [x] 2.2 Write property test for settings validation (Property 15)
    - **Property 15: Global settings defaults are validated on first use**
    - **Validates: Requirements 8.4, 8.8, 9.3**
    - Use `st.fixed_dictionaries` to generate settings combinations; assert correct exceptions are raised
    - _File: tests/test_conf.py_

- [x] 3. Task registry (`lambda_tasks/registry.py`)
  - [x] 3.1 Write failing tests for registry behaviour, then implement to make them pass
    - Tests: `get` on unknown name → `KeyError`; `register` then `get` → returns same wrapper; re-registering same name → overwrites
    - Implement `_TASK_REGISTRY: dict[str, LambdaTaskWrapper]`, `register(name, wrapper)`, and `get(name)`
    - _Requirements: 4.3_

- [x] 4. Serializer (`lambda_tasks/serializer.py`)
  - [x] 4.1 Write failing tests for `serialize` / `deserialize`, then implement to make them pass
    - Tests: round-trip preserves kwargs; `invocation_id` is a valid UUID4; two calls produce different `invocation_id`s; `task_name` is present in output; invalid JSON → `ValidationError`
    - Implement `SQSLambdaTaskMessage` Pydantic model and `serialize()` / `deserialize()` functions
    - `SQSLambdaTaskMessage` fields: `task_name`, `invocation_id`, `kwargs`, `soft_timeout`, `hard_timeout`
    - `serialize()` generates a UUID4 `invocation_id` and returns a JSON string
    - `deserialize()` parses and validates the JSON string via `model_validate`
    - _Requirements: 3.1, 3.2, 3.5, 3.6_

  - [x] 4.2 Write property test for serialization round-trip (Property 4)
    - **Property 4: Serialization round-trip**
    - **Validates: Requirements 3.1, 3.2, 3.4**
    - Use `st.fixed_dictionaries` with various value strategies; assert `deserialize(serialize(...)).kwargs == original`
    - _File: tests/test_serializer.py_

  - [x] 4.3 Write property test for message identity fields (Property 5)
    - **Property 5: Serialized message contains task identity and invocation ID**
    - **Validates: Requirements 3.5, 3.6**
    - Assert `task_name` is present and `invocation_id` is a valid UUID; assert two serializations produce different `invocation_id` values
    - _File: tests/test_serializer.py_

  - [x] 4.4 Write property test for type-invalid kwargs rejection (Property 6)
    - **Property 6: Type-invalid kwargs are rejected before enqueueing**
    - **Validates: Requirements 3.3**
    - Generate values that violate annotated types; assert `ValidationError` is raised and no message is produced
    - _File: tests/test_serializer.py_

- [x] 5. Decorator and registry wiring (`lambda_tasks/decorators.py`)
  - [x] 5.1 Write failing tests for `LambdaTaskWrapper`, then implement to make them pass
    - Tests: `__name__` and `__doc__` match original; `__wrapped__` is set; direct `__call__` invokes the original function; `on_commit` is callable
    - Implement `LambdaTaskWrapper` class with `__call__(**kwargs)` and `on_commit(**kwargs)` methods
    - `on_commit` accepts task kwargs plus `_delay`, `_soft_timeout`, `_hard_timeout`, `_queue` overrides
    - Preserve `__name__`, `__doc__`, `__wrapped__` via `functools.wraps`
    - _Requirements: 1.1, 1.4_

  - [x] 5.2 Write failing tests for `lambda_task` decorator validation, then implement to make them pass
    - Tests: function with positional args → `TypeError` at decoration time; `soft_timeout >= hard_timeout` → `ConfigurationError` at decoration time; decorated function is registered in `_TASK_REGISTRY`; zero-arg function is accepted
    - Implement `lambda_task` decorator factory
    - _Requirements: 1.2, 1.3, 1.5_

  - [x] 5.3 Write property test for decorator identity preservation (Property 1)
    - **Property 1: Decorator preserves function identity**
    - **Validates: Requirements 1.1, 1.4**
    - Use `st.text()` for names/docstrings; assert `wrapper.__name__ == func.__name__` and `wrapper.__doc__ == func.__doc__`
    - _File: tests/test_decorator.py_

  - [x] 5.4 Write property test for positional-argument rejection (Property 2)
    - **Property 2: Positional-argument functions are rejected at decoration time**
    - **Validates: Requirements 1.3**
    - Generate functions with 1–5 positional params; assert `TypeError` is raised before wrapper is returned
    - _File: tests/test_decorator.py_

  - [x] 5.5 Write property test for invalid timeout rejection at decoration (Property 3)
    - **Property 3: Invalid timeout configuration is rejected at decoration time**
    - **Validates: Requirements 1.5, 7.5**
    - Use `st.integers()` pairs where `soft >= hard`; assert `ConfigurationError` is raised
    - _File: tests/test_decorator.py_

- [x] 6. Checkpoint — core decorator and serializer
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Timeout enforcer (`lambda_tasks/timeouts.py`)
  - [x] 7.1 Write failing tests for `SoftTimeLimitExceeded` and `HardTimeLimitExceeded`, then implement to make them pass
    - Tests: both are subclasses of `Exception`; they are distinct types
    - Implement `SoftTimeLimitExceeded` and `HardTimeLimitExceeded` exception classes
    - _Requirements: 7.2, 7.3_

  - [x] 7.2 Write failing tests for `TimeoutContext`, then implement to make them pass
    - Tests: task sleeping past `soft_timeout` receives `SoftTimeLimitExceeded`; task ignoring soft and sleeping past `hard_timeout` receives `HardTimeLimitExceeded`; successful task within limits does not raise; pre-existing alarm is restored on exit; `signal.alarm(0)` is called on clean exit
    - Implement `TimeoutContext` context manager using `SIGALRM`
    - Two-phase approach: arm soft timeout first; on `SIGALRM` raise `SoftTimeLimitExceeded` and re-arm for `hard - soft` remaining seconds; second `SIGALRM` raises `HardTimeLimitExceeded`
    - Save and restore any pre-existing alarm on enter/exit
    - Use a state flag to distinguish soft vs hard phase
    - _Requirements: 7.2, 7.3_

- [x] 8. Django data model and migration (`lambda_tasks/models.py`)
  - [x] 8.1 Write failing tests for `TaskRecord`, then implement to make them pass
    - Tests: model can be created and queried via ORM; all required fields are present; `status` choices are `PENDING`, `RUNNING`, `SUCCESS`, `FAILED`; `invocation_id` is unique; default ordering is `-start_time`
    - Implement `TaskRecord(models.Model)` with all fields per design
    - Fields: `task_name`, `invocation_id` (UUIDField, unique), `kwargs` (JSONField), `status` (TextChoices: PENDING/RUNNING/SUCCESS/FAILED), `start_time`, `end_time`, `result`, `traceback`
    - `Meta.ordering = ["-start_time"]`
    - _Requirements: 6.4, 6.5_

  - [x] 8.2 Generate and commit the initial Django migration
    - Run `uv run python -m django makemigrations lambda_tasks --settings=...` or create `migrations/0001_initial.py` manually
    - _Requirements: 6.4_

- [x] 9. Task executor (`lambda_tasks/executor.py`)
  - [x] 9.1 Write failing tests for `execute_task`, then implement to make them pass
    - Tests: successful task → `TaskRecord` status `SUCCESS` with result and end_time; failing task → atomic block rolled back, `TaskRecord` status `FAILED` with traceback committed outside atomic; `soft_timeout >= hard_timeout` → `ConfigurationError`, task not executed; `TaskRecord` created with `RUNNING` status before task runs; ORM writes inside a failing task are not visible after execution
    - Implement `execute_task(message: SQSLambdaTaskMessage) -> None`
    - Create `TaskRecord` with `status=RUNNING`, `start_time=now`
    - Resolve timeouts: message → task decorator default → global settings default
    - Validate `soft_timeout < hard_timeout` → raise `ConfigurationError` if violated (write `FAILED` record, do not execute)
    - Wrap task call in `transaction.atomic` with `TimeoutContext`
    - On success: disarm signals, update record to `SUCCESS` with `result` and `end_time`
    - On any exception: roll back atomic block, then outside it update record to `FAILED` with `traceback` and `end_time`
    - _Requirements: 5.1, 5.2, 5.3, 6.1, 6.2, 6.3, 7.1–7.5_

  - [x] 9.2 Write property test for atomic rollback (Property 12)
    - **Property 12: Atomic execution — failed tasks do not commit DB changes**
    - **Validates: Requirements 5.1, 5.2, 5.3**
    - Generate tasks that write to DB then raise; assert ORM writes are rolled back and `TaskRecord.status == FAILED` is committed
    - _File: tests/test_executor.py_

  - [x] 9.3 Write property test for TaskRecord lifecycle invariant (Property 13)
    - **Property 13: Task_Record lifecycle invariant**
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
    - Use `st.text()` for task names and `st.dictionaries` for kwargs; assert correct field values at each lifecycle stage
    - _File: tests/test_executor.py_

  - [x] 9.4 Write property test for timeout resolution precedence (Property 14)
    - **Property 14: Timeout resolution follows the precedence chain**
    - **Validates: Requirements 7.1, 7.4**
    - Use `st.one_of(st.none(), st.integers(min_value=1))` for each level; assert effective timeouts follow message → task → global order
    - _File: tests/test_timeouts.py_

- [x] 10. Checkpoint — executor and timeouts
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Enqueuer (`lambda_tasks/enqueuer.py`)
  - [x] 11.1 Write failing tests for `enqueue`, then implement to make them pass
    - Tests: known queue name → `send_message` called with correct `QueueUrl` and `DelaySeconds`; unknown queue name → `ImproperlyConfigured`, no SQS call; boto3 raises → exception propagates; `_delay` override used as `DelaySeconds`; timeout values appear in message body
    - Implement `enqueue(task_name, kwargs, delay, soft_timeout, hard_timeout, queue) -> None`
    - Resolve queue URL from `conf.QUEUES` or fall back to `conf.SQS_QUEUE_URL`; raise `ImproperlyConfigured` for unknown queue names
    - Call `serializer.serialize()` to produce the message body
    - Call boto3 SQS `send_message` with `QueueUrl`, `MessageBody`, and `DelaySeconds`
    - Propagate any boto3 exception to the caller (no silent discard)
    - _Requirements: 2.1, 2.2, 2.6, 2.8, 9.5, 9.6, 9.7_

  - [x] 11.2 Write failing tests for `LambdaTaskWrapper.on_commit` enqueue wiring, then implement to make them pass
    - Tests: `_soft_timeout >= _hard_timeout` → `ConfigurationError`, no SQS call; `on_commit` outside transaction → dispatches immediately; `on_commit` inside transaction → dispatches after commit, not before
    - Wire `LambdaTaskWrapper.on_commit` to call `enqueuer.enqueue` via `transaction.on_commit`
    - Validate `_soft_timeout < _hard_timeout` overrides before registering the hook → raise `ConfigurationError`
    - _Requirements: 2.1, 2.3, 2.4, 2.5, 2.7_

  - [x] 11.3 Write property test for on_commit override embedding (Property 7)
    - **Property 7: on_commit overrides are faithfully embedded in the SQS message**
    - **Validates: Requirements 2.2, 2.3, 2.4**
    - Use `st.integers(min_value=0, max_value=899)` for delays/timeouts; mock boto3 and assert `send_message` is called with correct `DelaySeconds` and message body contains override timeout values
    - _File: tests/test_enqueuer.py_

  - [x] 11.4 Write property test for invalid enqueue timeout rejection (Property 8)
    - **Property 8: Invalid timeout overrides at enqueue time are rejected**
    - **Validates: Requirements 2.5**
    - Use `st.integers()` pairs where `soft >= hard`; assert `ConfigurationError` is raised and boto3 is never called
    - _File: tests/test_enqueuer.py_

  - [x] 11.5 Write property test for queue routing (Property 9)
    - **Property 9: Queue routing follows the resolution order**
    - **Validates: Requirements 2.6, 9.5, 9.6, 9.7**
    - Use `st.sampled_from` registered queue names; assert correct SQS URL is used; assert `ImproperlyConfigured` for unknown names
    - _File: tests/test_enqueuer.py_

  - [x] 11.6 Write property test for SQS failure propagation (Property 10)
    - **Property 10: SQS failures propagate as exceptions**
    - **Validates: Requirements 2.8**
    - Mock boto3 to raise; assert exception propagates and no task is silently discarded
    - _File: tests/test_enqueuer.py_

- [x] 12. Lambda handler (`lambda_tasks/handler.py`)
  - [x] 12.1 Implement `handler(event, context) -> dict` and `_process_record(record) -> None`
    - Iterate `event["Records"]` independently
    - For each record: deserialize, look up task via `registry.get()`, call `executor.execute_task()`
    - On failure: log error, append `{"itemIdentifier": record["messageId"]}` to `batchItemFailures`
    - Return `{"batchItemFailures": [...]}` (partial-batch failure reporting)
    - _Requirements: 4.1–4.5_

  - [x] 12.2 Write property test for batch independence (Property 11)
    - **Property 11: Batch records are processed independently**
    - **Validates: Requirements 4.2, 4.3, 4.5**
    - Use `st.lists` of mixed valid/invalid SQS records; assert every record is attempted and `batchItemFailures` contains exactly the failed message IDs
    - _File: tests/test_handler.py_

- [x] 13. Public API and package wiring
  - [x] 13.1 Populate `lambda_tasks/__init__.py` to export `lambda_task`, `TaskRecord`, `SoftTimeLimitExceeded`, `HardTimeLimitExceeded`, and `handler`
  - [x] 13.2 Ensure `lambda_tasks/apps.py` `BackgroundTasksConfig.ready()` imports task modules so the registry is populated before the handler processes messages
  - _Requirements: 1.1, 4.3_

- [x] 14. Final checkpoint — full integration
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP
- Each task references specific requirements for traceability
- Property tests use Hypothesis; run with `uv run pytest` (Hypothesis integrates with pytest automatically)
- `SIGALRM` is Unix-only — tests for timeouts must not run on Windows
- The `FAILED` `TaskRecord` write must always occur outside any `atomic` block to survive transaction rollback
