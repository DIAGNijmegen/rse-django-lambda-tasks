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
│   ├── decorators.py           # @lambda_task decorator + LambdaTaskWrapper
│   ├── handler.py              # AWS Lambda entry point
│   ├── logging.py              # task_logger — invocation-scoped LoggerAdapter
│   ├── models.py               # TaskRecord, SQSLambdaTaskMessage, SQSLambdaTask
│   ├── settings.py             # LambdaTasksSettings (lazy Django settings reader)
│   ├── secret_loader.py        # Resolves LAMBDA_TASKS_SECRET_* env vars at cold start
│   ├── environment_loader.py    # Loads env vars from Secrets Manager at cold start
│   ├── tasks.py                # Built-in maintenance tasks (cleanup_task_records)
│   ├── timeouts.py             # TimeoutContext implementation
│   └── migrations/             # Django migrations for TaskRecord
├── tests/                      # pytest test suite
│   ├── conftest.py
│   ├── settings.py             # Django settings for test environment
│   └── test_*.py               # One test file per module
├── example/                    # Runnable Django project (EAGER mode, no AWS needed)
│   ├── example_app/            # Sample app with a task definition and trigger view
│   ├── example_project/        # Django settings, URLs, WSGI
│   ├── manage.py
│   └── README.md
├── main.py                     # Placeholder entry point
├── pyproject.toml
└── uv.lock
```

## Module Responsibilities

- `decorators.py` — defines `@lambda_task`; enforces kwargs-only at decoration time
- `models.py` — `TaskRecord` (Django ORM), `SQSLambdaTaskMessage` (Pydantic, SQS schema + execution logic), `SQSLambdaTask` (Pydantic, holds message + routing; `_execute()` publishes to SQS or executes eagerly; `execute_on_commit()` registers `_execute` with `transaction.on_commit`)
- `handler.py` — Lambda entry point; calls `resolve_environment()` then `resolve_secrets_into_env()` then conditionally `django.setup()` at cold start; processes SQS records independently; returns `batchItemFailures`
- `environment_loader.py` — loads env vars from a Secrets Manager secret at cold start; validates flat JSON format; idempotent via `_loaded` sentinel
- `secret_loader.py` — resolves `LAMBDA_TASKS_SECRET_*` env vars from Secrets Manager before Django starts; validates format, detects conflicts, batches API calls; idempotent via `_loaded` sentinel
- `logging.py` — `task_logger` singleton; `message_id` set/cleared around each task execution
- `settings.py` — `LambdaTasksSettings` instantiated fresh per use (reads live Django settings)
- `admin.py` — Django admin registration for `TaskRecord`
- `tasks.py` — built-in maintenance tasks; `cleanup_task_records` deletes old `TaskRecord` rows

## Conventions

- Task definitions belong in `tasks.py` within each Django app
- All task functions must use keyword-only arguments — positional args raise `TypeError` at decoration time
- Task logic must be self-contained; no Django request context is available in Lambda
- New modules go inside `lambda_tasks/`; add a corresponding `tests/test_<module>.py`
- Tests for `models.py` (including `SQSLambdaTask._execute` and `SQSLambdaTask.execute_on_commit`) live in `tests/test_models.py`
- Do not add application-level code to `main.py` — it is a placeholder only
