"""Tests for LAMBDA_TASKS_NOOP_EXECUTION setting and deployment check."""

import logging
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured

DEFAULT_QUEUE = {
    "queue_url": "https://sqs.us-east-1.amazonaws.com/000000000000/default"
}
QUEUES = {"default": DEFAULT_QUEUE}


# ---------------------------------------------------------------------------
# NOOP_EXECUTION setting property
# ---------------------------------------------------------------------------


def test_noop_execution_defaults_to_false():
    from lambda_tasks.settings import LambdaTasksSettings

    conf = LambdaTasksSettings()
    assert conf.NOOP_EXECUTION is False


def test_noop_execution_true(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_NOOP_EXECUTION = True
    settings.LAMBDA_TASKS_EAGER = False
    settings.LAMBDA_TASKS_LOCAL_WORKERS = 0
    conf = LambdaTasksSettings()
    assert conf.NOOP_EXECUTION is True


def test_noop_execution_with_eager_raises(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_NOOP_EXECUTION = True
    settings.LAMBDA_TASKS_EAGER = True
    settings.LAMBDA_TASKS_LOCAL_WORKERS = 0
    conf = LambdaTasksSettings()
    with pytest.raises(ImproperlyConfigured):
        _ = conf.NOOP_EXECUTION


def test_noop_execution_with_local_workers_raises(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_NOOP_EXECUTION = True
    settings.LAMBDA_TASKS_EAGER = False
    settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
    conf = LambdaTasksSettings()
    with pytest.raises(ImproperlyConfigured):
        _ = conf.NOOP_EXECUTION


# ---------------------------------------------------------------------------
# _execute() noop behaviour
# ---------------------------------------------------------------------------


def test_execute_noop_does_not_call_sqs(settings):
    from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage

    settings.LAMBDA_TASKS_NOOP_EXECUTION = True
    settings.LAMBDA_TASKS_EAGER = False
    settings.LAMBDA_TASKS_LOCAL_WORKERS = 0
    settings.LAMBDA_TASKS_QUEUES = QUEUES

    message = SQSLambdaTaskMessage(
        task_name="myapp.tasks.my_task",
        kwargs={"user_id": 1},
        n_retries=0,
    )
    task = SQSLambdaTask(message=message, delay=0, queue="default")

    with patch("lambda_tasks.models.boto3") as mock_boto3:
        task._execute()
        mock_boto3.client.assert_not_called()


def test_execute_noop_logs_warning(settings, caplog):
    from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage

    settings.LAMBDA_TASKS_NOOP_EXECUTION = True
    settings.LAMBDA_TASKS_EAGER = False
    settings.LAMBDA_TASKS_LOCAL_WORKERS = 0
    settings.LAMBDA_TASKS_QUEUES = QUEUES

    message = SQSLambdaTaskMessage(
        task_name="myapp.tasks.my_task",
        kwargs={"user_id": 42},
        n_retries=0,
    )
    task = SQSLambdaTask(message=message, delay=0, queue="default")

    with caplog.at_level(logging.WARNING, logger="lambda_tasks"):
        task._execute()

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    assert "myapp.tasks.my_task" in record.message
    assert "noop" in record.message.lower()
    assert "{'user_id': 42}" in record.message


# ---------------------------------------------------------------------------
# Django deployment check
# ---------------------------------------------------------------------------


def test_deployment_check_passes_when_all_false(settings):
    from lambda_tasks.checks import check_noop_not_in_production

    settings.LAMBDA_TASKS_NOOP_EXECUTION = False
    settings.LAMBDA_TASKS_EAGER = False
    settings.LAMBDA_TASKS_LOCAL_WORKERS = 0
    errors = check_noop_not_in_production(app_configs=None)
    assert errors == []


def test_deployment_check_warns_noop(settings):
    from lambda_tasks.checks import check_noop_not_in_production

    settings.LAMBDA_TASKS_NOOP_EXECUTION = True
    settings.LAMBDA_TASKS_EAGER = False
    settings.LAMBDA_TASKS_LOCAL_WORKERS = 0
    errors = check_noop_not_in_production(app_configs=None)
    assert len(errors) == 1
    assert errors[0].id == "lambda_tasks.W001"


def test_deployment_check_warns_eager(settings):
    from lambda_tasks.checks import check_noop_not_in_production

    settings.LAMBDA_TASKS_NOOP_EXECUTION = False
    settings.LAMBDA_TASKS_EAGER = True
    settings.LAMBDA_TASKS_LOCAL_WORKERS = 0
    errors = check_noop_not_in_production(app_configs=None)
    assert len(errors) == 1
    assert errors[0].id == "lambda_tasks.W002"


def test_deployment_check_warns_local_workers(settings):
    from lambda_tasks.checks import check_noop_not_in_production

    settings.LAMBDA_TASKS_NOOP_EXECUTION = False
    settings.LAMBDA_TASKS_EAGER = False
    settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
    errors = check_noop_not_in_production(app_configs=None)
    assert len(errors) == 1
    assert errors[0].id == "lambda_tasks.W003"


def test_deployment_check_warns_multiple(settings):
    from lambda_tasks.checks import check_noop_not_in_production

    settings.LAMBDA_TASKS_NOOP_EXECUTION = True
    settings.LAMBDA_TASKS_EAGER = True
    settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
    errors = check_noop_not_in_production(app_configs=None)
    assert len(errors) == 3
