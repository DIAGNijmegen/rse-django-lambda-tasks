# Requirements Document

## Introduction

This feature adds support for deferred task enqueuing in the `django-lambda-tasks` library. Currently, tasks can only be enqueued immediately after a transaction commits via `LambdaTaskWrapper.on_commit()`. This feature allows a task invocation to be serialized into a plain JSON-compatible dict (suitable for storage in a Django `JSONField`), and later loaded and enqueued — from any context (management command, scheduled job, another task, etc.).

`on_commit` and `enqueue_from_json` share the same underlying enqueue path. This means eager mode, queue resolution, and SQS sending all behave identically regardless of how the task was triggered. The refactor extracts a shared `_do_enqueue(task_name, kwargs, delay, queue)` helper that both methods call.

The serialized form captures the task name, kwargs, enqueue-time options (delay, queue), and a stable `invocation_id` generated at serialization time. Using a stable ID means the same stored invocation can be enqueued multiple times and the second delivery will be deduplicated by `TaskRecord.get_or_create` — the same guarantee the existing SQS path provides.

## Glossary

- **LambdaTaskWrapper**: The wrapper object produced by `@lambda_task`; exposes `__call__`, `on_commit`, `to_json`, and `enqueue_from_json` methods.
- **SQSLambdaTask**: A JSON-compatible dict representing a serialized task invocation, including `task_name`, `invocation_id`, `kwargs`, `delay`, and `queue`.
- **Enqueuer**: The `enqueuer.enqueue()` function in `enqueuer.py` that serializes a `SQSLambdaTaskMessage` and sends it to SQS (or runs eagerly).
- **Serializer**: The `serializer.py` module containing `SQSLambdaTaskMessage`, `serialize()`, and `deserialize()`.
- **JSONField**: A Django model field that stores Python dicts as JSON in the database.
- **SQSLambdaSQSLambdaTaskMessage**: A new Pydantic model in `serializer.py` representing the schema of a serialized deferred task invocation.
- **`_do_enqueue`**: A private helper on `LambdaTaskWrapper` that performs the actual enqueue call; shared by `on_commit` and `enqueue_from_json`.

## Requirements

### Requirement 1: Serialize a Task Invocation to a JSON-Compatible Dict

**User Story:** As a developer, I want to serialize a task invocation (with its kwargs and enqueue options) into a plain dict, so that I can store it in a Django `JSONField` for later enqueuing.

#### Acceptance Criteria

1. THE `LambdaTaskWrapper` SHALL expose a `to_json` method that accepts task kwargs plus optional `_delay` and `_queue` override kwargs and returns a JSON-compatible dict.
2. WHEN `to_json` is called, THE `LambdaTaskWrapper` SHALL validate the provided kwargs against the task's declared parameter types before producing the dict.
3. WHEN `to_json` is called with invalid kwargs, THE `LambdaTaskWrapper` SHALL raise a `pydantic.ValidationError`.
4. THE dict returned by `to_json` SHALL contain the fields `task_name`, `invocation_id`, `kwargs`, `delay`, and `queue`.
5. THE `invocation_id` field in the returned dict SHALL be a freshly generated UUID4 string.
6. THE `task_name` field in the returned dict SHALL be the fully-qualified dotted name of the wrapped function (e.g. `"myapp.tasks.my_task"`).
7. WHEN `_delay` is not provided to `to_json`, THE `LambdaTaskWrapper` SHALL use the decorator-level `delay` default for the `delay` field in the returned dict.
8. WHEN `_queue` is not provided to `to_json`, THE `LambdaTaskWrapper` SHALL use the decorator-level `queue` default for the `queue` field in the returned dict.

---

### Requirement 2: Validate and Parse a Deferred Task Dict

**User Story:** As a developer, I want the library to validate a dict loaded from a `JSONField` before enqueuing it, so that corrupt or tampered data is rejected with a clear error.

#### Acceptance Criteria

1. THE `Serializer` SHALL expose a `SQSLambdaSQSLambdaTaskMessage` Pydantic model with required fields: `task_name: str`, `invocation_id: str`, `kwargs: dict`, `delay: int`, `queue: str`.
2. WHEN a dict is validated against `SQSLambdaSQSLambdaTaskMessage`, THE `Serializer` SHALL raise `pydantic.ValidationError` if any required field is missing or has the wrong type.
3. THE `SQSLambdaSQSLambdaTaskMessage` model SHALL reject extra fields not in its schema.
4. FOR ALL valid `SQSLambdaSQSLambdaTaskMessage` instances `m`, constructing `m` from `m.model_dump()` SHALL produce an equivalent object (round-trip property).

---

### Requirement 3: Enqueue a Deferred Task from a Stored Dict

**User Story:** As a developer, I want to load a dict from a `JSONField` and enqueue the represented task, so that I can defer task enqueuing to a later point in time (e.g. from a management command or scheduled job).

#### Acceptance Criteria

1. THE `LambdaTaskWrapper` SHALL expose an `enqueue_from_json` method that accepts a dict (as produced by `to_json`) and enqueues the task by calling the same underlying enqueue path as `on_commit`.
2. WHEN `enqueue_from_json` is called with a valid dict, THE `LambdaTaskWrapper` SHALL validate the dict against `SQSLambdaSQSLambdaTaskMessage` before enqueuing.
3. WHEN `enqueue_from_json` is called with an invalid dict, THE `LambdaTaskWrapper` SHALL raise `pydantic.ValidationError` without enqueuing.
4. WHEN `enqueue_from_json` is called, THE `Enqueuer` SHALL use the `invocation_id`, `delay`, and `queue` values from the dict.
5. WHEN the same dict is passed to `enqueue_from_json` twice, THE second call SHALL be deduplicated by `TaskRecord.get_or_create` because both calls use the same `invocation_id`.
6. WHEN `LAMBDA_TASKS_EAGER` is `True`, `enqueue_from_json` SHALL execute the task synchronously in-process, identical to the eager behaviour of `on_commit`.

---

### Requirement 4: Shared Enqueue Path

**User Story:** As a developer, I want `on_commit` and `enqueue_from_json` to use the same underlying enqueue logic, so that eager mode, queue resolution, and SQS behaviour are always consistent between the two call sites.

#### Acceptance Criteria

1. THE `LambdaTaskWrapper` SHALL extract a private `_do_enqueue(task_name, kwargs, delay, queue)` method that contains the call to `enqueuer.enqueue()`.
2. BOTH `on_commit` and `enqueue_from_json` SHALL delegate to `_do_enqueue` rather than calling `enqueuer.enqueue()` directly.
3. ANY change to enqueue behaviour (e.g. adding a new SQS parameter) SHALL only require a change in `_do_enqueue`.

---

### Requirement 5: Module-Level Standalone Enqueue Function

**User Story:** As a developer, I want a standalone function to enqueue a deferred task dict without needing a reference to the original `LambdaTaskWrapper`, so that I can enqueue from contexts where the wrapper is not easily accessible (e.g. a generic scheduler).

#### Acceptance Criteria

1. THE `Enqueuer` SHALL expose an `enqueue_deferred(deferred: dict) -> None` function that validates the dict against `SQSLambdaSQSLambdaTaskMessage` and calls `enqueuer.enqueue()`.
2. WHEN `enqueue_deferred` is called with an invalid dict, THE `Enqueuer` SHALL raise `pydantic.ValidationError` without calling `enqueuer.enqueue()`.
3. WHEN `enqueue_deferred` is called, THE `Enqueuer` SHALL use the `invocation_id`, `delay`, and `queue` values from the validated dict.
4. WHEN the same dict is passed to `enqueue_deferred` twice, THE second execution SHALL be deduplicated by `TaskRecord.get_or_create` because both calls use the same `invocation_id`.
5. WHEN `LAMBDA_TASKS_EAGER` is `True`, `enqueue_deferred` SHALL execute the task synchronously in-process, identical to the eager behaviour of `enqueue()`.

---

### Requirement 6: Round-Trip Consistency

**User Story:** As a developer, I want the serialization and enqueuing path to be consistent, so that a dict produced by `to_json` can always be enqueued without modification.

#### Acceptance Criteria

1. FOR ALL valid task invocations, calling `to_json(**kwargs)` followed by `enqueue_from_json(result)` SHALL enqueue a `SQSLambdaTaskMessage` with the same `task_name`, `invocation_id`, `kwargs`, `delay`, and `queue` as stored in the dict.
2. FOR ALL valid `SQSLambdaSQSLambdaTaskMessage` dicts `d`, calling `enqueue_deferred(d)` SHALL enqueue a `SQSLambdaTaskMessage` with `task_name`, `invocation_id`, `kwargs`, `delay`, and `queue` matching the fields of `d`.
