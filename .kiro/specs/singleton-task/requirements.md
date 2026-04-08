# Requirements Document

## Introduction

Add a `singleton` option to the `@lambda_task` decorator that prevents concurrent execution of the same task. When enabled, the executor acquires a Redis lock (via Django's cache framework) before running the task. If the lock cannot be acquired, the task is retried automatically.

## Glossary

- **Decorator**: The `@lambda_task` decorator factory and the resulting `LambdaTaskWrapper` instance defined in `decorators.py`
- **Executor**: The `SQSLambdaTaskMessage.execute_immediately()` method in `models.py` that runs a task and manages its lifecycle
- **Singleton_Lock**: A Redis lock acquired via Django's cache framework to enforce single-concurrency for a task
- **Lock_Key**: The cache key used for the singleton lock, formatted as `lambda_tasks.singleton_lock.{task_name}`
- **LockError**: The exception raised by Django's Redis cache backend when a lock cannot be acquired (i.e. `django.core.cache.backends.redis.LockError` or the underlying `redis.exceptions.LockError`)
- **Cache_Backend**: The Django cache backend identified by the `LAMBDA_TASKS_SINGLETON_CACHE` setting, accessed via `django.core.cache.caches`

## Requirements

### Requirement 1: Decorator accepts singleton option

**User Story:** As a developer, I want to pass `singleton=True` to `@lambda_task`, so that I can declare a task as single-concurrency at definition time.

#### Acceptance Criteria

1. THE Decorator SHALL accept a `singleton` keyword argument of type `bool` with a default value of `False`
2. WHEN `singleton=True` is passed, THE Decorator SHALL store the value on the `LambdaTaskWrapper` instance and expose it via a `singleton` property
3. WHEN `singleton=False` is passed or the argument is omitted, THE Decorator SHALL store `False` and the task SHALL execute without acquiring a Singleton_Lock

### Requirement 2: Singleton lock acquisition

**User Story:** As a developer, I want singleton tasks to acquire a Redis lock before execution, so that only one instance of the task runs at a time.

#### Acceptance Criteria

1. WHEN the Executor runs a task with `singleton=True`, THE Executor SHALL attempt to acquire a Singleton_Lock from the Cache_Backend using Lock_Key `lambda_tasks.singleton_lock.{task_name}` before executing the task function
2. WHEN the Singleton_Lock is successfully acquired, THE Executor SHALL execute the task function while holding the lock
3. WHEN the task function completes (success or failure), THE Executor SHALL release the Singleton_Lock

### Requirement 3: Lock contention triggers retry

**User Story:** As a developer, I want a singleton task that cannot acquire its lock to be retried, so that the task eventually runs when the lock is released.

#### Acceptance Criteria

1. IF a LockError is raised during Singleton_Lock acquisition, THEN THE Executor SHALL treat the LockError as a retryable exception and re-enqueue the task via the existing retry mechanism
2. IF a LockError is raised and `n_retries` has reached `LAMBDA_TASKS_MAX_RETRIES`, THEN THE Executor SHALL raise `MaxRetriesExceededError` and record the task as `FAILED`
3. WHEN a LockError triggers a retry, THE Executor SHALL record the current `TaskRecord` with status `RETRYING` and the LockError traceback

### Requirement 4: Singleton cache setting

**User Story:** As a developer, I want to configure which Django cache backend is used for singleton locks, so that I can point it at the appropriate Redis instance.

#### Acceptance Criteria

1. THE `LambdaTasksSettings` SHALL expose a `SINGLETON_CACHE` property that reads from `django.conf.settings.LAMBDA_TASKS_SINGLETON_CACHE` with a default value of `"default"`
2. WHEN a singleton task is executed, THE Executor SHALL retrieve the cache backend using `django.core.cache.caches[SINGLETON_CACHE]`

### Requirement 5: Singleton is not serialized into SQS message

**User Story:** As a developer, I want the singleton option to remain a decorator-level concern, so that the SQS message schema stays unchanged.

#### Acceptance Criteria

1. THE `singleton` value SHALL be stored only on the `LambdaTaskWrapper` instance and SHALL NOT be included in the `SQSLambdaTaskMessage` schema
2. THE Executor SHALL read the `singleton` value from the resolved `LambdaTaskWrapper` at execution time, consistent with how `ignore_errors` and `retry_on` are handled
