# Tasks: import-string-task-resolution

## Task List

- [x] 1. Move validation into `LambdaTaskWrapper.__init__`
  - [x] 1.1 Add `_validate_func(func=func)` and `_validate_timeouts(soft_timeout=soft_timeout, hard_timeout=hard_timeout)` calls at the top of `LambdaTaskWrapper.__init__`, before `functools.update_wrapper`
  - [x] 1.2 Remove the explicit `_validate_func` and `_validate_timeouts` calls from the `_decorate` inner function inside `lambda_task`

- [x] 2. Replace registry lookup with `import_string` in `executor.py`
  - [x] 2.1 Add `from django.utils.module_loading import import_string` and `from lambda_tasks.decorators import LambdaTaskWrapper` imports to `executor.py`
  - [x] 2.2 Replace `from lambda_tasks import registry` and `registry.get(name=message.task_name)` with `import_string(message.task_name)` followed by an `isinstance(wrapper, LambdaTaskWrapper)` guard that raises `TypeError` on mismatch; move this resolution before `TaskRecord.objects.create`
  - [x] 2.3 Remove the `from lambda_tasks import registry` import from `handler.py`

- [x] 3. Remove the registry
  - [x] 3.1 Remove the `registry.register(...)` call from the `_decorate` inner function in `lambda_task`
  - [x] 3.2 Delete `lambda_tasks/registry.py`

- [x] 4. Update tests for `executor.py`
  - [x] 4.1 Replace all module-level task helpers in `test_executor.py` that are defined locally (inside the test module) with module-level `@lambda_task` decorated functions so `import_string` can resolve them; remove any manual `registry.register` calls
  - [x] 4.2 Add a unit test: `execute_task` with `import_string` patched to raise `ImportError` → exception propagates and no `TaskRecord` is created (covers Requirement 1.2, Property 2)
  - [x] 4.3 Add a unit test: `execute_task` with `import_string` patched to return a plain function → `TypeError` is raised (covers Requirement 1.5, Property 3)
  - [x] 4.4 Add a property-based test (≥100 iterations): for any non-`LambdaTaskWrapper` object returned by `import_string`, `execute_task` raises `TypeError` (Property 3; tag: `Feature: import-string-task-resolution, Property 3`)
  - [x] 4.5 Add a property-based test (≥100 iterations): for any unresolvable `task_name`, `execute_task` propagates `ImportError` and leaves no `TaskRecord` (Property 2; tag: `Feature: import-string-task-resolution, Property 2`)

- [x] 5. Update tests for `decorators.py`
  - [x] 5.1 Add a unit test: `LambdaTaskWrapper(positional_func)` raises `TypeError` (covers Requirement 3.1, Property 5)
  - [x] 5.2 Add a unit test: `LambdaTaskWrapper(underscore_param_func)` raises `TypeError` (covers Requirement 3.2, Property 6)
  - [x] 5.3 Add a unit test: `LambdaTaskWrapper(func, soft_timeout=100, hard_timeout=50)` raises `ConfigurationError` (covers Requirement 3.3, Property 7)
  - [x] 5.4 Add a property-based test (≥100 iterations): for any function with ≥1 positional parameters, `LambdaTaskWrapper(func)` raises `TypeError` (Property 5; tag: `Feature: import-string-task-resolution, Property 5`)
  - [x] 5.5 Add a property-based test (≥100 iterations): for any function with a `_`-prefixed kwonly parameter, `LambdaTaskWrapper(func)` raises `TypeError` (Property 6; tag: `Feature: import-string-task-resolution, Property 6`)
  - [x] 5.6 Add a property-based test (≥100 iterations): for any invalid (soft, hard) pair (soft ≥ hard or either > 900), `LambdaTaskWrapper(func, ...)` raises `ConfigurationError` (Property 7; tag: `Feature: import-string-task-resolution, Property 7`)
  - [x] 5.7 Add a property-based test (≥100 iterations): for any valid (func, soft, hard), the wrapper constructs successfully and exposes `__call__`, `on_commit`, `__name__`, `__doc__`, `__wrapped__` (Property 8; tag: `Feature: import-string-task-resolution, Property 8`)
  - [x] 5.8 Remove or update any test in `test_decorator.py` that calls `registry.get` or `registry.register` directly (covers Requirement 6.6)

- [x] 6. Add registry-deletion smoke test
  - [x] 6.1 Add a unit test (in `test_decorator.py` or a new `test_registry.py`) that asserts `import lambda_tasks.registry` raises `ImportError` (covers Requirement 2.1 / 2.4, Property 4)

- [x] 7. Add import_string round-trip example test
  - [x] 7.1 Add an example-based test that imports a known module-level `@lambda_task` function, calls `import_string(task_name)`, and asserts the result is the same wrapper object (covers Requirement 1.1, Property 1)
