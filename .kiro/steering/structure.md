---
inclusion: always
---

# Project Structure

## Layout

```
django-lambda-tasks/
├── lambda_tasks/               # Core library package
│   ├── __init__.py
│   ├── admin.py                # Django admin for TaskRecord
│   ├── apps.py                 # Django AppConfig
│   ├── checks.py               # Django deployment checks (noop, eager, local_workers)
│   ├── decorators.py           # LambdaTaskWrapper, @lambda_task
│   ├── handler.py              # AWS Lambda entry point + AWS Batch container entry point (main)
│   ├── logging.py              # task_logger — invocation-scoped LoggerAdapter
│   ├── models.py               # TaskRecord, SQSLambdaTaskMessage, SQSLambdaTask
│   ├── settings.py             # LambdaTasksSettings, constants, queue type helpers
│   ├── secret_loader.py        # Resolves LAMBDA_TASKS_SECRET_* env vars at cold start
│   ├── environment_loader.py    # Loads env vars from Secrets Manager at cold start
│   ├── local_executor.py        # ProcessPoolExecutor for async local task execution
│   ├── tasks.py                # Built-in tasks (cleanup_task_records, submit_batch_job)
│   ├── timeouts.py             # TimeoutContext implementation
│   └── migrations/             # Django migrations for TaskRecord
├── tests/                      # pytest test suite
│   ├── conftest.py
│   ├── settings.py             # Django settings for test environment
│   └── test_*.py               # One test file per module
├── example/                    # Runnable Django project (LOCAL_WORKERS mode, no AWS needed)
│   ├── example_app/            # Sample app with a task definition and trigger view
│   ├── example_project/        # Django settings, URLs, WSGI
│   ├── manage.py
│   └── README.md
├── main.py                     # Placeholder entry point
├── pyproject.toml
└── uv.lock
```

## Module Responsibilities

- `decorators.py` — defines `LambdaTaskWrapper` and `@lambda_task` decorator factory; enforces kwargs-only at decoration time; validates timeouts (> 0, soft < hard) at decoration time; upper-bound timeout validation deferred to execution time based on queue type
- `models.py` — `TaskRecord` (Django ORM), `SQSLambdaTaskMessage` (Pydantic, SQS schema + execution logic), `SQSLambdaTask` (Pydantic, holds message + routing; `_execute()` inspects queue config to publish to SQS, submit to Batch via `submit_batch_job`, execute eagerly, or submit to the local process pool; `execute_on_commit()` registers `_execute` with `transaction.on_commit`)
- `local_executor.py` — `ProcessPoolExecutor`-based async local execution; `get_pool()` lazily creates a module-level pool; `submit_task()` fire-and-forget submission; `_execute_in_worker()` deserializes and runs the task in a child process; `_pool_initializer()` calls `django.setup()` per worker (and sets `SIGINT` to `SIG_IGN` so workers ignore Ctrl+C); `_install_shutdown_handlers()` installs main-thread `SIGINT`/`SIGTERM` handlers that release the pool before chaining to the previous handler
- `handler.py` — Lambda entry point (`handler(event, context)`); cold-start init (memory limit → temporary log handler → `resolve_environment()` → `resolve_secrets_into_env()` → handler removed → conditional `django.setup()`) runs inside the handler on first invocation, guarded by `_cold_start_done` sentinel; calls `db.close_old_connections()` before each SQS record to handle stale connections on warm starts; processes SQS records independently; returns `batchItemFailures`. Also provides `main()` as the AWS Batch container entry point (`python -m lambda_tasks.handler`) — reads `LAMBDA_TASKS_MESSAGE` env var, constructs a synthetic SQS event, and delegates to `handler()`
- `environment_loader.py` — loads env vars from a Secrets Manager secret at cold start; validates flat JSON format; idempotent via `_loaded` sentinel
- `secret_loader.py` — resolves `LAMBDA_TASKS_SECRET_*` env vars from Secrets Manager before Django starts; validates format, detects conflicts, batches API calls; idempotent via `_loaded` sentinel
- `logging.py` — `task_logger` singleton; `message_id` set/cleared around each task execution
- `settings.py` — constants (`MAX_DELAY`, `MAX_TIMEOUT`, `MAX_BATCH_TIMEOUT`); `_is_sqs_queue()` and `_is_batch_queue()` helpers; `LambdaTasksSettings` instantiated fresh per use (reads live Django settings); `queue_max_timeout()` method returns 900 or 3600 based on queue type
- `admin.py` — Django admin registration for `TaskRecord`
- `apps.py` — Django `AppConfig`; `ready()` imports `checks` to register deployment checks and installs the local-executor shutdown signal handlers when `LOCAL_WORKERS > 0`
- `checks.py` — Django deployment checks; `check_noop_not_in_production` warns if `NOOP_EXECUTION`, `EAGER`, or `LOCAL_WORKERS > 0` are set (runs with `manage.py check --deploy`)
- `tasks.py` — built-in tasks; `cleanup_task_records` deletes old `TaskRecord` rows; `submit_batch_job` receives a serialized task message and submits it to AWS Batch via `batch.submit_job()`

## Conventions

- Task definitions belong in `tasks.py` within each Django app
- All task functions must use keyword-only arguments — positional args raise `TypeError` at decoration time
- Task logic must be self-contained; no Django request context is available in Lambda or Batch
- New modules go inside `lambda_tasks/`; add a corresponding `tests/test_<module>.py`
- Tests for `models.py` (including `SQSLambdaTask._execute` and `SQSLambdaTask.execute_on_commit`) live in `tests/test_models.py`
- Do not add application-level code to `main.py` — it is a placeholder only
