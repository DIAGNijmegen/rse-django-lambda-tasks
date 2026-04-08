# Implementation Plan: Singleton Task

## Overview

Add `singleton=True` support to `@lambda_task` by extending `LambdaTaskWrapper`, `lambda_task` decorator factory, `LambdaTasksSettings`, and `execute_immediately()`. The singleton lock is acquired via Django's cache framework (Redis) before task execution; lock contention is handled by the existing retry mechanism.

## Tasks

- [x] 1. Add `SINGLETON_CACHE` setting to `LambdaTasksSettings`
  - [x] 1.1 Add `SINGLETON_CACHE` property to `LambdaTasksSettings` in `settings.py`
    - Add property that reads `LAMBDA_TASKS_SINGLETON_CACHE` from Django settings with default `"default"`
    - Return `str(getattr(django_settings, "LAMBDA_TASKS_SINGLETON_CACHE", "default"))`
    - _Requirements: 4.1_

  - [x] 1.2 Write unit tests for `SINGLETON_CACHE` in `tests/test_settings.py`
    - Test default value is `"default"` when setting is not configured
    - Test reads configured value when `LAMBDA_TASKS_SINGLETON_CACHE` is set
    - _Requirements: 4.1_

- [x] 2. Add `singleton` parameter to `LambdaTaskWrapper` and `lambda_task`
  - [x] 2.1 Add `singleton` kwarg to `LambdaTaskWrapper.__init__` and expose as property in `decorators.py`
    - Add `singleton: bool = False` keyword-only parameter to `__init__`
    - Store as `self._singleton`
    - Add `@property` returning `self._singleton`
    - _Requirements: 1.1, 1.2_

  - [x] 2.2 Forward `singleton` kwarg through `lambda_task` decorator factory in `decorators.py`
    - Add `singleton: bool = False` to `lambda_task` signature and both `@overload` signatures
    - Pass through to `LambdaTaskWrapper` in `_decorate`
    - _Requirements: 1.1, 1.2_

  - [x] 2.3 Write unit tests for singleton decorator in `tests/test_decorators.py`
    - Test `singleton` defaults to `False`
    - Test `singleton=True` is stored and exposed via property
    - Test `lambda_task(singleton=True)` forwards to wrapper
    - Test `@lambda_task` without `singleton` produces `wrapper.singleton == False`
    - _Requirements: 1.1, 1.2, 1.3_

  - [x] 2.4 Write property test for singleton storage round-trip in `tests/test_decorators.py`
    - **Property 1: Singleton storage round-trip**
    - Generate random booleans, verify `LambdaTaskWrapper(func, singleton=b).singleton == b`
    - **Validates: Requirements 1.1, 1.2**

- [x] 3. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement singleton lock acquisition in `execute_immediately()`
  - [x] 4.1 Modify `execute_immediately()` in `models.py` to acquire lock when `wrapper.singleton is True`
    - Import `LockError` from `redis.exceptions`
    - When `wrapper.singleton` is `True`:
      - Retrieve cache backend via `caches[LambdaTasksSettings().SINGLETON_CACHE]`
      - Compute lock key: `f"lambda_tasks.singleton_lock.{self.task_name}"`
      - Compute effective retry tuple: `effective_retry_on = (LockError, *wrapper.retry_on)` when singleton, else `wrapper.retry_on`
      - Wrap the `transaction.atomic()` + `TimeoutContext` block inside `cache.lock(lock_key)` context manager
    - When `wrapper.singleton` is `False`: no changes to existing behavior
    - Use `effective_retry_on` in the existing `isinstance(error, ...)` check instead of `wrapper.retry_on`
    - _Requirements: 1.3, 2.1, 2.2, 2.3, 3.1, 5.2_

  - [x] 4.2 Write property test for lock key format in `tests/test_models.py`
    - **Property 2: Lock key format**
    - Generate random task name strings, mock cache backend, verify `cache.lock()` called with `lambda_tasks.singleton_lock.{task_name}`
    - **Validates: Requirements 2.1**

  - [x] 4.3 Write property test for lock release on success and failure in `tests/test_models.py`
    - **Property 3: Lock release on success and failure**
    - Generate success/failure scenarios for singleton tasks, verify lock context manager `__exit__` is called
    - **Validates: Requirements 2.2, 2.3**

  - [x] 4.4 Write property test for LockError retry in `tests/test_models.py`
    - **Property 4: LockError triggers retry with RETRYING status and incremented n_retries**
    - Generate `n_retries` in `[0, MAX_RETRIES-1]`, mock cache.lock to raise LockError, verify TaskRecord status is RETRYING, traceback contains "LockError", and re-enqueued task has `n_retries + 1`
    - **Validates: Requirements 3.1, 3.3**

  - [x] 4.5 Write property test for LockError at MAX_RETRIES in `tests/test_models.py`
    - **Property 5: LockError at MAX_RETRIES raises MaxRetriesExceededError**
    - Generate `n_retries` in `[MAX_RETRIES, 32767]`, mock cache.lock to raise LockError, verify `MaxRetriesExceededError` raised and TaskRecord status is FAILED with non-null traceback
    - **Validates: Requirements 3.2**

  - [x] 4.6 Write unit tests for singleton execution in `tests/test_models.py`
    - Test `singleton=False` does not acquire a lock
    - Test executor uses `caches[SINGLETON_CACHE]` for lock acquisition
    - Test `SQSLambdaTaskMessage` schema does not include `singleton` field
    - _Requirements: 1.3, 4.2, 5.1_

- [x] 5. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Final wiring and documentation
  - [x] 6.1 Update `product.md` with singleton task documentation
    - Add singleton option to the decorator section
    - Document `LAMBDA_TASKS_SINGLETON_CACHE` setting in the Django Settings table
    - Document lock contention retry behavior
    - _Requirements: 1.1, 2.1, 3.1, 4.1_

- [x] 7. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
