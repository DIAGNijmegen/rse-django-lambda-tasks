# Requirements Document

## Introduction

Add automatic retry support to the `@lambda_task` decorator. When a task raises an exception matching one of the types listed in `retry_on`, the executor re-enqueues the task via `execute_on_commit` with the same kwargs and a fresh `invocation_id`, incrementing a retry counter. Retries continue until the counter reaches `LAMBDA_TASKS_MAX_RETRIES`, at which point a `MaxRetriesExceededError` is raised instead.

## Glossary

- **Executor**: The `SQSLambdaTaskMessage.execute_immediately()` method that runs a task inside a `transaction.atomic()` block.
- **LambdaTaskWrapper**: The object produced by the `@lambda_task` decorator; holds per-task configuration including `retry_on`.
- **MaxRetriesExceededError**: Exception raised by the Executor when the retry count reaches the configured maximum.
- **Retry**: Re-enqueuing a task via `execute_on_commit` with the same kwargs, a new `invocation_id`, and an incremented `n_retries` value.
- **RETRYING**: A `TaskRecord` status indicating the task failed and a retry has been enqueued; distinct from `FAILED`, which indicates no further retries will occur.
- **SQSLambdaTaskMessage**: Pydantic model representing the SQS message schema; carries `task_name`, `invocation_id`, `kwargs`, and `n_retries`.
- **Settings**: Django settings read by `LambdaTasksSettings`.

---

## Requirements

### Requirement 1: `retry_on` option on `@lambda_task`

**User Story:** As a developer, I want to declare which exception types should trigger an automatic retry, so that transient failures are handled without manual intervention.

#### Acceptance Criteria

1. THE `LambdaTaskWrapper` SHALL accept a `retry_on` parameter containing a tuple of exception types.
2. THE `LambdaTaskWrapper` SHALL default `retry_on` to an empty tuple when the parameter is not supplied.
3. WHEN `retry_on` is supplied with a value that is not a tuple of `BaseException` subclasses, THE `LambdaTaskWrapper` SHALL raise a `TypeError` at decoration time.
4. THE `lambda_task` decorator SHALL accept and forward the `retry_on` parameter to `LambdaTaskWrapper`.
5. WHEN `retry_on` and `ignore_errors` contain any exception type in common (exact match or subclass relationship), THE `LambdaTaskWrapper` SHALL raise a `TypeError` at decoration time.

---

### Requirement 2: Retry counter on `SQSLambdaTaskMessage`

**User Story:** As a developer, I want the SQS message to carry a retry count, so that the Executor can enforce the maximum retry limit.

#### Acceptance Criteria

1. THE `SQSLambdaTaskMessage` SHALL include a `n_retries` field of type `int` with a default value of `0`.
2. WHEN a retry is enqueued, THE Executor SHALL set `n_retries` on the new message to the previous `n_retries` value plus `1`.
3. THE `SQSLambdaTaskMessage` SHALL accept `n_retries` values of `0` or greater; negative values SHALL be rejected with a `ValidationError`.

---

### Requirement 3: Retry behaviour in the Executor

**User Story:** As a developer, I want failed tasks to be automatically re-enqueued when the raised exception matches `retry_on`, so that transient errors are retried without manual intervention.

#### Acceptance Criteria

1. WHEN a task raises an exception whose type is listed in `retry_on` (or is a subclass of a listed type), THE Executor SHALL enqueue a retry via `execute_on_commit` with the same kwargs and a new `invocation_id`.
2. WHEN a retry is enqueued, THE Executor SHALL set the `TaskRecord` status to `RETRYING` and record the traceback of the triggering exception.
3. WHEN a task raises an exception that is not listed in `retry_on`, THE Executor SHALL follow the existing failure path without enqueuing a retry.
4. WHEN `retry_on` is empty, THE Executor SHALL follow the existing failure path for all exceptions.

---

### Requirement 4: Maximum retry limit

**User Story:** As a developer, I want retries to stop after a configurable maximum, so that a permanently failing task does not loop indefinitely.

#### Acceptance Criteria

1. THE `Settings` SHALL expose a `MAX_RETRIES` property that reads `LAMBDA_TASKS_MAX_RETRIES` from Django settings, defaulting to `2880` (`60 * 24 * 2`).
2. WHEN `n_retries` on the incoming message is greater than or equal to `MAX_RETRIES`, THE Executor SHALL raise `MaxRetriesExceededError` instead of enqueuing another retry.
3. THE `MaxRetriesExceededError` SHALL be a subclass of `Exception` and SHALL carry the task name and the retry count in its message.
4. WHEN `MaxRetriesExceededError` is raised, THE Executor SHALL record the `TaskRecord` as `FAILED` with the traceback, consistent with the existing failure path.

---

### Requirement 5: Retry delay

**User Story:** As a developer, I want retried tasks to be enqueued with a delay, so that transient back-pressure or rate-limit errors are not immediately hammered again.

#### Acceptance Criteria

1. WHEN a retry is enqueued and the `LambdaTaskWrapper` has a non-zero `delay` configured, THE Executor SHALL use that `delay` value as the `_delay` override for the retry.
2. WHEN a retry is enqueued and the `LambdaTaskWrapper` has a `delay` of `0`, THE Executor SHALL use `max(1, round(random.uniform(0, 5)))` as the `_delay` override.
3. THE retry delay SHALL be passed as the `_delay` override to `execute_on_commit`, consistent with the existing per-call override mechanism.
