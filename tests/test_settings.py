"""
Tests for LambdaTasksSettings (settings.py).
"""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

DEFAULT_URL = "https://sqs.us-east-1.amazonaws.com/000000000000/default"
QUEUES = {"default": DEFAULT_URL}


def test_missing_queues_setting_raises(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_QUEUES = None
    s = LambdaTasksSettings()
    with pytest.raises(ImproperlyConfigured):
        _ = s.QUEUES


def test_queues_without_default_key_raises(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_QUEUES = {
        "high_memory": "https://sqs.us-east-1.amazonaws.com/000/high"
    }
    s = LambdaTasksSettings()
    with pytest.raises(ImproperlyConfigured):
        _ = s.QUEUES


def test_soft_equals_hard_does_not_raise_in_settings(settings):
    """Settings just reads values — cross-validation happens on the wrapper."""
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_QUEUES = QUEUES
    settings.LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT = 300
    settings.LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT = 300
    s = LambdaTasksSettings()
    assert s.DEFAULT_SOFT_TIMEOUT == 300


def test_soft_greater_than_hard_does_not_raise_in_settings(settings):
    """Settings just reads values — cross-validation happens on the wrapper."""
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_QUEUES = QUEUES
    settings.LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT = 400
    settings.LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT = 300
    s = LambdaTasksSettings()
    assert s.DEFAULT_SOFT_TIMEOUT == 400


def test_queues_with_default_key(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    queues = {
        "default": DEFAULT_URL,
        "high_memory": "https://sqs.us-east-1.amazonaws.com/000/high",
    }
    settings.LAMBDA_TASKS_QUEUES = queues
    s = LambdaTasksSettings()
    assert s.QUEUES == queues


def test_default_soft_timeout_default_value(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_QUEUES = QUEUES
    s = LambdaTasksSettings()
    assert s.DEFAULT_SOFT_TIMEOUT == 270


def test_default_hard_timeout_default_value(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_QUEUES = QUEUES
    s = LambdaTasksSettings()
    assert s.DEFAULT_HARD_TIMEOUT == 300


def test_custom_soft_and_hard_timeout(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_QUEUES = QUEUES
    settings.LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT = 60
    settings.LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT = 120
    s = LambdaTasksSettings()
    assert s.DEFAULT_SOFT_TIMEOUT == 60
    assert s.DEFAULT_HARD_TIMEOUT == 120


# ---------------------------------------------------------------------------
# Property-based: valid timeout pairs are accepted
# ---------------------------------------------------------------------------

_valid_timeout_pair = st.integers(min_value=1, max_value=899).flatmap(
    lambda soft: st.integers(min_value=soft + 1, max_value=900).map(
        lambda hard: (soft, hard)
    )
)


@given(timeout_pair=_valid_timeout_pair)
@h_settings(max_examples=100)
def test_valid_timeout_pair_does_not_raise(timeout_pair):
    from lambda_tasks.settings import LambdaTasksSettings

    soft, hard = timeout_pair
    with override_settings(
        LAMBDA_TASKS_QUEUES=QUEUES,
        LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT=soft,
        LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT=hard,
    ):
        s = LambdaTasksSettings()
        assert s.DEFAULT_SOFT_TIMEOUT == soft
        assert s.DEFAULT_HARD_TIMEOUT == hard


# ---------------------------------------------------------------------------
# MAX_RETRIES
# ---------------------------------------------------------------------------


def test_max_retries_default():
    from lambda_tasks.settings import LambdaTasksSettings

    conf = LambdaTasksSettings()
    assert conf.MAX_RETRIES == 2880


def test_max_retries_override(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_MAX_RETRIES = 100
    conf = LambdaTasksSettings()
    assert conf.MAX_RETRIES == 100


# ---------------------------------------------------------------------------
# SINGLETON_CACHE
# ---------------------------------------------------------------------------


def test_singleton_cache_default_value():
    from lambda_tasks.settings import LambdaTasksSettings

    conf = LambdaTasksSettings()
    assert conf.SINGLETON_CACHE == "default"


def test_singleton_cache_reads_configured_value(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_SINGLETON_CACHE = "redis_locks"
    conf = LambdaTasksSettings()
    assert conf.SINGLETON_CACHE == "redis_locks"


# ---------------------------------------------------------------------------
# BATCH_QUEUES
# ---------------------------------------------------------------------------

BATCH_QUEUES_VALID = {
    "default": {
        "job_queue": "arn:aws:batch:eu-west-1:123456789:job-queue/default",
        "job_definition": "arn:aws:batch:eu-west-1:123456789:job-definition/default:1",
    },
}


def test_batch_queues_not_set_returns_empty_dict(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    conf = LambdaTasksSettings()
    assert conf.BATCH_QUEUES == {}


def test_batch_queues_valid_config(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_BATCH_QUEUES = BATCH_QUEUES_VALID
    conf = LambdaTasksSettings()
    assert conf.BATCH_QUEUES == BATCH_QUEUES_VALID


def test_batch_queues_missing_default_key_raises(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_BATCH_QUEUES = {
        "heavy": {
            "job_queue": "arn:aws:batch:eu-west-1:123456789:job-queue/heavy",
            "job_definition": "arn:aws:batch:eu-west-1:123456789:job-definition/heavy:1",
        },
    }
    conf = LambdaTasksSettings()
    with pytest.raises(ImproperlyConfigured):
        _ = conf.BATCH_QUEUES


def test_batch_queues_missing_job_queue_raises(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_BATCH_QUEUES = {
        "default": {
            "job_definition": "arn:aws:batch:eu-west-1:123456789:job-definition/default:1",
        },
    }
    conf = LambdaTasksSettings()
    with pytest.raises(ImproperlyConfigured):
        _ = conf.BATCH_QUEUES


def test_batch_queues_missing_job_definition_raises(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_BATCH_QUEUES = {
        "default": {
            "job_queue": "arn:aws:batch:eu-west-1:123456789:job-queue/default",
        },
    }
    conf = LambdaTasksSettings()
    with pytest.raises(ImproperlyConfigured):
        _ = conf.BATCH_QUEUES


def test_batch_queues_non_dict_value_raises(settings):
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_BATCH_QUEUES = {
        "default": "not-a-dict",
    }
    conf = LambdaTasksSettings()
    with pytest.raises(ImproperlyConfigured):
        _ = conf.BATCH_QUEUES
