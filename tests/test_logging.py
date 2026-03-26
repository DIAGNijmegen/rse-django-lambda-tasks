"""
Tests for lambda_tasks.logging (task_logger).

Covers:
- task_logger prefixes messages with [message_id] when set
- task_logger emits undecorated messages when message_id is None
- message_id is cleared after execute_task completes (success and failure)
- log messages emitted during task execution carry the message_id prefix
"""

import logging
import uuid
from unittest.mock import patch

import pytest

from lambda_tasks.decorators import lambda_task
from lambda_tasks.logging import task_logger
from lambda_tasks.models import SQSLambdaTaskMessage

# ---------------------------------------------------------------------------
# Module-level task helpers
# ---------------------------------------------------------------------------


@lambda_task
def _logging_task_success(*, value: int) -> int:
    task_logger.info("inside task value=%s", value)
    return value


@lambda_task
def _logging_task_failure(*, value: int) -> None:
    task_logger.info("about to fail value=%s", value)
    raise RuntimeError("intentional")


def _task_name(wrapper) -> str:
    f = wrapper.__wrapped__
    return f"{f.__module__}.{f.__qualname__}"


def _make_message(task_name: str, kwargs: dict) -> SQSLambdaTaskMessage:
    return SQSLambdaTaskMessage(
        task_name=task_name,
        kwargs=kwargs,
    )


# ---------------------------------------------------------------------------
# Tests: _TaskLogger.process
# ---------------------------------------------------------------------------


class TestTaskLoggerProcess:
    def setup_method(self):
        task_logger.message_id = None

    def teardown_method(self):
        task_logger.message_id = None

    def test_prefixes_message_when_message_id_set(self):
        task_logger.message_id = "abc-123"
        msg, _ = task_logger.process("hello", {})
        assert msg == "[abc-123] hello"

    def test_no_prefix_when_message_id_is_none(self):
        task_logger.message_id = None
        msg, _ = task_logger.process("hello", {})
        assert msg == "hello"

    def test_kwargs_passed_through_unchanged(self):
        task_logger.message_id = "x"
        extra = {"exc_info": True}
        _, returned_kwargs = task_logger.process("msg", extra)
        assert returned_kwargs is extra


# ---------------------------------------------------------------------------
# Tests: message_id lifecycle in execute_task
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestTaskLoggerLifecycle:
    def test_message_id_cleared_after_success(self):
        msg = _make_message(_task_name(_logging_task_success), {"value": 1})
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=str(uuid.uuid4()))
        assert task_logger.message_id is None

    def test_message_id_cleared_after_failure(self):

        msg = _make_message(_task_name(_logging_task_failure), {"value": 2})
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=str(uuid.uuid4()))
        assert task_logger.message_id is None

    def test_log_records_during_task_carry_message_id(self, caplog):

        msg = _make_message(_task_name(_logging_task_success), {"value": 7})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            with caplog.at_level(logging.INFO, logger="lambda_tasks.task"):
                msg.execute_immediately(message_id=message_id)

        task_messages = [
            r.message for r in caplog.records if "inside task" in r.message
        ]
        assert len(task_messages) == 1
        assert f"[{message_id}]" in task_messages[0]

    def test_log_records_on_failure_carry_message_id(self, caplog):

        msg = _make_message(_task_name(_logging_task_failure), {"value": 3})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            with caplog.at_level(logging.INFO, logger="lambda_tasks.task"):
                msg.execute_immediately(message_id=message_id)

        task_messages = [
            r.message for r in caplog.records if "about to fail" in r.message
        ]
        assert len(task_messages) == 1
        assert f"[{message_id}]" in task_messages[0]
