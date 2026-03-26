---
inclusion: always
---

# Tech Stack

## Language & Runtime

- Python 3.13+ — use modern Python features (match statements, `X | Y` union types, etc.)
- Type hints are expected on all function signatures

## Core Dependencies

| Package | Version | Purpose |
|---|---|---|
| `django` | `>= 6.0.3` | Web framework, ORM, transaction management |
| `boto3` | `>= 1.42.69` | AWS SDK — SQS publishing, Lambda integration |
| `pydantic` | `>= 2.12.5` | Task argument serialization via `SQSLambdaTaskMessage` model |

## Dev Dependencies

| Package | Version | Purpose |
|---|---|---|
| `pytest` | `>= 9.0.2` | Test runner |
| `hypothesis` | present | Property-based testing (`.hypothesis/` directory exists) |

## Package Manager

`uv` manages dependencies and the virtual environment. The `uv.lock` file is committed and must stay in sync.

```bash
uv sync                          # install all dependencies
uv add <package>                 # add a runtime dependency
uv add --dev <package>           # add a dev dependency
```

## Running Tests

```bash
uv run pytest                    # full test suite
uv run pytest tests/test_foo.py  # single file
uv run pytest -x                 # stop on first failure
```

- Tests live in `tests/` — one file per source module (`executor.py` → `test_executor.py`)
- Django settings for tests are in `tests/settings.py`
- Use `hypothesis` for property-based tests where correctness properties can be expressed

## Code Style

- Prefer explicit over implicit — avoid magic, keep logic traceable
- Keep modules focused on a single responsibility (see `structure.md`)
- Do not add application-level code to `main.py` — it is a placeholder only
- boto3 exceptions propagate to callers — do not swallow them silently
