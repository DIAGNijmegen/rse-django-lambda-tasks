# Requirements Document

## Introduction

This feature adds an async local execution mode to django-lambda-tasks using `concurrent.futures.ProcessPoolExecutor`. When enabled, tasks are submitted to a process pool and execute in the background without blocking the calling request. This is a development-only feature (like `LAMBDA_TASKS_EAGER`) that provides true parallelism for CPU-bound tasks without requiring AWS infrastructure or a separate service.

## Glossary

- **Process_Pool**: A `concurrent.futures.ProcessPoolExecutor` instance that manages a fixed number of worker processes for executing tasks in the background.
- **Worker_Process**: A child process in the Process_Pool that calls `django.setup()` once via the pool initializer and then executes submitted tasks.
- **Dispatcher**: The component within `SQSLambdaTask._execute()` that decides whether to send a task to SQS, execute eagerly, or submit to the Process_Pool.
- **Pool_Initializer**: A function passed to `ProcessPoolExecutor(initializer=...)` that runs once per Worker_Process to configure the Django environment.
- **Async_Local_Mode**: The execution mode activated by `LAMBDA_TASKS_LOCAL_WORKERS` being set to a positive integer, causing tasks to be submitted to the Process_Pool instead of SQS.

## Requirements

### Requirement 1: Pool Configuration Setting

**User Story:** As a developer, I want to configure the local process pool size via a Django setting, so that I can control resource usage during development.

#### Acceptance Criteria

1. WHEN `LAMBDA_TASKS_LOCAL_WORKERS` is set to a positive integer, THE LambdaTasksSettings SHALL expose that value as the `LOCAL_WORKERS` property.
2. WHEN `LAMBDA_TASKS_LOCAL_WORKERS` is not set, THE LambdaTasksSettings SHALL return `0` as the `LOCAL_WORKERS` property.
3. IF `LAMBDA_TASKS_LOCAL_WORKERS` is set to a value less than zero, THEN THE LambdaTasksSettings SHALL raise `ImproperlyConfigured`.
4. IF both `LAMBDA_TASKS_EAGER` and `LAMBDA_TASKS_LOCAL_WORKERS` (greater than zero) are set, THEN THE LambdaTasksSettings SHALL raise `ImproperlyConfigured` indicating the two modes are mutually exclusive.

### Requirement 2: Process Pool Lifecycle

**User Story:** As a developer, I want the process pool to be created lazily and reused across requests, so that worker startup cost is paid only once.

#### Acceptance Criteria

1. WHEN the first task is submitted in Async_Local_Mode, THE Dispatcher SHALL create a Process_Pool with `max_workers` equal to the `LOCAL_WORKERS` setting.
2. WHILE the Process_Pool has been created, THE Dispatcher SHALL reuse the same Process_Pool instance for all subsequent task submissions.
3. THE Pool_Initializer SHALL call `django.setup()` once per Worker_Process using the `DJANGO_SETTINGS_MODULE` environment variable from the parent process.
4. THE Process_Pool SHALL be stored at module level so that it persists for the lifetime of the Django server process.

### Requirement 3: Task Submission

**User Story:** As a developer, I want tasks to be submitted to the process pool after transaction commit, so that the request returns immediately and the task runs in the background.

#### Acceptance Criteria

1. WHEN `LOCAL_WORKERS` is greater than zero and `EAGER` is `False`, THE Dispatcher SHALL submit the task to the Process_Pool via `ProcessPoolExecutor.submit()` instead of sending to SQS.
2. WHEN a task is submitted to the Process_Pool, THE Dispatcher SHALL pass the serialized `SQSLambdaTaskMessage` data and a new UUID4 `message_id` to the Worker_Process.
3. THE Worker_Process SHALL call `SQSLambdaTaskMessage.execute_immediately()` with the provided `message_id` to execute the task and write the TaskRecord.
4. WHEN a task is submitted to the Process_Pool, THE Dispatcher SHALL return immediately without waiting for the task to complete.

### Requirement 4: Transaction Commit Integration

**User Story:** As a developer, I want async local tasks to respect `transaction.on_commit` just like SQS tasks, so that tasks only run after the triggering transaction succeeds.

#### Acceptance Criteria

1. WHEN `execute_on_commit()` is called in Async_Local_Mode, THE Dispatcher SHALL register the pool submission with `transaction.on_commit` so the task is only submitted after the current transaction commits.
2. IF the transaction is rolled back, THEN THE Dispatcher SHALL not submit the task to the Process_Pool.

### Requirement 5: Worker Process Error Isolation

**User Story:** As a developer, I want worker process failures to be isolated from the Django server process, so that a crashing task does not bring down the dev server.

#### Acceptance Criteria

1. IF a task raises an unhandled exception in the Worker_Process, THEN THE Process_Pool SHALL continue operating and accept new task submissions.
2. IF a Worker_Process crashes, THEN THE Process_Pool SHALL replace the crashed Worker_Process with a new one that runs the Pool_Initializer.
3. WHEN a task is submitted to the Process_Pool, THE Dispatcher SHALL not attach any callbacks or wait on the returned Future object.

### Requirement 6: Picklability of Task Arguments

**User Story:** As a developer, I want clear feedback when task arguments cannot be sent to the worker process, so that I can fix serialization issues quickly.

#### Acceptance Criteria

1. THE Dispatcher SHALL submit the task message as a JSON string (the output of `model_dump_json()`) to the Worker_Process, ensuring picklability via standard string serialization.
2. WHEN the Worker_Process receives the JSON string, THE Worker_Process SHALL reconstruct the `SQSLambdaTaskMessage` via `model_validate_json()` before calling `execute_immediately()`.

### Requirement 7: Mode Precedence

**User Story:** As a developer, I want a clear precedence between execution modes, so that configuration is predictable.

#### Acceptance Criteria

1. WHEN `LAMBDA_TASKS_EAGER` is `True`, THE Dispatcher SHALL execute tasks synchronously regardless of the `LOCAL_WORKERS` setting (enforced by the mutual exclusion constraint in Requirement 1).
2. WHEN `LAMBDA_TASKS_EAGER` is `False` and `LOCAL_WORKERS` is greater than zero, THE Dispatcher SHALL submit tasks to the Process_Pool.
3. WHEN `LAMBDA_TASKS_EAGER` is `False` and `LOCAL_WORKERS` is zero, THE Dispatcher SHALL send tasks to SQS.

### Requirement 8: Timeout Behaviour in Async Local Mode

**User Story:** As a developer, I want timeouts to be enforced in worker processes, so that runaway tasks are terminated just like they would be in Lambda.

#### Acceptance Criteria

1. WHILE a task executes in a Worker_Process in Async_Local_Mode, THE TimeoutContext SHALL set up `SIGALRM`-based timeouts (same behaviour as Lambda execution).
2. IF a soft timeout fires in a Worker_Process, THEN THE TimeoutContext SHALL raise `SoftTimeoutError` in that Worker_Process without affecting the Django server process.
3. IF a hard timeout fires in a Worker_Process, THEN THE TimeoutContext SHALL raise `HardTimeoutError` in that Worker_Process without affecting the Django server process.
4. THE TimeoutContext SHALL NOT treat Async_Local_Mode the same as eager mode — timeouts are fully enforced because each Worker_Process is isolated from the dev server.
