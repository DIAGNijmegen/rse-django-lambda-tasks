# Implementation Tasks

## Tasks

- [x] 1. Fix `apps.py` — remove misleading comment and unnecessary import
  - [x] 1.1 Write a test confirming that applying `@lambda_task` registers the task in the registry (no `ready()` call involved) — this should already pass, confirming the decorator handles registration
  - [x] 1.2 Write a test asserting `BackgroundTasksConfig.ready()` does not import `lambda_tasks.registry` (i.e. the method body is empty or absent) — this will fail red
  - [x] 1.3 Remove the `import lambda_tasks.registry` line and the misleading comment from `ready()` — make 1.2 go green

- [x] 2. Rename `conf.py` → `settings.py` and remove `DEFAULT_DELAY`
  - [x] 2.1 Write a test asserting `from lambda_tasks.settings import LambdaTasksSettings` succeeds — red
  - [x] 2.2 Write a test asserting `LambdaTasksSettings()` has no `DEFAULT_DELAY` attribute — red
  - [x] 2.3 Rename `lambda_tasks/conf.py` to `lambda_tasks/settings.py` — makes 2.1 green
  - [x] 2.4 Remove the `DEFAULT_DELAY` property from `LambdaTasksSettings` — makes 2.2 green
  - [x] 2.5 Update all internal imports in `decorators.py`, `enqueuer.py`, and `executor.py` from `lambda_tasks.conf` to `lambda_tasks.settings`
  - [x] 2.6 Replace the `conf.DEFAULT_DELAY` reference in `LambdaTaskWrapper.on_commit()` with the literal `0`
  - [x] 2.7 Rename `tests/test_conf.py` to `tests/test_settings.py` and update its imports

- [x] 3. Add `LAMBDA_TASKS_EAGER` mode
  - [x] 3.1 Write a property-based test: when `LAMBDA_TASKS_EAGER=True`, calling `.on_commit()` executes the task synchronously and `boto3.client` is never called — red
  - [x] 3.2 Write a test: when `LAMBDA_TASKS_EAGER=False` (default), `.on_commit()` registers a `transaction.on_commit` callback and does not call the function directly — confirm still green after implementation
  - [x] 3.3 Add `EAGER` property to `LambdaTasksSettings` reading `LAMBDA_TASKS_EAGER` (default `False`)
  - [x] 3.4 In `LambdaTaskWrapper.on_commit()`, check `conf.EAGER`; if `True`, call `self._func(**task_kwargs)` directly and return — makes 3.1 green

- [x] 4. Cap timeouts at 900 seconds
  - [x] 4.1 Write property-based tests (using `hypothesis`): any `soft_timeout` or `hard_timeout` value `> 900` raises `ConfigurationError` at decoration time — red
  - [x] 4.2 Write property-based tests: same cap enforced at `.on_commit()` time via override kwargs — red
  - [x] 4.3 Write property-based tests: same cap enforced via `LambdaTasksSettings._resolve_timeouts()` when global settings exceed 900 — red
  - [x] 4.4 Write regression tests: valid timeout pairs (both `≤ 900`, soft `<` hard) continue to work — green baseline
  - [x] 4.5 Add `_MAX_TIMEOUT = 900` constant in `decorators.py` and extend `_validate_timeouts()` to raise `ConfigurationError` when any non-`None` value exceeds it — makes 4.1 and 4.2 green
  - [x] 4.6 Extend `LambdaTasksSettings._resolve_timeouts()` to raise `ConfigurationError` when resolved soft or hard timeout exceeds 900 — makes 4.3 green

- [x] 5. Enforce kwargs-only signatures across all library functions
  - [x] 5.1 Write a property-based test using `inspect.signature` that iterates every non-dunder callable in `lambda_tasks.*` and asserts all parameters are `KEYWORD_ONLY` — red
  - [x] 5.2 Add `*` before the first parameter in every non-dunder function and method in `settings.py`, `decorators.py`, `enqueuer.py`, `executor.py`, `handler.py`, `serializer.py`, `timeouts.py`, and `registry.py` — makes 5.1 green

- [x] 6. Add `ty` strict type checking and fix all type errors
  - [x] 6.1 Run `uv add --dev ty` to add `ty` as a dev dependency
  - [x] 6.2 Add `[tool.ty]` section to `pyproject.toml` with `strict = true`
  - [x] 6.3 Run `uv run ty check` — observe all errors (red baseline)
  - [x] 6.4 Fix all reported errors: annotate `_previous_handler` in `TimeoutContext` as `signal.Handlers | Callable[..., None]`, annotate `frame` parameters as `types.FrameType | None`, type bare `dict` as `dict[str, Any]`, add missing return types throughout
  - [x] 6.5 Run `uv run ty check` again and confirm zero errors — green

- [x] 7. Update `README.md` with full developer documentation
  - [x] 7.1 Remove any mention of importing task modules in `AppConfig.ready()`
  - [x] 7.2 Add `LAMBDA_TASKS_EAGER` to the settings reference table and add an eager mode section explaining local development usage
  - [x] 7.3 Document the 900s timeout cap in the timeout section and the error handling table
  - [x] 7.4 Verify all code examples use kwargs-only signatures
