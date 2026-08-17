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
