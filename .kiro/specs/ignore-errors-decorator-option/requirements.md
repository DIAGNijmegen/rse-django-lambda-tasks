# Requirements Document

## Introduction

This feature adds an `ignore_errors` parameter to the `@lambda_task` decorator. When specified, it accepts a tuple of exception types. If the task body raises one of those exceptions during execution, the task is still marked as `SUCCESS` and the database transaction is committed rather than rolled back. The ignored exception's traceback is still recorded on the `TaskRecord` for observability. Exceptions not listed in `ignore_errors` continue to cause a rollback and `FAILED` status. The same behaviour applies in eager mode.

This is useful for tasks where certain expected, non-fatal exceptions (e.g. a record-not-found condition that has already been handled elsewhere) should not pollute the failure dashboard or trigger alerting.

## Glossary

- **LambdaTaskWrapper**: The wrapper object produced by `@lambda_task`; holds decorator-level configuration including `ignore_errors`.
- **SQSLambdaTaskMessage**: The Pydantic model representing the SQS message schema; its `execute()` method runs the task and persists the outcome.
- **TaskRecord**: The Django ORM model that persists task execution state with statuses `RUNNING`, `SUCCESS`, and `FAILED`.
- **ignore_errors**: A tuple of exception types passed to `@lambda_task`; exceptions matching any type in the tuple are treated as non-fatal during task execution.
- **Ignored Exception**: An exception whose type is a subclass of at least one type in `ignore_errors`; causes `SUCCESS` outcome rather than `FAILED`.
- **Executor**: The `SQSLambdaTaskMessage.execute_immediately()` method in `models.py` that runs the task and updates the `TaskRecord`.

## Requirements

### Requirement 1: Decorator Accepts `ignore_errors` Parameter

**User Story:** As a developer, I want to declare which exception types are non-fatal on my task decorator, so that expected exceptions do not mark the task as failed.

#### Acceptance Criteria

1. THE `LambdaTaskWrapper` SHALL accept an `ignore_errors` keyword argument that is a tuple of exception types.
2. WHEN `ignore_errors` is not provided, THE `LambdaTaskWrapper` SHALL default `ignore_errors` to an empty tuple, preserving existing behaviour.
3. WHEN `ignore_errors` is provided, THE `LambdaTaskWrapper` SHALL store the tuple and make it accessible for use during task execution.
4. WHEN `ignore_errors` contains a value that is not an exception type (i.e. not a subclass of `BaseException`), THE `LambdaTaskWrapper` SHALL raise a `TypeError` at decoration time.
5. THE `lambda_task` decorator factory SHALL accept and forward the `ignore_errors` parameter to `LambdaTaskWrapper`.

---

### Requirement 2: Ignored Exceptions Produce SUCCESS Outcome

**User Story:** As a developer, I want tasks that raise a listed exception to be recorded as succeeded, so that non-fatal conditions do not appear as failures.

#### Acceptance Criteria

1. WHEN a task raises an exception whose type is a subclass of any type in `ignore_errors`, THE `Executor` SHALL mark the `TaskRecord` status as `SUCCESS`.
2. WHEN a task raises an ignored exception, THE `Executor` SHALL commit the database transaction rather than rolling it back.
3. WHEN a task raises an ignored exception, THE `Executor` SHALL record the exception traceback in the `TaskRecord.traceback` field.
4. WHEN a task raises an ignored exception, THE `Executor` SHALL set `TaskRecord.end_time` to the current time.
5. FOR ALL exception types `E` in `ignore_errors`, WHEN a task raises an instance of `E` or any subclass of `E`, THE `Executor` SHALL treat it as an ignored exception.

---

### Requirement 3: Non-Ignored Exceptions Preserve Existing FAILED Behaviour

**User Story:** As a developer, I want exceptions not listed in `ignore_errors` to continue causing task failure, so that real errors are still surfaced.

#### Acceptance Criteria

1. WHEN a task raises an exception whose type is not a subclass of any type in `ignore_errors`, THE `Executor` SHALL mark the `TaskRecord` status as `FAILED`.
2. WHEN a task raises a non-ignored exception, THE `Executor` SHALL roll back the database transaction.
3. WHEN `ignore_errors` is an empty tuple, THE `Executor` SHALL treat all exceptions as non-ignored, preserving the existing behaviour.
4. FOR ALL exception types `E` not in `ignore_errors`, WHEN a task raises an instance of `E`, THE `Executor` SHALL produce a `FAILED` `TaskRecord`.

---

### Requirement 4: `ignore_errors` Flows from Decorator to Executor

**User Story:** As a developer, I want the `ignore_errors` configuration to be automatically applied at execution time without any extra wiring, so that the decorator is the single place to configure this behaviour.

#### Acceptance Criteria

1. WHEN `SQSLambdaTaskMessage.execute_immediately()` resolves the `LambdaTaskWrapper` via `import_string`, THE `Executor` SHALL read `ignore_errors` from the resolved wrapper.
2. THE `SQSLambdaTaskMessage` model SHALL NOT carry `ignore_errors` as a field — it is resolved at execution time from the wrapper, not from the SQS message.
3. WHEN the same task is executed in eager mode (`LAMBDA_TASKS_EAGER = True`), THE `Executor` SHALL apply the same `ignore_errors` logic as in Lambda execution mode.

---

### Requirement 5: Observability of Ignored Exceptions

**User Story:** As a developer, I want to be able to inspect the traceback of an ignored exception after the task succeeds, so that I can diagnose unexpected patterns in non-fatal errors.

#### Acceptance Criteria

1. WHEN a task raises an ignored exception, THE `Executor` SHALL save the formatted traceback string to `TaskRecord.traceback`.
2. WHEN a task completes without raising any exception, THE `Executor` SHALL leave `TaskRecord.traceback` as `None`.
3. WHEN a task raises a non-ignored exception, THE `Executor` SHALL save the formatted traceback string to `TaskRecord.traceback` (existing behaviour, unchanged).
