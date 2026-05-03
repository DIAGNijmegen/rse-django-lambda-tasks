# Django Lambda Tasks

A Django library for offloading work to AWS Lambda outside of the request-response cycle. Tasks are defined with a decorator, enqueued to SQS on transaction commit, and executed by a Lambda handler that AWS invokes with SQS message batches. Task results, status, and metadata are persisted in the Django database.

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

### 3. Configure AWS

See [AWS Lambda and SQS setup](#aws-lambda-and-sqs-setup) for queue and Lambda configuration.

---

## Example app

A runnable Django project is included in the [`example/`](example/) directory. It uses `LAMBDA_TASKS_EAGER = True` so no AWS infrastructure is needed.

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
| `LAMBDA_TASKS_QUEUES` | `dict[str, str]` | Map of queue name → SQS queue URL. Must include a `"default"` key. |

```python
LAMBDA_TASKS_QUEUES = {
    "default": "https://sqs.us-east-1.amazonaws.com/123456789/default",
    "high_memory": "https://sqs.us-east-1.amazonaws.com/123456789/high-memory",
}
```

`LAMBDA_TASKS_QUEUES` must be set and must include a `"default"` key, otherwise `ImproperlyConfigured` is raised on first use.

### Timeout settings

| Setting | Type | Default | Description |
|---|---|---|---|
| `LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT` | `int` | `270` | Seconds before `SoftTimeLimitExceeded` is raised inside the task. |
| `LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT` | `int` | `300` | Seconds before the task is forcibly terminated. |

Both values must be between `1` and `900` seconds (the AWS Lambda maximum runtime is 15 minutes). `soft_timeout` must always be strictly less than `hard_timeout`.

```python
LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT = 240
LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT = 270
```

### Eager execution (development / testing)

| Setting | Type | Default | Description |
|---|---|---|---|
| `LAMBDA_TASKS_EAGER` | `bool` | `False` | When `True`, tasks run synchronously in-process instead of being sent to SQS. |
| `LAMBDA_TASKS_MAX_RETRIES` | `int` | `2880` | Maximum retry attempts before `MaxRetriesExceededError` is raised. |
| `LAMBDA_TASKS_SINGLETON_CACHE` | `str` | `"default"` | Django cache backend used for singleton task locks. |

```python
# settings/local.py
LAMBDA_TASKS_EAGER = True
```

With eager mode enabled, `.execute_on_commit()` executes the task immediately without touching SQS. Useful for local development and test suites where you don't want to mock AWS infrastructure.

> **Note:** Timeouts are not enforced in eager mode. `soft_timeout` and `hard_timeout` values are accepted and stored but `TimeoutContext` is never entered — the task runs without any time limit. This is intentional: `SIGALRM`-based timeouts require a Lambda/Unix worker process, not a Django dev server thread.

---

## Decorator options

```python
@lambda_task(
    soft_timeout=60,       # seconds — overrides global default for this task
    hard_timeout=90,       # seconds — overrides global default for this task
    queue="default",       # named queue from LAMBDA_TASKS_QUEUES
    ignore_errors=(),      # exception types treated as non-fatal
    retry_on=(),           # exception types that trigger automatic retry
    retry_delay=0,         # base retry delay in seconds (jitter always added, capped at 900)
    singleton=False,       # prevent concurrent execution via Redis lock
)
def my_task(*, arg: str) -> None:
    ...
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `soft_timeout` | `int \| None` | `None` (uses global default) | Per-task soft timeout in seconds (max 900). |
| `hard_timeout` | `int \| None` | `None` (uses global default) | Per-task hard timeout in seconds (max 900). |
| `queue` | `str` | `"default"` | Named queue to route this task to. |
| `ignore_errors` | `tuple[type[BaseException], ...]` | `()` | Exception types to treat as non-fatal (see [Ignored exceptions](#ignored-exceptions)). |
| `retry_on` | `tuple[type[BaseException], ...]` | `()` | Exception types that trigger an automatic retry (see [Automatic retries](#automatic-retries)). |
| `retry_delay` | `int` | `0` | Base delay in seconds when enqueuing a retry. Jitter (1–5s) is always added; result capped at 900. Requires `retry_on` to be non-empty. |
| `singleton` | `bool` | `False` | Prevent concurrent execution via a Redis lock (see [Singleton tasks](#singleton-tasks)). |

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

The returned dict matches the `SQSLambdaTask` schema. To reconstruct and enqueue it later:

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

---

## Named queues

Route tasks to queues backed by Lambda functions with different hardware profiles (e.g. more memory or CPU):

```python
# settings.py
LAMBDA_TASKS_QUEUES = {
    "default": "https://sqs.us-east-1.amazonaws.com/123456789/default",
    "high_memory": "https://sqs.us-east-1.amazonaws.com/123456789/high-memory",
}
```

```python
@lambda_task(queue="high_memory")
def process_large_file(*, file_id: int) -> None:
    ...
```

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
| `task_name` | `str` | Fully-qualified function name (e.g. `myapp.tasks.send_welcome_email`). |
| `kwargs` | `dict` | Serialized task arguments. |
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

Using your own `logging.getLogger(__name__)` is fine too; those records just won't carry the `message_id` prefix.

To filter by invocation in CloudWatch Logs Insights:

```
fields @timestamp, @message
| filter @message like "[your-invocation-id]"
| sort @timestamp asc
```

---

## Ignored exceptions

Pass a tuple of exception types to `ignore_errors` on `@lambda_task`. If the task raises an instance of any listed type (or a subclass), the executor treats it as a non-fatal outcome:

- `TaskRecord.status` is set to `SUCCESS`
- The exception traceback is saved to `TaskRecord.traceback` for observability
- Task-side ORM writes inside the `transaction.atomic()` block are still rolled back
- The `TaskRecord` update is committed outside the atomic block

Exceptions not in `ignore_errors` continue to produce `FAILED` with a rollback. The default (`()`) preserves existing behaviour.

```python
from lambda_tasks.decorators import lambda_task

class RecordNotFound(Exception):
    pass

@lambda_task(ignore_errors=(RecordNotFound,))
def sync_user(*, user_id: int) -> None:
    user = fetch_user(user_id)   # raises RecordNotFound if already deleted
    update_profile(user)
```

If `sync_user` raises `RecordNotFound`, the `TaskRecord` will have `status=SUCCESS` and the traceback recorded in `traceback`. Any ORM writes made before the exception are rolled back.

`ignore_errors` is validated at decoration time — passing a non-exception type raises `TypeError` immediately. It is stored on `LambdaTaskWrapper` and read by the executor at execution time; it is never serialised into the SQS message.

---

## Automatic retries

Pass a tuple of exception types to `retry_on` on `@lambda_task`. If the task raises an instance of any listed type (or a subclass), the executor re-enqueues the task with an incremented retry counter:

- `TaskRecord.status` is set to `RETRYING` and the traceback is recorded
- The retry is a new invocation — the current record is terminal at `RETRYING`
- Retries continue until `n_retries` reaches `LAMBDA_TASKS_MAX_RETRIES` (default `2880`), at which point `MaxRetriesExceededError` is raised and the record is saved as `FAILED`
- `ignore_errors` is checked first — a type in both `ignore_errors` and `retry_on` is treated as ignored (SUCCESS), not retried
- `retry_on` and `ignore_errors` must not overlap; overlapping raises `TypeError` at decoration time

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
    # Only one instance runs at a time
    ...
```

- Lock key format: `lambda_tasks.singleton_lock.{task_name}`
- The lock is acquired with `blocking_timeout=0` (fail immediately if held) and `timeout=hard_timeout` (auto-expire if the worker crashes)
- If the lock cannot be acquired (`LockError`), the task is retried via the existing retry mechanism — `TaskRecord` is set to `RETRYING`, the traceback is recorded, and the task is re-enqueued with `n_retries + 1`
- If `n_retries` reaches `LAMBDA_TASKS_MAX_RETRIES`, `MaxRetriesExceededError` is raised and the record is saved as `FAILED`
- The cache backend used for locks is controlled by `LAMBDA_TASKS_SINGLETON_CACHE` (default `"default"`)

`LockError` is retried automatically for singleton tasks — do not include it in `retry_on` (doing so raises `TypeError` at decoration time). You may include `LockError` in `ignore_errors` if you want lock contention to be treated as a non-fatal outcome instead of triggering a retry.

`singleton` is stored on `LambdaTaskWrapper` and read by the executor at execution time; it is never serialised into the SQS message.

---

## Atomicity

Each task runs inside a `django.db.transaction.atomic` block. If the task raises an unhandled exception, all ORM writes made inside the task are rolled back. The `TaskRecord` failure update is always written outside the atomic block so it survives the rollback.

When an exception matches `ignore_errors`, the same rollback applies to task-side writes — the atomic block still exits via exception. Only the `TaskRecord` update (written outside the block) is committed, recording the `SUCCESS` outcome and traceback.

---

## Error handling

| Scenario | Exception | When |
|---|---|---|
| Task function has positional parameters | `TypeError` | At decoration time |
| `soft_timeout >= hard_timeout` | `ValueError` | At decoration time |
| Timeout value exceeds 900 seconds | `ValueError` | At decoration time or settings load |
| Unknown queue name | `ImproperlyConfigured` | At `.execute_on_commit()` |
| `LAMBDA_TASKS_QUEUES` not set | `ImproperlyConfigured` | On first settings access |
| `LAMBDA_TASKS_QUEUES` missing `"default"` | `ImproperlyConfigured` | On first settings access |
| SQS `send_message` failure | boto3 exception (propagated) | At `.execute_on_commit()` |

---

## AWS Lambda and SQS setup

### SQS queue configuration

Each queue in `LAMBDA_TASKS_QUEUES` should be a standard SQS queue (not FIFO) with the following recommended settings:

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

Ensure the Lambda execution environment has `DJANGO_SETTINGS_MODULE` set and that all task modules are importable (i.e. your application code is on the Python path).

| Environment Variable | Required | Description |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | Yes | Django settings module path (e.g. `myapp.settings.production`). |
| `LAMBDA_TASKS_SSM_ENVIRONMENT` | No | SSM Parameter Store parameter name to load as environment variables at cold start. |
| `LAMBDA_TASKS_SECRET_*` | No | Secrets Manager references resolved into env vars at cold start (see below). |

### Resolving Django settings from AWS Secrets Manager

The Lambda handler supports loading secret values from AWS Secrets Manager into the environment before Django starts. This lets your Django settings file read from `os.environ` as normal while keeping secrets out of plaintext environment variables.

Set any env var with the prefix `LAMBDA_TASKS_SECRET_` to a full Secrets Manager dynamic reference. The unprefixed name becomes the target env var:

```
LAMBDA_TASKS_SECRET_DATABASE_URL=arn:aws:secretsmanager:eu-west-1:123456789012:secret:myapp/prod:DATABASE_URL:AWSCURRENT:v1
```

At cold start, before `django.setup()` is called, the handler calls `resolve_secrets_into_env()` which:

1. Scans all env vars for the `LAMBDA_TASKS_SECRET_` prefix
2. Validates every reference — malformed references raise immediately so the container fails to start rather than misconfiguring Django silently
3. Groups references by `(ARN, version-stage, version-id)` and makes one `GetSecretValue` call per unique combination
4. Extracts the named JSON key from the secret and writes it into `os.environ`
5. Caches fetched secrets in-process — warm invocations pay no extra cost

#### Reference format

Every value must follow the full dynamic reference syntax:

```
<arn>:<json-key>:<version-stage>:<version-id>
```

All four fields are required and must be non-empty. The secret value must be a JSON object; `json-key` names the field to extract.

```
# arn (7 segments) : json-key : version-stage : version-id
arn:aws:secretsmanager:eu-west-1:123456789012:secret:myapp/prod:DATABASE_URL:AWSCURRENT:v1
```

Multiple env vars can reference different keys from the same secret — only one `GetSecretValue` call is made for that `(ARN, version-stage, version-id)` combination:

```
LAMBDA_TASKS_SECRET_DATABASE_URL=arn:...:myapp/prod:DATABASE_URL:AWSCURRENT:v1
LAMBDA_TASKS_SECRET_SECRET_KEY=arn:...:myapp/prod:SECRET_KEY:AWSCURRENT:v1
```

#### Validation errors

The following all raise `ValueError` at cold start, preventing the Lambda container from starting with a misconfigured environment:

- Wrong number of colon-separated segments (must be exactly 10)
- Empty `json-key`, `version-stage`, or `version-id`
- Both `LAMBDA_TASKS_SECRET_FOO` and `FOO` are set — use one or the other
- The named JSON key does not exist in the fetched secret
- The secret value is not valid JSON

### Loading environment variables from SSM Parameter Store

The Lambda handler supports loading environment variables from an AWS SSM Parameter Store parameter at cold start. This lets you manage environment configuration centrally in Parameter Store without baking values into the Lambda deployment package.

Set the `LAMBDA_TASKS_SSM_ENVIRONMENT` environment variable to the name of an SSM parameter:

```
LAMBDA_TASKS_SSM_ENVIRONMENT=/myapp/prod/environment
```

The parameter value must be a flat JSON object where all keys and values are strings:

```json
{
  "DATABASE_URL": "postgres://user:pass@host:5432/db",
  "REDIS_URL": "redis://host:6379/0",
  "DJANGO_SETTINGS_MODULE": "myapp.settings.production"
}
```

At cold start, before `resolve_secrets_into_env()` and `django.setup()`, the handler calls `resolve_ssm_environment()` which:

1. Checks for the `LAMBDA_TASKS_SSM_ENVIRONMENT` env var — if not set, does nothing
2. Fetches the named parameter via `ssm.get_parameter(Name=..., WithDecryption=True)`
3. Validates the parameter value is a flat JSON object (all values must be strings, no empty keys)
4. Sets each key-value pair in `os.environ` — existing env vars are overridden
5. Caches the result via a module-level sentinel — subsequent calls are free no-ops

Because SSM loading runs first, the parameter can provide `DJANGO_SETTINGS_MODULE` itself, and secrets loaded by `resolve_secrets_into_env()` can reference SSM-loaded values.

#### Validation errors

The following raise `ValueError` at cold start, preventing the Lambda container from starting:

- Parameter value is not valid JSON
- JSON is not a flat object (contains non-string values) — error message lists the offending keys
- JSON contains an empty string key

AWS errors (parameter not found, permission denied) propagate as boto3 exceptions and crash the container at cold start.

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

This bypasses the queue entirely and runs the function in the current process and transaction.
