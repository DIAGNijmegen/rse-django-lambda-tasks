# Requirements Document

## Introduction

Add a `retry_delay` parameter to the `@lambda_task` decorator that gives callers explicit control over the delay (in seconds) used when a task is automatically re-enqueued after a retryable failure.

As part of this feature, the call-time `_delay` override kwarg accepted by `execute_on_commit()` is being removed. The `product.md` steering doc currently documents `execute_on_commit()` as accepting `_delay` as a per-call override — this behaviour is being removed.

The two delay resolution paths are entirely separate:

- **Normal enqueue** (`execute_on_commit` called directly): decorator `delay` only (no call-time override)
- **Retry enqueue** (triggered by a retryable exception): `min(retry_delay + round(random.uniform(1, 5)), 900)` — jitter is always added, capped at 900

`retry_delay` is only meaningful when `retry_on` is also configured. Setting `retry_delay` without `retry_on` raises `TypeError` at decoration time.

Both `delay` and `retry_delay` are validated at decoration time against the SQS maximum `DelaySeconds` of 900 seconds.

## Glossary

- **Decorator**: The `@lambda_task` decorator factory defined in `decorators.py`.
- **LambdaTaskWrapper**: The object produced by applying `@lambda_task` to a function; stores all decorator-level configuration.
- **Executor**: `SQSLambdaTaskMessage.execute_immediately()` in `models.py`; runs the task and handles retries.
- **retry_delay**: The new decorator parameter — a non-negative integer (seconds) used as the base SQS `DelaySeconds` when enqueuing a retry. Jitter is always added on top. Not used for normal enqueues.
- **delay**: The existing decorator parameter — a non-negative integer (seconds) used as the SQS `DelaySeconds` for normal (non-retry) enqueues only.
- **Jitter**: Random delay of `round(random.uniform(1, 5))` seconds, always added to `retry_delay` when enqueuing a retry. The total is capped at 900.
- **retry_on**: The existing decorator parameter — a tuple of exception types that trigger automatic retry.
- **SQS_MAX_DELAY**: The SQS maximum allowed `DelaySeconds` value: 900 seconds.

## Requirements

### Requirement 1: retry_delay Decorator Parameter

**User Story:** As a task author, I want to set a dedicated retry delay on my task decorator, so that retries use a predictable delay independent of the normal enqueue delay.

#### Acceptance Criteria

1. THE Decorator SHALL accept a `retry_delay` keyword argument of type `int` with a default value of `0`.
2. THE LambdaTaskWrapper SHALL store the `retry_delay` value and expose it via a `retry_delay` property.
3. WHEN `retry_delay` is set to a non-zero value and `retry_on` is a non-empty tuple, THE Decorator SHALL construct the LambdaTaskWrapper without raising an exception.
4. IF `retry_delay` is non-zero and `retry_on` is an empty tuple or not provided, THEN THE Decorator SHALL raise a `TypeError` at decoration time.

### Requirement 2: Validation of delay and retry_delay at Decoration Time

**User Story:** As a task author, I want invalid delay values to be caught immediately when I define my task, so that misconfiguration is surfaced before any task is ever enqueued.

#### Acceptance Criteria

1. IF `delay` is less than `0` or greater than `900`, THEN THE Decorator SHALL raise a `ValueError` at decoration time.
2. IF `retry_delay` is less than `0` or greater than `900`, THEN THE Decorator SHALL raise a `ValueError` at decoration time.
3. WHEN `delay` is an integer in the inclusive range `[0, 900]`, THE Decorator SHALL accept it without raising an exception.
4. WHEN `retry_delay` is an integer in the inclusive range `[0, 900]`, THE Decorator SHALL accept it without raising an exception.

### Requirement 3: Retry Delay Resolution

**User Story:** As a task author, I want retries to use `retry_delay` when set, and fall back to jitter otherwise, so that retry timing is predictable when configured and safe when not.

#### Acceptance Criteria

1. WHEN a retryable exception is raised and `retry_delay` is non-zero, THE Executor SHALL enqueue the retry with `DelaySeconds` set to `retry_delay`.
2. WHEN a retryable exception is raised and `retry_delay` is zero, THE Executor SHALL enqueue the retry with `DelaySeconds` set to `round(random.uniform(1, 5))`.
3. WHEN a retryable exception is raised and `retry_delay` is zero, THE Executor SHALL produce a `DelaySeconds` value in the inclusive integer range `[1, 5]`.

### Requirement 4: Remove Call-Time _delay Override

**User Story:** As a library maintainer, I want to remove the `_delay` per-call override from `execute_on_commit()`, so that delay configuration is centralised on the decorator and the public API is simpler.

#### Acceptance Criteria

1. THE LambdaTaskWrapper SHALL NOT accept `_delay` as a kwarg in `execute_on_commit()`.
2. IF `_delay` is passed to `execute_on_commit()`, THEN THE LambdaTaskWrapper SHALL raise a `TypeError`.
3. WHEN `execute_on_commit` is called directly (not as a retry), THE LambdaTaskWrapper SHALL resolve `DelaySeconds` using only the decorator `delay` value.
4. THE LambdaTaskWrapper SHALL NOT use `retry_delay` when building a task for a non-retry enqueue.
