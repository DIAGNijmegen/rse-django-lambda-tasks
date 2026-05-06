# Implementation Plan: Async Local Execution

## Overview

Adds a third execution mode to django-lambda-tasks using `concurrent.futures.ProcessPoolExecutor`. Tasks are submitted to a process pool when `LAMBDA_TASKS_LOCAL_WORKERS` is set to a positive integer, providing true async parallelism with timeout enforcement for local development. Implementation follows TDD: write failing tests first, then implement.

## Tasks

- [x] 1. Add `LOCAL_WORKERS` setting with validation
  - [x] 1.1 Write tests for `LOCAL_WORKERS` property in `tests/test_local_executor.py`
    - Test that `LOCAL_WORKERS` defaults to `0` when setting is absent
    - Test that a positive integer is returned correctly
    - Test that a negative integer raises `ImproperlyConfigured`
    - Test that setting both `EAGER=True` and `LOCAL_WORKERS > 0` raises `ImproperlyConfigured`
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

  - [x] 1.2 Write property tests for `LOCAL_WORKERS` validation
    - **Property 1: Positive LOCAL_WORKERS is preserved**
    - **Validates: Requirements 1.1**
    - **Property 2: Negative LOCAL_WORKERS is rejected**
    - **Validates: Requirements 1.3**
    - **Property 3: Mutual exclusion of EAGER and LOCAL_WORKERS**
    - **Validates: Requirements 1.4**

  - [x] 1.3 Implement `LOCAL_WORKERS` property in `lambda_tasks/settings.py`
    - Add `LOCAL_WORKERS` property to `LambdaTasksSettings`
    - Read from `LAMBDA_TASKS_LOCAL_WORKERS` Django setting with default `0`
    - Raise `ImproperlyConfigured` if value is negative
    - Raise `ImproperlyConfigured` if both `EAGER` and `LOCAL_WORKERS > 0`
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 2. Create `lambda_tasks/local_executor.py` module
  - [x] 2.1 Write tests for `get_pool()` in `tests/test_local_executor.py`
    - Test that `get_pool()` returns a `ProcessPoolExecutor` with `_max_workers` equal to `LOCAL_WORKERS`
    - Test that `get_pool()` returns the same instance on repeated calls (pool reuse)
    - Test that the pool is stored at module level (`local_executor._pool`)
    - _Requirements: 2.1, 2.2, 2.4_

  - [x] 2.2 Write property test for pool worker count
    - **Property 4: Pool created with correct worker count**
    - **Validates: Requirements 2.1**

  - [x] 2.3 Implement `get_pool()` and `_pool_initializer()` in `lambda_tasks/local_executor.py`
    - Create module-level `_pool: ProcessPoolExecutor | None = None`
    - Implement `_pool_initializer()` that calls `django.setup()`
    - Implement `get_pool()` that lazily creates the pool with `max_workers=conf.LOCAL_WORKERS`
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 2.4 Write tests for `submit_task()` in `tests/test_local_executor.py`
    - Test that `submit_task()` calls `pool.submit()` with `_execute_in_worker`, the JSON string, and a UUID message_id
    - Test that `submit_task()` does not wait on the returned Future
    - _Requirements: 3.1, 3.2, 3.4, 5.3_

  - [x] 2.5 Implement `submit_task()` and `_execute_in_worker()` in `lambda_tasks/local_executor.py`
    - Implement `_execute_in_worker(*, message_json: str, message_id: str)` that deserializes and executes
    - Implement `submit_task(*, message_json: str)` that generates a UUID and submits to the pool
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 6.1, 6.2_

  - [x] 2.6 Write property test for message serialization round-trip
    - **Property 6: Task message serialization round-trip**
    - **Validates: Requirements 6.1, 6.2, 3.2, 3.3**

- [x] 3. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Add async local dispatch branch to `SQSLambdaTask._execute()`
  - [x] 4.1 Write tests for the dispatch routing in `tests/test_local_executor.py`
    - Test that when `LOCAL_WORKERS > 0` and `EAGER=False`, `_execute()` calls `submit_task()` with the JSON-serialized message
    - Test that when `LOCAL_WORKERS > 0` and `EAGER=False`, `_execute()` does NOT call `boto3.client('sqs').send_message()`
    - Test that when `LOCAL_WORKERS=0` and `EAGER=False`, `_execute()` sends to SQS (existing behaviour preserved)
    - Test that when `EAGER=True`, `_execute()` calls `execute_immediately()` (existing behaviour preserved)
    - _Requirements: 3.1, 7.1, 7.2, 7.3_

  - [x] 4.2 Write property test for async local dispatch routing
    - **Property 5: Async local dispatch routes to pool**
    - **Validates: Requirements 3.1, 7.2**

  - [x] 4.3 Implement the third dispatch branch in `lambda_tasks/models.py`
    - Add `elif conf.LOCAL_WORKERS > 0` branch in `SQSLambdaTask._execute()`
    - Import `submit_task` from `lambda_tasks.local_executor`
    - Call `submit_task(message_json=self.message.model_dump_json())`
    - _Requirements: 3.1, 7.2_

- [x] 5. Write integration tests for transaction commit and error isolation
  - [x] 5.1 Write integration tests in `tests/test_local_executor.py`
    - Test that `execute_on_commit()` in async local mode submits after transaction commit
    - Test that a rolled-back transaction does not submit to the pool
    - Test that a worker exception does not crash the pool (pool continues accepting tasks)
    - Test that `_pool_initializer` calls `django.setup()`
    - _Requirements: 2.3, 4.1, 4.2, 5.1, 5.2_

- [x] 6. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- TDD order: tests are written before implementation in each group
- No changes to `lambda_tasks/timeouts.py` — workers inherit `EAGER=False` so `SIGALRM` works automatically
