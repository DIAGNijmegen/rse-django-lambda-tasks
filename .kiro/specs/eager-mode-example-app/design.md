# Design Document: eager-mode-example-app

## Overview

This feature adds a self-contained example Django application at `example/` that demonstrates `django-lambda-tasks` running in EAGER mode (`LAMBDA_TASKS_EAGER = True`). In EAGER mode the library executes tasks synchronously in-process instead of publishing to SQS, so the example requires no AWS infrastructure — only SQLite.

Two library-side changes are also required:

1. `lambda_tasks/admin.py` — registers `TaskRecord` with Django admin so any project that includes `lambda_tasks` in `INSTALLED_APPS` gets admin visibility for free.
2. `lambda_tasks/decorators.py` — the EAGER path in `LambdaTaskWrapper.on_commit()` must be updated to build a `SQSLambdaTaskMessage` and call `execute_task(message=message)` directly, so that a `TaskRecord` is written and the full execution path (transaction.atomic, timeout, status tracking) is exercised in EAGER mode.

One Lambda-side change is also required:

3. `lambda_tasks/handler.py` — must call `django.setup()` at module level (cold start) so that the Django app registry is initialized before any task executes. This is a no-op in EAGER mode (Django is already set up by the running server process).

---

## Architecture

```
Developer browser
      │
      ▼
example/manage.py  ──►  example Django project (SQLite, EAGER=True)
      │                  (Django already set up — django.setup() called by manage.py)
      │
      ├── example_app/views.py   ──►  task.on_commit(**kwargs)
      │                                      │
      │                          LambdaTaskWrapper.on_commit()
      │                                      │
      │                          conf.EAGER == True?
      │                                      │ yes
      │                                      ▼
      │                          SQSLambdaTaskMessage(task_name, invocation_id, kwargs, ...)
      │                                      │
      │                                      ▼
      │                          execute_task(message=message)   ← synchronous
      │                                      │
      │                                      ▼
      │                          TaskRecord written (RUNNING → SUCCESS/FAILED)
      │
      └── /admin/  ──►  Django admin  ──►  TaskRecord (via lambda_tasks/admin.py)

Lambda cold start (SQS/non-EAGER path)
      │
      ▼
import lambda_tasks.handler
      │
      ├── DJANGO_SETTINGS_MODULE set?
      │         │ yes, and not django.apps.apps.ready
      │         ▼
      │   django.setup()   ← runs once per container lifecycle
      │
      ▼
handler(event, context)  ──►  _process_record()  ──►  execute_task()
                               (django.apps.apps.ready == True guaranteed)
```

---

## Components and Interfaces

### 1. `lambda_tasks/admin.py` (new library file)

Registers `TaskRecord` with Django's admin site. Kept minimal — a `ModelAdmin` subclass with `list_display` covering the fields required by Requirement 3.3.

```python
from django.contrib import admin
from lambda_tasks.models import TaskRecord

@admin.register(TaskRecord)
class TaskRecordAdmin(admin.ModelAdmin):
    list_display = ("task_name", "status", "start_time", "end_time", "result")
    readonly_fields = ("task_name", "invocation_id", "kwargs", "status",
                       "start_time", "end_time", "result", "traceback")
```

### 2. `lambda_tasks/decorators.py` — EAGER path (updated)

The EAGER branch in `LambdaTaskWrapper.on_commit()` must be updated to go through the same serialization and execution path as SQS-delivered tasks. Instead of calling `self._func` directly, it builds a `SQSLambdaTaskMessage` and calls `execute_task()`:

```python
if conf.EAGER:
  from lambda_tasks.models import SQSLambdaTaskMessage, execute_task

  message = SQSLambdaTaskMessage(
    task_name=task_name,
    kwargs=task_kwargs,
  )
  execute_task(message=message)
  return
```

This ensures a `TaskRecord` is written and the full execution path (`transaction.atomic`, timeout enforcement, status tracking) is exercised in EAGER mode — identical to the Lambda/SQS path.

### 3. `lambda_tasks/handler.py` — Django initialization (updated)

In a Lambda environment Django's app registry is not initialized unless `django.setup()` is called explicitly. Without it, any task that touches the ORM or any Django app will raise `AppRegistryNotReady`.

The handler module must call `django.setup()` at module level so it runs exactly once per Lambda container lifecycle (cold start), not per invocation:

```python
import os
import django
from django.apps import apps as django_apps

# Cold-start Django setup — runs once per Lambda container.
# Guarded so it is safe to import this module in an already-running Django
# process (e.g. tests, EAGER mode) where setup has already been done.
if os.environ.get("DJANGO_SETTINGS_MODULE") and not django_apps.ready:
    django.setup()
```

Key design decisions:

- The `DJANGO_SETTINGS_MODULE` env var is the caller's responsibility (set in the Lambda function configuration). If it is absent the guard is skipped and any ORM usage will fail at runtime — this is intentional, as the handler has no sensible default settings to fall back to.
- The `django_apps.ready` check prevents a double-setup error when the module is imported in a context where Django is already initialized (tests, EAGER mode running inside a Django server process).
- This does **not** affect EAGER mode. In EAGER mode tasks run inside an already-running Django server process where `django.setup()` has already been called by `manage.py` or `wsgi.py`. The `django_apps.ready` guard ensures the handler module can be safely imported in that context without side effects.

### 4. Example Django project layout

```
example/
├── manage.py
├── README.md
├── example_project/
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── example_app/
    ├── __init__.py
    ├── apps.py
    ├── tasks.py
    ├── views.py
    └── urls.py
```

#### `example_project/settings.py` (key settings)

```python
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "lambda_tasks",
    "example_app",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

LAMBDA_TASKS_EAGER = True
```

#### `example_app/tasks.py`

```python
from lambda_tasks.decorators import lambda_task

@lambda_task
def greet(*, name: str) -> str:
    return f"Hello, {name}!"
```

#### `example_app/views.py`

```python
from django.http import HttpResponse
from example_app.tasks import greet


def trigger(request):
    name = request.GET.get("name", "world")
    greet.execute_on_commit(name=name)
    return HttpResponse(f"Task triggered for: {name}")
```

#### URL wiring

- `example_project/urls.py` includes `admin.site.urls` and delegates to `example_app/urls.py`
- `example_app/urls.py` maps `GET /trigger/` → `trigger` view

---

## Data Models

No new models. The existing `TaskRecord` model (in `lambda_tasks/models.py`) is used as-is. The `lambda_tasks/admin.py` file registers it with Django admin.

`TaskRecord` fields relevant to the example:

| Field | Type | Notes |
|---|---|---|
| `task_name` | CharField | Fully-qualified function name |
| `invocation_id` | UUIDField | Unique per enqueue |
| `kwargs` | JSONField | Task arguments |
| `status` | CharField | PENDING / RUNNING / SUCCESS / FAILED |
| `start_time` | DateTimeField | Set when execution begins |
| `end_time` | DateTimeField | Set when execution completes |
| `result` | JSONField | Return value of the task function |
| `traceback` | TextField | Set on failure |

> In EAGER mode, `on_commit()` builds a `SQSLambdaTaskMessage` and calls `execute_task()` directly, so a `TaskRecord` is written for every EAGER invocation — identical to the SQS/Lambda path. Developers can inspect these records immediately via the admin interface.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: All registered tasks use keyword-only arguments

*For any* task registered in the `lambda_tasks` registry after importing `example_app.tasks`, every parameter in that task's underlying function signature must be keyword-only (i.e., no `POSITIONAL_ONLY` or `POSITIONAL_OR_KEYWORD` parameters).

**Validates: Requirements 2.2**

### Property 2: EAGER mode executes tasks synchronously

*For any* `LambdaTaskWrapper` and any valid kwargs dict, when `LAMBDA_TASKS_EAGER = True` and `on_commit(**kwargs)` is called, the wrapped function must have been called with those exact kwargs by the time `on_commit()` returns — with no SQS interaction occurring.

**Validates: Requirements 2.4**

### Property 3: EAGER mode writes a TaskRecord

*For any* `LambdaTaskWrapper` and any valid kwargs dict, when `LAMBDA_TASKS_EAGER = True` and `on_commit(**kwargs)` is called, a `TaskRecord` with a matching `task_name` and `kwargs` must exist in the database with `status` of either `SUCCESS` or `FAILED` by the time `on_commit()` returns.

**Validates: Requirements 2.4, 3.3**

### Property 4: Django is set up before any task executes in the Lambda handler

*For any* SQS event processed by `handler()`, `django.apps.apps.ready` must be `True` before `execute_task()` is called for any record in that event. This must hold regardless of the number of records in the batch or the content of the task payloads.

**Validates: Lambda deployment correctness — tasks that use the Django ORM require an initialized app registry**

---

## Error Handling

| Scenario | Handling |
|---|---|
| `LAMBDA_TASKS_EAGER` not set | `LambdaTasksSettings.EAGER` defaults to `False`; SQS path is used |
| Task raises an exception in EAGER mode | Exception propagates to the caller (the view) after `execute_task()` updates the `TaskRecord` to `FAILED` with a traceback |
| Admin accessed without superuser | Standard Django admin authentication applies |
| `manage.py migrate` not run | Django raises `OperationalError` on first DB access; README instructs migration first |
| Task defined with positional args | `TypeError` raised at decoration time by `_validate_func` |
| `DJANGO_SETTINGS_MODULE` not set in Lambda | `django.setup()` guard is skipped; ORM usage in tasks will raise `AppRegistryNotReady` at runtime |
| Handler imported in EAGER / test context | `django_apps.ready` is already `True`; `django.setup()` is not called again — no double-setup error |

---

## Testing Strategy

### Unit / integration tests (pytest + pytest-django)

Focus on specific examples and edge cases:

- `tests/test_admin.py` — verify `TaskRecord` is registered in `admin.site._registry`; verify `list_display` contains the required fields
- `tests/test_decorators.py` (extend existing) — verify EAGER path calls `execute_task()` synchronously, does not call `enqueuer.enqueue`, and creates a `TaskRecord` with the correct `task_name` and `kwargs`
- Example app structural checks (can live in `tests/test_example_app.py`):
  - `lambda_tasks` in `INSTALLED_APPS`
  - `DATABASES` engine is SQLite
  - `LAMBDA_TASKS_EAGER` is `True`
  - `/trigger/` view returns HTTP 200
  - `example_app.tasks` module contains at least one `LambdaTaskWrapper`

### Property-based tests (Hypothesis)

Use `hypothesis` (already in dev dependencies). Minimum 100 iterations per property.

**Property 1 test** — `tests/test_decorators.py`

```python
# Feature: eager-mode-example-app, Property 1: all registered tasks use keyword-only arguments
@given(st.text(min_size=1))
@settings(max_examples=100)
def test_all_tasks_kwargs_only(task_name):
    ...
```

Generate random function signatures with keyword-only params, decorate them, and assert no positional parameters exist in the registry entry.

**Property 2 test** — `tests/test_decorators.py`

```python
# Feature: eager-mode-example-app, Property 2: EAGER mode executes tasks synchronously
@given(st.dictionaries(st.text(), st.integers()))
@settings(max_examples=100)
def test_eager_mode_synchronous(kwargs):
    ...
```

For any kwargs dict, with `LAMBDA_TASKS_EAGER=True`, calling `on_commit(**kwargs)` on a wrapper should result in the function being called with those kwargs before `on_commit` returns, and `enqueuer.enqueue` must not be called.

**Property 3 test** — `tests/test_decorators.py`

```python
# Feature: eager-mode-example-app, Property 3: EAGER mode writes a TaskRecord
@given(st.dictionaries(st.text(min_size=1, alphabet=st.characters(whitelist_categories=("Ll",))), st.integers()))
@settings(max_examples=100)
@pytest.mark.django_db
def test_eager_mode_writes_task_record(kwargs):
    ...
```

For any kwargs dict, with `LAMBDA_TASKS_EAGER=True`, calling `on_commit(**kwargs)` on a wrapper should result in a `TaskRecord` existing in the database with a matching `task_name` and `kwargs`, and `status` of `SUCCESS` or `FAILED`, before `on_commit` returns.

### Dual approach rationale

Unit tests catch concrete bugs in specific scenarios (admin registration, HTTP response codes, settings values). Property tests verify the universal behavioral guarantees (kwargs-only enforcement, EAGER synchrony) across a wide range of generated inputs. Both are necessary for comprehensive coverage.

**Property 4 test** — `tests/test_handler.py`

```python
# Feature: eager-mode-example-app, Property 4: Django is set up before any task executes in the Lambda handler
def test_django_setup_before_execute_task(monkeypatch):
    ...
```

Verify that when `handler.py` is imported with `DJANGO_SETTINGS_MODULE` set and `django.apps.apps.ready` is `False`, `django.setup()` is called before any invocation of `execute_task()`. This is an example-style test (not property-based) because the setup/ready state is a boolean condition, not a range of generated inputs. The test should:

1. Patch `django.apps.apps.ready` to `False` and `DJANGO_SETTINGS_MODULE` to a valid value
2. Patch `django.setup` to a spy
3. Patch `execute_task` to a spy
4. Invoke `handler(event, context)` with a minimal SQS event
5. Assert `django.setup` was called before `execute_task`
