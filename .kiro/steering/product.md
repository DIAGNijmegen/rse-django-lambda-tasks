---
inclusion: always
---

# django-lambda-tasks

A Django library for offloading tasks to AWS Lambda outside of the request-response cycle. Tasks are defined with a decorator, enqueued to SQS on transaction commit, and executed by a Lambda worker.

## Architecture

```
View → @lambda_task.execute_on_commit() → SQS → Lambda handler → SQSLambdaTaskMessage.execute_immediately() → TaskRecord
```

Key modules:
- `decorators.py` — `@lambda_task` decorator and `LambdaTaskWrapper`
- `models.py` — `TaskRecord` (Django ORM), `SQSLambdaTaskMessage` (SQS schema + execution), `SQSLambdaTask` (routing + SQS publish)
- `handler.py` — AWS Lambda entry point with partial-batch failure reporting
- `logging.py` — `task_logger` for invocation-scoped log output
- `settings.py` — lazy `LambdaTasksSettings` reading from Django settings

## Task Definition

```python
# Always kwargs-only — positional args raise TypeError at decoration time
@lambda_task(delay=0, soft_timeout=60, hard_timeout=120, queue="default",
             ignore_errors=(SomeExpectedException,),
             retry_on=(TransientError,))
def my_task(*, user_id: int, action: str) -> None:
    ...
```

- Tasks are resolved at execution time via `import_string` on the fully-qualified task name
- Task files should be named `tasks.py` within the Django app
- Direct call `my_task(user_id=1, action="x")` runs synchronously
- `my_task.execute_on_commit(user_id=1, action="x")` enqueues after transaction commit

## Enqueuing

`execute_on_commit()` accepts per-call overrides via underscore-prefixed kwargs:
- `_delay`

Resolution order for delay: call override → decorator default

## ignore_errors

Pass a tuple of exception types to `ignore_errors` on `@lambda_task`. If the task raises an instance of any of those types (or a subclass), the executor treats it as a non-fatal outcome:

- `TaskRecord.status` is set to `SUCCESS`
- The exception traceback is saved to `TaskRecord.traceback` for observability
- Task-side ORM writes inside the `transaction.atomic()` block are still rolled back
- The `TaskRecord` update itself is committed outside the atomic block

Exceptions not listed in `ignore_errors` continue to produce `FAILED` with a rollback. Omitting `ignore_errors` (or passing `()`) preserves the existing behaviour.

```python
@lambda_task(ignore_errors=(RecordNotFound,))
def sync_user(*, user_id: int) -> None:
    # RecordNotFound → SUCCESS + traceback recorded; anything else → FAILED
    ...
```

`ignore_errors` is validated at decoration time — passing a non-exception type raises `TypeError` immediately. It is stored on `LambdaTaskWrapper` and read by the executor at execution time; it is never serialised into the SQS message.

## retry_on

Pass a tuple of exception types to `retry_on` on `@lambda_task`. If the task raises an instance of any of those types (or a subclass), the executor automatically re-enqueues the task via `execute_on_commit` with the same kwargs, a fresh `invocation_id`, and an incremented `_n_retries` counter.

- `TaskRecord.status` is set to `RETRYING` and the traceback is recorded
- The retry is a new invocation — the current record is terminal at `RETRYING`
- Retries continue until `n_retries` reaches `LAMBDA_TASKS_MAX_RETRIES`, at which point `MaxRetriesExceededError` is raised and the record is saved as `FAILED`
- `ignore_errors` is checked first — a type in both `ignore_errors` and `retry_on` is treated as ignored (SUCCESS), not retried
- `retry_on` and `ignore_errors` must not overlap (exact match or subclass relationship); overlapping raises `TypeError` at decoration time

```python
@lambda_task(retry_on=(RateLimitError, ConnectionError))
def sync_data(*, record_id: int) -> None:
    # RateLimitError or ConnectionError → RETRYING + re-enqueued; anything else → FAILED
    ...
```

**Retry delay:** when a retry is enqueued, the `_delay` is set to the wrapper's configured `delay` if non-zero, otherwise `max(1, round(random.uniform(0, 5)))` seconds.

**Max retries:** controlled by `LAMBDA_TASKS_MAX_RETRIES` (default `2880`, i.e. 60 × 24 × 2). When `n_retries >= MAX_RETRIES`, `MaxRetriesExceededError` is raised instead of enqueuing another retry. This exception propagates to the Lambda handler and is reported as a `batchItemFailure`.

`retry_on` is validated at decoration time — passing a non-exception type raises `TypeError` immediately. It is stored on `LambdaTaskWrapper` and read by the executor at execution time; it is never serialised into the SQS message.

## SQS Message Schema (`SQSLambdaTaskMessage`)

```python
class SQSLambdaTaskMessage(BaseModel):
    task_name: str        # fully-qualified: "myapp.tasks.my_task"
    invocation_id: str    # UUID4, generated fresh per enqueue
    kwargs: dict
    n_retries: int        # retry counter, default 0, must be >= 0
```

## Execution

`SQSLambdaTaskMessage.execute_immediately()` in `models.py`:
1. Checks for an existing `TaskRecord` with the same `invocation_id` via `get_or_create`
2. If a record already exists (any status), logs and returns immediately — duplicate deliveries are silently skipped
3. Resolves timeouts: decorator default → settings defaults (soft=270s, hard=300s)
4. Runs task inside `transaction.atomic()` + `TimeoutContext`
5. On success: updates record to `SUCCESS` with result and `end_time`
6. On ignored exception (type matches `ignore_errors`): rolls back task-side writes, commits record as `SUCCESS` with traceback and `end_time`
7. On retryable exception (type matches `retry_on`, `n_retries < MAX_RETRIES`): rolls back task-side writes, enqueues retry via `execute_on_commit` with `n_retries + 1`, commits record as `RETRYING` with traceback and `end_time`
8. On retryable exception with `n_retries >= MAX_RETRIES`: commits record as `FAILED` with traceback, raises `MaxRetriesExceededError`
9. On any other exception: rolls back atomic block, updates record to `FAILED` with traceback

## Lambda Handler

`handler(event, context)` in `handler.py`:
- Processes each SQS record independently
- Returns `{"batchItemFailures": [...]}` for partial-batch failure reporting
- Only pre-execution failures (malformed message, import error, misconfiguration) are reported as `batchItemFailures` — task logic failures are caught and recorded as `FAILED` TaskRecords without raising
- Recommended SQS queue settings: `maxReceiveCount=1` with a DLQ configured; automatic retries are not useful since task failures are not re-driven by design
- Calls `resolve_secrets_into_env()` before `django.setup()` at cold start to populate env vars from AWS Secrets Manager

## Secret Loader

`resolve_secrets_into_env()` in `secret_loader.py` runs once at Lambda cold start, before `django.setup()`.

Any env var prefixed `AWS_SECRETS_MANAGER_` is treated as a Secrets Manager reference. The unprefixed name is the target env var.

Required format: `<arn>:<json-key>:<version-stage>:<version-id>` (10 colon-separated segments, all fields non-empty).

Behaviour:
- All references are validated before any AWS call — malformed references raise `ValueError` immediately
- Setting both `AWS_SECRETS_MANAGER_FOO` and `FOO` is a configuration error and raises `ValueError`
- Calls are batched by `(ARN, version-stage, version-id)` — one `GetSecretValue` per unique combination
- Fetched secrets are cached in-process; warm invocations pay no extra cost

## Models

`models.py` contains three classes:

- `TaskRecord` — Django ORM model; persists task execution state with statuses `RUNNING`, `SUCCESS`, `FAILED`, `RETRYING`
- `SQSLambdaTaskMessage` — Pydantic model; the SQS message schema; `execute_immediately()` runs the task inside `transaction.atomic()` + `TimeoutContext`
- `SQSLambdaTask` — Pydantic model; holds a `SQSLambdaTaskMessage` plus routing fields (`delay`, `queue`); `_execute()` publishes to SQS or executes eagerly; `execute_on_commit()` registers `_execute` with `transaction.on_commit`

## TaskRecord Model

Fields: `task_name`, `invocation_id` (unique UUID), `kwargs`, `status`, `start_time`, `end_time`, `result`, `traceback`

Statuses: `RUNNING`, `SUCCESS`, `FAILED`, `RETRYING`

- `RETRYING` — task failed with a retryable exception and a new invocation has been enqueued; this record is terminal

## Django Settings

| Setting | Default | Description |
|---|---|---|
| `LAMBDA_TASKS_QUEUES` | — | Dict of `{queue_name: sqs_url}`, must include `"default"` |
| `LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT` | `270` | Soft timeout in seconds |
| `LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT` | `300` | Hard timeout in seconds |
| `LAMBDA_TASKS_EAGER` | `False` | Run tasks synchronously in-process (no SQS) |
| `LAMBDA_TASKS_MAX_RETRIES` | `2880` | Maximum retry attempts before `MaxRetriesExceededError` is raised (60 × 24 × 2) |

`LAMBDA_TASKS_QUEUES` must be set and include a `"default"` key. `soft_timeout` must always be strictly less than `hard_timeout`.

## Eager Mode

Set `LAMBDA_TASKS_EAGER = True` to run tasks synchronously in-process (no SQS). Useful for local development and tests.

**Timeouts are not enforced in eager mode.** `TimeoutContext` is skipped entirely — `SIGALRM`-based timeouts require a Lambda worker process, not a Django dev server thread. Timeout values are still validated at decoration time.

## Logging

Import `task_logger` to emit log records that are automatically prefixed with the active `invocation_id`:

```python
from lambda_tasks.logging import task_logger
from lambda_tasks.decorators import lambda_task

@lambda_task(...)
def my_task(*, user_id: int) -> None:
    task_logger.info("processing user %s", user_id)
    # → "[abc-123] processing user 42"
```

`task_logger` is a `LoggerAdapter` wrapping the `lambda_tasks.task` logger. The executor sets the `invocation_id` before each task runs and clears it in a `finally` block. Using your own `logging.getLogger(__name__)` is fine — those records just won't carry the prefix.

## Conventions

- All task functions must use keyword-only arguments (enforced at decoration time)
- Tasks are resolved at execution time via `import_string` — task modules must be importable in the Lambda environment
- Tasks run in a Lambda environment — no Django request context available
- `LambdaTasksSettings` is instantiated fresh per use (lazy, reads live Django settings)
- boto3 exceptions propagate directly from `SQSLambdaTask._execute()` — callers should handle them
