"""
Unit tests and property-based tests for lambda_tasks.handler.

Covers:
- Empty batch → {"batchItemFailures": []}
- All records succeed → {"batchItemFailures": []}
- One record fails → that messageId in batchItemFailures, others not
- All records fail → all messageIds in batchItemFailures
- Unknown task name → messageId in batchItemFailures (KeyError from registry)
- Invalid JSON body → messageId in batchItemFailures (ValidationError from SQSLambdaTaskMessage)

Property 11: Batch records are processed independently
Feature: django-lambda-tasks, Property 11: Batch records are processed independently
Validates: Requirements 4.2, 4.3, 4.5
"""

import inspect
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lambda_tasks.handler import handler

# ---------------------------------------------------------------------------
# Signature test
# ---------------------------------------------------------------------------


class TestHandlerSignature:
    def test_handler_accepts_event_dict_and_context_object(self):
        """handler signature must be (event: dict, context: object) — the Lambda runtime contract."""
        sig = inspect.signature(handler)
        params = list(sig.parameters.values())

        assert len(params) == 2, f"Expected 2 parameters, got {len(params)}: {params}"

        event_param = params[0]
        assert event_param.name == "event"
        assert event_param.annotation is dict
        assert event_param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD

        context_param = params[1]
        assert context_param.name == "context"
        assert context_param.annotation is object
        assert context_param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(message_id: str, body: str) -> dict:
    return {"messageId": message_id, "body": body}


def _valid_body(task_name: str = "my_module.my_task", **kwargs) -> str:
    return json.dumps(
        {
            "task_name": task_name,
            "kwargs": kwargs,
        }
    )


def _patch_model_validate(side_effect):
    """Patch SQSLambdaTaskMessage.model_validate_json in the handler module."""
    return patch(
        "lambda_tasks.models.SQSLambdaTaskMessage.model_validate_json",
        side_effect=side_effect,
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestHandlerEmptyBatch:
    def test_empty_records_returns_no_failures(self):
        """Empty batch → returns {"batchItemFailures": []}."""
        result = handler(event={"Records": []}, context=None)
        assert result == {"batchItemFailures": []}


class TestHandlerAllSucceed:
    def test_all_records_succeed_returns_no_failures(self):
        """All records succeed → returns {"batchItemFailures": []}."""
        records = [
            _make_record("msg-1", _valid_body()),
            _make_record("msg-2", _valid_body()),
        ]
        with _patch_model_validate(side_effect=lambda body: MagicMock()):
            result = handler(event={"Records": records}, context=None)
        assert result == {"batchItemFailures": []}


class TestHandlerPartialFailure:
    def test_one_record_fails_only_that_id_in_failures(self):
        """One record fails → only that messageId in batchItemFailures."""
        ok_body, fail_body = (f"{_valid_body()}-{i}" for i in range(2))
        records = [
            _make_record("msg-ok", ok_body),
            _make_record("msg-fail", fail_body),
        ]

        def _make_message(body: str):
            msg = MagicMock()
            if body == fail_body:
                msg.execute_immediately.side_effect = RuntimeError("boom")
            return msg

        with _patch_model_validate(_make_message):
            result = handler(event={"Records": records}, context=None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-fail"}]}

    def test_all_records_fail_all_ids_in_failures(self):
        """All records fail → all messageIds in batchItemFailures."""
        records = [
            _make_record("msg-1", _valid_body()),
            _make_record("msg-2", _valid_body()),
            _make_record("msg-3", _valid_body()),
        ]

        def _make_message(body: str):
            msg = MagicMock()
            msg.execute_immediately.side_effect = RuntimeError("fail")
            return msg

        with _patch_model_validate(_make_message):
            result = handler(event={"Records": records}, context=None)

        assert result == {
            "batchItemFailures": [
                {"itemIdentifier": "msg-1"},
                {"itemIdentifier": "msg-2"},
                {"itemIdentifier": "msg-3"},
            ]
        }


class TestHandlerUnknownTask:
    def test_unknown_task_name_adds_to_failures(self):
        """Unknown task name → ImportError from import_string → messageId in batchItemFailures."""
        record = _make_record("msg-unknown", _valid_body(task_name="nonexistent.task"))
        with patch(
            "lambda_tasks.models.import_string",
            side_effect=ImportError("nonexistent.task"),
        ):
            result = handler(event={"Records": [record]}, context=None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-unknown"}]}


class TestHandlerInvalidJson:
    def test_invalid_json_body_adds_to_failures(self):
        """Invalid JSON body → ValidationError from SQSLambdaTaskMessage → messageId in batchItemFailures."""
        record = _make_record("msg-bad-json", "not valid json {{{")
        result = handler(event={"Records": [record]}, context=None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-bad-json"}]}


class TestHandlerIndependentProcessing:
    def test_failure_does_not_prevent_subsequent_records(self):
        """A failure in one record does not prevent processing of subsequent records."""
        processed = []
        before_body, fail_body, after_body = (f"{_valid_body()}-{i}" for i in range(3))

        records = [
            _make_record("msg-before", before_body),
            _make_record("msg-fail", fail_body),
            _make_record("msg-after", after_body),
        ]

        body_to_id = {
            before_body: "msg-before",
            fail_body: "msg-fail",
            after_body: "msg-after",
        }

        def _make_message(body: str):
            msg_id = body_to_id[body]
            msg = MagicMock()
            if msg_id == "msg-fail":
                msg.execute_immediately.side_effect = RuntimeError("boom")
            else:

                def _execute(mid=msg_id, **kwargs):
                    processed.append(mid)

                msg.execute_immediately.side_effect = _execute
            return msg

        with _patch_model_validate(_make_message):
            result = handler(event={"Records": records}, context=None)

        assert "msg-before" in processed
        assert "msg-after" in processed
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-fail"}]}

    def test_every_record_is_attempted(self):
        """Every record in the batch is attempted regardless of failures."""
        attempted_bodies = []

        def _make_message(body: str):
            attempted_bodies.append(body)
            msg = MagicMock()
            msg.execute_immediately.side_effect = RuntimeError("always fail")
            return msg

        bodies = [_valid_body() for _ in range(5)]
        records = [_make_record(f"msg-{i}", bodies[i]) for i in range(5)]

        with _patch_model_validate(_make_message):
            handler(event={"Records": records}, context=None)

        assert attempted_bodies == bodies


# ---------------------------------------------------------------------------
# Property 11: Batch records are processed independently
# Feature: django-lambda-tasks, Property 11: Batch records are processed independently
# Validates: Requirements 4.2, 4.3, 4.5
# ---------------------------------------------------------------------------


@given(flags=st.lists(st.booleans(), min_size=1, max_size=10))
@settings(max_examples=100)
def test_property_11_batch_records_processed_independently(flags):
    """Property 11: Every record is attempted; batchItemFailures contains exactly the failed IDs."""
    bodies = [f"{_valid_body()}-{i}" for i in range(len(flags))]
    records = [_make_record(f"msg-{i}", bodies[i]) for i in range(len(flags))]
    expected_failures = {f"msg-{i}" for i, ok in enumerate(flags) if not ok}
    attempted_bodies = []

    body_to_idx = {body: i for i, body in enumerate(bodies)}

    def _make_message(body: str):
        attempted_bodies.append(body)
        idx = body_to_idx[body]
        msg = MagicMock()
        if not flags[idx]:
            msg.execute_immediately.side_effect = RuntimeError(
                f"simulated failure for msg-{idx}"
            )
        return msg

    with _patch_model_validate(_make_message):
        result = handler(event={"Records": records}, context=None)

    assert set(attempted_bodies) == set(
        bodies
    ), f"Not all records were attempted. Attempted: {attempted_bodies}"

    returned_failures = {item["itemIdentifier"] for item in result["batchItemFailures"]}
    assert (
        returned_failures == expected_failures
    ), f"Expected failures {expected_failures}, got {returned_failures}"


# ---------------------------------------------------------------------------
# Property 4: Django is set up before any task executes in the Lambda handler
# Feature: eager-mode-example-app, Property 4
# Validates: Lambda deployment correctness
# ---------------------------------------------------------------------------


def test_property_4_django_setup_before_execute_task(monkeypatch):
    import importlib

    import django.apps

    import lambda_tasks.handler as handler_module

    call_order = []

    monkeypatch.setattr(django.apps.apps, "ready", False)
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.settings")

    def spy_setup(*args, **kwargs):
        call_order.append("django.setup")

    monkeypatch.setattr(django, "setup", spy_setup)

    def spy_model_validate(body):
        call_order.append("execute_task")
        return MagicMock()

    monkeypatch.setattr(
        "lambda_tasks.models.SQSLambdaTaskMessage.model_validate_json",
        spy_model_validate,
    )

    importlib.reload(handler_module)

    body = json.dumps(
        {
            "task_name": "some.task",
            "kwargs": {},
        }
    )
    event = {"Records": [{"messageId": "msg-1", "body": body}]}
    handler_module.handler(event=event, context=None)

    assert (
        "django.setup" in call_order
    ), f"django.setup was never called; call_order={call_order}"
    setup_idx = call_order.index("django.setup")
    execute_idx = (
        call_order.index("execute_task")
        if "execute_task" in call_order
        else len(call_order)
    )
    assert (
        setup_idx < execute_idx
    ), f"Expected django.setup before execute_task, got order: {call_order}"
