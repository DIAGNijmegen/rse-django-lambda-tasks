"""
Tests for timeout > 0 validation in decorators and resolved_timeouts.

Covers:
- Decorator-time rejection of zero and negative timeouts
- Settings-sourced rejection of zero and negative timeouts via resolved_timeouts
- Upper bound validation against queue type at resolution time
- Property-based: any positive timeout pair with soft < hard is accepted at decoration time
"""

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from lambda_tasks.decorators import LambdaTaskWrapper

SQS_QUEUES = {"default": {"queue_url": "https://sqs.example.com/default"}}
BATCH_QUEUES = {
    "default": {"queue_url": "https://sqs.example.com/default"},
    "heavy": {
        "job_queue_arn": "arn:aws:batch:eu-west-1:123456789:job-queue/q",
        "job_definition_arn": "arn:aws:batch:eu-west-1:123456789:job-definition/d:1",
    },
}


def _make_func():
    def _task(*, x: int) -> None:
        pass

    return _task


# ---------------------------------------------------------------------------
# Decorator-time: zero timeouts rejected
# ---------------------------------------------------------------------------


def test_soft_timeout_zero_raises_value_error():
    """soft_timeout=0 raises ValueError at decoration time."""
    with pytest.raises(ValueError, match="soft_timeout"):
        LambdaTaskWrapper(_make_func(), soft_timeout=0, hard_timeout=10)


def test_hard_timeout_zero_raises_value_error():
    """hard_timeout=0 raises ValueError at decoration time."""
    with pytest.raises(ValueError, match="hard_timeout"):
        LambdaTaskWrapper(_make_func(), soft_timeout=None, hard_timeout=0)


# ---------------------------------------------------------------------------
# Decorator-time: negative timeouts rejected
# ---------------------------------------------------------------------------


def test_soft_timeout_negative_raises_value_error():
    """soft_timeout < 0 raises ValueError at decoration time."""
    with pytest.raises(ValueError, match="soft_timeout"):
        LambdaTaskWrapper(_make_func(), soft_timeout=-1, hard_timeout=10)


def test_hard_timeout_negative_raises_value_error():
    """hard_timeout < 0 raises ValueError at decoration time."""
    with pytest.raises(ValueError, match="hard_timeout"):
        LambdaTaskWrapper(_make_func(), hard_timeout=-5)


# ---------------------------------------------------------------------------
# Decorator-time: boundary — timeout=1 is the minimum accepted value
# ---------------------------------------------------------------------------


def test_soft_timeout_one_is_accepted():
    """soft_timeout=1 is the minimum valid value."""
    wrapper = LambdaTaskWrapper(_make_func(), soft_timeout=1, hard_timeout=2)
    assert wrapper._soft_timeout == 1


def test_hard_timeout_one_is_accepted():
    """hard_timeout=1 is the minimum valid value (soft left to settings)."""
    wrapper = LambdaTaskWrapper(_make_func(), hard_timeout=1)
    assert wrapper._hard_timeout == 1


# ---------------------------------------------------------------------------
# Decorator-time: large timeouts accepted (no upper bound at decoration time)
# ---------------------------------------------------------------------------


def test_large_timeout_accepted_at_decoration_time():
    """Timeouts > 900 are accepted at decoration time (validated at resolution time)."""
    wrapper = LambdaTaskWrapper(_make_func(), soft_timeout=1800, hard_timeout=3500)
    assert wrapper._soft_timeout == 1800
    assert wrapper._hard_timeout == 3500


# ---------------------------------------------------------------------------
# resolved_timeouts: settings-sourced zero rejected
# ---------------------------------------------------------------------------


def test_resolved_soft_timeout_zero_from_settings_raises_value_error(settings):
    """soft_timeout=0 from settings raises ValueError on resolved_timeouts."""
    settings.LAMBDA_TASKS_QUEUES = SQS_QUEUES
    settings.LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT = 0
    settings.LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT = 300

    wrapper = LambdaTaskWrapper(_make_func())
    with pytest.raises(ValueError, match="soft_timeout"):
        _ = wrapper.resolved_timeouts


def test_resolved_hard_timeout_zero_from_settings_raises_value_error(settings):
    """hard_timeout=0 from settings raises ValueError on resolved_timeouts."""
    settings.LAMBDA_TASKS_QUEUES = SQS_QUEUES
    settings.LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT = 0
    settings.LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT = 0

    wrapper = LambdaTaskWrapper(_make_func())
    with pytest.raises(ValueError, match="timeout"):
        _ = wrapper.resolved_timeouts


# ---------------------------------------------------------------------------
# resolved_timeouts: settings-sourced negative rejected
# ---------------------------------------------------------------------------


def test_resolved_soft_timeout_negative_from_settings_raises_value_error(settings):
    """soft_timeout < 0 from settings raises ValueError on resolved_timeouts."""
    settings.LAMBDA_TASKS_QUEUES = SQS_QUEUES
    settings.LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT = -10
    settings.LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT = 300

    wrapper = LambdaTaskWrapper(_make_func())
    with pytest.raises(ValueError, match="soft_timeout"):
        _ = wrapper.resolved_timeouts


def test_resolved_hard_timeout_negative_from_settings_raises_value_error(settings):
    """hard_timeout < 0 from settings raises ValueError on resolved_timeouts."""
    settings.LAMBDA_TASKS_QUEUES = SQS_QUEUES
    settings.LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT = -10
    settings.LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT = -5

    wrapper = LambdaTaskWrapper(_make_func())
    with pytest.raises(ValueError, match="timeout"):
        _ = wrapper.resolved_timeouts


# ---------------------------------------------------------------------------
# resolved_timeouts: upper bound validation against queue type
# ---------------------------------------------------------------------------


def test_sqs_queue_rejects_timeout_over_900(settings):
    """A task on an SQS queue with hard_timeout > 900 raises at resolution time."""
    settings.LAMBDA_TASKS_QUEUES = SQS_QUEUES

    wrapper = LambdaTaskWrapper(_make_func(), soft_timeout=800, hard_timeout=901)
    with pytest.raises(ValueError, match="901"):
        _ = wrapper.resolved_timeouts


def test_batch_queue_accepts_timeout_up_to_3600(settings):
    """A task on a Batch queue with timeouts up to 3600 resolves fine."""
    settings.LAMBDA_TASKS_QUEUES = BATCH_QUEUES

    wrapper = LambdaTaskWrapper(
        _make_func(), soft_timeout=1800, hard_timeout=3500, queue="heavy"
    )
    soft, hard = wrapper.resolved_timeouts
    assert soft == 1800
    assert hard == 3500


def test_batch_queue_rejects_timeout_over_3600(settings):
    """A task on a Batch queue with hard_timeout > 3600 raises at resolution time."""
    settings.LAMBDA_TASKS_QUEUES = BATCH_QUEUES

    wrapper = LambdaTaskWrapper(
        _make_func(), soft_timeout=3500, hard_timeout=3601, queue="heavy"
    )
    with pytest.raises(ValueError, match="3601"):
        _ = wrapper.resolved_timeouts


# ---------------------------------------------------------------------------
# Property-based: valid timeout pairs (both > 0, soft < hard) accepted at decoration
# ---------------------------------------------------------------------------


_valid_timeout_pair = st.integers(min_value=1, max_value=3599).flatmap(
    lambda soft: st.integers(min_value=soft + 1, max_value=3600).map(
        lambda hard: (soft, hard)
    )
)


@given(timeout_pair=_valid_timeout_pair)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_valid_timeout_pair_accepted(timeout_pair):
    """Any (soft, hard) with 1 <= soft < hard <= 3600 is accepted at decoration time."""
    soft, hard = timeout_pair
    wrapper = LambdaTaskWrapper(_make_func(), soft_timeout=soft, hard_timeout=hard)
    assert wrapper._soft_timeout == soft
    assert wrapper._hard_timeout == hard


# ---------------------------------------------------------------------------
# Property-based: non-positive timeouts always rejected
# ---------------------------------------------------------------------------


@given(value=st.integers(max_value=0))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_non_positive_soft_timeout_rejected(value):
    """Any soft_timeout <= 0 raises ValueError at decoration time."""
    with pytest.raises(ValueError):
        LambdaTaskWrapper(_make_func(), soft_timeout=value, hard_timeout=10)


@given(value=st.integers(max_value=0))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_non_positive_hard_timeout_rejected(value):
    """Any hard_timeout <= 0 raises ValueError at decoration time."""
    with pytest.raises(ValueError):
        LambdaTaskWrapper(_make_func(), hard_timeout=value)
