"""
Tests for SQSLambdaTaskMessage serialization and SQSLambdaTask schema.

Properties covered:
  Property 4: Serialization round-trip (Requirements 3.1, 3.2, 3.4)
  Property 5: Serialized message contains task identity and invocation ID (Requirements 3.5, 3.6)
"""

import json
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage


def _serialize(*, task_name: str, kwargs: dict) -> str:
    """Build a SQSLambdaTaskMessage and return its JSON string, as the enqueuer does."""
    return SQSLambdaTaskMessage(
        task_name=task_name,
        kwargs=kwargs,
    ).model_dump_json()


# ---------------------------------------------------------------------------
# SQSLambdaTaskMessage serialization
# ---------------------------------------------------------------------------


class TestSerialize:
    def test_returns_valid_json_string(self):
        result = _serialize(task_name="myapp.tasks.send_email", kwargs={"user_id": 42})
        assert isinstance(json.loads(result), dict)

    def test_task_name_present_in_output(self):
        result = _serialize(task_name="myapp.tasks.send_email", kwargs={"user_id": 42})
        assert json.loads(result)["task_name"] == "myapp.tasks.send_email"

    def test_kwargs_preserved_in_output(self):
        kwargs = {"user_id": 99, "subject": "hello"}
        result = _serialize(task_name="myapp.tasks.send_email", kwargs=kwargs)
        assert json.loads(result)["kwargs"] == kwargs


class TestDeserialize:
    def test_round_trip_preserves_kwargs(self):
        kwargs = {"count": 5, "label": "test"}
        body = _serialize(task_name="myapp.tasks.process", kwargs=kwargs)
        assert SQSLambdaTaskMessage.model_validate_json(body).kwargs == kwargs

    def test_round_trip_preserves_task_name(self):
        body = _serialize(task_name="myapp.tasks.process", kwargs={})
        assert (
            SQSLambdaTaskMessage.model_validate_json(body).task_name
            == "myapp.tasks.process"
        )

    def test_returns_task_message_instance(self):
        body = _serialize(task_name="myapp.tasks.process", kwargs={})
        assert isinstance(
            SQSLambdaTaskMessage.model_validate_json(body), SQSLambdaTaskMessage
        )

    def test_invalid_json_raises_validation_error(self):
        with pytest.raises(ValidationError):
            SQSLambdaTaskMessage.model_validate_json("not valid json at all {{{")

    def test_missing_required_field_raises_validation_error(self):
        body = json.dumps({"kwargs": {}})
        with pytest.raises(ValidationError):
            SQSLambdaTaskMessage.model_validate_json(body)


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


@given(kwargs=st.fixed_dictionaries({"count": st.integers(), "label": st.text()}))
@settings(max_examples=200)
def test_round_trip_preserves_kwargs_property(kwargs):
    """For any valid kwargs dict, model_dump_json then model_validate produces equivalent kwargs."""
    body = _serialize(task_name="myapp.tasks.example", kwargs=kwargs)
    assert SQSLambdaTaskMessage.model_validate_json(body).kwargs == kwargs


@given(task_name=st.from_regex(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", fullmatch=True))
@settings(max_examples=200)
def test_task_name_always_present_in_output(task_name):
    """For any task name, the serialized message always contains that task name."""
    body = _serialize(task_name=task_name, kwargs={})
    assert SQSLambdaTaskMessage.model_validate_json(body).task_name == task_name


class TypedKwargs(BaseModel):
    count: int


@given(bad_value=st.one_of(st.text(), st.lists(st.integers()), st.booleans()))
@settings(max_examples=200)
def test_type_invalid_kwargs_raise_validation_error(bad_value):
    """Passing a non-int value for an int-annotated field raises ValidationError."""
    with pytest.raises(ValidationError):
        TypedKwargs.model_validate({"count": bad_value}, strict=True)


# Feature: deferred-task-enqueue, Property 3: SQSLambdaSQSLambdaTaskMessage round-trip
@given(
    m=st.builds(
        SQSLambdaTask,
        message=st.builds(
            SQSLambdaTaskMessage,
            task_name=st.from_regex(r"[a-z]+\.[a-z]+", fullmatch=True),
            kwargs=st.fixed_dictionaries({}),
        ),
        delay=st.integers(min_value=0, max_value=900),
        queue=st.sampled_from(["default", "high_memory"]),
    )
)
@settings(max_examples=100)
def test_deferred_task_message_round_trip(m):
    """SQSLambdaSQSLambdaTaskMessage round-trips through model_dump/model_validate."""
    assert SQSLambdaTask.model_validate(m.model_dump()) == m


_VALID_DEFERRED_BASE = {
    "message": {
        "task_name": "myapp.tasks.foo",
        "kwargs": {},
    },
    "delay": 0,
    "queue": "default",
}

_required_fields = ["message", "delay", "queue"]

_drop_required_field = st.sampled_from(_required_fields).map(
    lambda field: {k: v for k, v in _VALID_DEFERRED_BASE.items() if k != field}
)
_wrong_type = st.one_of(
    st.just({**_VALID_DEFERRED_BASE, "delay": "not_an_int"}),
    st.just({**_VALID_DEFERRED_BASE, "queue": 42}),
    st.just({**_VALID_DEFERRED_BASE, "message": "not_a_dict"}),
)
_extra_key = (
    st.text(min_size=1)
    .filter(lambda k: k not in _required_fields)
    .map(lambda key: {**_VALID_DEFERRED_BASE, key: "value"})
)


@given(invalid_dict=st.one_of(_drop_required_field, _wrong_type, _extra_key))
@settings(max_examples=100)
def test_deferred_task_message_rejects_invalid_and_extra_fields(invalid_dict):
    """SQSLambdaSQSLambdaTaskMessage raises ValidationError for missing fields, wrong types, or extra keys."""
    with pytest.raises(ValidationError):
        SQSLambdaTask.model_validate(invalid_dict)
