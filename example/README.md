# Example App

A minimal Django app demonstrating `django-lambda-tasks` with `LAMBDA_TASKS_LOCAL_WORKERS = 2`. Tasks execute in a background process pool — no AWS infrastructure required.

## Prerequisites

- `uv` installed
- Docker installed (for PostgreSQL)
- All commands run from the `example/` directory

## Setup

**1. Start PostgreSQL**

```bash
docker compose up -d
```

**2. Apply migrations**

```bash
uv run python manage.py migrate
```

**3. Create a superuser** (to access the admin interface)

```bash
uv run python manage.py createsuperuser
```

**4. Start the development server**

```bash
uv run python manage.py runserver
```

## Usage

**Trigger the example task**

Visit: `http://127.0.0.1:8000/trigger/?name=Alice`

The task runs in a background worker process and the response confirms it was triggered.

**View task records in the admin**

Visit: `http://127.0.0.1:8000/admin/` → Lambda Tasks → Task records

Log in with the superuser credentials you created above.

## Testing the Sentry integration (optional)

Set `SENTRY_DSN` to see per-task traces and error attribution in Sentry. The init lives in `example_project/sentry.py` and uses `DjangoIntegration` + `LoggingIntegration(event_level=ERROR)` **without** `AwsLambdaIntegration`, so each task is traced under its own task-named transaction (see the library README's Sentry section for why).

```bash
SENTRY_DSN="https://<key>@<org>.ingest.sentry.io/<project>" uv run python manage.py runserver
```

- `http://127.0.0.1:8000/trigger/?name=Alice` — a successful task; produces a transaction named `example_app.tasks.greet`.
- `http://127.0.0.1:8000/trigger-error/?name=Bob` — a failing task; produces an error attributed to `example_app.tasks.boom` (via the library's `capture_exception` on the terminal failure path).

Without `SENTRY_DSN` set, Sentry initialisation is skipped and the app runs exactly as above.
