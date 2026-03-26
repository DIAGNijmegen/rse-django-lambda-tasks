"""Unit tests for SoftTimeLimitExceeded and HardTimeLimitExceeded exception classes.

Validates: Requirements 7.2, 7.3
"""

import uuid

import pytest

from lambda_tasks.timeouts import HardTimeLimitExceeded, SoftTimeLimitExceeded


def test_soft_time_limit_exceeded_is_exception():
    assert isinstance(SoftTimeLimitExceeded(), Exception)


def test_hard_time_limit_exceeded_is_exception():
    assert isinstance(HardTimeLimitExceeded(), Exception)


def test_exceptions_are_distinct_types():
    assert SoftTimeLimitExceeded is not HardTimeLimitExceeded


def test_soft_time_limit_exceeded_can_be_raised_and_caught():
    with pytest.raises(SoftTimeLimitExceeded):
        raise SoftTimeLimitExceeded("soft timeout")


def test_hard_time_limit_exceeded_can_be_raised_and_caught():
    with pytest.raises(HardTimeLimitExceeded):
        raise HardTimeLimitExceeded("hard timeout")


def test_catching_soft_does_not_catch_hard():
    with pytest.raises(HardTimeLimitExceeded):
        try:
            raise HardTimeLimitExceeded()
        except SoftTimeLimitExceeded:
            pass  # should not reach here


def test_catching_hard_does_not_catch_soft():
    with pytest.raises(SoftTimeLimitExceeded):
        try:
            raise SoftTimeLimitExceeded()
        except HardTimeLimitExceeded:
            pass  # should not reach here


# ---------------------------------------------------------------------------
# TimeoutContext tests (task 7.2)
# Validates: Requirements 7.2, 7.3
# ---------------------------------------------------------------------------

import signal
import sys
import time

import pytest

from lambda_tasks.timeouts import (
    HardTimeLimitExceeded,
    SoftTimeLimitExceeded,
    TimeoutContext,
)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGALRM not available on Windows")
def test_soft_timeout_raises_soft_time_limit_exceeded():
    """Task sleeping past soft_timeout receives SoftTimeLimitExceeded."""
    with pytest.raises(SoftTimeLimitExceeded):
        with TimeoutContext(soft_timeout=1, hard_timeout=3):
            time.sleep(2)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGALRM not available on Windows")
def test_hard_timeout_raises_hard_time_limit_exceeded():
    """Task ignoring soft timeout and sleeping past hard_timeout receives HardTimeLimitExceeded."""
    with pytest.raises(HardTimeLimitExceeded):
        with TimeoutContext(soft_timeout=1, hard_timeout=2):
            try:
                time.sleep(3)
            except SoftTimeLimitExceeded:
                time.sleep(3)  # ignore soft, keep sleeping into hard timeout


@pytest.mark.skipif(sys.platform == "win32", reason="SIGALRM not available on Windows")
def test_successful_task_within_limits_does_not_raise():
    """Successful task completing within limits does not raise any timeout exception."""
    with TimeoutContext(soft_timeout=5, hard_timeout=10):
        pass  # completes immediately — well within limits


@pytest.mark.skipif(sys.platform == "win32", reason="SIGALRM not available on Windows")
def test_pre_existing_alarm_is_restored_on_exit():
    """A pre-existing alarm is restored after TimeoutContext exits."""
    # Arm a 60-second alarm before entering the context
    signal.alarm(60)
    try:
        with TimeoutContext(soft_timeout=5, hard_timeout=10):
            pass
        remaining = signal.alarm(0)  # read and cancel the restored alarm
        # The restored alarm should be close to 60 seconds (allow some elapsed time)
        assert remaining > 0, "Pre-existing alarm should have been restored"
    finally:
        signal.alarm(0)  # clean up


@pytest.mark.skipif(sys.platform == "win32", reason="SIGALRM not available on Windows")
def test_signal_alarm_zero_called_on_clean_exit():
    """signal.alarm(0) is called on clean exit, cancelling any pending alarm."""
    with TimeoutContext(soft_timeout=5, hard_timeout=10):
        pass
    # After clean exit with no pre-existing alarm, no alarm should be pending
    remaining = signal.alarm(0)
    assert remaining == 0, "No alarm should be pending after clean exit"


# ---------------------------------------------------------------------------
# Property 14: Timeout resolution follows the precedence chain
# Feature: django-lambda-tasks, Property 14: Timeout resolution follows the precedence chain
# Validates: Requirements 7.1, 7.4
# ---------------------------------------------------------------------------

import uuid as _uuid
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lambda_tasks.decorators import lambda_task
from lambda_tasks.models import SQSLambdaTaskMessage

_timeout_level = st.one_of(st.none(), st.integers(min_value=1, max_value=100))


def _make_valid_pair(soft, hard, fallback_soft=270, fallback_hard=300):
    """Return (soft, hard) ensuring soft < hard, using fallbacks when None."""
    s = soft if soft is not None else fallback_soft
    h = hard if hard is not None else fallback_hard
    return s, h


@given(
    dec_soft=_timeout_level,
    dec_hard=_timeout_level,
    glob_soft=st.integers(min_value=1, max_value=100),
    glob_hard=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
@pytest.mark.django_db(transaction=True)
def test_property_14_timeout_resolution_precedence(
    dec_soft, dec_hard, glob_soft, glob_hard
):
    """Property 14: Effective timeout follows decorator → global precedence."""

    # If both decorator values are provided and invalid, treat as (None, None)
    if dec_soft is not None and dec_hard is not None and dec_soft >= dec_hard:
        effective_dec_soft = None
        effective_dec_hard = None
    else:
        effective_dec_soft = dec_soft
        effective_dec_hard = dec_hard

    # Compute expected effective timeouts: decorator → global
    expected_soft = effective_dec_soft if effective_dec_soft is not None else glob_soft
    expected_hard = effective_dec_hard if effective_dec_hard is not None else glob_hard

    # Skip cases where the resolved pair would be invalid
    if expected_soft >= expected_hard:
        return

    def _noop(*, x: int = 0) -> int:
        return x

    dec_kwargs: dict = {}
    if effective_dec_soft is not None:
        dec_kwargs["soft_timeout"] = effective_dec_soft
    if effective_dec_hard is not None:
        dec_kwargs["hard_timeout"] = effective_dec_hard

    task_wrapper = lambda_task(**dec_kwargs)(_noop)

    msg = SQSLambdaTaskMessage(
        task_name=f"{_noop.__module__}.{_noop.__qualname__}",
        kwargs={"x": 1},
    )

    captured: dict = {}

    class CapturingTimeoutContext:
        def __init__(self, soft_timeout: int, hard_timeout: int) -> None:
            captured["soft"] = soft_timeout
            captured["hard"] = hard_timeout

        def __enter__(self) -> "CapturingTimeoutContext":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    # Clear the resolved_timeouts cache so the patched settings take effect
    task_wrapper.__dict__.pop("_resolved_timeouts_cache", None)

    with (
        patch("lambda_tasks.models.TimeoutContext", CapturingTimeoutContext),
        patch("lambda_tasks.decorators.LambdaTasksSettings") as mock_settings_cls,
        patch("lambda_tasks.models.import_string", return_value=task_wrapper),
    ):
        mock_conf = MagicMock()
        mock_conf.DEFAULT_SOFT_TIMEOUT = glob_soft
        mock_conf.DEFAULT_HARD_TIMEOUT = glob_hard
        mock_settings_cls.return_value = mock_conf
        msg.execute_immediately(message_id=str(uuid.uuid4()))

    assert captured.get("soft") == expected_soft, (
        f"Expected soft={expected_soft}, got {captured.get('soft')} "
        f"(dec={dec_soft}, glob={glob_soft})"
    )
    assert captured.get("hard") == expected_hard, (
        f"Expected hard={expected_hard}, got {captured.get('hard')} "
        f"(dec={dec_hard}, glob={glob_hard})"
    )
