# Implementation Plan: retry-delay

## Overview

Add `retry_delay` to `@lambda_task`, remove the `_delay` call-time override from `execute_on_commit`, and update the retry path in `execute_immediately` to use `wrapper.retry_delay` directly via a `SQSLambdaTask` built in-place. Follow red/green TDD: tests are written before each implementation step.

## Tasks

- [x] 1. Add `_validate_delay` to `LambdaTaskWrapper`
  - [x] 1.1 Write failing tests for `_validate_delay`
    - In `tests/test_decorators.py`, add unit tests asserting:
      - `delay < 0` raises `ValueError`
      - `delay > 900` raises `ValueError`
      - `delay=0` and `delay=900` are accepted (boundary values)
    - Tests must fail before implementation exists
    - _Requirements: 2.1, 2.3_
  - [x] 1.2 Implement `_validate_delay` static method on `LambdaTaskWrapper`
    - Add `@staticmethod _validate_delay(*, delay: int) -> None` to `LambdaTaskWrapper` in `decorators.py`
    - Raises `ValueError` if `delay < 0` or `delay > 900`
    - Call it from `__init__` (after `_validate_timeouts`)
    - _Requirements: 2.1, 2.3_

- [x] 2. Add `retry_delay` parameter and `_validate_retry_delay` to `LambdaTaskWrapper`
  - [x] 2.1 Write failing tests for `retry_delay` init and `_validate_retry_delay`
    - In `tests/test_decorators.py`, add unit tests asserting:
      - `retry_delay` defaults to `0`
      - `retry_delay=0` with empty `retry_on` is accepted
      - `retry_delay > 0` with non-empty `retry_on` is accepted
      - `retry_delay > 0` with empty `retry_on` raises `TypeError`
      - `retry_delay < 0` raises `ValueError`
      - `retry_delay > 900` raises `ValueError`
    - Tests must fail before implementation exists
    - _Requirements: 1.1, 1.3, 1.4, 2.2, 2.4_
  - [x] 2.2 Implement `retry_delay` parameter and `_validate_retry_delay`
    - Add `retry_delay: int = 0` to `LambdaTaskWrapper.__init__` signature
    - Add `@staticmethod _validate_retry_delay(*, retry_delay: int, retry_on: tuple[type[BaseException], ...]) -> None`
    - Raises `ValueError` if `retry_delay < 0` or `retry_delay > 900`
    - Raises `TypeError` if `retry_delay != 0` and `retry_on` is empty
    - Call it from `__init__` (after `_validate_delay`)
    - Store as `self._retry_delay = retry_delay`
    - _Requirements: 1.1, 1.3, 1.4, 2.2, 2.4_

- [x] 3. Add `retry_delay` property to `LambdaTaskWrapper`
  - [x] 3.1 Write failing tests for `retry_delay` property
    - In `tests/test_decorators.py`, add unit tests asserting:
      - `wrapper.retry_delay` returns the value passed at construction
      - Default is `0`
    - Tests must fail before implementation exists
    - _Requirements: 1.2_
  - [x] 3.2 Implement `retry_delay` property
    - Add `@property retry_delay(self) -> int` to `LambdaTaskWrapper`, returning `self._retry_delay`
    - Mirror the existing `retry_on` and `ignore_errors` property pattern
    - _Requirements: 1.2_

- [x] 4. Add `queue` property to `LambdaTaskWrapper`
  - [x] 4.1 Write failing tests for `queue` property
    - In `tests/test_decorators.py`, add unit tests asserting:
      - `wrapper.queue` returns the queue name passed at construction
      - Default is `"default"`
    - Tests must fail before implementation exists
  - [x] 4.2 Implement `queue` property
    - Add `@property queue(self) -> str` to `LambdaTaskWrapper`, returning `self._queue`
    - _Requirements: 3.1 (needed by retry path in models.py)_

- [x] 5. Update `_build_task` to remove `_delay` pop and use `self._delay` directly
  - [x] 5.1 Write failing tests for updated `_build_task` behaviour
    - In `tests/test_decorators.py`, add unit tests asserting:
      - Passing `_delay` as a kwarg to `execute_on_commit` raises (Pydantic `ValidationError` from `extra="forbid"`)
      - Normal `execute_on_commit` without `_delay` still works and uses the decorator `delay`
    - Tests must fail before implementation exists
    - _Requirements: 4.1, 4.2, 4.3_
  - [x] 5.2 Remove `_delay` pop from `_build_task`; use `self._delay` directly
    - In `decorators.py`, remove `delay = kwargs.pop("_delay", self._delay)` from `_build_task`
    - Replace with `delay = self._delay` (no pop)
    - Only `_n_retries` is still popped from kwargs
    - Update `_build_task` docstring to remove all `_delay` references
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 6. Update `execute_on_commit` and `serialize` docstrings
  - Remove all references to `_delay` from the docstrings of `execute_on_commit` and `serialize` in `decorators.py`
  - No behaviour change — docstring-only update
  - _Requirements: 4.1_

- [x] 7. Add `retry_delay` to the `lambda_task` decorator factory
  - [x] 7.1 Write failing tests for `lambda_task` forwarding `retry_delay`
    - In `tests/test_decorators.py`, add unit tests asserting:
      - `@lambda_task(retry_delay=30, retry_on=(ValueError,))` produces `wrapper.retry_delay == 30`
      - `@lambda_task` without `retry_delay` produces `wrapper.retry_delay == 0`
    - Tests must fail before implementation exists
    - _Requirements: 1.1, 1.2_
  - [x] 7.2 Add `retry_delay` to both `@overload` signatures and the `lambda_task` implementation
    - Add `retry_delay: int = 0` to both `@overload` stubs and the concrete `lambda_task` function in `decorators.py`
    - Pass `retry_delay=retry_delay` through to `LambdaTaskWrapper(...)` inside `_decorate`
    - _Requirements: 1.1_

- [x] 8. Checkpoint — ensure all decorator tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Update `execute_immediately` retry path in `models.py`
  - [x] 9.1 Write failing tests for the updated retry path
    - In `tests/test_models.py`, add unit tests asserting:
      - Retry path with non-zero `retry_delay` enqueues a `SQSLambdaTask` with `delay == retry_delay`
      - Retry path with `retry_delay=0` enqueues a `SQSLambdaTask` with `delay` in `[1, 5]`
      - Normal `execute_on_commit` (non-retry) uses the decorator `delay`, not `retry_delay`
      - Passing `_delay` to `execute_on_commit` raises `ValidationError`
    - Tests must fail before implementation exists
    - _Requirements: 3.1, 3.2, 3.3, 4.1, 4.2_
  - [x] 9.2 Update the retry path in `SQSLambdaTaskMessage.execute_immediately`
    - Replace `wrapper._delay` with `wrapper.retry_delay` for the delay resolution
    - Replace the `wrapper.execute_on_commit(**self.kwargs, _delay=delay, _n_retries=...)` call with a direct `SQSLambdaTask` construction and `execute_on_commit()`:
      ```python
      delay = wrapper.retry_delay if wrapper.retry_delay != 0 else round(random.uniform(1, 5))
      retry_task = SQSLambdaTask(
          message=SQSLambdaTaskMessage(
              task_name=self.task_name,
              kwargs=self.kwargs,
              n_retries=self.n_retries + 1,
          ),
          delay=delay,
          queue=wrapper.queue,
      )
      retry_task.execute_on_commit()
      ```
    - _Requirements: 3.1, 3.2, 3.3_

- [x] 10. Update `product.md` steering doc
  - In `.kiro/steering/product.md`:
    - Update the `## Enqueuing` section: remove `_delay` from the list of per-call overrides; state that `execute_on_commit()` uses only the decorator `delay` value
    - Update the `## retry_on` section: replace the "Retry delay" paragraph to describe `retry_delay` (non-zero → use `retry_delay`; zero → jitter `round(random.uniform(1, 5))`)
    - Add `retry_delay` to the `## Task Definition` example snippet
  - _Requirements: 4.1_

- [x] 11. Checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Property-based tests (hypothesis) for the 6 correctness properties
  - [x] 12.1 Property 1: `retry_delay` storage round-trip
    - In `tests/test_decorators.py`, add `@given(st.integers(min_value=0, max_value=900))`
    - For `retry_delay=0`: construct with empty `retry_on`; for `retry_delay > 0`: construct with `retry_on=(ValueError,)`
    - Assert `wrapper.retry_delay == input`
    - **Property 1: retry_delay storage round-trip**
    - **Validates: Requirements 1.2**
  - [x] 12.2 Property 2: `retry_delay` requires `retry_on`
    - In `tests/test_decorators.py`, add `@given(st.integers(min_value=1, max_value=900))`
    - Construct `LambdaTaskWrapper` with `retry_delay=value` and empty `retry_on`
    - Assert `TypeError` is raised
    - **Property 2: retry_delay requires retry_on**
    - **Validates: Requirements 1.4**
  - [x] 12.3 Property 3: out-of-range `delay` and `retry_delay` raise `ValueError`
    - In `tests/test_decorators.py`, add `@given(st.integers().filter(lambda x: x < 0 or x > 900))`
    - Test both `delay=value` and `retry_delay=value` (with `retry_on=(ValueError,)` for the latter)
    - Assert `ValueError` is raised in both cases
    - **Property 3: out-of-range delay and retry_delay raise ValueError**
    - **Validates: Requirements 2.1, 2.2**
  - [x] 12.4 Property 4: non-zero `retry_delay` is used in the retry enqueue
    - In `tests/test_models.py`, add `@given(st.integers(min_value=1, max_value=900))`
    - Mock `SQSLambdaTask.execute_on_commit` or capture the constructed task; trigger a retryable exception
    - Assert the enqueued `SQSLambdaTask.delay == retry_delay`
    - **Property 4: non-zero retry_delay is used in the retry enqueue**
    - **Validates: Requirements 3.1**
  - [x] 12.5 Property 5: zero `retry_delay` produces jitter in `[1, 5]`
    - In `tests/test_models.py`, use repeated sampling (e.g. 100 iterations) with `retry_delay=0`
    - Assert every enqueued delay is an integer in `[1, 5]`
    - **Property 5: zero retry_delay produces jitter in [1, 5]**
    - **Validates: Requirements 3.2, 3.3**
  - [x] 12.6 Property 6: normal enqueue uses decorator `delay`
    - In `tests/test_decorators.py` or `tests/test_models.py`, add `@given(st.integers(min_value=0, max_value=900))`
    - Call `execute_on_commit` directly (not via retry path); capture the `SQSLambdaTask`
    - Assert `task.delay == decorator delay`
    - **Property 6: normal enqueue uses decorator delay**
    - **Validates: Requirements 4.3, 4.4**

- [x] 13. Final checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- TDD order is strict: each `x.1` test task must be completed before its `x.2` implementation task
- Existing tests for `_delay` call-time override (e.g. `test_property_on_commit_delay_embedded_in_sqs_message` and `test_property_10_non_zero_delay_used_as_retry_delay`) will need to be updated or removed as part of tasks 5 and 9 — they test behaviour that is being removed
- `retry_delay` is never serialised into the SQS message; it lives only on `LambdaTaskWrapper`
- Property tests use `min_examples=100` per the design doc
