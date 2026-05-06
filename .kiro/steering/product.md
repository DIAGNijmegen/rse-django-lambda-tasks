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
- `models.py` — `TaskRecord` (Django ORM), `SQSLambdaTaskMessage` (SQS schema + execution), `SQSLambdaTask` (routing + SQS publish or local pool submit)
- `local_executor.py` — `ProcessPoolExecutor`-based async local execution for development
- `handler.py` — AWS Lambda entry point; cold-start init runs on first invocation (not at import time) with partial-batch failure reporting
- `logging.py` — `task_logger` for invocation-scoped log output
- `settings.py` — lazy `LambdaTasksSettings` reading from Django settings

## Task Definition

```python
# Always kwargs-only — positional args raise TypeError at decoration time
@lambda_task(delay=0, retry_delay=30, soft_timeout=60, hard_timeout=120, queue="default",
             ignore_errors=(SomeExpectedException,),
             retry_on=(TransientError,),
             singleton=True)
def my_task(*, user_id: int, action: str) -> None:
    ...
```

- Tasks are resolved at execution time via `import_string` on the fully-qualified task name
- Task files should be named `tasks.py` within the Django app
- Direct call `my_task(user_id=1, action="x")` runs synchronously
- `my_task.execute_on_commit(user_id=1, action="x")` enqueues after transaction commit

## Enqueuing

`execute_on_commit()` uses the decorator `delay` value. There are no per-call overrides.

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

Pass a tuple of exception types to `retry_on` on `@lambda_task`. If the task raises an instance of any of those types (or a subclass), the executor automatically re-enqueues the task with the same kwargs and an incremented `n_retries` counter on the `SQSLambdaTaskMessage`.

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

**Retry delay:** when a retry is enqueued, the delay is set to `min(retry_delay + round(random.uniform(1, 5)), 900)` seconds. The jitter is always added to spread out competing retries. The result is capped at 900 (the SQS `DelaySeconds` maximum).

**Max retries:** controlled by `LAMBDA_TASKS_MAX_RETRIES` (default `2880`, i.e. 60 × 24 × 2). When `n_retries >= MAX_RETRIES`, `MaxRetriesExceededError` is raised instead of enqueuing another retry. This exception propagates to the Lambda handler and is reported as a `batchItemFailure`.

`retry_on` is validated at decoration time — passing a non-exception type raises `TypeError` immediately. It is stored on `LambdaTaskWrapper` and read by the executor at execution time; it is never serialised into the SQS message.

## singleton

Pass `singleton=True` on `@lambda_task` to prevent concurrent execution of the same task. When enabled, the executor acquires a Redis lock via Django's cache framework before running the task function. The lock wraps the entire `transaction.atomic()` block — acquired before, released after.

- Lock key format: `lambda_tasks.singleton_lock.{task_name}`
- The lock is acquired with `blocking_timeout=0` (fail immediately if held) and `timeout=hard_timeout` (auto-expire if the worker crashes)
- If the lock cannot be acquired (`LockError`), the executor treats it as a retryable exception — same code path as `retry_on`. The `TaskRecord` is set to `RETRYING`, the traceback is recorded, and the task is re-enqueued with `n_retries + 1`
- If `n_retries` has reached `LAMBDA_TASKS_MAX_RETRIES`, `MaxRetriesExceededError` is raised and the record is saved as `FAILED`
- The cache backend used for locks is controlled by `LAMBDA_TASKS_SINGLETON_CACHE` (default `"default"`)

```python
@lambda_task(singleton=True)
def sync_inventory(*, warehouse_id: int) -> None:
    # Only one instance runs at a time; LockError → RETRYING + re-enqueued
    ...
```

`singleton` is stored on `LambdaTaskWrapper` and read by the executor at execution time; it is never serialised into the SQS message.

## SQS Message Schema (`SQSLambdaTaskMessage`)

```python
class SQSLambdaTaskMessage(BaseModel):
    task_name: str   # fully-qualified: "myapp.tasks.my_task"
    kwargs: dict
    n_retries: int   # retry counter, default 0, must be >= 0
```

`invocation_id` was removed — deduplication now uses the SQS `messageId` as the `TaskRecord` primary key. kwargs are serialized to JSON via `model_dump(mode="json")` before being stored on `TaskRecord`.

## Execution

`SQSLambdaTaskMessage.execute_immediately(*, message_id: str)` in `models.py`:
1. Checks for an existing `TaskRecord` with the same `pk` (`message_id`) via `get_or_create`
2. If a record already exists (any status), logs and returns immediately — duplicate deliveries are silently skipped
3. Resolves timeouts: decorator default → settings defaults (soft=270s, hard=300s)
4. If `wrapper.singleton` is `True`, acquires a Redis lock via `caches[SINGLETON_CACHE].lock(lock_key)` wrapping the atomic block; if `False`, no lock is acquired
5. Runs task inside `transaction.atomic()` + `TimeoutContext`
6. On success: updates record to `SUCCESS` with result and `end_time`
7. On ignored exception (type matches `ignore_errors`): rolls back task-side writes, commits record as `SUCCESS` with traceback and `end_time`
8. On retryable exception (type matches `retry_on` or `LockError` for singleton tasks, `n_retries < MAX_RETRIES`): rolls back task-side writes, enqueues retry via `execute_on_commit` with `n_retries + 1`, commits record as `RETRYING` with traceback and `end_time`
9. On retryable exception with `n_retries >= MAX_RETRIES`: commits record as `FAILED` with traceback, raises `MaxRetriesExceededError`
10. On any other exception: rolls back atomic block, updates record to `FAILED` with traceback and `end_time`

## Lambda Handler

`handler(event, context)` in `handler.py`:
- Processes each SQS record independently
- Passes `record["messageId"]` as `message_id` to `execute_immediately()` — this becomes the `TaskRecord` primary key, enabling deduplication on redelivery
- Returns `{"batchItemFailures": [...]}` for partial-batch failure reporting
- Only pre-execution failures (malformed message, import error, misconfiguration) are reported as `batchItemFailures` — task logic failures are caught and recorded as `FAILED` TaskRecords without raising
- Recommended SQS queue settings: `maxReceiveCount=1` with a DLQ configured; automatic retries are not useful since task failures are not re-driven by design
- Cold-start sequence runs inside the handler on the first invocation (not at module import time) to avoid Lambda init-duration timeouts: a temporary `StreamHandler` is attached to the `lambda_tasks` logger, then `resolve_environment()` → `resolve_secrets_into_env()` (handler removed) → conditional `django.setup()`
- A module-level `_cold_start_done` sentinel ensures the sequence runs only once; subsequent warm invocations skip it
- Both loaders run unconditionally (outside the `DJANGO_SETTINGS_MODULE` check) — the environment secret may provide that var, and individual secrets may depend on environment-loaded vars
- A temporary `StreamHandler` is attached to the `lambda_tasks` logger for the duration of the loaders so their log output is visible before Django's `LOGGING` dictConfig has run; it is removed immediately after so that Django's configuration is the sole authority on logging from that point on

## Environment Loader

`resolve_environment()` in `environment_loader.py` runs once at Lambda cold start, before `resolve_secrets_into_env()` and `django.setup()`.

When the environment variable `LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN` is set, the loader parses the reference, fetches the named Secrets Manager secret, parses its JSON content as a flat key-value mapping, and sets the resulting pairs as environment variables.

Required format: `<arn>:<version-stage>:<version-id>` (9 colon-separated segments — the ARN is 7 segments, plus version-stage and version-id). Both suffix fields must be non-empty.

Behaviour:
- If `LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN` is not set, does nothing (no AWS API calls)
- Validates the reference format before any AWS call — malformed references raise `ValueError` immediately
- Fetches the secret via `secretsmanager.get_secret_value(SecretId=..., VersionStage=..., VersionId=...)`
- Validates the secret value is a flat JSON object (all values must be strings, no empty keys)
- Sets each key-value pair in `os.environ` — existing env vars are overridden (no conflict detection)
- Idempotent via a module-level `_loaded` sentinel — subsequent calls are free no-ops
- Invalid reference format, invalid JSON, non-flat objects, or empty keys raise `ValueError` at cold start

## Secret Loader

`resolve_secrets_into_env()` in `secret_loader.py` runs once at Lambda cold start, after `resolve_environment()` and before `django.setup()`.

Any env var prefixed `LAMBDA_TASKS_SECRET_` is treated as a Secrets Manager reference. The unprefixed name is the target env var.

Required format: `<arn>:<json-key>:<version-stage>:<version-id>` (10 colon-separated segments, all fields non-empty).

Behaviour:
- All references are validated before any AWS call — malformed references raise `ValueError` immediately
- Setting both `LAMBDA_TASKS_SECRET_FOO` and `FOO` is a configuration error and raises `ValueError`
- Calls are batched by `(ARN, version-stage, version-id)` — one `GetSecretValue` per unique combination
- Fetched secrets are cached in-process; warm invocations pay no extra cost

## Models

`models.py` contains three classes:

- `TaskRecord` — Django ORM model; persists task execution state with statuses `RUNNING`, `SUCCESS`, `FAILED`, `RETRYING`
- `SQSLambdaTaskMessage` — Pydantic model; the SQS message schema; `execute_immediately()` runs the task inside `transaction.atomic()` + `TimeoutContext`
- `SQSLambdaTask` — Pydantic model; holds a `SQSLambdaTaskMessage` plus routing fields (`delay`, `queue`); `_execute()` publishes to SQS or executes eagerly; `execute_on_commit()` registers `_execute` with `transaction.on_commit`

## TaskRecord Model

Fields: `id` (UUID primary key — set to the SQS `messageId`), `task_name`, `kwargs`, `n_retries`, `status`, `start_time`, `end_time`, `result`, `traceback`

Statuses: `RUNNING`, `SUCCESS`, `FAILED`, `RETRYING`

- `RETRYING` — task failed with a retryable exception and a new invocation has been enqueued; this record is terminal

## Django Settings

| Setting | Default | Description |
|---|---|---|
| `LAMBDA_TASKS_QUEUES` | — | Dict of `{queue_name: sqs_url}`, must include `"default"` |
| `LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT` | `270` | Soft timeout in seconds |
| `LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT` | `300` | Hard timeout in seconds |
| `LAMBDA_TASKS_EAGER` | `False` | Run tasks synchronously in-process (no SQS) |
| `LAMBDA_TASKS_LOCAL_WORKERS` | `0` | Number of worker processes for async local execution (development only; mutually exclusive with `EAGER`) |
| `LAMBDA_TASKS_MAX_RETRIES` | `2880` | Maximum retry attempts before `MaxRetriesExceededError` is raised (60 × 24 × 2) |
| `LAMBDA_TASKS_SINGLETON_CACHE` | `"default"` | Django cache backend used for singleton task locks |

`LAMBDA_TASKS_QUEUES` must be set and include a `"default"` key. Both timeout values must be greater than zero and at most `900` seconds. `soft_timeout` must always be strictly less than `hard_timeout`.

## Eager Mode

Set `LAMBDA_TASKS_EAGER = True` to run tasks synchronously in-process (no SQS). Useful for local development and tests.

In eager mode a random UUID4 is generated as the `message_id` passed to `execute_immediately()`.

**Timeouts are not enforced in eager mode.** `TimeoutContext` is still entered but becomes a no-op — it checks `LAMBDA_TASKS_EAGER` internally and skips `SIGALRM` setup. `SIGALRM`-based timeouts require a Lambda worker process, not a Django dev server thread. Timeout values are still validated at decoration time.

## Async Local Mode

Set `LAMBDA_TASKS_LOCAL_WORKERS` to a positive integer to run tasks in a background `ProcessPoolExecutor`. Tasks are submitted after transaction commit (same as SQS mode) but execute in local worker processes instead of Lambda. This provides true parallelism with timeout enforcement for local development.

```python
# settings/local.py
LAMBDA_TASKS_LOCAL_WORKERS = 4
```

The execution mode hierarchy is:
1. **Eager mode** (`LAMBDA_TASKS_EAGER=True`) — synchronous, in-process, no timeouts
2. **Async local mode** (`LOCAL_WORKERS > 0`) — async, separate processes, timeouts enforced
3. **SQS mode** (default) — async, Lambda workers, timeouts enforced

`LAMBDA_TASKS_LOCAL_WORKERS` and `LAMBDA_TASKS_EAGER` are mutually exclusive — setting both raises `ImproperlyConfigured`. A negative value also raises `ImproperlyConfigured`.

Key behaviours:
- The process pool is created lazily on first task submission and reused for the server lifetime
- Each worker process calls `django.setup()` once via the pool initializer
- Tasks are serialized as JSON strings (via `model_dump_json()`) for IPC — same path as SQS
- The dispatcher discards the `Future` (fire-and-forget) — worker failures are isolated
- `SIGALRM`-based timeouts work in worker processes because they are separate OS processes
- `transaction.on_commit` is respected — tasks only submit after the transaction commits

Implementation lives in `lambda_tasks/local_executor.py`:
- `get_pool()` — lazily creates and returns the shared `ProcessPoolExecutor`
- `submit_task(*, message_json: str)` — generates a UUID4 message_id and submits to the pool
- `_execute_in_worker(*, message_json: str, message_id: str)` — worker entry point; deserializes and calls `execute_immediately()`
- `_pool_initializer()` — calls `django.setup()` once per worker

## Logging

Import `task_logger` to emit log records that are automatically prefixed with the active `message_id`:

```python
from lambda_tasks.logging import task_logger
from lambda_tasks.decorators import lambda_task

@lambda_task(...)
def my_task(*, user_id: int) -> None:
    task_logger.info("processing user %s", user_id)
    # → "[abc-123] processing user 42"
```

`task_logger` is a `LoggerAdapter` wrapping the `lambda_tasks.task` logger. The executor sets the `message_id` before each task runs and clears it in a `finally` block. Using your own `logging.getLogger(__name__)` is fine — those records just won't carry the prefix.

## Built-in Tasks

`lambda_tasks.tasks` provides maintenance tasks that ship with the library:

- `cleanup_task_records(*, retention_days: int = 7) -> int` — deletes `TaskRecord` rows whose `start_time` is strictly older than `retention_days`. Returns the number of deleted rows. Decorated with `@lambda_task` so it can be enqueued via `cleanup_task_records.execute_on_commit()` or called directly. Users are responsible for scheduling (e.g. EventBridge rule, cron, management command).

## Conventions

- All task functions must use keyword-only arguments (enforced at decoration time)
- Tasks are resolved at execution time via `import_string` — task modules must be importable in the Lambda environment
- Tasks run in a Lambda environment — no Django request context available
- `LambdaTasksSettings` is instantiated fresh per use (lazy, reads live Django settings)
- boto3 exceptions propagate directly from `SQSLambdaTask._execute()` — callers should handle them
