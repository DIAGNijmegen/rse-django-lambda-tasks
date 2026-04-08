# Design Document: retry-delay

## Overview

This feature adds a `retry_delay` parameter to the `@lambda_task` decorator, giving task authors explicit control over the SQS `DelaySeconds` used when a task is automatically re-enqueued after a retryable failure.

As part of this change, the call-time `_delay` override kwarg accepted by `execute_on_commit()` is removed. Delay configuration is centralised on the decorator — there are no per-call overrides.

The two delay resolution paths remain entirely separate after this change:

- **Normal enqueue** (`execute_on_commit` called directly by application code): uses `self._delay` from the decorator.
- **Retry enqueue** (triggered by a retryable exception in the executor): uses `min(wrapper.retry_delay + round(random.uniform(1, 5)), 900)` — jitter is always added, capped at 900.

Both `delay` and `retry_delay` are validated at decoration time against the SQS maximum `DelaySeconds` of 900 seconds. `retry_delay` is additionally validated to require a non-empty `retry_on` tuple when non-zero.

## Architecture

No new modules are introduced. Changes are confined to three files:

```
lambda_tasks/
  decorators.py   — add retry_delay param, validation, property; remove _delay pop in _build_task
  models.py       — update retry path to use wrapper.retry_delay; remove _delay kwarg from retry enqueue
.kiro/steering/
  product.md      — update Enqueuing section and retry_on retry delay description
```

The flow after this change:

```
execute_on_commit(**kwargs)
  → _build_task(kwargs)          # pops only _n_retries; uses self._delay directly
  → SQSLambdaTask(delay=self._delay, ...)

execute_immediately() retry path
  → delay = wrapper.retry_delay if wrapper.retry_delay != 0 else round(random.uniform(1, 5))
  → SQSLambdaTask(message=..., delay=delay, queue=wrapper.queue).execute_on_commit()
```

## Components and Interfaces

### `LambdaTaskWrapper.__init__` (decorators.py)

Add `retry_delay: int = 0` to the parameter list. Call two new validation methods before `functools.update_wrapper`. Store as `self._retry_delay`.

```python
def __init__(
    self,
    func: Callable[..., Any],
    *,
    delay: int = 0,
    retry_delay: int = 0,
    soft_timeout: int | None = None,
    hard_timeout: int | None = None,
    queue: str = "default",
    ignore_errors: tuple[type[BaseException], ...] = (),
    retry_on: tuple[type[BaseException], ...] = (),
) -> None:
    self._validate_func(func=func)
    self._validate_timeouts(soft_timeout=soft_timeout, hard_timeout=hard_timeout)
    self._validate_delay(delay=delay)
    self._validate_retry_delay(retry_delay=retry_delay, retry_on=retry_on)
    self._validate_ignore_errors(ignore_errors=ignore_errors)
    self._validate_retry_on(retry_on=retry_on)
    self._validate_no_overlap(retry_on=retry_on, ignore_errors=ignore_errors)
    ...
    self._retry_delay = retry_delay
```

### `LambdaTaskWrapper.retry_delay` property (decorators.py)

Expose `_retry_delay` as a read-only property, mirroring the existing `retry_on` and `ignore_errors` properties:

```python
@property
def retry_delay(self) -> int:
    """Delay in seconds used when enqueuing a retry. 0 means use jitter."""
    return self._retry_delay
```

### `LambdaTaskWrapper._validate_delay` (decorators.py)

New static method. Validates `delay` is in `[0, 900]`:

```python
@staticmethod
def _validate_delay(*, delay: int) -> None:
    if delay < 0 or delay > 900:
        raise ValueError(
            f"delay ({delay}) must be in the range [0, 900] (SQS maximum DelaySeconds)."
        )
```

### `LambdaTaskWrapper._validate_retry_delay` (decorators.py)

New static method. Validates `retry_delay` is in `[0, 900]` and, if non-zero, that `retry_on` is non-empty:

```python
@staticmethod
def _validate_retry_delay(
    *, retry_delay: int, retry_on: tuple[type[BaseException], ...]
) -> None:
    if retry_delay < 0 or retry_delay > 900:
        raise ValueError(
            f"retry_delay ({retry_delay}) must be in the range [0, 900] (SQS maximum DelaySeconds)."
        )
    if retry_delay != 0 and not retry_on:
        raise TypeError(
            "retry_delay is only meaningful when retry_on is non-empty. "
            "Either set retry_on or remove retry_delay."
        )
```

### `LambdaTaskWrapper._build_task` (decorators.py)

Remove the `_delay` pop. Use `self._delay` directly. Only `_n_retries` is still popped from kwargs:

```python
def _build_task(self, *, kwargs: dict[str, Any]) -> SQSLambdaTask:
    n_retries = kwargs.pop("_n_retries", 0)

    self._kwargs_model.model_validate(kwargs)

    message = SQSLambdaTaskMessage(
        task_name=f"{self._func.__module__}.{self._func.__qualname__}",
        kwargs=dict(kwargs),
        n_retries=n_retries,
    )

    return SQSLambdaTask(
        message=message,
        delay=self._delay,
        queue=self._queue,
    )
```

The docstring for `_build_task`, `serialize`, and `execute_on_commit` must also drop all references to `_delay`.

### `lambda_task` decorator factory (decorators.py)

Add `retry_delay: int = 0` to both `@overload` signatures and the implementation. Pass it through to `LambdaTaskWrapper`:

```python
def lambda_task(
    func: Callable[..., Any] | None = None,
    *,
    delay: int = 0,
    retry_delay: int = 0,
    soft_timeout: int | None = None,
    hard_timeout: int | None = None,
    queue: str = "default",
    ignore_errors: tuple[type[BaseException], ...] = (),
    retry_on: tuple[type[BaseException], ...] = (),
) -> LambdaTaskWrapper | Callable[[Callable[..., Any]], LambdaTaskWrapper]:
    def _decorate(f: Callable[..., Any]) -> LambdaTaskWrapper:
        return LambdaTaskWrapper(
            f,
            delay=delay,
            retry_delay=retry_delay,
            ...
        )
    ...
```

### `SQSLambdaTaskMessage.execute_immediately` retry path (models.py)

Replace the `wrapper._delay` reference with `wrapper.retry_delay`. Remove `_delay=delay` from the `execute_on_commit` call — `_build_task` no longer accepts it:

```python
# Before:
delay = wrapper._delay if wrapper._delay != 0 else round(random.uniform(1, 5))
wrapper.execute_on_commit(**self.kwargs, _delay=delay, _n_retries=self.n_retries + 1)

# After:
delay = wrapper.retry_delay if wrapper.retry_delay != 0 else round(random.uniform(1, 5))
wrapper.execute_on_commit(**self.kwargs, _n_retries=self.n_retries + 1)
```

The `delay` local variable is still computed and used — it must be passed to `_build_task` via the retry enqueue. Since `_build_task` now uses `self._delay` directly for normal enqueues, the retry path needs a different mechanism to inject the computed delay.

**Revised approach:** The retry enqueue cannot use `execute_on_commit` directly if `_delay` is removed, because `execute_on_commit` always uses `self._delay`. Instead, the retry path in `execute_immediately` should build the `SQSLambdaTask` directly and call `execute_on_commit` on it:

```python
delay = wrapper.retry_delay if wrapper.retry_delay != 0 else round(random.uniform(1, 5))
retry_task = SQSLambdaTask(
    message=SQSLambdaTaskMessage(
        task_name=self.task_name,
        kwargs=self.kwargs,
        n_retries=self.n_retries + 1,
    ),
    delay=delay,
    queue=wrapper._queue,
)
retry_task.execute_on_commit()
```

This avoids any need for a `_delay` override kwarg and keeps `_build_task` clean. The `wrapper._queue` access uses the private attribute — this is acceptable since `execute_immediately` is part of the same library and `_queue` has no public property. Alternatively, expose a `queue` property on `LambdaTaskWrapper` (preferred for clarity):

```python
@property
def queue(self) -> str:
    """The SQS queue name this task is routed to."""
    return self._queue
```

Then the retry path becomes:

```python
delay = wrapper.retry_delay if wrapper.retry_delay != 0 else round(random.uniform(1, 5))
retry_task = SQSLambdaTask(
    message=SQSLambdaTaskMessage(
        task_name=self.task_name,
        kwargs=self.kwargs,
        n_retries=self.n_retries + 1,
    ),
    delay=delay,
    queue=wrapper.queue,
)
retry_task.execute_on_commit()
```

## Data Models

No changes to `SQSLambdaTaskMessage`, `SQSLambdaTask`, or `TaskRecord` schemas. `retry_delay` is a decorator-level configuration value stored on `LambdaTaskWrapper` — it is never serialised into the SQS message.

`LambdaTaskWrapper` gains one new stored attribute:

| Attribute | Type | Description |
|---|---|---|
| `_retry_delay` | `int` | Seconds to delay a retry enqueue. 0 = use jitter. |

Exposed via the `retry_delay` property.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: retry_delay storage round-trip

*For any* integer `retry_delay` in `[0, 900]` (with `retry_on` non-empty when `retry_delay > 0`), constructing a `LambdaTaskWrapper` and reading `wrapper.retry_delay` SHALL return the same value that was passed in.

**Validates: Requirements 1.2**

### Property 2: retry_delay requires retry_on

*For any* integer `retry_delay` in `[1, 900]`, constructing a `LambdaTaskWrapper` with an empty `retry_on` tuple SHALL raise `TypeError`.

**Validates: Requirements 1.4**

### Property 3: Out-of-range delay and retry_delay raise ValueError

*For any* integer `delay` outside `[0, 900]`, constructing a `LambdaTaskWrapper` SHALL raise `ValueError`. Likewise, *for any* integer `retry_delay` outside `[0, 900]`, constructing a `LambdaTaskWrapper` SHALL raise `ValueError`.

**Validates: Requirements 2.1, 2.2**

### Property 4: Non-zero retry_delay is used in the retry enqueue

*For any* `retry_delay` in `[1, 900]` and any retryable exception, the `SQSLambdaTask` enqueued by the retry path SHALL have `delay == retry_delay`.

**Validates: Requirements 3.1**

### Property 5: Zero retry_delay produces jitter in [1, 5]

*For any* execution where `retry_delay` is `0` and a retryable exception is raised, the `delay` on the enqueued retry `SQSLambdaTask` SHALL be an integer in the inclusive range `[1, 5]`.

**Validates: Requirements 3.2, 3.3**

### Property 6: Normal enqueue uses decorator delay

*For any* `delay` in `[0, 900]`, calling `execute_on_commit` directly (not via the retry path) SHALL produce a `SQSLambdaTask` with `delay` equal to the decorator-configured value.

**Validates: Requirements 4.3, 4.4**

## Error Handling

| Condition | Error | Raised at |
|---|---|---|
| `delay < 0` or `delay > 900` | `ValueError` | Decoration time |
| `retry_delay < 0` or `retry_delay > 900` | `ValueError` | Decoration time |
| `retry_delay != 0` and `retry_on == ()` | `TypeError` | Decoration time |
| `_delay` passed to `execute_on_commit()` | `TypeError` (from pydantic `extra="forbid"` on the kwargs model, or from `_build_task` rejecting it) | Call time |

The `_delay` rejection at call time is a natural consequence of removing the `kwargs.pop("_delay", ...)` line from `_build_task`. If `_delay` is passed, it will reach `self._kwargs_model.model_validate(kwargs)` and be rejected by the `extra="forbid"` Pydantic config with a `ValidationError`. This is acceptable — the public contract says `_delay` is no longer supported, and the error message from Pydantic is clear enough. No special handling is needed.

## Testing Strategy

Tests use `pytest` and `hypothesis` (already present in the project).

**Unit tests** (`tests/test_decorators.py`):
- `retry_delay` defaults to `0`
- `retry_delay` is stored and returned via the property
- `retry_delay=0` with empty `retry_on` is accepted (default case)
- `retry_delay > 0` with non-empty `retry_on` is accepted
- `retry_delay > 0` with empty `retry_on` raises `TypeError`
- `delay < 0` raises `ValueError`; `delay > 900` raises `ValueError`
- `retry_delay < 0` raises `ValueError`; `retry_delay > 900` raises `ValueError`
- `delay=0` and `delay=900` are accepted (boundary values)
- `_delay` passed to `execute_on_commit` raises (via Pydantic `ValidationError`)
- `lambda_task` decorator factory passes `retry_delay` through correctly

**Unit tests** (`tests/test_models.py`):
- Retry path with non-zero `retry_delay` enqueues with that exact delay
- Retry path with `retry_delay=0` enqueues with a delay in `[1, 5]`
- Normal `execute_on_commit` uses decorator `delay`, not `retry_delay`

**Property-based tests** (using `hypothesis`, in `tests/test_decorators.py` and `tests/test_models.py`):

Each property test runs a minimum of 100 iterations.

- **Feature: retry-delay, Property 1**: `@given(st.integers(min_value=0, max_value=900))` — verify `wrapper.retry_delay == input`
- **Feature: retry-delay, Property 2**: `@given(st.integers(min_value=1, max_value=900))` — verify `TypeError` raised with empty `retry_on`
- **Feature: retry-delay, Property 3**: `@given(st.integers().filter(lambda x: x < 0 or x > 900))` — verify `ValueError` for both `delay` and `retry_delay`
- **Feature: retry-delay, Property 4**: `@given(st.integers(min_value=1, max_value=900))` — mock retry enqueue, verify `task.delay == retry_delay`
- **Feature: retry-delay, Property 5**: `@given(st.integers(min_value=0, max_value=0))` with repeated sampling — verify jitter delay in `[1, 5]`
- **Feature: retry-delay, Property 6**: `@given(st.integers(min_value=0, max_value=900))` — mock enqueue, verify `task.delay == decorator delay`
