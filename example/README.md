# Example App

A minimal Django app demonstrating `django-lambda-tasks` with `LAMBDA_TASKS_EAGER = True`. In eager mode, tasks execute synchronously in-process — no AWS infrastructure required.

## Prerequisites

- `uv` installed
- All commands run from the `example/` directory

## Setup

**1. Apply migrations**

```bash
uv run python manage.py migrate
```

**2. Create a superuser** (to access the admin interface)

```bash
uv run python manage.py createsuperuser
```

**3. Start the development server**

```bash
uv run python manage.py runserver
```

## Usage

**Trigger the example task**

Visit: `http://127.0.0.1:8000/trigger/?name=Alice`

The task runs synchronously and the response confirms it was triggered.

**View task records in the admin**

Visit: `http://127.0.0.1:8000/admin/` → Lambda Tasks → Task records

Log in with the superuser credentials you created above.
