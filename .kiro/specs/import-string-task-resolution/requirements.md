# Requirements Document

## Introduction

This feature refactors the `lambda_tasks` library in two ways:

1. **Import-string task resolution**: Replace the global task registry (`registry.py`) with Django's `import_string` utility. Instead of registering wrappers in a dict at decoration time and looking them up in `executor.py`, the executor will call `django.utils.module_loading.import_string(message.task_name)` at execution time to resolve the wrapper directly.

2. **Validation in `LambdaTaskWrapper.__init__`**: Move `_validate_func` and `_validate_timeouts` calls into `LambdaTaskWrapper.__init__` so that validation (kwargs-only params, no underscore-prefixed params, timeout constraints) is enforced regardless of whether the user uses the `@lambda_task` decorator or constructs `LambdaTaskWrapper` directly.

## Glossary

- **LambdaTaskWrapper**: The class in `decorators.py` that wraps a callable to expose `__call__` and `on_commit`.
- **lambda_task**: The decorator factory in `decorators.py` that creates a `LambdaTaskWrapper`.
- **Registry**: The global dict in `registry.py` mapping fully-qualified task names to `LambdaTaskWrapper` instances.
- **import_string**: `django.utils.module_loading.import_string` — resolves a dotted Python path to the object it names.
- **task_name**: The fully-qualified dotted path `module.qualname` stored in `SQSLambdaTaskMessage` and used to identify a task.
- **Executor**: `executor.py`, specifically `execute_task()`.
- **Handler**: `handler.py`, the AWS Lambda entry point.
- **ConfigurationError**: The library-specific exception raised for invalid decorator or timeout configuration.
- **_validate_func**: Module-level function in `decorators.py` that checks kwargs-only and no-underscore-prefix constraints.
- **_validate_timeouts**: Module-level function in `decorators.py` that checks timeout bounds and ordering.

## Requirements

### Requirement 1: Import-string task resolution in executor

**User Story:** As a library maintainer, I want the executor to resolve tasks via `import_string` at execution time, so that tasks do not need to be pre-registered in a global dict before the handler runs.

#### Acceptance Criteria

1. WHEN `execute_task` is called with a `SQSLambdaTaskMessage`, THE Executor SHALL resolve the task wrapper by calling `django.utils.module_loading.import_string(message.task_name)`.
2. WHEN `import_string` raises `ImportError`, THE Executor SHALL propagate the exception to the caller without creating a `TaskRecord`.
3. THE Executor SHALL NOT import or reference `lambda_tasks.registry` for task resolution.
4. WHEN the object returned by `import_string` is a `LambdaTaskWrapper`, THE Executor SHALL use it to resolve timeouts and invoke the task.
5. WHEN the object returned by `import_string` is not a `LambdaTaskWrapper`, THE Executor SHALL raise `TypeError` with a descriptive message.

### Requirement 2: Registry removal

**User Story:** As a library maintainer, I want to remove the global task registry, so that the codebase has one fewer source of mutable global state.

#### Acceptance Criteria

1. THE System SHALL delete `lambda_tasks/registry.py`.
2. THE Handler SHALL NOT import `lambda_tasks.registry`.
3. THE Decorator SHALL NOT call `registry.register` after creating a `LambdaTaskWrapper`.
4. IF any module still imports from `lambda_tasks.registry`, THEN THE System SHALL raise `ImportError` at import time (i.e. the module no longer exists).

### Requirement 3: Validation moved into LambdaTaskWrapper.__init__

**User Story:** As a library user, I want `LambdaTaskWrapper` to validate its arguments on construction, so that invalid configurations are caught regardless of whether I use the decorator or construct the wrapper directly.

#### Acceptance Criteria

1. WHEN `LambdaTaskWrapper.__init__` is called with a function that has positional parameters, THE LambdaTaskWrapper SHALL raise `TypeError` with a message identifying the offending parameter.
2. WHEN `LambdaTaskWrapper.__init__` is called with a function that has a parameter whose name starts with `_`, THE LambdaTaskWrapper SHALL raise `TypeError` with a message identifying the offending parameter.
3. WHEN `LambdaTaskWrapper.__init__` is called with `soft_timeout` and `hard_timeout` both non-`None` and `soft_timeout >= hard_timeout`, THE LambdaTaskWrapper SHALL raise `ConfigurationError`.
4. WHEN `LambdaTaskWrapper.__init__` is called with `soft_timeout` greater than 900, THE LambdaTaskWrapper SHALL raise `ConfigurationError`.
5. WHEN `LambdaTaskWrapper.__init__` is called with `hard_timeout` greater than 900, THE LambdaTaskWrapper SHALL raise `ConfigurationError`.
6. WHEN `LambdaTaskWrapper.__init__` is called with a valid function and valid timeout values, THE LambdaTaskWrapper SHALL construct successfully without raising.

### Requirement 4: Decorator delegates validation to LambdaTaskWrapper

**User Story:** As a library maintainer, I want the `lambda_task` decorator to rely on `LambdaTaskWrapper.__init__` for validation, so that validation logic is not duplicated.

#### Acceptance Criteria

1. THE lambda_task decorator SHALL NOT call `_validate_func` or `_validate_timeouts` directly.
2. WHEN `lambda_task` is applied to a function with positional parameters, THE lambda_task decorator SHALL raise `TypeError` (propagated from `LambdaTaskWrapper.__init__`).
3. WHEN `lambda_task` is applied with invalid timeout values, THE lambda_task decorator SHALL raise `ConfigurationError` (propagated from `LambdaTaskWrapper.__init__`).
4. THE System SHALL retain `_validate_func` and `_validate_timeouts` as module-level functions (called from `__init__`) or inline their logic into `__init__` — either approach is acceptable provided the validation behaviour is unchanged.

### Requirement 5: Backward-compatible public API

**User Story:** As a library user, I want the public API of `LambdaTaskWrapper` and `lambda_task` to remain unchanged, so that existing task definitions and call sites require no modification.

#### Acceptance Criteria

1. THE LambdaTaskWrapper SHALL continue to expose `__call__`, `on_commit`, `__name__`, `__doc__`, and `__wrapped__`.
2. THE lambda_task decorator SHALL continue to accept `delay`, `soft_timeout`, `hard_timeout`, and `queue` keyword arguments.
3. WHEN `on_commit` is called with `_soft_timeout` or `_hard_timeout` overrides that are invalid, THE LambdaTaskWrapper SHALL raise `ConfigurationError` (existing behaviour preserved).
4. THE System SHALL preserve the `task_name` format `module.qualname` used in `SQSLambdaTaskMessage` and as the `import_string` path.

### Requirement 6: Test coverage

**User Story:** As a library maintainer, I want tests to cover the new resolution path and the moved validation, so that regressions are caught.

#### Acceptance Criteria

1. THE test suite SHALL include a test that verifies `execute_task` resolves a wrapper via `import_string` rather than the registry.
2. THE test suite SHALL include a test that verifies constructing `LambdaTaskWrapper` directly with a positional-arg function raises `TypeError`.
3. THE test suite SHALL include a test that verifies constructing `LambdaTaskWrapper` directly with invalid timeouts raises `ConfigurationError`.
4. THE test suite SHALL include a test that verifies `execute_task` raises `ImportError` (or propagates it) when `import_string` cannot resolve the task name.
5. THE test suite SHALL include a test that verifies `execute_task` raises `TypeError` when `import_string` returns an object that is not a `LambdaTaskWrapper`.
6. WHEN existing tests reference `lambda_tasks.registry`, THE test suite SHALL update those references to remove or replace them so all tests pass.
