---
inclusion: always
---

# django-lambda-tasks

A Django library for offloading tasks to AWS Lambda or AWS Batch outside of the request-response cycle. Tasks are defined with a decorator, enqueued to SQS on transaction commit, and executed by a Lambda worker or Batch container.

## Architecture

### Lambda tasks

```
View → @lambda_task.execute_on_commit() → SQS → Lambda handler → SQSLambdaTaskMessage.execute_immediately() → TaskRecord
```

### Batch tasks (via queue config)

```
View → @lambda_task(queue="heavy").execute_on_commit() → SQS → Lambda handler → submit_batch_job → batch.submit_job()
     → Container → python -m lambda_tasks.handler → handler() → execute_immediately() → TaskRecord
```

Retry path (Batch):
```
Container (retry_on exception) → execute_on_commit() → SQS → Lambda → submit_batch_job → new Batch job
```

Key modules:
- `decorators.py` — `LambdaTaskWrapper` and `@lambda_task` decorator factory
- `models.py` — `TaskRecord` (Django ORM), `SQSLambdaTaskMessage` (SQS schema + execution), `SQSLambdaTask` (routing + SQS publish, Batch submit, or local pool submit)
- `local_executor.py` — `ProcessPoolExecutor`-based async local execution for development
- `handler.py` — AWS Lambda entry point + AWS Batch container entry point (`main()`)
- `logging.py` — `task_logger` for invocation-scoped log output
- `settings.py` — lazy `LambdaTasksSettings` reading from Django settings, queue type helpers

## Task Definition

```python
# Lambda task — max timeout determined by queue type (900s for SQS, 3600s for Batch)
@lambda_task(retry_delay=30, soft_timeout=60, hard_timeout=120, queue="default",
             retry_on=(TransientError,),
             singleton=True, retry_singleton=True)
def my_task(*, user_id: int, action: str) -> None:
    ...

# Batch task — same decorator, just routed to a Batch queue
@lambda_task(queue="heavy", soft_timeout=1800, hard_timeout=3500,
            retry_on=(TransientError,))
def heavy_task(*, file_id: int) -> None:
    ...
```

- Tasks are resolved at execution time via `import_string` on the fully-qualified task name
- Task files should be named `tasks.py` within the Django app
- Direct call `my_task(user_id=1, action="x")` runs synchronously
- `my_task.execute_on_commit(user_id=1, action="x")` enqueues after transaction commit
- The execution backend (Lambda vs Batch) is determined by the queue configuration, not the decorator

## Enqueuing

`execute_on_commit()` defaults to delay 0. Pass `_delay=<seconds>` at call time to set the SQS delay for that specific enqueue:

```python
my_task.execute_on_commit(user_id=1, action="x")            # delay 0
my_task.execute_on_commit(user_id=1, action="x", _delay=60) # 60s delay
```

The `_delay` override is validated against the range `[0, 900]`. It only affects the SQS `DelaySeconds` — it has no effect in eager or async-local mode.

## retry_on

Pass a tuple of exception types to `retry_on` on `@lambda_task`. If the task raises an instance of any of those types (or a subclass), the executor automatically re-enqueues the task with the same kwargs and an incremented `n_retries` counter on the `SQSLambdaTaskMessage`.

- `TaskRecord.status` is set to `RETRYING` and the traceback is recorded
- The retry is a new invocation — the current record is terminal at `RETRYING`
- Retries continue until `n_retries` reaches `LAMBDA_TASKS_MAX_RETRIES`, at which point `MaxRetriesExceededError` is raised and the record is saved as `FAILED`

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
- If the lock cannot be acquired (`LockError`) and `retry_singleton=True` (default), the executor treats it as a retryable exception — same code path as `retry_on`. The `TaskRecord` is set to `RETRYING`, the traceback is recorded, and the task is re-enqueued with `n_retries + 1`
- If `retry_singleton=False`, lock contention is treated as a successful no-op — `TaskRecord` is set to `SUCCESS` with the traceback recorded, and no retry is enqueued
- If `n_retries` has reached `LAMBDA_TASKS_MAX_RETRIES`, `MaxRetriesExceededError` is raised and the record is saved as `FAILED`
- The cache backend used for locks is controlled by `LAMBDA_TASKS_SINGLETON_CACHE` (default `"default"`)

```python
@lambda_task(singleton=True)
def sync_inventory(*, warehouse_id: int) -> None:
    # Only one instance runs at a time; LockError → RETRYING + re-enqueued
    ...

@lambda_task(singleton=True, retry_singleton=False)
def sync_inventory(*, warehouse_id: int) -> None:
    # Only one instance runs at a time; LockError → SUCCESS + traceback (no retry)
    ...
```

`singleton` and `retry_singleton` are stored on `LambdaTaskWrapper` and read by the executor at execution time; they are never serialised into the SQS message.

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
3. Resolves timeouts: decorator default → settings defaults (soft=270s, hard=300s), validated against queue max (900 for SQS, 3600 for Batch)
4. If `wrapper.singleton` is `True`, acquires a Redis lock via `caches[SINGLETON_CACHE].lock(lock_key)` wrapping the atomic block; if `False`, no lock is acquired
5. Runs task inside `transaction.atomic()` + `TimeoutContext`
6. On success: updates record to `SUCCESS` with result and `end_time`
7. On `LockError` when `singleton=True` and `retry_singleton=False`: rolls back task-side writes, commits record as `SUCCESS` with traceback and `end_time`
8. On retryable exception (type matches `retry_on` or `LockError` for singleton tasks with `retry_singleton=True`, `n_retries < MAX_RETRIES`): rolls back task-side writes, enqueues retry via `execute_on_commit` with `n_retries + 1`, commits record as `RETRYING` with traceback and `end_time`
9. On retryable exception with `n_retries >= MAX_RETRIES`: commits record as `FAILED` with traceback, raises `MaxRetriesExceededError`
10. On any other exception: rolls back atomic block, updates record to `FAILED` with traceback and `end_time`

## Lambda Handler

`handler(event, context)` in `handler.py`:
- Processes each SQS record independently
- Calls `django.db.close_old_connections()` before each record to close database connections that have become unusable between warm invocations (idle timeouts, RDS failovers, network interruptions)
- Passes `record["messageId"]` as `message_id` to `execute_immediately()` — this becomes the `TaskRecord` primary key, enabling deduplication on redelivery
- Returns `{"batchItemFailures": [...]}` for partial-batch failure reporting
- Only pre-execution failures (malformed message, import error, misconfiguration) are reported as `batchItemFailures` — task logic failures are caught and recorded as `FAILED` TaskRecords without raising
- Recommended SQS queue settings: `maxReceiveCount=1` with a DLQ configured; automatic retries are not useful since task failures are not re-driven by design
- Cold-start sequence runs inside the handler on the first invocation (not at module import time) to avoid Lambda init-duration timeouts: memory limit is set, a temporary `StreamHandler` is attached to the `lambda_tasks` logger, then `resolve_environment()` → `resolve_secrets_into_env()` (handler removed) → conditional `django.setup()`
- A module-level `_cold_start_done` sentinel ensures the sequence runs only once; subsequent warm invocations skip it
- Both loaders run unconditionally (outside the `DJANGO_SETTINGS_MODULE` check) — the environment secret may provide that var, and individual secrets may depend on environment-loaded vars
- A temporary `StreamHandler` is attached to the `lambda_tasks` logger for the duration of the loaders so their log output is visible before Django's `LOGGING` dictConfig has run; it is removed immediately after so that Django's configuration is the sole authority on logging from that point on
- `resource.setrlimit(RLIMIT_AS)` is set from `AWS_LAMBDA_FUNCTION_MEMORY_SIZE` (Lambda) or the ECS task metadata endpoint (Fargate/Batch) so that excessive allocation raises `MemoryError` instead of triggering the OOM killer

`main()` in `handler.py` — AWS Batch container entry point (`python -m lambda_tasks.handler`):
- Reads `LAMBDA_TASKS_MESSAGE` env var (the serialized task JSON, set by `submit_batch_job` via container overrides)
- Uses `AWS_BATCH_JOB_ID` as the `message_id` (falls back to UUID4 if not set)
- Constructs a synthetic SQS event and delegates to `handler()` — reuses the full cold-start and execution path
- Returns exit code 0 on success, 1 on failure

## Environment Loader

`resolve_environment()` in `environment_loader.py` runs once at Lambda cold start, before `resolve_secrets_into_env()` and `django.setup()`.

When the environment variable `LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN` is set, the loader parses the reference, fetches the named Secrets Manager secret, parses its JSON content as a flat key-value mapping, and sets the resulting pairs as environment variables.

Required format: `<arn>:<version-id>` (8 colon-separated segments — the ARN is 7 segments, plus the version-id). The version-id must be non-empty.

Behaviour:
- If `LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN` is not set, does nothing (no AWS API calls)
- Validates the reference format before any AWS call — malformed references raise `ValueError` immediately
- Fetches the secret via `secretsmanager.get_secret_value(SecretId=..., VersionId=...)`
- Validates the secret value is a flat JSON object (all values must be strings, no empty keys)
- Sets each key-value pair in `os.environ` — existing env vars are overridden (no conflict detection)
- Idempotent via a module-level `_loaded` sentinel — subsequent calls are free no-ops
- Invalid reference format, invalid JSON, non-flat objects, or empty keys raise `ValueError` at cold start

## Secret Loader

`resolve_secrets_into_env()` in `secret_loader.py` runs once at Lambda cold start, after `resolve_environment()` and before `django.setup()`.

Any env var prefixed `LAMBDA_TASKS_SECRET_` is treated as a Secrets Manager reference. The unprefixed name is the target env var.

Required format: `<arn>:<json-key>:<version-stage>:<version-id>` (10 colon-separated segments). The version-stage segment must be empty. The json-key and version-id must be non-empty.

Behaviour:
- All references are validated before any AWS call — malformed references raise `ValueError` immediately
- Setting both `LAMBDA_TASKS_SECRET_FOO` and `FOO` is a configuration error and raises `ValueError`
- Calls are batched by `(ARN, version-id)` — one `GetSecretValue` per unique combination
- Fetched secrets are cached in-process; warm invocations pay no extra cost

## Models

`models.py` contains three classes:

- `TaskRecord` — Django ORM model; persists task execution state with statuses `RUNNING`, `SUCCESS`, `FAILED`, `RETRYING`
- `SQSLambdaTaskMessage` — Pydantic model; the SQS message schema; `execute_immediately()` runs the task inside `transaction.atomic()` + `TimeoutContext`
- `SQSLambdaTask` — Pydantic model; holds a `SQSLambdaTaskMessage` plus routing fields (`delay`, `queue`); `_execute()` inspects queue config to publish to SQS, submit to Batch via `submit_batch_job`, or execute eagerly/locally; `execute_on_commit()` registers `_execute` with `transaction.on_commit`

## TaskRecord Model

Fields: `id` (UUID primary key — set to the SQS `messageId`), `task_name`, `kwargs`, `n_retries`, `status`, `start_time`, `end_time`, `result`, `traceback`

Statuses: `RUNNING`, `SUCCESS`, `FAILED`, `RETRYING`

- `RETRYING` — task failed with a retryable exception and a new invocation has been enqueued; this record is terminal

## Django Settings

| Setting | Default | Description |
|---|---|---|
| `LAMBDA_TASKS_QUEUES` | — | Dict of `{queue_name: queue_config}`. Each value is a dict with either `queue_url` (SQS) or `job_queue_arn`+`job_definition_arn` (Batch). Must include `"default"` which must be SQS. |
| `LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT` | `270` | Soft timeout in seconds |
| `LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT` | `300` | Hard timeout in seconds |
| `LAMBDA_TASKS_EAGER` | `False` | Run tasks synchronously in-process (no SQS) |
| `LAMBDA_TASKS_LOCAL_WORKERS` | `0` | Number of worker processes for async local execution (development only; mutually exclusive with `EAGER`) |
| `LAMBDA_TASKS_NOOP_EXECUTION` | `False` | Drop all tasks silently (for local tests; mutually exclusive with `EAGER` and `LOCAL_WORKERS`) |
| `LAMBDA_TASKS_MAX_RETRIES` | `2880` | Maximum retry attempts before `MaxRetriesExceededError` is raised (60 × 24 × 2) |
| `LAMBDA_TASKS_SINGLETON_CACHE` | `"default"` | Django cache backend used for singleton task locks |

`LAMBDA_TASKS_QUEUES` must be set and include a `"default"` key. The `"default"` queue must be an SQS queue. Both timeout values must be greater than zero and `soft_timeout` must always be strictly less than `hard_timeout`. The maximum allowed timeout depends on the queue type: 900 for SQS, 3600 for Batch.

## Noop Execution Mode

Set `LAMBDA_TASKS_NOOP_EXECUTION = True` to silently drop all tasks. Useful for local tests where task execution is irrelevant. When a task is dispatched, a warning is logged with the task name, kwargs, and queue, and execution returns immediately — no SQS message is sent, no task is executed.

```python
# settings/test.py
LAMBDA_TASKS_NOOP_EXECUTION = True
```

`LAMBDA_TASKS_NOOP_EXECUTION` can only be set when `LAMBDA_TASKS_EAGER` is `False` and `LAMBDA_TASKS_LOCAL_WORKERS` is `0`. Setting it alongside either raises `ImproperlyConfigured`.

A Django deployment check (`lambda_tasks.W001`) warns if this setting is enabled, ensuring it is not accidentally left on in production.

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
1. **Noop mode** (`LAMBDA_TASKS_NOOP_EXECUTION=True`) — tasks are dropped with a warning log
2. **Eager mode** (`LAMBDA_TASKS_EAGER=True`) — synchronous, in-process, no timeouts
3. **Async local mode** (`LOCAL_WORKERS > 0`) — async, separate processes, timeouts enforced
4. **SQS mode** (default) — async, Lambda workers, timeouts enforced

`LAMBDA_TASKS_LOCAL_WORKERS` and `LAMBDA_TASKS_EAGER` are mutually exclusive — setting both raises `ImproperlyConfigured`. A negative value also raises `ImproperlyConfigured`.

`LAMBDA_TASKS_NOOP_EXECUTION` is mutually exclusive with both `LAMBDA_TASKS_EAGER` and `LAMBDA_TASKS_LOCAL_WORKERS`.

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
- `_pool_initializer()` — calls `django.setup()` once per worker and sets `SIGINT` to `SIG_IGN` so workers ignore Ctrl+C
- `_install_shutdown_handlers()` — installs main-thread `SIGINT`/`SIGTERM` handlers that release the pool, then chain to the previous handler

### Shutdown and the Ctrl+C semaphore-leak race

`runserver` runs the development server in an autoreloader **child** process spawned via `subprocess.run()`. On Ctrl+C the terminal delivers `SIGINT` to the whole process group; the autoreloader **parent** unwinds out of `subprocess.run` and immediately calls `process.kill()` (`SIGKILL`) on the child. The pool's POSIX semaphores must be unlinked before that `SIGKILL` lands, otherwise multiprocessing's `resource_tracker` prints `There appear to be N leaked semaphore objects to clean up at shutdown`.

Relying on `atexit` alone loses this race in applications with a heavy shutdown sequence (many `atexit` handlers, open DB connections), because `atexit` runs only after the full interpreter unwind — `SIGKILL` arrives first. To win the race, `LambdaTasksConfig.ready()` calls `_install_shutdown_handlers()` when `LOCAL_WORKERS > 0`. The handler shuts the pool down as its **first** action, then chains to the previously installed handler so normal shutdown behaviour (`KeyboardInterrupt`, autoreloader exit) is preserved. `atexit` registration remains as a fallback for non-signal exits. Handler installation is idempotent and only effective on the main thread (`signal.signal` raises off the main thread, in which case it is a no-op).

`_shutdown_pool()` does **not** use `pool.shutdown(wait=True)`. `wait=True` blocks on joining the worker processes, and a worker that ran a heavy `django.setup()` is slow to exit — `concurrent.futures` does not unlink the pool's queue semaphores until the workers actually die, so the parent's near-instant `SIGKILL` still wins and the semaphores leak. Instead `_shutdown_pool()` **terminates the worker processes first** (a near-instant signal to children we own), then calls `pool.shutdown(wait=False, cancel_futures=True)`. With the workers already gone, the queue semaphores are unlinked in milliseconds — fast enough to beat the parent's `SIGKILL`.

## Deployment Checks

`lambda_tasks.checks` registers a Django deployment check (runs with `manage.py check --deploy`) that warns if any non-production execution mode is enabled:

| ID | Condition | Message |
|---|---|---|
| `lambda_tasks.W001` | `LAMBDA_TASKS_NOOP_EXECUTION = True` | Tasks will be silently dropped |
| `lambda_tasks.W002` | `LAMBDA_TASKS_EAGER = True` | Tasks will run synchronously in-process |
| `lambda_tasks.W003` | `LAMBDA_TASKS_LOCAL_WORKERS > 0` | Tasks will run in a local process pool |

The checks are registered via `AppConfig.ready()` and tagged with `deploy=True` so they only fire during `--deploy` checks.

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
- `submit_batch_job(*, message_json: str, batch_queue: str) -> str` — submits a batch task to AWS Batch. Reads `job_queue_arn` and `job_definition_arn` from `LAMBDA_TASKS_QUEUES[batch_queue]`, sanitizes the task name for use as `jobName`, and calls `batch.submit_job()` with the message passed as a `LAMBDA_TASKS_MESSAGE` environment variable override. Returns the Batch job ID. This task is enqueued automatically by `SQSLambdaTask._execute()` when the queue is a Batch queue — users do not call it directly.

## Conventions

- All task functions must use keyword-only arguments (enforced at decoration time)
- Tasks are resolved at execution time via `import_string` — task modules must be importable in the Lambda environment
- Tasks run in a Lambda environment — no Django request context available
- `LambdaTasksSettings` is instantiated fresh per use (lazy, reads live Django settings)
- boto3 exceptions propagate directly from `SQSLambdaTask._execute()` — callers should handle them
