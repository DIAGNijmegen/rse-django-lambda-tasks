# Implementation Plan: `ignore_errors` Decorator Option

## Overview

Add `ignore_errors` to `LambdaTaskWrapper` and `lambda_task`, then update
`SQSLambdaTaskMessage.execute_immediately()` to treat matching exceptions as non-fatal
(SUCCESS + committed traceback) while leaving all other exception handling
unchanged.

## Tasks

- [x] 1. Add `ignore_errors` to `LambdaTaskWrapper` in `lambda_tasks/decorators.py`
  - Add `ignore_errors: tuple[type[BaseException], ...] = ()` parameter to `LambdaTaskWrapper.__init__`
  - Store as `self._ignore_errors` after calling `_validate_ignore_errors`
  - Add `_validate_ignore_errors` static method that raises `TypeError` for any element that is not a subclass of `BaseException`
  - Add read-only `ignore_errors` property returning `self._ignore_errors`
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

  - [x] 1.1 Write property test for `ignore_errors` round-trip on `LambdaTaskWrapper`
    - **Property 1: `ignore_errors` round-trip on `LambdaTaskWrapper`**
    - **Validates: Requirements 1.1, 1.3**

  - [x] 1.2 Write property test for non-exception type rejection
    - **Property 2: Non-exception types in `ignore_errors` are rejected at decoration time**
    - **Validates: Requirements 1.4**

- [x] 2. Update `lambda_task` factory in `lambda_tasks/decorators.py`
  - Add `ignore_errors: tuple[type[BaseException], ...] = ()` to both `@overload` signatures and the concrete `lambda_task` function
  - Forward `ignore_errors` to `LambdaTaskWrapper(...)` inside `_decorate`
  - _Requirements: 1.5_

  - [x] 2.1 Write unit tests for `lambda_task` factory forwarding
    - Verify `lambda_task(ignore_errors=(ValueError,))` produces a wrapper with `ignore_errors == (ValueError,)`
    - Verify `lambda_task` with no `ignore_errors` defaults to `()`
    - Verify `SQSLambdaTaskMessage` has no `ignore_errors` field
    - _Requirements: 1.5, 4.2_

- [x] 3. Checkpoint — ensure decorator tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Restructure `SQSLambdaTaskMessage.execute_immediately()` in `lambda_tasks/models.py`
  - After `import_string` resolution, read `wrapper.ignore_errors`
  - Introduce `ignored_exc: BaseException | None = None` sentinel before the `try/except`
  - In the `except Exception as error` block: if `wrapper.ignore_errors` is non-empty and `isinstance(error, wrapper.ignore_errors)`, set `ignored_exc = error`; otherwise follow the existing FAILED path (set `record.status = FAILED`, save traceback, log warning) and return
  - After the `try/except`, branch on `ignored_exc`: if `None` → existing SUCCESS path (result + end_time); if set → SUCCESS path with traceback instead of result, log `"Succeeded (ignored {type(ignored_exc).__name__}) in {record.duration}"`
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 4.1, 5.1, 5.2_

  - [x] 4.1 Write property test for ignored exception producing SUCCESS
    - **Property 3: Ignored exception produces SUCCESS with traceback and end_time**
    - **Validates: Requirements 2.1, 2.3, 2.4, 5.1**

  - [x] 4.2 Write property test for ignored exception committing the record
    - **Property 4: Ignored exception commits the transaction (task-side ORM writes rolled back, record committed)**
    - **Validates: Requirements 2.2**

  - [x] 4.3 Write property test for subclass of ignored type being ignored
    - **Property 5: Subclass of ignored exception type is also ignored**
    - **Validates: Requirements 2.5**

  - [x] 4.4 Write property test for non-ignored exception producing FAILED
    - **Property 6: Non-ignored exception produces FAILED with rollback**
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4**

- [x] 5. Add unit tests for `execute()` regression guard in `tests/test_models.py`
  - Test clean success path: `traceback` remains `None` (regression guard)
  - Test non-ignored exception path: `status=FAILED`, traceback non-null, task-side writes rolled back
  - Test `ignore_errors=()` (default): all exceptions still produce `FAILED`
  - _Requirements: 3.3, 5.2, 5.3_

- [x] 6. Write property test for eager mode parity in `tests/test_models.py`
  - [x] 6.1 Write property test for eager mode applying the same `ignore_errors` logic
    - **Property 7: Eager mode applies the same `ignore_errors` logic**
    - **Validates: Requirements 4.3**

- [x] 7. Final checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP
- Each task references specific requirements for traceability
- Property tests use Hypothesis with `st.sampled_from` for exception types and dynamically created subclasses for Property 5
- The transaction semantics for ignored exceptions are intentional: `transaction.atomic()` rolls back task-side writes; the `TaskRecord` save happens outside the atomic block and commits in autocommit context
