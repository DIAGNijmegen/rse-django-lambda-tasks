# Django Lambda Tasks

A Django library for offloading work to AWS Lambda or AWS Batch outside of the request-response cycle. Tasks are defined with a decorator, enqueued to SQS on transaction commit, and executed by a Lambda handler or Batch container. Task results, status, and metadata are persisted in the Django database.

> **Platform note:** Unix-based systems only (Linux, macOS). Timeout enforcement relies on `SIGALRM`.

---

## Installation

```bash
pip install django-lambda-tasks
```

Add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...
    "lambda_tasks",
]
```

Run migrations:

```bash
python manage.py migrate lambda_tasks
```

---

## Quick Start

### 1. Define a task

Create a `tasks.py` in your Django app and decorate any function with `@lambda_task`. All task arguments must be keyword-only:

```python
# myapp/tasks.py
from lambda_tasks.decorators import lambda_task

@lambda_task
def send_welcome_email(*, user_id: int, template: str) -> str:
    # ... send the email
    return "sent"
```

### 2. Enqueue from a view

Call `.execute_on_commit()` to enqueue the task. It will be dispatched to SQS after the current database transaction commits:

```python
# myapp/views.py
from myapp.tasks import send_welcome_email

def register(request):
    user = User.objects.create(...)
    send_welcome_email.execute_on_commit(user_id=user.id, template="welcome")
    return HttpResponse("Registered")
```

You can override the delay for a specific enqueue by passing `_delay`:

```python
# Delay this particular invocation by 60 seconds
send_welcome_email.execute_on_commit(user_id=user.id, template="welcome", _delay=60)
```

### 3. Configure AWS

See [AWS Lambda and SQS setup](#aws-lambda-and-sqs-setup) for queue and Lambda configuration.

---

## Example app

A runnable Django project is included in the [`example/`](example/) directory. It uses `LAMBDA_TASKS_LOCAL_WORKERS = 2` so no AWS infrastructure is needed.

```bash
cd example
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Then visit `http://127.0.0.1:8000/trigger/?name=Alice` to trigger a task and `http://127.0.0.1:8000/admin/` to inspect the resulting `TaskRecord`.

See [`example/README.md`](example/README.md) for full details.

---

## Configuration

All settings are read from your Django settings module and prefixed with `LAMBDA_TASKS_`.

### Queue settings

| Setting | Type | Description |
|---|---|---|
| `LAMBDA_TASKS_QUEUES` | `dict[str, dict]` | Map of queue name → queue configuration. Must include a `"default"` key. The `"default"` queue must be an SQS queue. |

Each queue value is a dict. The queue type is determined by the keys present:

- **SQS queue:** `{"queue_url": "https://sqs.../queue-name"}` — tasks execute on Lambda (max timeout 900s)
- **Batch queue:** `{"job_queue_arn": "arn:...", "job_definition_arn": "arn:..."}` — tasks execute on AWS Batch (max timeout 3600s)

```python
LAMBDA_TASKS_QUEUES = {
    "default": {"queue_url": "https://sqs.us-east-1.amazonaws.com/123456789/default"},
    "high_memory": {"queue_url": "https://sqs.us-east-1.amazonaws.com/123456789/high-memory"},
    "heavy": {
        "job_queue_arn": "arn:aws:batch:eu-west-1:123456789:job-queue/my-queue",
        "job_definition_arn": "arn:aws:batch:eu-west-1:123456789:job-definition/my-def:1",
    },
}
```

`LAMBDA_TASKS_QUEUES` must be set and must include a `"default"` key, otherwise `ImproperlyConfigured` is raised on first use. The `"default"` queue must be an SQS queue.

### Timeout settings

| Setting | Type | Default | Description |
|---|---|---|---|
| `LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT` | `int` | `270` | Seconds before `SoftTimeLimitExceeded` is raised inside the task. |
| `LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT` | `int` | `300` | Seconds before the task is forcibly terminated. |

Both values must be greater than zero and `soft_timeout` must always be strictly less than `hard_timeout`. The maximum allowed value depends on the queue type: 900 seconds for SQS queues (Lambda's 15-minute limit), 3600 seconds for Batch queues (1 hour). Upper bound validation happens at execution time based on the task's queue.

```python
LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT = 240
LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT = 270
```

### Retry settings

| Setting | Type | Default | Description |
|---|---|---|---|
| `LAMBDA_TASKS_MAX_RETRIES` | `int` | `2880` | Maximum retry attempts before `MaxRetriesExceededError` is raised (default is 60 × 24 × 2). |

### Singleton settings

| Setting | Type | Default | Description |
|---|---|---|---|
| `LAMBDA_TASKS_SINGLETON_CACHE` | `str` | `"default"` | Django cache backend used for singleton task locks. |

### Environment and secrets

| Setting | Type | Description |
|---|---|---|
| `LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN` | env var | Secrets Manager reference (`<arn>:<version-id>`) to load as environment variables at Lambda cold start. |
| `LAMBDA_TASKS_SECRET_*` | env var(s) | Secrets Manager references resolved into env vars at Lambda cold start. The unprefixed name becomes the target env var. |

These are environment variables set on the Lambda function, not Django settings. See [Loading environment variables from Secrets Manager](#loading-environment-variables-from-secrets-manager) and [Resolving individual secrets from AWS Secrets Manager](#resolving-individual-secrets-from-aws-secrets-manager) for full details.

### Eager execution (development / testing)

| Setting | Type | Default | Description |
|---|---|---|---|
| `LAMBDA_TASKS_EAGER` | `bool` | `False` | When `True`, tasks run synchronously in-process instead of being sent to SQS. |

```python
# settings/local.py
LAMBDA_TASKS_EAGER = True
```

With eager mode enabled, `.execute_on_commit()` executes the task immediately without touching SQS. Useful for local development and test suites where you don't want to mock AWS infrastructure.

> **Note:** Timeouts are not enforced in eager mode. `soft_timeout` and `hard_timeout` values are accepted and stored but `TimeoutContext` becomes a no-op — it checks `LAMBDA_TASKS_EAGER` internally and skips `SIGALRM` setup. This is intentional: `SIGALRM`-based timeouts require a Lambda/Unix worker process, not a Django dev server thread.

### Async local execution (development)

| Setting | Type | Default | Description |
|---|---|---|---|
| `LAMBDA_TASKS_LOCAL_WORKERS` | `int` | `0` | Number of worker processes for async local execution. When set to a positive integer, tasks run in a background process pool instead of SQS. |

```python
# settings/local.py
LAMBDA_TASKS_LOCAL_WORKERS = 4
```

Async local mode bridges the gap between eager mode and full SQS/Lambda deployment. Tasks execute in background worker processes with true parallelism and full timeout enforcement via `SIGALRM`, without requiring AWS infrastructure.

The execution mode hierarchy is:
1. **Noop** (`LAMBDA_TASKS_NOOP_EXECUTION=True`) — tasks are dropped with a warning log
2. **Eager** (`LAMBDA_TASKS_EAGER=True`) — synchronous, in-process, no timeouts
3. **Async local** (`LAMBDA_TASKS_LOCAL_WORKERS > 0`) — async, separate processes, timeouts enforced
4. **SQS** (default) — async, Lambda workers, timeouts enforced

Key characteristics:
- `LAMBDA_TASKS_LOCAL_WORKERS` and `LAMBDA_TASKS_EAGER` are mutually exclusive — setting both raises `ImproperlyConfigured`
- The process pool is created lazily on first task submission and reused for the server lifetime
- Each worker process calls `django.setup()` once at startup
- Tasks are serialized as JSON for IPC (same code path as SQS)
- Worker failures are isolated from the Django server process — a crashing task does not bring down the dev server
- `transaction.on_commit` is respected — tasks only submit after the transaction commits
- Timeouts (`SIGALRM`) are fully enforced because workers are separate OS processes

---

### Noop execution (testing)

| Setting | Type | Default | Description |
|---|---|---|---|
| `LAMBDA_TASKS_NOOP_EXECUTION` | `bool` | `False` | When `True`, all tasks are silently dropped. A warning is logged with the task name, kwargs, and queue. |

```python
# settings/test.py
LAMBDA_TASKS_NOOP_EXECUTION = True
```

Noop mode is useful for test suites where task execution is irrelevant and you want to avoid mocking SQS or running tasks at all. When a task is dispatched via `execute_on_commit()`, it logs a warning and returns immediately — no SQS message is sent, no task is executed, no `TaskRecord` is created.

`LAMBDA_TASKS_NOOP_EXECUTION` is mutually exclusive with both `LAMBDA_TASKS_EAGER` and `LAMBDA_TASKS_LOCAL_WORKERS` — setting it alongside either raises `ImproperlyConfigured`.

### Deployment checks

A Django deployment check (runs with `manage.py check --deploy`) warns if any non-production execution mode is enabled:

| Check ID | Condition | Message |
|---|---|---|
| `lambda_tasks.W001` | `LAMBDA_TASKS_NOOP_EXECUTION = True` | Tasks will be silently dropped |
| `lambda_tasks.W002` | `LAMBDA_TASKS_EAGER = True` | Tasks will run synchronously in-process |
| `lambda_tasks.W003` | `LAMBDA_TASKS_LOCAL_WORKERS > 0` | Tasks will run in a local process pool |

Add `manage.py check --deploy` to your CI pipeline to catch these misconfigurations before they reach production.

---

## Decorator options

```python
@lambda_task(
    soft_timeout=60,       # seconds — overrides global default for this task
    hard_timeout=90,       # seconds — overrides global default for this task
    queue="default",       # named queue from LAMBDA_TASKS_QUEUES
    retry_on=(),           # exception types that trigger automatic retry
    retry_delay=0,         # base retry delay in seconds (jitter always added, capped at 900)
    singleton=False,       # prevent concurrent execution via Redis lock
    retry_singleton=True,  # retry on LockError for singleton tasks (or treat as success if False)
)
def my_task(*, arg: str) -> None:
    ...
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `soft_timeout` | `int \| None` | `None` (uses global default) | Per-task soft timeout in seconds. Max depends on queue type (900 for SQS, 3600 for Batch). |
| `hard_timeout` | `int \| None` | `None` (uses global default) | Per-task hard timeout in seconds. Max depends on queue type (900 for SQS, 3600 for Batch). |
| `queue` | `str` | `"default"` | Named queue to route this task to. Determines both execution backend and timeout limits. |
| `retry_on` | `tuple[type[BaseException], ...]` | `()` | Exception types that trigger an automatic retry (see [Automatic retries](#automatic-retries)). |
| `retry_delay` | `int` | `0` | Base delay in seconds when enqueuing a retry. Jitter (1–5s) is always added; result capped at 900. Requires `retry_on` to be non-empty. |
| `singleton` | `bool` | `False` | Prevent concurrent execution via a Redis lock (see [Singleton tasks](#singleton-tasks)). |
| `retry_singleton` | `bool` | `True` | When `True`, `LockError` on a singleton task triggers a retry. When `False`, lock contention is treated as a successful no-op (traceback recorded). |

### Per-call delay override

By default, `execute_on_commit()` uses a delay of 0 seconds. To set the SQS `DelaySeconds` for a specific call, pass `_delay` to `execute_on_commit()` or `serialize()`:

```python
@lambda_task
def notify_user(*, user_id: int) -> None:
    ...

# Default (0 seconds delay)
notify_user.execute_on_commit(user_id=1)

# Delay this specific invocation by 120 seconds
notify_user.execute_on_commit(user_id=1, _delay=120)
```

`_delay` is validated against the range `[0, 900]`. It only affects the SQS `DelaySeconds` — it has no effect in eager or async-local mode.

---

## Serializing a task invocation

`serialize()` builds and validates a task invocation the same way `execute_on_commit()` does, but returns the payload as a plain dict instead of enqueuing it. Useful when you need to inspect, store, or forward the message before deciding to send it.

```python
payload = send_welcome_email.serialize(user_id=42, template="welcome")
# {
#   "message": {
#     "task_name": "myapp.tasks.send_welcome_email",
#     "kwargs": {"user_id": 42, "template": "welcome"}
#   },
#   "delay": 0,
#   "queue": "default"
# }
```

The returned dict matches the `SQSLambdaTask` schema (`message`, `delay`, `queue`). The `delay` value comes from the decorator's `delay` parameter. To reconstruct and enqueue it later:

```python
from lambda_tasks.models import SQSLambdaTask

task = SQSLambdaTask.model_validate(payload)
task.execute_on_commit()
```

---

## Timeouts

Tasks support a two-phase timeout mechanism:

1. **Soft timeout** — `SoftTimeLimitExceeded` is raised inside the running task, giving it a chance to catch the exception and clean up gracefully.
2. **Hard timeout** — if the task is still running after the hard timeout, it is forcibly terminated and the `TaskRecord` is marked `FAILED`.

```python
from lambda_tasks.decorators import lambda_task
from lambda_tasks.timeouts import SoftTimeLimitExceeded

@lambda_task(soft_timeout=25, hard_timeout=30)
def long_running_task(*, item_id: int) -> None:
    try:
        do_expensive_work(item_id)
    except SoftTimeLimitExceeded:
        # clean up before the hard timeout kills the process
        cleanup(item_id)
        raise
```

Timeout resolution order (first non-`None` value wins):

1. Per-task decorator default (`soft_timeout` / `hard_timeout` on `@lambda_task`)
2. Global settings (`LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT` / `LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT`)

The maximum allowed timeout is determined by the queue type at execution time: 900 seconds for SQS queues, 3600 seconds for Batch queues.

---

## Named queues

Route tasks to different execution backends by assigning them to named queues:

```python
# settings.py
LAMBDA_TASKS_QUEUES = {
    "default": {"queue_url": "https://sqs.us-east-1.amazonaws.com/123456789/default"},
    "high_memory": {"queue_url": "https://sqs.us-east-1.amazonaws.com/123456789/high-memory"},
    "heavy": {
        "job_queue_arn": "arn:aws:batch:eu-west-1:123456789:job-queue/my-queue",
        "job_definition_arn": "arn:aws:batch:eu-west-1:123456789:job-definition/my-def:1",
    },
}
```

```python
# Runs on Lambda with more memory
@lambda_task(queue="high_memory")
def process_images(*, batch_id: int) -> None:
    ...

# Runs on AWS Batch (up to 1 hour, large disk)
@lambda_task(queue="heavy", soft_timeout=1800, hard_timeout=3500)
def process_large_file(*, file_id: int) -> str:
    ...
```

Tasks routed to a Batch queue are automatically submitted to AWS Batch via the built-in `submit_batch_job` task. The flow is:

1. `execute_on_commit()` enqueues a message to the default SQS queue
2. The Lambda handler picks up the message and runs `submit_batch_job`
3. `submit_batch_job` calls `batch.submit_job()` with the task message as a `LAMBDA_TASKS_MESSAGE` environment variable override
4. The Batch container runs `python -m lambda_tasks.handler`, which reads the env var, performs cold-start init, and calls `execute_immediately()`
5. The task runs with full timeout enforcement (`SIGALRM`), retry support, and `TaskRecord` tracking

---

## Task results

Every task invocation creates a `TaskRecord` row in the database. You can query it via the Django ORM:

```python
from lambda_tasks.models import TaskRecord

# All recent tasks
TaskRecord.objects.all()

# Filter by status
TaskRecord.objects.filter(status=TaskRecord.TaskStatus.FAILED)

# Look up a specific invocation
TaskRecord.objects.get(pk="<uuid>")
```

### `TaskRecord` fields

| Field | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key — set to the SQS `messageId` for deduplication. |
| `task_name` | `str` | Fully-qualified function name (e.g. `myapp.tasks.send_welcome_email`). |
| `kwargs` | `dict` | Serialized task arguments. |
| `n_retries` | `int` | Number of retries attempted so far (starts at 0). |
| `status` | `str` | One of `RUNNING`, `SUCCESS`, `FAILED`, `RETRYING`. |
| `start_time` | `datetime \| None` | When the worker began executing the task. |
| `end_time` | `datetime \| None` | When the task completed or failed. |
| `result` | `any \| None` | Return value of the task on success. `None` when the task raised an ignored exception. |
| `traceback` | `str \| None` | Full traceback string on failure, or on success when an ignored exception was raised. `None` on clean success. |

---

## Logging

Import `task_logger` to emit log records that are automatically prefixed with the active `message_id`. This makes it straightforward to filter all logs for a specific task invocation in CloudWatch Logs Insights.

```python
from lambda_tasks.logging import task_logger
from lambda_tasks.decorators import lambda_task

@lambda_task(soft_timeout=60, hard_timeout=90)
def send_welcome_email(*, user_id: int, template: str) -> str:
    task_logger.info("sending email to user %s", user_id)
    # → "[abc-123] sending email to user 42"
    return "sent"
```

`task_logger` is a `LoggerAdapter` wrapping the `lambda_tasks.task` logger. `SQSLambdaTaskMessage.execute_immediately()` sets the `message_id` before each task runs and clears it afterwards — you don't need to manage it yourself.

The Lambda handler configures the `lambda_tasks` logger hierarchy to `INFO` at cold start so that `task_logger` lines appear in CloudWatch. You can control the level by configuring the `lambda_tasks` logger in your Django `LOGGING` setting (e.g. set it to `DEBUG` or `WARNING`). If your `LOGGING` dictConfig sets a root logger with a handler (as most Django projects do), `lambda_tasks` will inherit from it via propagation.

Using your own `logging.getLogger(__name__)` is fine too; those records just won't carry the `message_id` prefix.

To filter by invocation in CloudWatch Logs Insights:

```
fields @timestamp, @message
| filter @message like "[your-invocation-id]"
| sort @timestamp asc
```

---

## Automatic retries

Pass a tuple of exception types to `retry_on` on `@lambda_task`. If the task raises an instance of any listed type (or a subclass), the executor re-enqueues the task with an incremented retry counter:

- `TaskRecord.status` is set to `RETRYING` and the traceback is recorded
- The retry is a new invocation — the current record is terminal at `RETRYING`
- Retries continue until `n_retries` reaches `LAMBDA_TASKS_MAX_RETRIES` (default `2880`), at which point `MaxRetriesExceededError` is raised and the record is saved as `FAILED`

```python
from lambda_tasks.decorators import lambda_task

@lambda_task(retry_on=(RateLimitError, ConnectionError), retry_delay=30)
def sync_data(*, record_id: int) -> None:
    # RateLimitError or ConnectionError → RETRYING + re-enqueued
    # anything else → FAILED
    ...
```

When a retry is enqueued, the delay is set to `min(retry_delay + round(random.uniform(1, 5)), 900)` seconds. The jitter is always added to spread out competing retries. The result is capped at 900 (the SQS `DelaySeconds` maximum).

`retry_on` is validated at decoration time — passing a non-exception type raises `TypeError` immediately. It is stored on `LambdaTaskWrapper` and read by the executor at execution time; it is never serialised into the SQS message.

---

## Singleton tasks

Pass `singleton=True` on `@lambda_task` to prevent concurrent execution of the same task. When enabled, the executor acquires a Redis lock via Django's cache framework before running the task function. The lock wraps the entire `transaction.atomic()` block.

```python
from lambda_tasks.decorators import lambda_task

@lambda_task(singleton=True)
def sync_inventory(*, warehouse_id: int) -> None:
    # Only one instance runs at a time; LockError → RETRYING + re-enqueued
    ...
```

- Lock key format: `lambda_tasks.singleton_lock.{task_name}`
- The lock is acquired with `blocking_timeout=0` (fail immediately if held) and `timeout=hard_timeout` (auto-expire if the worker crashes)
- If the lock cannot be acquired (`LockError`) and `retry_singleton=True` (the default), the task is retried — `TaskRecord` is set to `RETRYING`, the traceback is recorded, and the task is re-enqueued with `n_retries + 1`
- If `retry_singleton=False`, lock contention is treated as a successful no-op — `TaskRecord` is set to `SUCCESS` with the traceback recorded, and no retry is enqueued
- If `n_retries` reaches `LAMBDA_TASKS_MAX_RETRIES`, `MaxRetriesExceededError` is raised and the record is saved as `FAILED`
- The cache backend used for locks is controlled by `LAMBDA_TASKS_SINGLETON_CACHE` (default `"default"`)

`LockError` is handled automatically for singleton tasks — do not include it in `retry_on` (doing so raises `TypeError` at decoration time).

`singleton` and `retry_singleton` are stored on `LambdaTaskWrapper` and read by the executor at execution time; they are never serialised into the SQS message.

```python
@lambda_task(singleton=True, retry_singleton=False)
def sync_inventory(*, warehouse_id: int) -> None:
    # Lock contention → SUCCESS with traceback (no retry)
    ...
```

---

## Atomicity

Each task runs inside a `django.db.transaction.atomic` block. If the task raises an unhandled exception, all ORM writes made inside the task are rolled back. The `TaskRecord` failure update is always written outside the atomic block so it survives the rollback.

---

## Error handling

| Scenario | Exception | When |
|---|---|---|
| Task function has positional parameters | `TypeError` | At decoration time |
| `soft_timeout >= hard_timeout` | `ValueError` | At decoration time |
| Timeout value exceeds queue max (900 or 3600) | `ValueError` | At execution time (timeout resolution) |
| Unknown queue name | `ImproperlyConfigured` | At `.execute_on_commit()` |
| `LAMBDA_TASKS_QUEUES` not set | `ImproperlyConfigured` | On first settings access |
| `LAMBDA_TASKS_QUEUES` missing `"default"` | `ImproperlyConfigured` | On first settings access |
| `"default"` queue is not SQS | `ImproperlyConfigured` | On first settings access |
| SQS `send_message` failure | boto3 exception (propagated) | At `.execute_on_commit()` |

---

## AWS Lambda and SQS setup

### SQS queue configuration

Each SQS queue in `LAMBDA_TASKS_QUEUES` should be a standard SQS queue (not FIFO) with the following recommended settings:

- **Visibility timeout** — set this higher than your Lambda function's `hard_timeout`. If the Lambda times out before SQS receives a deletion confirmation, the message becomes visible again and will be redelivered.
- **Dead letter queue (DLQ)** — configure a DLQ to capture messages that cannot be processed. Without one, messages that exhaust retries are silently deleted.
- **`maxReceiveCount`** — controls how many times a message is retried before being moved to the DLQ. A value of `1` is recommended: task failures are recorded as `FAILED` `TaskRecord` entries and are not retried by design, so the only messages that reach the DLQ are pre-execution failures (malformed message body, import errors, misconfiguration). These are unlikely to succeed on automatic retry and are better handled by fixing the underlying issue and manually redriving from the DLQ.

### Lambda trigger

Configure the SQS queue as an event source trigger for your Lambda function. AWS will invoke the handler with batches of messages and use the `batchItemFailures` response to determine which messages to return to the queue.

Only pre-execution failures (e.g. a malformed message or an import error) are reported as `batchItemFailures`. Task logic failures are caught by `SQSLambdaTaskMessage.execute_immediately()`, recorded in `TaskRecord`, and treated as successful from SQS's perspective — they will not be redelivered.

### Lambda handler

Point your Lambda function's handler at:

```
lambda_tasks.handler.handler
```

The handler performs cold-start initialisation on the first invocation (not at module import time) to avoid Lambda init-duration timeouts. The sequence is: temporary log handler attached → `resolve_environment()` → `resolve_secrets_into_env()` → log handler removed → conditional `django.setup()`. A module-level sentinel ensures this runs only once; subsequent warm invocations skip it entirely.

Ensure the Lambda execution environment has `DJANGO_SETTINGS_MODULE` set and that all task modules are importable (i.e. your application code is on the Python path).

| Environment Variable | Required | Description |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | Yes | Django settings module path (e.g. `myapp.settings.production`). |
| `LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN` | No | Secrets Manager reference (`<arn>:<version-id>`) to load as environment variables at cold start (runs first). |
| `LAMBDA_TASKS_SECRET_*` | No | Secrets Manager references resolved into individual env vars at cold start (runs second, after environment loading). |

### Loading environment variables from Secrets Manager

The Lambda handler supports loading environment variables from an AWS Secrets Manager secret at cold start. This lets you manage environment configuration centrally in Secrets Manager without baking values into the Lambda deployment package.

Set the `LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN` environment variable to a full reference including version ID:

```
LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN=arn:aws:secretsmanager:eu-west-1:123456789012:secret:myapp/prod/environment:v1
```

The format is `<arn>:<version-id>` (8 colon-separated segments — the ARN is 7 segments, plus the version-id). The version-id must be non-empty.

The secret value must be a flat JSON object where all keys and values are strings:

```json
{
  "DATABASE_URL": "postgres://user:pass@host:5432/db",
  "REDIS_URL": "redis://host:6379/0",
  "DJANGO_SETTINGS_MODULE": "myapp.settings.production"
}
```

At cold start (on the first handler invocation), before `resolve_secrets_into_env()` and `django.setup()`, the handler calls `resolve_environment()` which:

1. Checks for the `LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN` env var — if not set, does nothing
2. Parses and validates the reference format (8 segments, non-empty version-id)
3. Fetches the secret via `secretsmanager.get_secret_value(SecretId=..., VersionId=...)`
4. Validates the secret value is a flat JSON object (all values must be strings, no empty keys)
5. Sets each key-value pair in `os.environ` — existing env vars are overridden
6. Caches the result via a module-level sentinel — subsequent calls are free no-ops

Because environment loading runs first, the secret can provide `DJANGO_SETTINGS_MODULE` itself, and individual secrets loaded by `resolve_secrets_into_env()` can reference environment-loaded values.

#### Validation errors

The following raise `ValueError` at cold start, preventing the Lambda container from starting:

- Reference format is invalid (wrong segment count, empty version-id)
- Secret value is not valid JSON
- JSON is not a flat object (contains non-string values) — error message lists the offending keys
- JSON contains an empty string key

AWS errors (secret not found, permission denied) propagate as boto3 exceptions and crash the container at cold start.

### Resolving individual secrets from AWS Secrets Manager

The Lambda handler supports loading individual secret values from AWS Secrets Manager into the environment before Django starts. This lets your Django settings file read from `os.environ` as normal while keeping secrets out of plaintext environment variables.

Set any env var with the prefix `LAMBDA_TASKS_SECRET_` to a full Secrets Manager dynamic reference. The unprefixed name becomes the target env var:

```
LAMBDA_TASKS_SECRET_DATABASE_URL=arn:aws:secretsmanager:eu-west-1:123456789012:secret:myapp/prod:DATABASE_URL::v1
```

At cold start (on the first handler invocation), after `resolve_environment()` and before `django.setup()`, the handler calls `resolve_secrets_into_env()` which:

1. Scans all env vars for the `LAMBDA_TASKS_SECRET_` prefix
2. Validates every reference — malformed references raise immediately so the container fails to start rather than misconfiguring Django silently
3. Groups references by `(ARN, version-id)` and makes one `GetSecretValue` call per unique combination
4. Extracts the named JSON key from the secret and writes it into `os.environ`
5. Caches fetched secrets in-process — warm invocations pay no extra cost

#### Reference format

Every value must follow the full dynamic reference syntax:

```
<arn>:<json-key>:<version-stage>:<version-id>
```

The version-stage segment must be empty. The json-key and version-id fields are required and must be non-empty. The secret value must be a JSON object; `json-key` names the field to extract.

```
# arn (7 segments) : json-key : version-stage (empty) : version-id
arn:aws:secretsmanager:eu-west-1:123456789012:secret:myapp/prod:DATABASE_URL::v1
```

Multiple env vars can reference different keys from the same secret — only one `GetSecretValue` call is made for that `(ARN, version-id)` combination:

```
LAMBDA_TASKS_SECRET_DATABASE_URL=arn:...:myapp/prod:DATABASE_URL::v1
LAMBDA_TASKS_SECRET_SECRET_KEY=arn:...:myapp/prod:SECRET_KEY::v1
```

#### Validation errors

The following all raise `ValueError` at cold start, preventing the Lambda container from starting with a misconfigured environment:

- Wrong number of colon-separated segments (must be exactly 10)
- Empty `json-key` or `version-id`
- Non-empty `version-stage` (version-stage is not supported)
- Both `LAMBDA_TASKS_SECRET_FOO` and `FOO` are set — use one or the other
- The named JSON key does not exist in the fetched secret
- The secret value is not valid JSON

---

## AWS Batch setup

For tasks that exceed Lambda's 15-minute timeout or 10GB ephemeral storage limit, route them to a Batch queue:

```python
@lambda_task(queue="heavy", soft_timeout=1800, hard_timeout=3500)
def process_large_file(*, file_id: int) -> str:
    # long-running work with large disk usage
    return "done"
```

### Batch job definition setup

Your Batch job definition should:
- Use the same Docker image as your application
- Set `DJANGO_SETTINGS_MODULE` as an environment variable
- Configure secrets via the job definition's `secrets` field or via `LAMBDA_TASKS_ENVIRONMENT_SECRETS_MANAGER_ARN` / `LAMBDA_TASKS_SECRET_*` env vars (same mechanism as Lambda)
- Set the default command to `["python", "-m", "lambda_tasks.handler"]`

### Retries

Batch tasks support `retry_on` — when a retryable exception is raised, the retry goes back through SQS → Lambda → `submit_batch_job` → new Batch job. The same `LAMBDA_TASKS_MAX_RETRIES` limit applies.

### Container overrides limit

The total `containerOverrides` payload (including JSON formatting) is limited to 8192 characters by AWS Batch. For typical task kwargs (IDs, short strings) this is not a concern.

### Eager and local worker modes

Tasks on Batch queues respect `LAMBDA_TASKS_EAGER` and `LAMBDA_TASKS_LOCAL_WORKERS` — in development, they run in-process or in the local process pool just like SQS-backed tasks, without requiring AWS infrastructure.

---

## Built-in tasks

### `cleanup_task_records`

A maintenance task that deletes `TaskRecord` rows older than a given number of days. This is the equivalent of Celery's backend cleanup — without periodic pruning, the `TaskRecord` table will grow indefinitely.

```python
from lambda_tasks.tasks import cleanup_task_records

# Delete records older than 7 days (default)
cleanup_task_records.execute_on_commit()

# Delete records older than 30 days
cleanup_task_records.execute_on_commit(retention_days=30)
```

The task deletes all records whose `start_time` is strictly before `now() - retention_days`, regardless of status. It returns the number of deleted rows.

Schedule it however suits your infrastructure — an EventBridge rule triggering a Lambda, a Django management command in a cron job, or a call from another task. The library does not impose a scheduling mechanism.

---

## Direct (synchronous) invocation

You can call a decorated task directly like a normal function — useful in tests or management commands where you want synchronous execution without going through SQS:

```python
result = send_welcome_email(user_id=1, template="welcome")
```

This bypasses the queue entirely and runs the function in the current process. No `TaskRecord` is created, no `transaction.atomic()` block is used, and no timeout enforcement applies — it behaves exactly like calling the underlying function directly. Kwargs are still validated against the task's type annotations via Pydantic.
