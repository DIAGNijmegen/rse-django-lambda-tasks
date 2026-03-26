# Implementation Plan: task-retry

## Overview

Add automatic retry support to `@lambda_task`. Changes span `models.py` (new field, new exception, new status, retry logic), `settings.py` (new setting), a new migration, and tests for both `models.py` and `decorators.py`. The `retry_on` parameter, `_validate_retry_on`, and `_validate_no_overlap` are already implemented in `decorators.py` — those do not need to be re-implemented, but tests for them are required.

## Tasks

- [x] 1. Add `_n_retries` field to `SQSLambdaTaskMessage` and thread it through `_build_task`
  - Add `_n_retries: int = Field(default=0, ge=0)` to `SQSLambdaTaskMessage` in `models.py`
  - Add `model_config = ConfigDict(populate_by_name=True)` to `SQSLambdaTaskMessage` so the underscore-prefixed field is accepted by name
  - In `LambdaTaskWrapper._build_task` in `decorators.py`, pop `_n_retries` from kwargs (alongside `_delay`) and pass it to `SQSLambdaTaskMessage`
  - _Requirements: 2.1, 2.3_

  - [x] 1.1 Write property test for `_n_retries` non-negative validation
    - **Property 3: `_n_retries` non-negative validation**
    - **Validates: Requirements 2.3**
    - Use `st.integers(max_value=-1)` to assert `ValidationError` is raised; use `st.integers(min_value=0)` to assert construction succeeds

- [x] 2. Add `MaxRetriesExceededError` to `models.py`
  - Define `MaxRetriesExceededError(Exception)` at module level in `models.py`
  - Constructor takes `*, task_name: str, n_retries: int` and formats a message containing both values
  - _Requirements: 4.3_

  - [x] 2.1 Write unit + property tests for `MaxRetriesExceededError`
    - Unit test: verify it is a subclass of `Exception`
    - **Property 9: `MaxRetriesExceededError` message contains task name and retry count**
    - **Validates: Requirements 4.3**
    - Use `st.text(min_size=1)` for task name and `st.integers(min_value=0)` for retry count

- [x] 3. Add `MAX_RETRIES` property to `LambdaTasksSettings` in `settings.py`
  - Add `MAX_RETRIES` property that reads `LAMBDA_TASKS_MAX_RETRIES` from Django settings, defaulting to `2880`
  - _Requirements: 4.1_

  - [x] 3.1 Write unit tests for `MAX_RETRIES`
    - Test default value is `2880` when `LAMBDA_TASKS_MAX_RETRIES` is not set
    - Test that setting `LAMBDA_TASKS_MAX_RETRIES` in Django settings overrides the default
    - _Requirements: 4.1_

- [x] 4. Add `RETRYING` status to `TaskRecord` and create the migration
  - Add `RETRYING = "RETRYING"` to `TaskRecord.Status` in `models.py`
  - Update the `CheckConstraint` in `TaskRecord.Meta` to include `"RETRYING"` in the `status__in` list
  - Create migration `lambda_tasks/migrations/0002_taskrecord_retrying_status.py` that alters the `status` field choices and drops/recreates the `CheckConstraint`
  - _Requirements: 3.2_

  - [x] 4.1 Write unit test for `RETRYING` status
    - Test that `TaskRecord.Status.RETRYING == "RETRYING"`
    - Test that a `TaskRecord` can be saved with `status=RETRYING` via the ORM
    - _Requirements: 3.2_

- [x] 5. Implement retry logic in `execute_immediately()`
  - In `models.py`, add a retry branch in the `except Exception` block of `execute_immediately()`, between the `ignore_errors` check and the existing failure path
  - When `wrapper.retry_on` is non-empty and `isinstance(error, wrapper.retry_on)`:
    - If `self._n_retries >= conf.MAX_RETRIES`: save record as `FAILED` with traceback, then raise `MaxRetriesExceededError`
    - Otherwise: compute delay (`wrapper._delay` if non-zero, else `max(1, round(random.uniform(0, 5)))`), call `wrapper.execute_on_commit(**self.kwargs, _delay=delay, _n_retries=self._n_retries + 1)`, save record as `RETRYING` with traceback, and return
  - Import `random` at the top of `models.py`
  - _Requirements: 2.2, 3.1, 3.2, 3.3, 3.4, 4.2, 4.4, 5.1, 5.2, 5.3_

  - [x] 5.1 Write property test for retry increments `_n_retries`
    - **Property 4: Retry increments `_n_retries`**
    - **Validates: Requirements 2.2**
    - Use `st.integers(min_value=0, max_value=MAX_RETRIES-1)` for starting `_n_retries`; assert the message passed to `execute_on_commit` has `_n_retries == n + 1`

  - [x] 5.2 Write property test for matching exception enqueues retry with same kwargs and new `invocation_id`
    - **Property 5: Matching exception enqueues retry with same kwargs and new `invocation_id`**
    - **Validates: Requirements 3.1**
    - Use `st.fixed_dictionaries` for kwargs; assert `execute_on_commit` called once, kwargs match, `invocation_id` differs

  - [x] 5.3 Write property test for `RETRYING` status and non-null traceback
    - **Property 6: Retried task record has `RETRYING` status and non-null traceback**
    - **Validates: Requirements 3.2**
    - Assert `record.status == RETRYING`, `record.traceback` is non-null, `record.end_time` is non-null

  - [x] 5.4 Write property test for non-matching exception follows failure path
    - **Property 7: Non-matching exception → `FAILED`, no retry enqueued**
    - **Validates: Requirements 3.3, 3.4**
    - Use exception types not in `retry_on`; assert `execute_on_commit` not called and `record.status == FAILED`

  - [x] 5.5 Write property test for `_n_retries >= MAX_RETRIES` raises `MaxRetriesExceededError`
    - **Property 8: `_n_retries >= MAX_RETRIES` raises `MaxRetriesExceededError` and records `FAILED`**
    - **Validates: Requirements 4.2, 4.4**
    - Use `st.integers(min_value=MAX_RETRIES)` for `_n_retries`; assert `MaxRetriesExceededError` raised, `execute_on_commit` not called, `record.status == FAILED`

  - [x] 5.6 Write property test for non-zero wrapper delay used as retry `_delay`
    - **Property 10: Non-zero wrapper delay is used as retry `_delay`**
    - **Validates: Requirements 5.1**
    - Use `st.integers(min_value=1, max_value=900)` for delay; assert `_delay` passed to `execute_on_commit` equals wrapper delay

  - [x] 5.7 Write property test for zero wrapper delay produces `_delay` in `[1, 5]`
    - **Property 11: Zero wrapper delay produces retry `_delay` in `[1, 5]`**
    - **Validates: Requirements 5.2**
    - Fix `delay=0`, run 100 iterations; assert `_delay` is an integer in `[1, 5]`

- [x] 6. Checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Write tests for `decorators.py` (`retry_on` validation and no-overlap validation)
  - Create `tests/test_decorators.py`
  - Unit tests:
    - `retry_on` defaults to empty tuple (Requirement 1.2)
    - `lambda_task` decorator forwards `retry_on` to `LambdaTaskWrapper` (Requirement 1.4)
    - Valid `retry_on` tuple constructs without error
    - Invalid `retry_on` element raises `TypeError`
    - Overlapping `retry_on` and `ignore_errors` raises `TypeError`
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [x] 7.1 Write property test for valid `retry_on` tuples
    - **Property 1: `retry_on` accepts any tuple of `BaseException` subclasses**
    - **Validates: Requirements 1.1**
    - Use `st.lists(st.sampled_from([ValueError, RuntimeError, TypeError, OSError, KeyError]), min_size=0).map(tuple)`

  - [x] 7.2 Write property test for invalid `retry_on` raises `TypeError`
    - **Property 2: Invalid `retry_on` raises `TypeError` at decoration time**
    - **Validates: Requirements 1.3**
    - Use `st.one_of(st.integers(), st.text(), st.none(), st.booleans())` as invalid elements

  - [x] 7.3 Write property test for overlapping `retry_on` and `ignore_errors` raises `TypeError`
    - **Property 12: Overlapping `retry_on` and `ignore_errors` raises `TypeError` at decoration time**
    - **Validates: Requirements 1.5**
    - Generate tuples with guaranteed overlap (same type in both, or subclass relationship); assert `TypeError` raised

- [x] 8. Update `product.md` to document the retry feature
  - Add `retry_on` to the task definition example in `product.md`
  - Add a `retry_on` section describing behaviour, `MAX_RETRIES`, `MaxRetriesExceededError`, `RETRYING` status, and delay logic
  - Add `_n_retries` to the SQS Message Schema table
  - Add `RETRYING` to the `TaskRecord` statuses list
  - Add `LAMBDA_TASKS_MAX_RETRIES` to the Django Settings table
  - Update the `execute_immediately()` execution steps to include the retry branch
  - _Requirements: 1.1–1.5, 2.1–2.3, 3.1–3.4, 4.1–4.4, 5.1–5.3_

- [x] 9. Final checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- `retry_on`, `_validate_retry_on`, and `_validate_no_overlap` in `decorators.py` are already implemented — do not re-implement them
- Each task references specific requirements for traceability
- Property tests validate universal correctness properties; unit tests cover specific examples and edge cases
