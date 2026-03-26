# Requirements Document

## Introduction

`django-lambda-tasks` is a Django library that allows developers to offload work to AWS Lambda outside of the request-response cycle. Tasks are defined with a decorator, enqueued to SQS on transaction commit, and executed by a Worker that runs as an AWS Lambda handler function — AWS handles SQS polling and delivers message batches directly to the handler. Task results, status, and metadata are persisted in the Django database.

This library targets Unix-based systems (Linux and macOS) only. Windows and other non-Unix platforms are not supported.

## Glossary

- **Background_Task**: A Python function decorated with `@lambda_task` that is eligible for asynchronous execution via the worker.
- **Decorator**: The `@lambda_task` Python decorator used to register a function as a Background_Task.
- **Enqueuer**: The component responsible for serializing task invocations and sending them to SQS.
- **SQS_Message**: An AWS SQS message containing the serialized task name, kwargs, and invocation ID.
- **Worker**: The AWS Lambda handler function that receives SQS messages as events, deserializes them, and executes Background_Tasks.
- **Invocation_ID**: A UUID generated at enqueue time that uniquely identifies a single task invocation, included in the SQS_Message.
- **Task_Record**: The Django ORM model instance that stores task execution metadata (status, kwargs, start time, end time, result).
- **Serializer**: The Pydantic-based component responsible for converting task kwargs to and from JSON for SQS transport.
- **on_commit**: The method on a decorated task used to enqueue it after the current database transaction commits.
- **delay**: The SQS message visibility delay in seconds before the Worker can pick up the message.
- **soft_timeout**: The duration in seconds after which the Worker sends a `SoftTimeLimitExceeded` exception to the running Background_Task, giving it an opportunity to clean up gracefully before forced termination.
- **hard_timeout**: The duration in seconds after which the Worker forcibly terminates the Background_Task if it is still running. Must always be strictly greater than `soft_timeout`.
- **SoftTimeLimitExceeded**: The exception raised in the Background_Task when the soft timeout is exceeded, allowing the task to catch it and perform cleanup before the hard timeout kills it.
- **Queue**: A named SQS queue registered in settings, identified by a string name (e.g. `"default"`, `"high_memory"`). Each queue maps to an SQS queue URL.
- **Queue_Name**: The string identifier used to reference a Queue in task decoration and invocation.

## Requirements

### Requirement 1: Task Registration via Decorator

**User Story:** As a Django developer, I want to register a function as a background task using a decorator, so that I can clearly mark which functions run asynchronously.

#### Acceptance Criteria

1. THE Decorator SHALL accept a plain function with zero or more keyword-only arguments (including functions with no arguments at all) and return a wrapped callable that exposes an `on_commit` method.
2. THE Decorator SHALL accept optional `delay`, `soft_timeout`, `hard_timeout`, and `queue` parameters to set per-task defaults.
3. WHEN the Decorator is applied to a function that has positional arguments, THE Decorator SHALL raise a `TypeError` at decoration time.
4. THE Decorator SHALL preserve the original function's name and docstring on the wrapped callable.
5. WHEN the Decorator is applied with a `soft_timeout` that is greater than or equal to `hard_timeout`, THE Decorator SHALL raise a `ConfigurationError` at decoration time.

---

### Requirement 2: Task Enqueueing via on_commit

**User Story:** As a Django developer, I want to enqueue a background task after the current database transaction commits, so that tasks are only dispatched when the triggering data is safely persisted.

#### Acceptance Criteria

1. WHEN `on_commit` is called, THE Enqueuer SHALL register the task dispatch to occur after the current Django database transaction commits.
2. WHEN the transaction commits, THE Enqueuer SHALL serialize the task name and kwargs into a SQS_Message using the Serializer and send it to the configured SQS queue.
3. WHEN `on_commit` is called with a `_delay` integer argument, THE Enqueuer SHALL use that value as the SQS message delay, overriding the per-task default.
4. WHEN `on_commit` is called with `_soft_timeout` and/or `_hard_timeout` integer arguments, THE Enqueuer SHALL embed those values in the SQS_Message, overriding the per-task defaults.
5. WHEN `on_commit` is called with a `_soft_timeout` that is greater than or equal to `_hard_timeout`, THE Enqueuer SHALL raise a `ConfigurationError` and SHALL NOT enqueue the SQS_Message.
6. WHEN `on_commit` is called with a `_queue` string argument, THE Enqueuer SHALL route the SQS_Message to the queue URL corresponding to that Queue_Name, overriding the per-task default queue.
7. WHEN `on_commit` is called outside of an active database transaction, THE Enqueuer SHALL dispatch the SQS_Message immediately without waiting for a commit.
8. IF the SQS send operation fails, THEN THE Enqueuer SHALL raise an exception and SHALL NOT silently discard the task.

---

### Requirement 3: Task Argument Serialization

**User Story:** As a library maintainer, I want task kwargs to be serialized and deserialized reliably using Pydantic, so that type safety is preserved across the SQS transport boundary.

#### Acceptance Criteria

1. THE Serializer SHALL serialize task kwargs to a JSON-compatible dictionary using Pydantic model validation.
2. THE Serializer SHALL deserialize a JSON dictionary back into validated Python kwargs before task execution.
3. WHEN a kwarg value does not conform to the annotated type, THE Serializer SHALL raise a `ValidationError` and SHALL NOT enqueue the SQS_Message.
4. FOR ALL valid sets of task kwargs, serializing then deserializing SHALL produce an equivalent set of kwargs (round-trip property).
5. THE Serializer SHALL include the fully-qualified task function name in the SQS_Message to allow the Worker to locate the correct Background_Task.
6. THE Serializer SHALL generate a unique Invocation_ID (UUID) at enqueue time and include it in the SQS_Message.

---

### Requirement 4: Worker — SQS Message Processing

**User Story:** As a platform engineer, I want a Lambda handler function that processes SQS messages and executes background tasks, so that tasks run reliably outside of the web request cycle.

#### Acceptance Criteria

1. THE Worker SHALL be implemented as an AWS Lambda handler function that accepts an SQS event payload (a batch of SQS records) and a Lambda context object.
2. WHEN the Worker receives a SQS_Message, THE Worker SHALL deserialize the message using the Serializer and invoke the corresponding Background_Task.
3. WHEN the Worker cannot find a registered Background_Task matching the name in the SQS_Message, THE Worker SHALL log an error and SHALL raise an exception so that AWS does not delete the message from the queue.
4. WHEN a SQS_Message has been successfully processed, THE Worker SHALL return without raising an exception, allowing AWS to delete the message from the SQS queue.
5. WHEN the Worker receives a batch of SQS records, THE Worker SHALL process each record independently so that a failure in one record does not prevent processing of other records in the batch.

---

### Requirement 5: Task Atomicity

**User Story:** As a Django developer, I want each background task to run inside a database transaction, so that partial task failures do not leave the database in an inconsistent state.

#### Acceptance Criteria

1. WHEN the Worker executes a Background_Task, THE Worker SHALL wrap the execution in a Django atomic transaction.
2. IF the Background_Task raises an unhandled exception, THEN THE Worker SHALL roll back the transaction and SHALL NOT commit any database changes made during that execution.
3. IF the Background_Task raises an unhandled exception and the transaction is rolled back, THEN THE Worker SHALL update the Task_Record with status `FAILED` in a separate database operation outside the rolled-back atomic block, so that the failure is persisted regardless of the rollback.

---

### Requirement 6: Task Result Persistence

**User Story:** As a Django developer, I want task execution results and metadata stored in the database, so that I can inspect task history, debug failures, and monitor throughput.

#### Acceptance Criteria

1. WHEN the Worker begins executing a Background_Task, THE Worker SHALL create a Task_Record with status `RUNNING` and the recorded start time.
2. WHEN a Background_Task completes successfully, THE Worker SHALL update the Task_Record with status `SUCCESS`, the return value, and the end time.
3. WHEN a Background_Task raises an unhandled exception, THE Worker SHALL update the Task_Record with status `FAILED`, the full traceback string of the exception, and the end time.
4. THE Task_Record SHALL store the task name, serialized kwargs, status, start time, end time, and result.
5. THE Task_Record SHALL be queryable via the standard Django ORM.

---

### Requirement 7: Task Timeout Enforcement

**User Story:** As a platform engineer, I want tasks to be terminated if they exceed their configured timeout, so that a hung task does not block the worker indefinitely.

#### Acceptance Criteria

1. WHEN the Worker begins executing a Background_Task, THE Worker SHALL enforce the `soft_timeout` and `hard_timeout` values embedded in the SQS_Message, falling back to per-task defaults, then global defaults.
2. WHEN a Background_Task exceeds its `soft_timeout` duration, THE Worker SHALL raise a `SoftTimeLimitExceeded` exception inside the running task, allowing the task to catch it and perform cleanup.
3. WHEN a Background_Task is still running after its `hard_timeout` duration, THE Worker SHALL forcibly terminate the task and update the Task_Record with status `FAILED`, the full traceback string indicating hard timeout, and the end time.
4. WHEN no soft or hard timeout is configured at the message level or task level, THE Worker SHALL apply configurable global default values for `soft_timeout` and `hard_timeout`.
5. THE Worker SHALL enforce that the resolved `soft_timeout` is strictly less than the resolved `hard_timeout`; IF this invariant is violated, THEN THE Worker SHALL raise a `ConfigurationError` and SHALL NOT execute the Background_Task.

---

### Requirement 8: Library Configuration

**User Story:** As a Django developer, I want to configure the library via Django settings, so that I can control SQS queue URLs, AWS credentials, and global defaults without modifying task code.

#### Acceptance Criteria

1. THE Library SHALL read its configuration from individual settings in the Django settings module, each prefixed with `LAMBDA_TASKS_`.
2. THE Library SHALL support a `LAMBDA_TASKS_QUEUES` setting — a dictionary mapping Queue_Name strings to SQS queue URLs — as the primary way to configure one or more queues. `LAMBDA_TASKS_SQS_QUEUE_URL` is superseded by `LAMBDA_TASKS_QUEUES` for multi-queue setups.
3. WHERE `LAMBDA_TASKS_QUEUES` is not defined, THE Library SHALL fall back to `LAMBDA_TASKS_SQS_QUEUE_URL` as the sole default queue.
4. IF neither `LAMBDA_TASKS_QUEUES` nor `LAMBDA_TASKS_SQS_QUEUE_URL` is present in settings, THEN THE Library SHALL raise an `ImproperlyConfigured` exception on first use.
5. THE Library SHALL support an optional `LAMBDA_TASKS_DEFAULT_DELAY` integer setting (default: `0`).
6. THE Library SHALL support an optional `LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT` integer setting (default: `270`).
7. THE Library SHALL support an optional `LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT` integer setting (default: `300`).
8. IF `LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT` is greater than or equal to `LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT`, THEN THE Library SHALL raise a `ConfigurationError` on first use.

---

### Requirement 9: Named Queue Routing

**User Story:** As a platform engineer, I want to route tasks to specific named queues backed by different Lambda workers, so that I can match tasks to workers with appropriate hardware characteristics (e.g. memory, CPU).

#### Acceptance Criteria

1. THE Library SHALL support a `LAMBDA_TASKS_QUEUES` setting — a dictionary mapping Queue_Name strings to SQS queue URLs (e.g. `{"default": "https://sqs.../default", "high_memory": "https://sqs.../high-memory"}`).
2. THE Library SHALL designate one queue as the default queue, identified by the key `"default"` in `LAMBDA_TASKS_QUEUES`.
3. IF `LAMBDA_TASKS_QUEUES` is defined but does not contain a `"default"` key, THEN THE Library SHALL raise an `ImproperlyConfigured` exception on first use.
4. THE Decorator SHALL accept an optional `queue` string parameter to set the default queue for that task.
5. WHEN `on_commit` is called with a `_queue` string argument, THE Enqueuer SHALL route the SQS_Message to the queue URL corresponding to that Queue_Name, overriding the per-task default.
6. WHEN no `queue` is specified at the task or invocation level, THE Enqueuer SHALL route the SQS_Message to the `"default"` queue.
7. WHEN a `queue` name is specified that does not exist in `LAMBDA_TASKS_QUEUES`, THE Enqueuer SHALL raise an `ImproperlyConfigured` exception and SHALL NOT enqueue the SQS_Message.
