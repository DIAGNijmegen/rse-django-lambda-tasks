# Design Document: django-lambda-tasks

## Overview

`django-lambda-tasks` is a Django library that enables developers to offload work to AWS Lambda outside of the HTTP request-response cycle. The library provides a decorator-based API for defining tasks, a Pydantic-based serialization layer for SQS transport, and a Lambda handler function that processes message batches and persists execution results to the Django database.

The system follows a producer/consumer model:

- **Producer side** (Django web process): A decorated function gains an `.on_commit()` method. Calling it registers a `transaction.on_commit` hook that serializes the task invocation and sends it to an SQS queue.
- **Consumer side** (AWS Lambda): AWS polls SQS and invokes the Lambda handler with a batch of records. The handler deserializes each message, executes the task inside a Django atomic transaction, enforces timeouts via Unix signals, and persists the result to a `Task_Record` database row.

### Design Goals

- Zero-boilerplate task definition — one decorator, one method call.
- Reliable delivery — tasks are only enqueued after the triggering transaction commits.
- Observability — every invocation produces a `Task_Record` row with full status, kwargs, result, and traceback.
- Graceful degradation — soft timeouts give tasks a chance to clean up; hard timeouts guarantee termination.
- Multi-queue routing — tasks can be routed to queues backed by Lambda functions with different hardware profiles.

### Scope Constraints

- Unix-only (Linux, macOS). Timeout enforcement relies on `SIGALRM`.
- AWS Lambda execution model only — no long-running worker process or management command.
- Task arguments must be keyword-only.

---

## Architecture

```mermaid
graph TD
    subgraph Django Web Process
        A[View / Service Layer] -->|calls .on_commit(**kwargs)| B[Enqueuer]
        B -->|transaction.on_commit hook| C[SQS Client - boto3]
    end

    subgraph AWS
        C -->|JSON message| D[SQS Queue]
        D -->|batch trigger| E[Lambda Handler]
    end

    subgraph Lambda Execution Environment
        E -->|per record| F[Deserializer]
        F --> G[Task Executor]
        G -->|atomic| H[Background Task Function]
        G -->|SIGALRM| I[Timeout Enforcer]
        G -->|ORM write| J[(Django DB - Task_Record)]
    end
```

### Component Interaction Flow

1. Developer calls `my_task.on_commit(arg=value)` inside a Django view.
2. `Enqueuer` validates kwargs, generates an `Invocation_ID`, serializes to JSON via Pydantic, and registers a `transaction.on_commit` callback.
3. On transaction commit, the callback sends the JSON payload to the resolved SQS queue URL via boto3.
4. AWS Lambda is triggered with a batch of SQS records.
5. The `Lambda Handler` iterates records independently. For each:
   a. Deserializes the message body.
   b. Looks up the registered `Background_Task` by fully-qualified name.
   c. Creates a `Task_Record` with status `RUNNING`.
   d. Wraps execution in `django.db.transaction.atomic`.
   e. Arms `SIGALRM` for soft and hard timeouts.
   f. Executes the task function.
   g. On success: updates `Task_Record` to `SUCCESS`.
   h. On failure: rolls back the atomic block, then writes `FAILED` status outside it.

---

## Components and Interfaces

### 1. `@lambda_task` Decorator

**Module**: `lambda_tasks.decorators`

```python
def lambda_task(
    func=None,
    *,
    delay: int = 0,
    soft_timeout: int | None = None,
    hard_timeout: int | None = None,
    queue: str = "default",
) -> LambdaTaskWrapper: ...
```

- Validates that `func` has no positional parameters (raises `TypeError` at decoration time).
- Validates that `soft_timeout < hard_timeout` when both are provided (raises `ConfigurationError`).
- Returns a `LambdaTaskWrapper` that preserves `__name__`, `__doc__`, and `__wrapped__`.
- Registers the wrapper in a module-level `_TASK_REGISTRY: dict[str, LambdaTaskWrapper]` keyed by fully-qualified name (`module.qualname`).

**`LambdaTaskWrapper`**

```python
class LambdaTaskWrapper:
    def __call__(self, **kwargs) -> Any: ...          # direct synchronous call
    def on_commit(self, **kwargs) -> None: ...        # enqueue via transaction.on_commit
```

`on_commit` accepts the task kwargs plus reserved override kwargs prefixed with `_`:
- `_delay: int`
- `_soft_timeout: int`
- `_hard_timeout: int`
- `_queue: str`

### 2. Enqueuer

**Module**: `lambda_tasks.enqueuer`

Responsible for:
- Resolving the target queue URL from settings.
- Calling the `Serializer` to produce the SQS message body.
- Calling `boto3` SQS `send_message` with the resolved `DelaySeconds`.
- Raising on SQS failure (no silent discard).

```python
def enqueue(
    task_name: str,
    kwargs: dict,
    delay: int,
    soft_timeout: int | None,
    hard_timeout: int | None,
    queue: str,
) -> None: ...
```

### 3. Serializer

**Module**: `lambda_tasks.serializer`

Uses Pydantic to validate and serialize/deserialize task payloads.

```python
class SQSLambdaTaskMessage(BaseModel):
    task_name: str          # fully-qualified function name
    invocation_id: str      # UUID4 string
    kwargs: dict            # validated task kwargs
    soft_timeout: int | None
    hard_timeout: int | None

def serialize(task_name: str, kwargs: dict, soft_timeout, hard_timeout) -> str:
    """Returns JSON string for SQS MessageBody."""

def deserialize(body: str) -> SQSLambdaTaskMessage:
    """Parses and validates JSON string from SQS record."""
```

Pydantic's `model_validate` is used for kwargs validation against the task's annotated signature. A `ValidationError` from Pydantic propagates to the caller and prevents enqueueing.

### 4. Lambda Handler

**Module**: `lambda_tasks.handler`

```python
def handler(event: dict, context: object) -> dict:
    """AWS Lambda entry point. Processes a batch of SQS records."""
```

- Iterates `event["Records"]` independently.
- Uses partial-batch failure reporting: returns `{"batchItemFailures": [...]}` so AWS only re-drives failed records.
- Each record is processed by `_process_record(record)`.

```python
def _process_record(record: dict) -> None:
    """Deserialize, look up task, execute with timeout and atomicity."""
```

### 5. Task Executor

**Module**: `lambda_tasks.executor`

Encapsulates the per-task execution logic called by the handler:

```python
def execute_task(message: SQSLambdaTaskMessage) -> None: ...
```

Responsibilities:
- Create `Task_Record` (status=`RUNNING`, start_time=now).
- Resolve timeouts (message → task default → global default).
- Validate `soft_timeout < hard_timeout` (raises `ConfigurationError` otherwise).
- Arm `SIGALRM` for soft timeout (raises `SoftTimeLimitExceeded` in the task).
- Arm a second `SIGALRM` for hard timeout (forcibly raises `HardTimeLimitExceeded`).
- Wrap task call in `transaction.atomic`.
- On success: disarm signals, update `Task_Record` to `SUCCESS`.
- On any exception: roll back atomic block, then (outside it) update `Task_Record` to `FAILED` with traceback.

### 6. Timeout Enforcer

**Module**: `lambda_tasks.timeouts`

Unix `SIGALRM`-based implementation. Only one `SIGALRM` can be armed at a time, so the implementation uses a two-phase approach:

1. Arm soft timeout. When `SIGALRM` fires, raise `SoftTimeLimitExceeded` inside the task and immediately re-arm for `hard_timeout - soft_timeout` remaining seconds.
2. When the second `SIGALRM` fires, raise `HardTimeLimitExceeded` (caught by the executor, not the task).

```python
class SoftTimeLimitExceeded(Exception): ...
class HardTimeLimitExceeded(Exception): ...

class TimeoutContext:
    def __enter__(self): ...   # arms SIGALRM
    def __exit__(self, ...): ...  # disarms SIGALRM
```

### 7. Settings

**Module**: `lambda_tasks.conf`

A lazy settings object that reads from `django.conf.settings` on first access:

| Setting | Type | Default | Description |
|---|---|---|---|
| `LAMBDA_TASKS_QUEUES` | `dict[str, str]` | — | Queue name → SQS URL mapping |
| `LAMBDA_TASKS_SQS_QUEUE_URL` | `str` | — | Fallback single-queue URL |
| `LAMBDA_TASKS_DEFAULT_DELAY` | `int` | `0` | Default SQS delay seconds |
| `LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT` | `int` | `270` | Default soft timeout seconds |
| `LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT` | `int` | `300` | Default hard timeout seconds |

Raises `ImproperlyConfigured` if neither queue setting is present, or if `LAMBDA_TASKS_QUEUES` lacks a `"default"` key.
Raises `ConfigurationError` if `DEFAULT_SOFT_TIMEOUT >= DEFAULT_HARD_TIMEOUT`.

### 8. Task Registry

**Module**: `lambda_tasks.registry`

```python
_TASK_REGISTRY: dict[str, LambdaTaskWrapper] = {}

def register(name: str, wrapper: LambdaTaskWrapper) -> None: ...
def get(name: str) -> LambdaTaskWrapper: ...  # raises KeyError if not found
```

The registry is populated at import time when `@lambda_task` is applied. The Lambda handler must ensure all task modules are imported before processing begins (standard Django `AppConfig.ready()` pattern or explicit import in the handler module).

---

## Data Models

### `Task_Record`

**App**: `lambda_tasks`
**Table**: `lambda_tasks_task_record`

```python
class TaskRecord(models.Model):
    class Status(models.TextChoices):
        PENDING  = "PENDING"
        RUNNING  = "RUNNING"
        SUCCESS  = "SUCCESS"
        FAILED   = "FAILED"

    task_name     = models.CharField(max_length=255, db_index=True)
    invocation_id = models.UUIDField(unique=True, db_index=True)
    kwargs        = models.JSONField()
    status        = models.CharField(max_length=10, choices=Status, default=Status.PENDING, db_index=True)
    start_time    = models.DateTimeField(null=True, blank=True)
    end_time      = models.DateTimeField(null=True, blank=True)
    result        = models.JSONField(null=True, blank=True)
    traceback     = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-start_time"]
```

**Field notes**:
- `invocation_id` is set from the UUID in the `SQSLambdaTaskMessage`, ensuring idempotent record lookup.
- `result` stores the return value of the task function serialized as JSON (or `null`).
- `traceback` stores the full formatted traceback string on failure.
- `status` transitions: `PENDING` → `RUNNING` → `SUCCESS` | `FAILED`.
- The `FAILED` update is always written outside any `atomic` block to survive transaction rollback.

### `SQSLambdaTaskMessage` (Pydantic, not a DB model)

```python
class SQSLambdaTaskMessage(BaseModel):
    task_name:     str
    invocation_id: str        # UUID4
    kwargs:        dict
    soft_timeout:  int | None = None
    hard_timeout:  int | None = None
```

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Decorator preserves function identity

*For any* function with keyword-only arguments, a name, and a docstring, applying `@lambda_task` should produce a wrapper whose `__name__` and `__doc__` match the original function, and which exposes an `on_commit` callable.

**Validates: Requirements 1.1, 1.4**

---

### Property 2: Positional-argument functions are rejected at decoration time

*For any* function that has one or more positional parameters, applying `@lambda_task` should raise `TypeError` before the wrapper is returned.

**Validates: Requirements 1.3**

---

### Property 3: Invalid timeout configuration is rejected at decoration time

*For any* pair of `(soft_timeout, hard_timeout)` values where `soft_timeout >= hard_timeout`, applying `@lambda_task` with those values should raise `ConfigurationError` before the wrapper is returned.

**Validates: Requirements 1.5, 7.5**

---

### Property 4: Serialization round-trip

*For any* valid set of task kwargs (conforming to the task's type annotations), serializing them into a `SQSLambdaTaskMessage` JSON string and then deserializing that string should produce a `SQSLambdaTaskMessage` whose `kwargs` are equivalent to the originals.

**Validates: Requirements 3.1, 3.2, 3.4**

---

### Property 5: Serialized message contains task identity and invocation ID

*For any* task and any valid kwargs, the serialized `SQSLambdaTaskMessage` should contain the task's fully-qualified name and a non-empty UUID string as the `invocation_id`. Two separate serializations of the same task should produce different `invocation_id` values.

**Validates: Requirements 3.5, 3.6**

---

### Property 6: Type-invalid kwargs are rejected before enqueueing

*For any* kwarg value that does not conform to the task's annotated parameter type, calling `on_commit` with that value should raise `ValidationError` and no SQS message should be sent.

**Validates: Requirements 3.3**

---

### Property 7: on_commit overrides are faithfully embedded in the SQS message

*For any* valid combination of `_delay`, `_soft_timeout`, `_hard_timeout`, and `_queue` override arguments passed to `on_commit`, the resulting SQS `send_message` call should use the override `_delay` as `DelaySeconds` and the override timeout values should appear verbatim in the serialized message body.

**Validates: Requirements 2.2, 2.3, 2.4**

---

### Property 8: Invalid timeout overrides at enqueue time are rejected

*For any* pair `(_soft_timeout, _hard_timeout)` passed to `on_commit` where `_soft_timeout >= _hard_timeout`, the call should raise `ConfigurationError` and no SQS message should be sent.

**Validates: Requirements 2.5**

---

### Property 9: Queue routing follows the resolution order

*For any* registered queue name passed as `_queue` to `on_commit`, the SQS `send_message` call should use the URL mapped to that name in `LAMBDA_TASKS_QUEUES`. When no queue is specified at the invocation or task level, the `"default"` queue URL should be used. When an unknown queue name is specified, `ImproperlyConfigured` should be raised and no message should be sent.

**Validates: Requirements 2.6, 9.5, 9.6, 9.7**

---

### Property 10: SQS failures propagate as exceptions

*For any* `on_commit` call where the underlying boto3 `send_message` raises an exception, that exception should propagate to the caller and no task should be silently discarded.

**Validates: Requirements 2.8**

---

### Property 11: Batch records are processed independently

*For any* SQS event batch containing a mix of valid and invalid records, the Lambda handler should attempt to process every record, and a failure on one record should not prevent the remaining records from being processed. The returned `batchItemFailures` list should contain exactly the message IDs of failed records.

**Validates: Requirements 4.2, 4.3, 4.5**

---

### Property 12: Atomic execution — failed tasks do not commit DB changes

*For any* background task that raises an unhandled exception, any Django ORM writes made inside the task body should be rolled back, while the `Task_Record` update to `FAILED` status (written outside the atomic block) should be committed and visible in the database.

**Validates: Requirements 5.1, 5.2, 5.3**

---

### Property 13: Task_Record lifecycle invariant

*For any* task execution, the following should hold:
- At execution start: a `Task_Record` exists with `status=RUNNING`, a non-null `start_time`, the correct `task_name`, `invocation_id`, and `kwargs`.
- On success: the same record has `status=SUCCESS`, a non-null `end_time`, and a `result` matching the task's return value.
- On failure: the same record has `status=FAILED`, a non-null `end_time`, and a non-empty `traceback` string.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4**

---

### Property 14: Timeout resolution follows the precedence chain

*For any* task execution, the effective `soft_timeout` and `hard_timeout` should be the first non-`None` value found in the chain: SQS message → task decorator default → global settings default. When no value is set at any level, the library defaults (`270` / `300`) should be used.

**Validates: Requirements 7.1, 7.4**

---

### Property 15: Global settings defaults are validated on first use

*For any* Django settings configuration where `LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT >= LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT`, accessing the library's settings object should raise `ConfigurationError`. When neither `LAMBDA_TASKS_QUEUES` nor `LAMBDA_TASKS_SQS_QUEUE_URL` is present, it should raise `ImproperlyConfigured`. When `LAMBDA_TASKS_QUEUES` is present but has no `"default"` key, it should raise `ImproperlyConfigured`.

**Validates: Requirements 8.4, 8.8, 9.3**

---

## Error Handling

### Exception Hierarchy

```
lambda_tasks.exceptions
├── ConfigurationError          # Invalid timeout pairs, bad decorator usage
└── (re-exports Django's ImproperlyConfigured for settings errors)

lambda_tasks.timeouts
├── SoftTimeLimitExceeded       # Raised inside the task at soft timeout
└── HardTimeLimitExceeded       # Raised by the executor at hard timeout (not catchable by task)
```

### Error Scenarios and Responses

| Scenario | Where detected | Response |
|---|---|---|
| Positional args on decorated function | Decoration time | `TypeError` |
| `soft_timeout >= hard_timeout` at decoration | Decoration time | `ConfigurationError` |
| `soft_timeout >= hard_timeout` at `on_commit` | Enqueue time | `ConfigurationError`, no SQS send |
| Kwarg type mismatch | Enqueue time (Pydantic) | `ValidationError`, no SQS send |
| Unknown queue name | Enqueue time | `ImproperlyConfigured`, no SQS send |
| SQS `send_message` failure | Enqueue time | Exception propagates to caller |
| Unregistered task name in message | Worker, per-record | Log error, record added to `batchItemFailures` |
| Task raises unhandled exception | Worker, per-record | Rollback atomic block, write `FAILED` Task_Record outside it, record added to `batchItemFailures` |
| Soft timeout exceeded | Worker, inside task | `SoftTimeLimitExceeded` raised in task; if uncaught, treated as unhandled exception |
| Hard timeout exceeded | Worker, executor | `HardTimeLimitExceeded` raised by executor; task terminated, `FAILED` Task_Record written |
| `soft_timeout >= hard_timeout` at execution | Worker, executor | `ConfigurationError`, task not executed, `FAILED` Task_Record written |
| Missing queue settings | Settings load | `ImproperlyConfigured` on first access |
| `LAMBDA_TASKS_QUEUES` missing `"default"` | Settings load | `ImproperlyConfigured` on first access |
| Invalid global timeout defaults | Settings load | `ConfigurationError` on first access |

### SIGALRM Interaction Notes

- Only one `SIGALRM` can be active at a time per process. The `TimeoutContext` saves and restores any pre-existing alarm.
- The Lambda execution environment is single-threaded per invocation; `SIGALRM` is safe to use.
- The hard timeout handler must not raise inside a `finally` block that is already handling the soft timeout — the implementation uses a state flag to distinguish the two phases.
- On successful task completion, `signal.alarm(0)` is called to cancel any pending alarm before the `TimeoutContext` exits.

---

## Testing Strategy

### Dual Testing Approach

Both unit tests and property-based tests are required. They are complementary:

- **Unit tests** cover specific examples, integration points, and edge cases (e.g. the exact `on_commit`-outside-transaction behavior, soft/hard timeout examples with real `time.sleep` tasks, settings fallback examples).
- **Property-based tests** verify universal properties across randomly generated inputs (e.g. serialization round-trips, decorator invariants, queue routing correctness).

### Property-Based Testing Library

Use **[Hypothesis](https://hypothesis.readthedocs.io/)** — the standard Python property-based testing library, well-integrated with pytest.

```bash
uv add --dev hypothesis
```

Each property test must run a minimum of **100 iterations** (Hypothesis default is 100; increase with `@settings(max_examples=200)` for critical properties).

Each property test must include a comment referencing the design property it validates:

```python
# Feature: django-lambda-tasks, Property 4: Serialization round-trip
@given(kwargs=st.fixed_dictionaries({"count": st.integers(), "label": st.text()}))
def test_serialization_round_trip(kwargs):
    ...
```

### Test File Layout

```
tests/
├── test_decorator.py        # Properties 1, 2, 3
├── test_serializer.py       # Properties 4, 5, 6
├── test_enqueuer.py         # Properties 7, 8, 9, 10
├── test_handler.py          # Property 11
├── test_executor.py         # Properties 12, 13
├── test_timeouts.py         # Properties 14 (unit examples for 7.2, 7.3)
└── test_conf.py             # Property 15
```

### Unit Test Focus Areas

- `on_commit` fires after transaction commit, not before (requires `TestCase` with `transaction=True` or mocked `connection.on_commit`).
- Soft timeout: a task that sleeps past `soft_timeout` receives `SoftTimeLimitExceeded`.
- Hard timeout: a task that ignores `SoftTimeLimitExceeded` and sleeps past `hard_timeout` is terminated and its `Task_Record` is `FAILED`.
- Settings fallback: `LAMBDA_TASKS_SQS_QUEUE_URL` is used when `LAMBDA_TASKS_QUEUES` is absent.
- `batchItemFailures` response format matches the AWS partial-batch failure contract.

### Property Test Focus Areas

| Property | Hypothesis Strategy |
|---|---|
| 1 — Decorator identity | `st.text()` for names/docstrings, `st.integers()` for param counts |
| 2 — Positional arg rejection | Generate functions with 1–5 positional params |
| 3 — Invalid timeout rejection | `st.integers()` pairs where `soft >= hard` |
| 4 — Serialization round-trip | `st.fixed_dictionaries` matching task signatures |
| 5 — Message identity fields | `st.just` for task, verify UUID uniqueness across draws |
| 6 — Type-invalid kwargs | Generate values that violate annotated types |
| 7 — Override embedding | `st.integers(min_value=0, max_value=899)` for delays/timeouts |
| 8 — Invalid enqueue timeouts | `st.integers()` pairs where `soft >= hard` |
| 9 — Queue routing | `st.sampled_from` registered queue names |
| 10 — SQS failure propagation | Mock boto3 to raise; verify propagation |
| 11 — Batch independence | `st.lists` of mixed valid/invalid records |
| 12 — Atomic rollback | Generate tasks that write then raise |
| 13 — Task_Record lifecycle | `st.text()` for task names, `st.dictionaries` for kwargs |
| 14 — Timeout precedence | `st.one_of(st.none(), st.integers(min_value=1))` for each level |
| 15 — Settings validation | `st.fixed_dictionaries` for settings combinations |
