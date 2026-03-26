# Implementation Plan: deferred-task-enqueue

## Overview

Implement deferred task enqueuing by adding `SQSLambdaSQSLambdaTaskMessage` to `serializer.py`, extracting
`_send_message` from `enqueuer.py`, adding `enqueue_deferred` to `enqueuer.py`, and adding
`to_json`, `_do_enqueue`, and `enqueue_from_json` to `LambdaTaskWrapper` in `decorators.py`.

TDD order: write failing tests first, then implement until they pass.

All new functions and methods use kwargs-only signatures (enforced by `*`).

## Tasks

- [x] 1. Add `SQSLambdaSQSLambdaTaskMessage` to `serializer.py`
  - Add `SQSLambdaSQSLambdaTaskMessage(BaseModel)` with `model_config = ConfigDict(extra="forbid")`,
    fields `message: SQSLambdaTaskMessage`, `delay: int`, `queue: str`
  - Import `ConfigDict` from pydantic
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 1.1 Write property test for `SQSLambdaSQSLambdaTaskMessage` round-trip (P3)
    - In `tests/test_serializer.py`, add a `@given` test using `st.builds(SQSLambdaSQSLambdaTaskMessage, ...)`
    - Verify `SQSLambdaSQSLambdaTaskMessage.model_validate(m.model_dump()) == m` for all valid instances
    - Tag: `# Feature: deferred-task-enqueue, Property 3: SQSLambdaSQSLambdaTaskMessage round-trip`
    - `@settings(max_examples=100)`
    - **Property 3: SQSLambdaSQSLambdaTaskMessage round-trip**
    - **Validates: Requirements 2.4**

  - [x] 1.2 Write property test for `SQSLambdaSQSLambdaTaskMessage` rejecting invalid/extra fields (P4)
    - In `tests/test_serializer.py`, add a `@given` test that mutates valid dicts (drop required
      field, wrong type, add extra key) and asserts `ValidationError` is raised
    - Tag: `# Feature: deferred-task-enqueue, Property 4: SQSLambdaSQSLambdaTaskMessage rejects invalid and extra fields`
    - `@settings(max_examples=100)`
    - **Property 4: SQSLambdaSQSLambdaTaskMessage rejects invalid and extra fields**
    - **Validates: Requirements 2.2, 2.3**

- [x] 2. Refactor `enqueuer.py`: extract `_send_message` and add `enqueue_deferred`
  - Extract `_send_message(*, body: str, delay: int, queue: str) -> None` from `enqueue()`;
    it owns the `EAGER` branch and the boto3 `send_message` call
  - Rewrite `enqueue()` to call `serialize()` then `_send_message()`; existing behaviour unchanged
  - Add `enqueue_deferred(*, deferred: dict) -> None`: validates via
    `SQSLambdaSQSLambdaTaskMessage.model_validate(deferred)`, then calls
    `_send_message(body=msg.message.model_dump_json(), delay=msg.delay, queue=msg.queue)`
  - Import `SQSLambdaSQSLambdaTaskMessage` from `lambda_tasks.serializer`
  - _Requirements: 4.1, 5.1, 5.2, 5.3, 5.4, 5.5_

  - [x] 2.1 Write unit tests for `_send_message` extraction in `tests/test_enqueuer.py`
    - Test that `enqueue()` still routes to the correct queue URL and delay (existing tests
      must continue to pass — this is a pure refactor)
    - Test that `_send_message` called directly with a pre-serialized body sends to SQS
    - _Requirements: 4.1_

  - [x] 2.2 Write unit tests for `enqueue_deferred` in `tests/test_enqueuer.py`
    - Valid dict → `send_message` called with correct `QueueUrl`, `DelaySeconds`, `MessageBody`
      (body must contain the original `invocation_id`)
    - Invalid dict → `ValidationError` raised, `send_message` not called
    - Eager mode → task executes in-process, `send_message` not called
    - _Requirements: 5.1, 5.2, 5.5_

  - [x] 2.3 Write property test for `enqueue_deferred` passing all fields including stable `invocation_id` (P9)
    - In `tests/test_enqueuer.py`, add a `@given` test using `st.builds(SQSLambdaSQSLambdaTaskMessage, ...)`
    - Capture the `MessageBody` passed to `send_message` and assert `task_name`,
      `invocation_id`, `kwargs`, `delay`, `queue` all match the input dict
    - Tag: `# Feature: deferred-task-enqueue, Property 9: enqueue_deferred passes all fields including stable invocation_id`
    - `@settings(max_examples=100)`
    - **Property 9: enqueue_deferred passes all fields including stable invocation_id**
    - **Validates: Requirements 5.1, 5.3, 5.4, 6.2**

  - [ ] 2.4 Write property test for `enqueue_deferred` rejecting invalid dicts (P10)
    - In `tests/test_enqueuer.py`, add a `@given` test with dicts missing required fields
    - Assert `ValidationError` raised and `send_message` not called
    - Tag: `# Feature: deferred-task-enqueue, Property 10: enqueue_deferred rejects invalid dicts`
    - `@settings(max_examples=100)`
    - **Property 10: enqueue_deferred rejects invalid dicts**
    - **Validates: Requirements 5.2**

  - [ ] 2.5 Write property test for `enqueue_deferred` eager mode (P11)
    - In `tests/test_enqueuer.py`, add a `@given` test with `LAMBDA_TASKS_EAGER=True`
    - Assert task executes in-process and `boto3.client` is never called
    - Tag: `# Feature: deferred-task-enqueue, Property 11: enqueue_deferred eager mode executes synchronously`
    - `@settings(max_examples=100)`
    - **Property 11: enqueue_deferred eager mode executes synchronously**
    - **Validates: Requirements 5.5**

- [ ] 3. Checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Add `to_json`, `_do_enqueue`, and `enqueue_from_json` to `LambdaTaskWrapper`
  - Add `_do_enqueue(self, *, message: SQSLambdaTaskMessage, delay: int, queue: str) -> None`:
    calls `enqueuer._send_message(body=message.model_dump_json(), delay=delay, queue=queue)`
  - Refactor `on_commit` to build a `SQSLambdaTaskMessage` (with fresh UUID4 `invocation_id`) and
    delegate to `self._do_enqueue(message, delay, queue)` instead of calling
    `enqueuer.enqueue()` directly; existing `on_commit` tests must continue to pass
  - Add `to_json(self, **kwargs: Any) -> dict`:
    - Pop `_delay` / `_queue` overrides (same resolution logic as `on_commit`)
    - Validate remaining kwargs via `self._kwargs_model.model_validate(kwargs)`
    - Build `SQSLambdaTaskMessage(task_name=..., kwargs=...)`
    - Return `SQSLambdaSQSLambdaTaskMessage(message=task_message, delay=delay, queue=queue).model_dump()`
  - Add `enqueue_from_json(self, *, data: dict) -> None`:
    - Validate via `SQSLambdaSQSLambdaTaskMessage.model_validate(data)`
    - Call `self._do_enqueue(msg.message, msg.delay, msg.queue)`
  - Import `SQSLambdaSQSLambdaTaskMessage` and `SQSLambdaTaskMessage` from `lambda_tasks.serializer`; import `uuid`
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 3.1, 3.2, 3.3, 3.4, 3.6, 4.1, 4.2_

  - [x] 4.1 Write unit tests for `to_json` in `tests/test_deferred_enqueue.py`
    - Returns a dict that validates as `SQSLambdaSQSLambdaTaskMessage`
    - Uses decorator defaults when `_delay` / `_queue` omitted
    - Uses call-site overrides when `_delay` / `_queue` provided
    - `task_name` matches `module.qualname`
    - `invocation_id` is a valid UUID4 string
    - Raises `ValidationError` for wrong-type kwargs
    - Raises `ValidationError` for missing required kwargs
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8_

  - [x] 4.2 Write unit tests for `enqueue_from_json` in `tests/test_deferred_enqueue.py`
    - Valid dict → `_send_message` called with the stored `invocation_id` unchanged
    - Invalid dict → `ValidationError` raised, `_send_message` not called
    - Eager mode → task executes in-process
    - `on_commit` and `enqueue_from_json` both call `_do_enqueue`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.6, 4.2_

  - [x] 4.3 Write property test for `to_json` structural invariant (P1)
    - In `tests/test_deferred_enqueue.py`, add a `@given` test with valid kwargs
    - Assert returned dict round-trips through `SQSLambdaSQSLambdaTaskMessage.model_validate`
    - Assert `task_name`, `invocation_id`, `kwargs`, `delay`, `queue` all correct
    - Tag: `# Feature: deferred-task-enqueue, Property 1: to_json structural invariant`
    - `@settings(max_examples=100)`
    - **Property 1: to_json structural invariant**
    - **Validates: Requirements 1.1, 1.4, 1.5, 1.6, 1.7, 1.8**

  - [x] 4.4 Write property test for `to_json` rejecting invalid kwargs (P2)
    - In `tests/test_deferred_enqueue.py`, add a `@given` test with wrong-type kwargs
    - Assert `ValidationError` raised and no dict returned
    - Tag: `# Feature: deferred-task-enqueue, Property 2: to_json rejects invalid kwargs`
    - `@settings(max_examples=100)`
    - **Property 2: to_json rejects invalid kwargs**
    - **Validates: Requirements 1.2, 1.3**

  - [x] 4.5 Write property test for `enqueue_from_json` passing stable `invocation_id` (P5)
    - In `tests/test_deferred_enqueue.py`, add a `@given` test with valid deferred dicts
    - Assert `_send_message` receives the same `invocation_id` on two calls with the same dict
    - Tag: `# Feature: deferred-task-enqueue, Property 5: enqueue_from_json passes stable invocation_id to _send_message`
    - `@settings(max_examples=100)`
    - **Property 5: enqueue_from_json passes stable invocation_id to _send_message**
    - **Validates: Requirements 3.4, 3.5, 6.1**

  - [x] 4.6 Write property test for `enqueue_from_json` rejecting invalid dicts (P6)
    - In `tests/test_deferred_enqueue.py`, add a `@given` test with dicts missing required fields
    - Assert `ValidationError` raised and `_send_message` not called
    - Tag: `# Feature: deferred-task-enqueue, Property 6: enqueue_from_json rejects invalid dicts`
    - `@settings(max_examples=100)`
    - **Property 6: enqueue_from_json rejects invalid dicts**
    - **Validates: Requirements 3.2, 3.3**

  - [x] 4.7 Write property test for `enqueue_from_json` eager mode (P7)
    - In `tests/test_deferred_enqueue.py`, add a `@given` test with `LAMBDA_TASKS_EAGER=True`
    - Assert task executes in-process and `boto3.client` is never called
    - Tag: `# Feature: deferred-task-enqueue, Property 7: enqueue_from_json eager mode executes synchronously`
    - `@settings(max_examples=100)`
    - **Property 7: enqueue_from_json eager mode executes synchronously**
    - **Validates: Requirements 3.6**

  - [x] 4.8 Write property test for shared send path between `on_commit` and `enqueue_from_json` (P8)
    - In `tests/test_deferred_enqueue.py`, add a `@given` test with valid task kwargs
    - Assert both `on_commit(**kwargs)` and `enqueue_from_json(to_json(**kwargs))` call
      `_send_message` with the same `task_name`, `kwargs`, `delay`, and `queue`
    - Tag: `# Feature: deferred-task-enqueue, Property 8: on_commit and enqueue_from_json share the same send path`
    - `@settings(max_examples=100)`
    - **Property 8: on_commit and enqueue_from_json share the same send path**
    - **Validates: Requirements 4.1, 4.2**

- [x] 5. Final checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- TDD order: write the failing test sub-task before the implementation in each parent task
- All new functions/methods use kwargs-only signatures (`*` in the parameter list)
- The `_send_message` extraction is a pure refactor — all existing `test_enqueuer.py` tests
  must continue to pass without modification
- The `on_commit` refactor to use `_do_enqueue` must not change observable behaviour —
  all existing `test_decorator.py` tests must continue to pass
- Property tests use `@settings(max_examples=100)` and the tag format
  `# Feature: deferred-task-enqueue, Property N: <text>`
