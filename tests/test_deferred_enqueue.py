"""
Unit tests for LambdaTaskWrapper.to_json (TDD red state — to_json does not exist yet).

Task 4.1: unit tests only (no hypothesis).
"""

import json
import uuid

import pytest
from pydantic import ValidationError

from lambda_tasks.decorators import LambdaTaskWrapper
from lambda_tasks.models import SQSLambdaTask

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/000000000000/default"


def _task(*, x: int) -> None:
    pass


def _uuid_task(*, user_id: uuid.UUID) -> None:
    pass


_wrapper = LambdaTaskWrapper(_task, delay=0, queue="default")
_wrapper_with_defaults = LambdaTaskWrapper(_task, delay=5, queue="high_memory")
_uuid_wrapper = LambdaTaskWrapper(_uuid_task, delay=0, queue="default")


# ---------------------------------------------------------------------------
# to_json unit tests
# ---------------------------------------------------------------------------


def test_to_json_returns_dict_validating_as_deferred_task_message():
    """to_json(x=1) returns a dict that validates as SQSLambdaSQSLambdaTaskMessage."""
    result = _wrapper.serialize(x=1)
    assert isinstance(result, dict)
    # Must not raise
    SQSLambdaTask.model_validate(result)


def test_to_json_uses_decorator_defaults_for_delay_and_queue():
    """When no _delay/_queue overrides are given, decorator defaults are used."""
    result = _wrapper_with_defaults.serialize(x=1)
    assert result["delay"] == 5
    assert result["queue"] == "high_memory"


def test_to_json_uses_call_site_override_for_delay():
    """_delay at call site overrides the decorator default."""
    result = _wrapper.serialize(x=1, _delay=30)
    assert result["delay"] == 30


def test_to_json_task_name_matches_module_qualname():
    """result['message']['task_name'] equals f'{func.__module__}.{func.__qualname__}'."""
    func = _task
    result = _wrapper.serialize(x=1)
    expected = f"{func.__module__}.{func.__qualname__}"
    assert result["message"]["task_name"] == expected


def test_to_json_raises_validation_error_for_wrong_type_kwargs():
    """to_json(x='not_an_int') raises ValidationError when x is annotated as int."""
    with pytest.raises(ValidationError):
        _wrapper.serialize(x="not_an_int")


def test_to_json_raises_validation_error_for_missing_required_kwargs():
    """to_json() with no kwargs raises ValidationError when x: int is required."""
    with pytest.raises(ValidationError):
        _wrapper.serialize()


def test_serialize_produces_json_serializable_output_with_uuid_kwargs():
    """serialize() with UUID kwargs returns a dict that is JSON-serializable."""
    test_id = uuid.uuid4()
    result = _uuid_wrapper.serialize(user_id=test_id)
    # Must be JSON-serializable — this raises TypeError if UUID objects remain
    json.dumps(result)
    # The UUID should be serialized as a string
    assert result["message"]["kwargs"]["user_id"] == str(test_id)


# ---------------------------------------------------------------------------
# Property-based tests (P1, P2)
# ---------------------------------------------------------------------------

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Shared strategy for valid deferred dicts
# ---------------------------------------------------------------------------

_valid_deferred_dict_st = st.fixed_dictionaries(
    {
        "message": st.fixed_dictionaries(
            {
                "task_name": st.just("tests.test_deferred_enqueue._task"),
                "kwargs": st.fixed_dictionaries({"x": st.integers()}),
            }
        ),
        "delay": st.integers(min_value=0, max_value=900),
        "queue": st.sampled_from(["default"]),
    }
)


# ---------------------------------------------------------------------------
# P1: to_json structural invariant
# Feature: deferred-task-enqueue, Property 1: to_json structural invariant
# Validates: Requirements 1.1, 1.4, 1.5, 1.6, 1.7, 1.8
# ---------------------------------------------------------------------------


@given(x=st.integers())
@settings(max_examples=100)
def test_p1_to_json_structural_invariant(x: int) -> None:
    """**Validates: Requirements 1.1, 1.4, 1.5, 1.6, 1.7, 1.8**"""
    # Feature: deferred-task-enqueue, Property 1: to_json structural invariant
    result = _wrapper.serialize(x=x)
    SQSLambdaTask.model_validate(result)
    assert result["message"]["task_name"] == f"{_task.__module__}.{_task.__qualname__}"
    assert result["message"]["kwargs"] == {"x": x}
    assert result["delay"] == 0
    assert result["queue"] == "default"


# ---------------------------------------------------------------------------
# P2: to_json rejects invalid kwargs
# Feature: deferred-task-enqueue, Property 2: to_json rejects invalid kwargs
# Validates: Requirements 1.2, 1.3
# ---------------------------------------------------------------------------


@given(bad_x=st.one_of(st.text(), st.lists(st.integers()), st.none()))
@settings(max_examples=100)
def test_p2_to_json_rejects_invalid_kwargs(bad_x: object) -> None:
    """**Validates: Requirements 1.2, 1.3**"""
    # Feature: deferred-task-enqueue, Property 2: to_json rejects invalid kwargs
    with pytest.raises(ValidationError):
        _wrapper.serialize(x=bad_x)
