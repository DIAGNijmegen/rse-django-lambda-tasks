# Design Document

## Overview

This document describes the targeted changes required to fix seven issues in the `django-lambda-tasks` library. Each fix is scoped to the minimum change needed; no architectural changes are required.

---

## Bug 1 — Remove misleading comment from `apps.py`

### Analysis

`apps.py` currently contains a comment instructing users to import their task modules in `AppConfig.ready()`. This is misleading because `@lambda_task` already registers tasks at decoration time (via `registry.register()` inside `_decorate`). The comment implies a manual step that is not needed.

The `import lambda_tasks.registry` line in `ready()` is also unnecessary — the registry module is a plain dict and does not need explicit initialisation.

### Change

Remove the misleading comment and the unnecessary registry import from `BackgroundTasksConfig.ready()`. The method body can be left empty (or the method removed entirely, since `AppConfig.ready()` is a no-op by default).

```python
class LambdaTasksConfig(AppConfig):
    name = "lambda_tasks"
    verbose_name = "Lambda Tasks"
```

---

## Bug 2 — Remove `DEFAULT_DELAY` from `LambdaTasksSettings`

### Analysis

`LambdaTasksSettings.DEFAULT_DELAY` reads `LAMBDA_TASKS_DEFAULT_DELAY` from Django settings. Delay is always `0` in practice and exposing it as a configurable setting adds unnecessary surface area. The decorator already defaults `delay=0` and `on_commit` already defaults to the wrapper's `_delay`.

### Change

- Remove the `DEFAULT_DELAY` property from `LambdaTasksSettings`.
- Remove the `conf.DEFAULT_DELAY` reference in `LambdaTaskWrapper.on_commit()` — replace with the literal `0`.

```python
# decorators.py — on_commit resolution
delay = override_delay if override_delay is not None else (self._delay if self._delay is not None else 0)
```

---

## Bug 3 — Rename `conf.py` → `settings.py`

### Analysis

Django convention names app-level settings helpers `settings.py`. The current `conf.py` name is inconsistent with this convention.

### Change

- Rename `lambda_tasks/conf.py` → `lambda_tasks/settings.py`.
- Update all internal imports from `lambda_tasks.conf` → `lambda_tasks.settings`:
  - `decorators.py`
  - `enqueuer.py`
  - `executor.py`
- The public class name `LambdaTasksSettings` and the module-level alias `conf` (if any) remain unchanged.

---

## Bug 4 — Cap timeouts at 900 seconds

### Analysis

AWS Lambda's maximum runtime is 900 seconds (15 minutes). Any `soft_timeout` or `hard_timeout` value above 900 is invalid. The library currently accepts these values silently.

The cap must be enforced at every point where a timeout value is accepted:

1. `_validate_timeouts()` in `decorators.py` — called at decoration time and in `on_commit`.
2. `LambdaTasksSettings._resolve_timeouts()` in `settings.py` — called when reading global defaults.

### Change

Add a `_MAX_TIMEOUT = 900` constant. Extend `_validate_timeouts` to check each non-`None` value:

```python
_MAX_TIMEOUT = 900

def _validate_timeouts(soft_timeout: int | None, hard_timeout: int | None) -> None:
    for name, value in (("soft_timeout", soft_timeout), ("hard_timeout", hard_timeout)):
        if value is not None and value > _MAX_TIMEOUT:
            raise ConfigurationError(
                f"{name} ({value}) exceeds the maximum allowed value of {_MAX_TIMEOUT} seconds."
            )
    if soft_timeout is not None and hard_timeout is not None:
        if soft_timeout >= hard_timeout:
            raise ConfigurationError(
                f"soft_timeout ({soft_timeout}) must be strictly less than hard_timeout ({hard_timeout})."
            )
```

Extend `LambdaTasksSettings._resolve_timeouts()` similarly:

```python
def _resolve_timeouts(self) -> tuple[int, int]:
    soft = ...
    hard = ...
    if soft > 900:
        raise ConfigurationError(...)
    if hard > 900:
        raise ConfigurationError(...)
    if soft >= hard:
        raise ConfigurationError(...)
    return soft, hard
```

---

## Bug 5 — Add `LAMBDA_TASKS_EAGER` mode

### Analysis

There is no way to run tasks synchronously in-process during local development or testing without mocking SQS. An `EAGER` flag on `LambdaTasksSettings` solves this cleanly.

### Change

Add an `EAGER` property to `LambdaTasksSettings`:

```python
@property
def EAGER(self) -> bool:
    val = self._get("LAMBDA_TASKS_EAGER", False)
    return bool(val)
```

In `LambdaTaskWrapper.on_commit()`, check `EAGER` before registering the `transaction.on_commit` callback. When eager, call the function directly:

```python
def on_commit(self, **kwargs: Any) -> None:
    # ... pop overrides, resolve delay/timeouts/queue ...
    conf = LambdaTasksSettings()
    if conf.EAGER:
        self._func(**task_kwargs)
        return
    # ... existing transaction.on_commit path ...
```

Eager mode bypasses SQS and `transaction.on_commit` entirely — the task runs immediately in the current call stack.

---

## Bug 6 — Enforce kwargs-only signatures across all library functions

### Analysis

The library enforces kwargs-only on *task functions* at decoration time, but its own internal and public functions accept positional arguments. This is inconsistent and makes call sites ambiguous.

### Affected signatures

Every function and method parameter that is currently `POSITIONAL_OR_KEYWORD` must become `KEYWORD_ONLY` by inserting `*` before the first parameter.

Affected locations:

| Module | Function / Method |
|---|---|
| `settings.py` | `LambdaTasksSettings._get`, `_resolve_queues`, `_resolve_timeouts` |
| `decorators.py` | `_validate_func`, `_validate_timeouts`, `lambda_task`, `LambdaTaskWrapper.__init__`, `LambdaTaskWrapper.on_commit` |
| `enqueuer.py` | `enqueue` |
| `executor.py` | `execute_task`, `_resolve_timeouts`, `_now` |
| `handler.py` | `handler`, `_process_record` |
| `serializer.py` | `serialize`, `deserialize`, `SQSLambdaTaskMessage._parse_json_string` |
| `timeouts.py` | `TimeoutContext.__init__`, `TimeoutContext._handler` |
| `registry.py` | `register`, `get` |

`__call__`, `__enter__`, `__exit__`, and dunder methods that must match Python's fixed protocol signatures are exempt.

---

## Bug 7 — Add `ty` strict type checking

### Analysis

The codebase has incomplete or incorrect type annotations that would be caught by `ty` in strict mode. Adding `ty` as a dev dependency and configuring it with `strict = true` in `pyproject.toml` provides ongoing enforcement.

### Change

1. Add `ty` as a dev dependency:

```bash
uv add --dev ty
```

2. Add `[tool.ty]` configuration to `pyproject.toml`:

```toml
[tool.ty]
strict = true
```

3. Fix all type errors reported by `ty --strict` across all modules. Common fixes expected:
   - Add return type annotations to functions currently missing them.
   - Annotate `_previous_handler` in `TimeoutContext` with the correct `signal.Handlers | Callable` type.
   - Annotate `frame` parameter in signal handlers as `types.FrameType | None`.
   - Ensure `SQSLambdaTaskMessage.kwargs` is typed as `dict[str, Any]`.
   - Ensure `TaskRecord.result` field and related ORM interactions are correctly typed.
   - Replace bare `dict` with `dict[str, Any]` or more specific types where needed.

---

## Files Changed

| File | Change |
|---|---|
| `lambda_tasks/apps.py` | Remove misleading comment and unnecessary import |
| `lambda_tasks/conf.py` → `lambda_tasks/settings.py` | Rename; remove `DEFAULT_DELAY`; add `EAGER`; add 900s cap in `_resolve_timeouts`; kwargs-only |
| `lambda_tasks/decorators.py` | Update import; remove `conf.DEFAULT_DELAY` ref; add 900s cap in `_validate_timeouts`; add eager check in `on_commit`; kwargs-only |
| `lambda_tasks/enqueuer.py` | Update import; kwargs-only |
| `lambda_tasks/executor.py` | Update import; kwargs-only |
| `lambda_tasks/handler.py` | kwargs-only |
| `lambda_tasks/serializer.py` | kwargs-only; type fixes |
| `lambda_tasks/timeouts.py` | kwargs-only; type fixes |
| `lambda_tasks/registry.py` | kwargs-only |
| `pyproject.toml` | Add `ty` dev dependency; add `[tool.ty]` config |
| `README.md` | Full developer documentation update |

---

## Correctness Properties

### P1 — Registration at decoration time
`@lambda_task` applied to a valid function MUST result in the task being present in the registry immediately, with no additional import required.

### P2 — No `DEFAULT_DELAY` attribute
`LambdaTasksSettings()` MUST NOT expose a `DEFAULT_DELAY` attribute.

### P3 — Module renamed
`lambda_tasks.settings` MUST be importable; `lambda_tasks.conf` MUST NOT exist.

### P4 — 900s cap enforced everywhere
For any timeout value `v > 900`, supplying `v` as `soft_timeout` or `hard_timeout` at decoration time, `on_commit` time, or via global settings MUST raise `ConfigurationError`.

### P5 — Eager execution
When `LAMBDA_TASKS_EAGER = True`, calling `.on_commit(**kwargs)` MUST execute the task synchronously and MUST NOT call `boto3.client("sqs").send_message`.

### P6 — kwargs-only signatures
Every non-dunder function and method in the library MUST have all parameters as `KEYWORD_ONLY` (i.e. `inspect.Parameter.KEYWORD_ONLY`).

### P7 — Zero `ty` errors
Running `ty check` with `strict = true` against all library modules MUST report zero errors.
