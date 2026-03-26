# Requirements Document

## Introduction

Add a self-contained example Django application to the repository that demonstrates how to use `django-lambda-tasks` with `LAMBDA_TASKS_EAGER = True`. In EAGER mode tasks execute synchronously in-process instead of being enqueued to SQS, making the library usable for local development without any AWS infrastructure. The example app must run with no extra dependencies beyond those already declared in `pyproject.toml`, and must include clear instructions for running it locally.

## Glossary

- **Example_App**: The Django project located at `example/` that demonstrates EAGER mode usage.
- **Background_Tasks**: The `lambda_tasks` library package being demonstrated.
- **EAGER_Mode**: The operating mode enabled by `LAMBDA_TASKS_EAGER = True` in Django settings, where `on_commit()` executes the task function synchronously instead of publishing to SQS.
- **Task**: A Python function decorated with `@lambda_task` and registered in the task registry.
- **TaskRecord**: The Django model in `lambda_tasks.models` that persists task execution state.
- **Developer**: A person running the example app locally to evaluate or develop the library.

## Requirements

### Requirement 1: Example App Structure

**User Story:** As a Developer, I want a minimal Django project in the repository that uses `lambda_tasks`, so that I can see a working integration without setting up a full project myself.

#### Acceptance Criteria

1. THE Example_App SHALL be located under an `example/` directory at the repository root.
2. THE Example_App SHALL be a valid Django project containing at minimum: a `manage.py` entry point, a Django settings module, a URL configuration, and at least one Django app with a `tasks.py` file.
3. THE Example_App SHALL declare `lambda_tasks` in `INSTALLED_APPS`.
4. THE Example_App SHALL use `django.db.backends.sqlite3` as its database backend so no external database is required.
5. THE Example_App SHALL set `LAMBDA_TASKS_EAGER = True` in its settings module.
6. THE Example_App SHALL NOT require any Python package that is not already listed as a dependency in `pyproject.toml`.

### Requirement 2: Demonstrated Task

**User Story:** As a Developer, I want the example app to define and invoke at least one background task, so that I can observe the full lifecycle of a task in EAGER mode.

#### Acceptance Criteria

1. THE Example_App SHALL define at least one Task using the `@lambda_task` decorator in a `tasks.py` file.
2. THE Task SHALL use keyword-only arguments, consistent with the library's enforcement of kwargs-only signatures.
3. WHEN a Developer triggers the Task via an HTTP endpoint, THE Example_App SHALL invoke `task.on_commit()` so that the EAGER mode path in `LambdaTaskWrapper` is exercised.
4. WHEN the Task executes in EAGER mode, THE Background_Tasks library SHALL call the task function synchronously within the same process and request cycle.
5. WHEN the Task completes, THE Example_App SHALL return an HTTP response that confirms the task was triggered.

### Requirement 3: Task Result Visibility

**User Story:** As a Developer, I want to see the TaskRecord created by the example task, so that I can verify the task executed and inspect its outcome.

#### Acceptance Criteria

1. THE Background_Tasks library SHALL register `TaskRecord` with the Django admin in `lambda_tasks/admin.py`, so that the admin integration is available to any project that includes `lambda_tasks` in `INSTALLED_APPS`.
2. THE Example_App SHALL expose the Django admin interface, relying on the registration provided by Background_Tasks, so that a Developer can inspect task execution records via a browser without any additional admin configuration in the example app.
3. WHEN a Developer visits the admin interface, THE Example_App SHALL display `TaskRecord` entries including `task_name`, `status`, `start_time`, `end_time`, and `result`.

### Requirement 4: Local Run Instructions

**User Story:** As a Developer, I want step-by-step instructions for running the example app locally, so that I can get it working without guessing at the setup.

#### Acceptance Criteria

1. THE Example_App SHALL include a `README.md` inside the `example/` directory with instructions for running the app locally.
2. THE README SHALL document all commands required to set up and start the app, using `uv run` as the command prefix consistent with the project's use of `uv`.
3. THE README SHALL include the command to apply database migrations before starting the server.
4. THE README SHALL include the command to create a Django superuser so the Developer can access the admin interface.
5. THE README SHALL specify the URL a Developer should visit to trigger the example task.
6. THE README SHALL specify the URL a Developer should visit to view task records in the admin interface.
7. IF a Developer follows the README instructions in order, THEN THE Example_App SHALL start successfully and the example task SHALL be triggerable without additional configuration.
