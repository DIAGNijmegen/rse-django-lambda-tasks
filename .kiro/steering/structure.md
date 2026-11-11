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
│   ├── sentry.py               # task_scope + handler_error_scope — per-task trace, tags, failure attribution
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
- `models.py` — `TaskRecord` (Django ORM), `SQSLambdaTaskMessage` (Pydantic, SQS schema + execution logic; `execute_immediately` owns the logging + Sentry context and delegates the run to a private `_run` method), `SQSLambdaTask` (Pydantic, holds message + routing; `_execute()` inspects queue config to publish to SQS, submit to Batch via `submit_batch_job`, execute eagerly, or submit to the local process pool; `execute_on_commit()` registers `_execute` with `transaction.on_commit`)
- `local_executor.py` — `ProcessPoolExecutor`-based async local execution; `get_pool()` lazily creates a module-level pool; `submit_task()` fire-and-forget submission; `_execute_in_worker()` deserializes and runs the task in a child process; `_pool_initializer()` calls `django.setup()` per worker (and sets `SIGINT` to `SIG_IGN` so workers ignore Ctrl+C); `_install_shutdown_handlers()` installs main-thread `SIGINT`/`SIGTERM` handlers that release the pool before chaining to the previous handler
- `handler.py` — Lambda entry point (`handler(event, context)`); cold-start init (memory limit → temporary log handler → `resolve_environment()` → `resolve_secrets_into_env()` → handler removed → conditional `django.setup()`) runs inside the handler on first invocation, guarded by `_cold_start_done` sentinel; calls `db.close_old_connections()` before each SQS record to handle stale connections on warm starts; processes SQS records independently; returns `batchItemFailures`; logs pre-execution failures (malformed message, import error, misconfiguration) at ERROR inside `handler_error_scope` so the host's `LoggingIntegration` reports them tagged `lambda_task.handler_error`; cold-start failures are reported the same way and re-raised. Also provides `main()` as the AWS Batch container entry point (`python -m lambda_tasks.handler`) — reads `LAMBDA_TASKS_MESSAGE` env var, constructs a synthetic SQS event, and delegates to `handler()`
- `environment_loader.py` — loads env vars from a Secrets Manager secret at cold start; validates flat JSON format; idempotent via `_loaded` sentinel
- `secret_loader.py` — resolves `LAMBDA_TASKS_SECRET_*` env vars from Secrets Manager before Django starts; validates format, detects conflicts, batches API calls; idempotent via `_loaded` sentinel
- `logging.py` — `task_logger` singleton; `message_id` set/cleared around each task execution
- `sentry.py` — `task_scope` context manager + `handler_error_scope` context manager. `task_scope` opens an isolated `new_scope` and traces the task. With the **recommended** host config (no `AwsLambdaIntegration`) nothing is active, so the task starts its **own transaction named after the task** on both Lambda and Batch. If a host keeps `AwsLambdaIntegration`, the task is instead traced as a **child span** of the active invocation transaction (detected via `sentry_sdk.get_current_scope().transaction` on 2.x; `start_span` is auto root-or-child on 3.0) — never a second, competing transaction, which would double trace volume and defeat `traces_sample_rate`. Records `lambda_task` / `lambda_task.message_id` / `lambda_task.n_retries` / `lambda_task.runtime` as span metadata and scope tags (runtime read from `AWS_LAMBDA_FUNCTION_NAME` or `AWS_BATCH_JQ_NAME`). Deliberately does not call `set_transaction_name` (would rename an active invocation transaction and break 3.0 segment naming); error attribution rides on the `lambda_task` tag. **Failure reporting** is via error logs, not explicit captures: the terminal failure branches in `models.py` (plain failure + max-retries) call `task_logger.error(error, exc_info=True)`, and the host's `LoggingIntegration(event_level=ERROR)` turns that into the Sentry event (the retry branch logs at WARNING and is not reported). `handler_error_scope(message_id=...)` is a context manager used in `handler.py` to tag an isolated scope (`lambda_task.handler_error` + message id) around the handler's `logger.error`, so pre-task failures are reported and identifiable via the same LoggingIntegration. Future-proof across the SDK 2.x→3.0 boundary (verified against 2.74 and 3.0.0b4): `start_transaction`/`set_tag`/child `start_span` on 2.x, `start_span`/`set_attribute` on 3.0, detected at runtime via `hasattr`. No-op when `sentry_sdk` is absent or uninitialised (`sentry-sdk` is a dev dependency only, pinned `>=2,<3`). Wrapped around the task body in `SQSLambdaTaskMessage.execute_immediately`. Error reporting requires `LoggingIntegration(event_level=logging.ERROR)` in the host init (default on 2.x, must be set on 3.0); demonstrated in the example app
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
