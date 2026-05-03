# Design Document: SSM Environment Loader

## Overview

This feature adds a new module `ssm_environment_loader.py` to the `lambda_tasks` package that reads an AWS SSM Parameter Store parameter at Lambda cold start and sets its JSON content as environment variables. It follows the same pattern as the existing `secret_loader.py` — a module-level cached function called before `django.setup()` in `handler.py`.

The module exposes a single public function `resolve_ssm_environment()` that:
1. Checks for the `LAMBDA_TASKS_SSM_ENVIRONMENT` env var
2. If present, fetches the named SSM parameter via boto3
3. Parses the parameter value as a flat JSON object (`dict[str, str]`)
4. Sets each key-value pair in `os.environ`
5. Caches the result so subsequent calls are no-ops

This runs **before** `resolve_secrets_into_env()` in the cold-start sequence, allowing SSM-loaded env vars to be referenced by the secret loader.

## Architecture

```mermaid
sequenceDiagram
    participant Lambda as Lambda Container
    participant SSM as ssm_environment_loader
    participant Secret as secret_loader
    participant Django as django.setup()

    Lambda->>SSM: resolve_ssm_environment()
    alt LAMBDA_TASKS_SSM_ENVIRONMENT is set
        SSM->>SSM: Check module-level cache
        alt Not cached
            SSM->>AWS: ssm.get_parameter(Name=param_name, WithDecryption=True)
            AWS-->>SSM: Parameter value (JSON string)
            SSM->>SSM: Validate JSON (flat str→str object)
            SSM->>SSM: Set os.environ for each key-value pair
            SSM->>SSM: Store in module-level cache
        end
    end
    Lambda->>Secret: resolve_secrets_into_env()
    Lambda->>Lambda: Check DJANGO_SETTINGS_MODULE
    alt DJANGO_SETTINGS_MODULE is set and apps not ready
        Lambda->>Django: django.setup()
    end
```

Both loaders run **unconditionally** before the `DJANGO_SETTINGS_MODULE` check — they are both idempotent and cached. The SSM parameter may provide `DJANGO_SETTINGS_MODULE` itself, and secrets may reference SSM-loaded vars.

## Components and Interfaces

### Module: `lambda_tasks/ssm_environment_loader.py`

**Public API:**

```python
def resolve_ssm_environment() -> None:
    """Load SSM parameter content into os.environ.

    Reads the parameter named by LAMBDA_TASKS_SSM_ENVIRONMENT,
    parses it as a flat JSON object, and sets each key-value pair
    as an environment variable. Idempotent — cached after first call.

    Raises:
        ValueError: If the parameter content is not valid JSON,
                    not a flat string→string mapping, or contains
                    an empty string key.
    """
```

**Internal helpers (keyword-only args, fully typed):**

```python
def _fetch_parameter(*, parameter_name: str) -> str:
    """Fetch a single SSM parameter value using boto3."""

def _validate_and_parse(*, raw_value: str, parameter_name: str) -> dict[str, str]:
    """Parse JSON and validate it is a flat str→str mapping.

    Raises ValueError with descriptive messages on failure.
    """
```

**Module-level state:**

```python
_cache: dict[str, str] | None = None  # None = not yet loaded; dict = loaded content
_loaded: bool = False  # Sentinel to distinguish "loaded empty" from "not loaded"
```

### Integration point: `lambda_tasks/handler.py`

The cold-start block changes from:

```python
if os.environ.get("DJANGO_SETTINGS_MODULE") and not apps.ready:
    resolve_secrets_into_env()
    django.setup()
```

To:

```python
from lambda_tasks.ssm_environment_loader import resolve_ssm_environment

# Both loaders are idempotent and run unconditionally before the
# DJANGO_SETTINGS_MODULE check — SSM may provide that var, and
# secrets may depend on SSM-loaded vars.
resolve_ssm_environment()
resolve_secrets_into_env()

if os.environ.get("DJANGO_SETTINGS_MODULE") and not apps.ready:
    django.setup()
```

## Data Models

### SSM Parameter Content Format

The SSM parameter value must be a JSON object where all keys and values are strings:

```json
{
  "DATABASE_URL": "postgres://user:pass@host:5432/db",
  "REDIS_URL": "redis://host:6379/0",
  "DJANGO_SECRET_KEY": "some-secret-key"
}
```

**Validation rules:**
1. Must be valid JSON (parseable by `json.loads`)
2. Must be a JSON object (top-level `dict`)
3. All values must be strings (no nested objects, arrays, numbers, booleans, or null)
4. No empty string keys

### Module-level Cache

```python
_loaded: bool = False
```

A simple boolean sentinel. Once `resolve_ssm_environment()` completes successfully, `_loaded` is set to `True`. Subsequent calls check this flag and return immediately. This is simpler than caching the parsed dict since we only need to know "did we already run?"

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Parameter content round-trip into environment

*For any* valid flat JSON object (where all keys are non-empty strings and all values are strings), when the SSM parameter returns that JSON, calling `resolve_ssm_environment()` SHALL result in every key-value pair from the JSON being present in `os.environ` with the correct value.

**Validates: Requirements 1.3**

### Property 2: Invalid JSON rejection

*For any* string that is not valid JSON, when the SSM parameter returns that string, calling `resolve_ssm_environment()` SHALL raise a `ValueError` whose message contains the parameter name.

**Validates: Requirements 2.1**

### Property 3: Non-flat JSON rejection with key identification

*For any* valid JSON object containing at least one non-string value (int, float, list, dict, bool, or null), calling `resolve_ssm_environment()` SHALL raise a `ValueError` whose message contains the names of all offending keys.

**Validates: Requirements 2.2**

### Property 4: Idempotent execution

*For any* valid SSM parameter content, calling `resolve_ssm_environment()` N times (where N ≥ 1) SHALL result in exactly one SSM API call, with all subsequent calls returning immediately without contacting AWS.

**Validates: Requirements 3.1, 3.2**

## Error Handling

| Condition | Behaviour | Rationale |
|---|---|---|
| `LAMBDA_TASKS_SSM_ENVIRONMENT` not set | Return immediately, no API call | Opt-in behaviour; no-op when not configured |
| SSM parameter not found (boto3 `ParameterNotFound`) | Let exception propagate | Fail fast at cold start; misconfiguration should crash the container |
| SSM API error (network, permissions) | Let exception propagate | Fail fast; boto3 exceptions propagate per project convention |
| Parameter value is not valid JSON | Raise `ValueError` with parameter name | Fail fast with actionable error message |
| JSON is not a flat object | Raise `ValueError` listing offending keys | Fail fast; identify exactly what's wrong |
| JSON contains empty string key | Raise `ValueError` | Empty env var names are invalid on all platforms |

**Design decision:** SSM keys override existing env vars without conflict detection. This differs from `secret_loader.py` which raises on conflicts. The rationale is that SSM parameters represent the canonical environment configuration — they are expected to override deployment-time defaults. This keeps the mental model simple: "SSM wins."

## Testing Strategy

### Property-Based Tests (Hypothesis)

The project already uses Hypothesis (`.hypothesis/` directory exists, `hypothesis` in dev dependencies). Each correctness property maps to a Hypothesis test with minimum 100 iterations.

| Property | Generator Strategy | Assertion |
|---|---|---|
| 1: Content round-trip | `st.dictionaries(keys=st.text(min_size=1, ...), values=st.text())` | All pairs present in `os.environ` |
| 2: Invalid JSON rejection | `st.text().filter(lambda s: not is_valid_json(s))` | `ValueError` raised, parameter name in message |
| 3: Non-flat JSON rejection | `st.dictionaries(...)` with at least one non-string value | `ValueError` raised, offending keys in message |
| 4: Idempotent execution | Valid content + `st.integers(min_value=2, max_value=10)` for call count | API called exactly once |

**Property test configuration:**
- Library: `hypothesis` (already installed)
- Minimum iterations: 100 (Hypothesis default is higher, which is fine)
- Tag format: `# Feature: ssm-environment-loader, Property {N}: {title}`

### Unit Tests (Example-Based)

| Scenario | Type |
|---|---|
| Env var not set → no-op, no boto3 client created | Example (Req 1.2) |
| Empty string key in JSON → ValueError | Edge case (Req 2.3) |
| Handler cold-start ordering (SSM → secrets → Django) | Integration (Req 1.4, 1.5) |
| Module importable from `lambda_tasks.ssm_environment_loader` | Smoke (Req 4.2) |
| `resolve_ssm_environment` callable with no args | Smoke (Req 4.1) |
| SSM parameter override of existing env var | Example (confirms override behaviour) |

### Test File

Tests live in `tests/test_ssm_environment_loader.py` following the project convention of one test file per source module.

### Mocking Strategy

- boto3 SSM client is mocked at the module level (same pattern as `test_secret_loader.py`)
- `os.environ` manipulation via `monkeypatch` (pytest fixture)
- Module-level cache (`_loaded`) reset via autouse fixture between tests
- No real AWS calls in any test

### TDD Approach

Following the project's TDD convention:
1. Write a failing test for each property/example
2. Implement the minimum code to make it pass
3. Refactor while keeping tests green
