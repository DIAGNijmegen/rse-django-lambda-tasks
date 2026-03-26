# Implementation Plan: eager-mode-example-app

## Overview

Implement EAGER mode support in the `lambda_tasks` library and add a self-contained example Django app that demonstrates it. Library changes are minimal and targeted; the example app is a thin Django project using SQLite with no AWS dependencies.

## Tasks

- [x] 1. Update `lambda_tasks/decorators.py` — EAGER path writes a TaskRecord
  - In `LambdaTaskWrapper.on_commit()`, replace the direct `self._func(**task_kwargs)` call in the EAGER branch with a `SQSLambdaTaskMessage` construction + `execute_task(message=message)` call
  - Import `uuid` at the top of the module (if not already present)
  - The `task_name`, `soft_timeout`, and `hard_timeout` locals are already resolved above the EAGER branch — use them directly
  - _Requirements: 2.4, 3.3_

  - [x] 1.1 Write property test for EAGER mode synchronous execution (Property 2)
    - **Property 2: EAGER mode executes tasks synchronously**
    - **Validates: Requirements 2.4**
    - Use `@given(st.dictionaries(st.text(min_size=1), st.integers()))` with `max_examples=100`
    - Patch `enqueuer.enqueue` to assert it is never called; assert the wrapped function was called with the exact kwargs before `on_commit()` returns

  - [x] 1.2 Write property test for EAGER mode TaskRecord creation (Property 3)
    - **Property 3: EAGER mode writes a TaskRecord**
    - **Validates: Requirements 2.4, 3.3**
    - Use `@given(st.dictionaries(st.text(min_size=1, alphabet=st.characters(whitelist_categories=("Ll",))), st.integers()))` with `max_examples=100`
    - Mark with `@pytest.mark.django_db`
    - Assert a `TaskRecord` with matching `task_name` and `kwargs` exists with `status` in `{SUCCESS, FAILED}` after `on_commit()` returns

- [x] 2. Create `lambda_tasks/admin.py` — register TaskRecord with Django admin
  - Create the file with a `TaskRecordAdmin(ModelAdmin)` subclass using `@admin.register(TaskRecord)`
  - Set `list_display = ("task_name", "status", "start_time", "end_time", "result")`
  - Set `readonly_fields` to all `TaskRecord` fields
  - _Requirements: 3.1, 3.3_

  - [x] 2.1 Write unit tests for admin registration
    - In `tests/test_admin.py`, assert `TaskRecord` is in `admin.site._registry`
    - Assert `list_display` contains `task_name`, `status`, `start_time`, `end_time`, `result`
    - _Requirements: 3.1, 3.3_

- [x] 3. Update `lambda_tasks/handler.py` — add `django.setup()` guard
  - Add `import os`, `import django`, and `from django.apps import apps as django_apps` at the top
  - After the imports, add the module-level guard: `if os.environ.get("DJANGO_SETTINGS_MODULE") and not django_apps.ready: django.setup()`
  - _Requirements: Lambda deployment correctness (design §3)_

  - [x] 3.1 Write unit test for django.setup() guard (Property 4)
    - **Property 4: Django is set up before any task executes in the Lambda handler**
    - **Validates: Lambda deployment correctness**
    - In `tests/test_handler.py`, monkeypatch `django.apps.apps.ready` to `False`, `DJANGO_SETTINGS_MODULE` to a non-empty string, spy on `django.setup` and `execute_task`
    - Invoke `handler()` with a minimal SQS event and assert `django.setup` was called before `execute_task`

- [x] 4. Checkpoint — ensure all library-side tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Create the example Django project skeleton
  - Create `example/manage.py` — standard Django manage entry point pointing at `example_project.settings`
  - Create `example/example_project/__init__.py` (empty)
  - Create `example/example_project/wsgi.py` — standard WSGI entry point
  - Create `example/example_project/urls.py` — include `admin.site.urls` and delegate to `example_app.urls`
  - _Requirements: 1.1, 1.2_

- [x] 6. Create `example/example_project/settings.py`
  - Set `INSTALLED_APPS` to include `django.contrib.admin`, `django.contrib.auth`, `django.contrib.contenttypes`, `django.contrib.sessions`, `django.contrib.messages`, `django.contrib.staticfiles`, `lambda_tasks`, `example_app`
  - Set `DATABASES` to SQLite at `BASE_DIR / "db.sqlite3"`
  - Set `LAMBDA_TASKS_EAGER = True`
  - Include `MIDDLEWARE`, `TEMPLATES` (with `django.contrib.auth.context_processors.auth` and `django.contrib.messages.context_processors.messages`), `STATIC_URL`, `DEFAULT_AUTO_FIELD`, and a `SECRET_KEY`
  - _Requirements: 1.3, 1.4, 1.5_

  - [ ]* 6.1 Write structural tests for example app settings
    - In `tests/test_example_app.py`, import `example.example_project.settings` directly and assert `lambda_tasks` in `INSTALLED_APPS`, `DATABASES["default"]["ENGINE"]` is SQLite, and `LAMBDA_TASKS_EAGER` is `True`
    - _Requirements: 1.3, 1.4, 1.5_

- [x] 7. Create `example/example_app/` — app, task, and view
  - Create `example/example_app/__init__.py` (empty)
  - Create `example/example_app/apps.py` — `ExampleAppConfig` with `name = "example_app"`
  - Create `example/example_app/tasks.py` — `greet(*, name: str) -> str` decorated with `@lambda_task`, returning `f"Hello, {name}!"`
  - Create `example/example_app/views.py` — `trigger(request)` view that reads `name` from `request.GET`, calls `greet.on_commit(name=name)`, and returns `HttpResponse`
  - Create `example/example_app/urls.py` — maps `GET /trigger/` to the `trigger` view
  - _Requirements: 2.1, 2.2, 2.3, 2.5_

  - [ ]* 7.1 Write property test for all registered tasks using keyword-only arguments (Property 1)
    - **Property 1: All registered tasks use keyword-only arguments**
    - **Validates: Requirements 2.2**
    - In `tests/test_example_app.py`, import `example_app.tasks` to populate the registry, then for each registered wrapper assert no parameter has kind `POSITIONAL_ONLY` or `POSITIONAL_OR_KEYWORD`

  - [ ]* 7.2 Write structural test for the trigger view
    - In `tests/test_example_app.py`, use Django's `RequestFactory` to call `trigger(request)` with `LAMBDA_TASKS_EAGER=True` and assert the response status is 200
    - _Requirements: 2.5_

- [x] 8. Create `example/README.md`
  - Document all commands using `uv run` as the prefix: `migrate`, `createsuperuser`, `runserver`
  - Include the URL to trigger the task (`/trigger/?name=Alice`) and the admin URL (`/admin/`)
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

- [x] 9. Final checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
