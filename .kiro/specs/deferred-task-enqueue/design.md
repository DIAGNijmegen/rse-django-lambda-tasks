# Design Document: deferred-task-enqueue

## Overview

This feature adds deferred task enqueuing to `django-lambda-tasks`. A task invocation can be
serialized into a plain JSON-compatible dict (suitable for a Django `JSONField`), stored, and
later enqueued from any context — a management command, a scheduled job, another task, etc.

`SQSLambdaSQSLambdaTaskMessage` wraps a `SQSLambdaTaskMessage` as an attribute alongside `delay` and `queue`. This
avoids field duplication and means the deferred path can serialize the embedded `SQSLambdaTaskMessage`
directly — no changes to `serialize()` or `enqueue()` are needed.

`on_commit` and `enqueue_from_json` share a private `_do_enqueue` helper on
`LambdaTaskWrapper`. `enqueue_deferred` in `enqueuer.py` calls the same lower-level
`_send_message` function that `enqueue()` already delegates to after serialization.

---

## Architecture

```
Developer code
    │
    ├─ wrapper.to_json(**kwargs)
    │       └─ SQSLambdaSQSLambdaTaskMessage(message=SQSLambdaTaskMessage(...), delay=..., queue=...)
    │          stored as dict in JSONField
    │
    ├─ wrapper.on_commit(**kwargs)       ─┐
    │                                     ├─ wrapper._do_enqueue(message, delay, queue)
    └─ wrapper.enqueue_from_json(dict)   ─┘         │
                                                     └─ enqueuer._send_message(body, delay, queue)
                                                                  │
    enqueue_deferred(dict) ─────────────────────────────────────►│
                                                                  │
                                                      ┌───────────┴───────────┐
                                                 EAGER=True             EAGER=False
                                              execute_message()       boto3 SQS send
```

---

## Components and Interfaces

### `serializer.py` — `SQSLambdaSQSLambdaTaskMessage`

New Pydantic model added alongside `SQSLambdaTaskMessage`. No field duplication — `SQSLambdaTaskMessage` is stored
as a nested attribute:

```python
class SQSLambdaSQSLambdaTaskMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: SQSLambdaTaskMessage
    delay: int
    queue: str
```

`message` is a full `SQSLambdaTaskMessage` (with `task_name`, `invocation_id`, `kwargs`) generated at
serialization time by `to_json`. The `invocation_id` is stable — the same stored dict always
carries the same ID, enabling deduplication via `TaskRecord.get_or_create`.

`serialize()` and `deserialize()` are unchanged.

### `enqueuer.py` — refactor to extract `_send_message`

`enqueue()` is refactored to extract a private `_send_message(body, delay, queue)` helper that
owns the SQS send (and eager execution). `enqueue()` continues to call `serialize()` then
`_send_message()`. `enqueue_deferred` calls `_send_message()` directly with the pre-serialized
body from the embedded `SQSLambdaTaskMessage`:

```python
def _send_message(*, body: str, delay: int, queue: str) -> None:
    conf = LambdaTasksSettings()
    if conf.EAGER:
        execute_message(message_body=body)
    else:
        queue_url = conf.QUEUES[queue]  # raises ImproperlyConfigured if missing
        boto3.client("sqs").send_message(
            QueueUrl=queue_url, MessageBody=body, DelaySeconds=delay
        )

def enqueue(*, task_name, kwargs, delay, queue) -> None:
    body = serialize(task_name=task_name, kwargs=kwargs)
    _send_message(body=body, delay=delay, queue=queue)

def enqueue_deferred(*, deferred: dict) -> None:
    msg = SQSLambdaSQSLambdaTaskMessage.model_validate(deferred)
    _send_message(body=msg.message.model_dump_json(), delay=msg.delay, queue=msg.queue)
```

### `decorators.py` — `LambdaTaskWrapper`

Three additions:

**`to_json(**kwargs) -> dict`**
- Pops `_delay` / `_queue` overrides (same resolution logic as `on_commit`).
- Validates remaining kwargs against `_kwargs_model`.
- Builds a `SQSLambdaTaskMessage` with a fresh UUID4 `invocation_id`.
- Returns `SQSLambdaSQSLambdaTaskMessage(message=task_message, delay=..., queue=...).model_dump()`.

**`_do_enqueue(message: SQSLambdaTaskMessage, delay: int, queue: str) -> None`** (private)
- Single call site for `enqueuer._send_message(body=message.model_dump_json(), delay=delay, queue=queue)`.
- Both `on_commit` and `enqueue_from_json` delegate here.

**`enqueue_from_json(*, data: dict) -> None`**
- Validates `data` against `SQSLambdaSQSLambdaTaskMessage` (raises `pydantic.ValidationError` on failure).
- Calls `self._do_enqueue(msg.message, msg.delay, msg.queue)`.

`on_commit` is refactored to build a `SQSLambdaTaskMessage` and call `self._do_enqueue(...)` instead of
calling `enqueuer.enqueue()` directly.

---

## Data Models

### `SQSLambdaSQSLambdaTaskMessage` schema (as stored in JSONField)

```json
{
  "message": {
    "task_name": "myapp.tasks.my_task",
    "invocation_id": "550e8400-e29b-41d4-a716-446655440000",
    "kwargs": {"user_id": 42}
  },
  "delay": 0,
  "queue": "default"
}
```

Extra fields are forbidden at the top level. `message` is validated as a full `SQSLambdaTaskMessage`.

---

## Correctness Properties

### Property 1: `to_json` structural invariant

*For any* `LambdaTaskWrapper` and any valid task kwargs (with optional `_delay` / `_queue`
overrides), `to_json` returns a dict that round-trips through `SQSLambdaSQSLambdaTaskMessage.model_validate`
and contains a nested `message` dict with `task_name`, `invocation_id`, and `kwargs`, plus
top-level `delay` and `queue` reflecting the resolved overrides or decorator defaults.

**Validates: Requirements 1.1, 1.4, 1.5, 1.6, 1.7, 1.8**

---

### Property 2: `to_json` rejects invalid kwargs

*For any* kwargs that fail the task's declared type annotations (wrong type, missing required
field, or extra field), `to_json` raises `pydantic.ValidationError` and returns no dict.

**Validates: Requirements 1.2, 1.3**

---

### Property 3: `SQSLambdaSQSLambdaTaskMessage` round-trip

*For any* valid `SQSLambdaSQSLambdaTaskMessage` instance `m`, constructing a new instance from
`m.model_dump()` produces an object equal to `m`.

**Validates: Requirements 2.4**

---

### Property 4: `SQSLambdaSQSLambdaTaskMessage` rejects invalid and extra fields

*For any* dict that is missing a required field, has a field with the wrong type, or contains
extra top-level keys, `SQSLambdaSQSLambdaTaskMessage.model_validate` raises `pydantic.ValidationError`.

**Validates: Requirements 2.2, 2.3**

---

### Property 5: `enqueue_from_json` passes stable `invocation_id` to `_send_message`

*For any* valid deferred dict `d`, calling `enqueue_from_json(d)` passes the `invocation_id`
from `d["message"]` to `_send_message` unchanged, so two calls with the same dict produce the
same `invocation_id` in the SQS body.

**Validates: Requirements 3.4, 3.5, 6.1**

---

### Property 6: `enqueue_from_json` rejects invalid dicts

*For any* dict that fails `SQSLambdaSQSLambdaTaskMessage` validation, `enqueue_from_json` raises
`pydantic.ValidationError` without calling `_send_message`.

**Validates: Requirements 3.2, 3.3**

---

### Property 7: `enqueue_from_json` eager mode executes synchronously

*For any* valid deferred dict, when `LAMBDA_TASKS_EAGER=True`, `enqueue_from_json` executes
the task in-process without sending to SQS, identical to the eager behaviour of `on_commit`.

**Validates: Requirements 3.6**

---

### Property 8: `on_commit` and `enqueue_from_json` share the same send path

*For any* valid task kwargs, both `on_commit(**kwargs)` and `enqueue_from_json(to_json(**kwargs))`
call `_send_message` with the same `task_name`, `kwargs`, `delay`, and `queue`.

**Validates: Requirements 4.1, 4.2**

---

### Property 9: `enqueue_deferred` passes all fields including stable `invocation_id`

*For any* valid `SQSLambdaSQSLambdaTaskMessage` dict `d`, `enqueue_deferred(d)` calls `_send_message` with
a body whose `task_name`, `invocation_id`, and `kwargs` match `d["message"]`, and `delay` and
`queue` match the top-level fields of `d`.

**Validates: Requirements 5.1, 5.3, 5.4, 6.2**

---

### Property 10: `enqueue_deferred` rejects invalid dicts

*For any* dict that fails `SQSLambdaSQSLambdaTaskMessage` validation, `enqueue_deferred` raises
`pydantic.ValidationError` without calling `_send_message`.

**Validates: Requirements 5.2**

---

### Property 11: `enqueue_deferred` eager mode executes synchronously

*For any* valid deferred dict, when `LAMBDA_TASKS_EAGER=True`, `enqueue_deferred` executes
the task in-process without sending to SQS.

**Validates: Requirements 5.5**

---

## Error Handling

| Situation | Behaviour |
|---|---|
| `to_json` called with wrong-type or missing kwargs | `pydantic.ValidationError` raised; no dict returned |
| `enqueue_from_json` called with invalid dict | `pydantic.ValidationError` raised; `_send_message` not called |
| `enqueue_deferred` called with invalid dict | `pydantic.ValidationError` raised; `_send_message` not called |
| Queue name not in `LAMBDA_TASKS_QUEUES` | `ImproperlyConfigured` raised by `_send_message` (same as current `enqueue()` behaviour) |
| boto3 / SQS error | Exception propagates from `_send_message` (unchanged) |

No new exception types are introduced.

---

## Testing Strategy

Tests follow red/green TDD: failing tests are written first, then the implementation makes them
pass. One new test file: `tests/test_deferred_enqueue.py`. Existing files `test_decorator.py`,
`test_enqueuer.py`, and `test_serializer.py` receive targeted additions for the touched code.

### Unit tests

- `to_json` returns a dict that validates as `SQSLambdaSQSLambdaTaskMessage` with correct fields
- `to_json` uses decorator defaults when `_delay` / `_queue` are omitted
- `to_json` uses call-site overrides when `_delay` / `_queue` are provided
- `to_json` raises `ValidationError` for wrong-type kwargs
- `to_json` raises `ValidationError` for missing required kwargs
- `SQSLambdaSQSLambdaTaskMessage` accepts a valid dict with nested `message`
- `SQSLambdaSQSLambdaTaskMessage` rejects a dict missing `message`
- `SQSLambdaSQSLambdaTaskMessage` rejects a dict with extra top-level fields
- `enqueue_from_json` passes the stored `invocation_id` through to `_send_message`
- `enqueue_from_json` raises `ValidationError` for an invalid dict without calling `_send_message`
- `on_commit` and `enqueue_from_json` both call `_do_enqueue`
- `enqueue_deferred` calls `_send_message` with all correct fields including `invocation_id`
- `enqueue_deferred` raises `ValidationError` for an invalid dict without calling `_send_message`
- `enqueue()` still works correctly after `_send_message` extraction
- Eager mode: `enqueue_from_json` executes task in-process
- Eager mode: `enqueue_deferred` executes task in-process

### Property-based tests

Using `hypothesis` with a minimum of 100 iterations per property.

Tag format: `# Feature: deferred-task-enqueue, Property {N}: {property_text}`

| Property | Test description | Hypothesis strategy |
|---|---|---|
| P1 | `to_json` always returns a valid `SQSLambdaSQSLambdaTaskMessage` dict with correct fields | `st.fixed_dictionaries` for valid kwargs |
| P2 | `to_json` always raises `ValidationError` for wrong-type kwargs | `st.one_of` wrong types per field |
| P3 | `SQSLambdaSQSLambdaTaskMessage` round-trip | `st.builds(SQSLambdaSQSLambdaTaskMessage, ...)` |
| P4 | `SQSLambdaSQSLambdaTaskMessage` rejects missing/wrong-type/extra fields | `st.fixed_dictionaries` with dropped/mutated fields |
| P5 | `enqueue_from_json` always passes stable `invocation_id` to `_send_message` | `st.fixed_dictionaries` for valid dicts |
| P6 | `enqueue_from_json` always raises for invalid dicts | `st.fixed_dictionaries` with missing fields |
| P7 | `enqueue_from_json` with `EAGER=True` always executes in-process | `st.fixed_dictionaries` for valid dicts |
| P8 | `on_commit` and `enqueue_from_json` always call `_send_message` with same task/kwargs/delay/queue | `st.fixed_dictionaries` for valid kwargs |
| P9 | `enqueue_deferred` always passes all fields including `invocation_id` to `_send_message` | `st.builds(SQSLambdaSQSLambdaTaskMessage, ...)` |
| P10 | `enqueue_deferred` always raises for invalid dicts | `st.fixed_dictionaries` with missing fields |
| P11 | `enqueue_deferred` with `EAGER=True` always executes in-process | `st.builds(SQSLambdaSQSLambdaTaskMessage, ...)` |

Each property-based test must run a minimum of 100 iterations (`@settings(max_examples=100)`).
Each correctness property is implemented by exactly one property-based test.
