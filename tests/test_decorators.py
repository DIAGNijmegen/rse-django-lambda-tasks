"""
Tests for lambda_tasks.decorators — LambdaTaskWrapper and lambda_task decorator.

Covers retry_on validation, no-overlap validation, and decorator forwarding.
"""

import pydantic
import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from lambda_tasks.decorators import LambdaTaskWrapper, lambda_task

# ---------------------------------------------------------------------------
# Helper: a minimal valid task function
# ---------------------------------------------------------------------------


def _make_func():
    def _task(*, x: int) -> None:
        pass

    return _task


# ---------------------------------------------------------------------------
# Unit tests: retry_on defaults and forwarding
# ---------------------------------------------------------------------------


def test_retry_on_defaults_to_empty_tuple():
    """retry_on defaults to empty tuple when not supplied. Requirement 1.2"""
    wrapper = LambdaTaskWrapper(_make_func())
    assert wrapper.retry_on == ()


def test_lambda_task_decorator_forwards_retry_on():
    """lambda_task decorator forwards retry_on to LambdaTaskWrapper. Requirement 1.4"""

    @lambda_task(retry_on=(ValueError,))
    def _task(*, x: int) -> None:
        pass

    assert _task.retry_on == (ValueError,)


def test_valid_retry_on_tuple_constructs_without_error():
    """Valid retry_on tuple constructs without error."""
    wrapper = LambdaTaskWrapper(_make_func(), retry_on=(ValueError, RuntimeError))
    assert wrapper.retry_on == (ValueError, RuntimeError)


def test_invalid_retry_on_element_raises_type_error():
    """Invalid retry_on element raises TypeError at decoration time. Requirement 1.3"""
    with pytest.raises(TypeError):
        LambdaTaskWrapper(_make_func(), retry_on=(42,))  # type: ignore[arg-type]


def test_overlapping_retry_on_and_ignore_errors_raises_type_error():
    """Overlapping retry_on and ignore_errors raises TypeError at decoration time. Requirement 1.5"""
    with pytest.raises(TypeError):
        LambdaTaskWrapper(
            _make_func(),
            retry_on=(ValueError,),
            ignore_errors=(ValueError,),
        )


# ---------------------------------------------------------------------------
# Property 1: retry_on accepts any tuple of BaseException subclasses
# ---------------------------------------------------------------------------

_EXC_TYPES = [ValueError, RuntimeError, TypeError, OSError, KeyError]
_valid_retry_on_st = st.lists(st.sampled_from(_EXC_TYPES), min_size=0).map(tuple)


# Feature: task-retry, Property 1: retry_on accepts any tuple of BaseException subclasses
# Validates: Requirements 1.1
@given(retry_on=_valid_retry_on_st)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_valid_retry_on_tuples(retry_on):
    """Property 1: retry_on accepts any tuple of BaseException subclasses.
    Validates: Requirements 1.1"""
    wrapper = LambdaTaskWrapper(_make_func(), retry_on=retry_on)
    assert wrapper.retry_on == retry_on


# ---------------------------------------------------------------------------
# Property 2: Invalid retry_on raises TypeError at decoration time
# ---------------------------------------------------------------------------

_invalid_element_st = st.one_of(
    st.integers(),
    st.text(),
    st.none(),
    st.booleans(),
)


# Feature: task-retry, Property 2: Invalid retry_on raises TypeError at decoration time
# Validates: Requirements 1.3
@given(invalid_element=_invalid_element_st)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_invalid_retry_on_raises_type_error(invalid_element):
    """Property 2: invalid retry_on element raises TypeError at decoration time.
    Validates: Requirements 1.3"""
    with pytest.raises(TypeError):
        LambdaTaskWrapper(_make_func(), retry_on=(invalid_element,))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Property 12: Overlapping retry_on and ignore_errors raises TypeError
# ---------------------------------------------------------------------------

_shared_exc_type_st = st.sampled_from(_EXC_TYPES)
_other_exc_types_st = st.lists(st.sampled_from(_EXC_TYPES), min_size=0, max_size=2).map(
    tuple
)


# Feature: task-retry, Property 12: Overlapping retry_on and ignore_errors raises TypeError at decoration time
# Validates: Requirements 1.5
@given(
    shared=_shared_exc_type_st,
    extra_retry=_other_exc_types_st,
    extra_ignore=_other_exc_types_st,
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_12_overlapping_retry_on_ignore_errors_raises_type_error(
    shared, extra_retry, extra_ignore
):
    """Property 12: overlapping retry_on and ignore_errors raises TypeError at decoration time.
    Validates: Requirements 1.5"""
    retry_on = (shared,) + extra_retry
    ignore_errors = (shared,) + extra_ignore
    with pytest.raises(TypeError):
        LambdaTaskWrapper(
            _make_func(),
            retry_on=retry_on,
            ignore_errors=ignore_errors,
        )


# ---------------------------------------------------------------------------
# Unit tests: _validate_delay (Requirements 2.1, 2.3)
# ---------------------------------------------------------------------------


def test_validate_delay_negative_raises_value_error():
    """delay < 0 raises ValueError. Requirement 2.1"""
    with pytest.raises(ValueError):
        LambdaTaskWrapper(_make_func(), delay=-1)


def test_validate_delay_above_max_raises_value_error():
    """delay > 900 raises ValueError. Requirement 2.1"""
    with pytest.raises(ValueError):
        LambdaTaskWrapper(_make_func(), delay=901)


def test_validate_delay_zero_is_accepted():
    """delay=0 is accepted (lower boundary). Requirement 2.3"""
    wrapper = LambdaTaskWrapper(_make_func(), delay=0)
    assert wrapper._delay == 0


def test_validate_delay_max_is_accepted():
    """delay=900 is accepted (upper boundary). Requirement 2.3"""
    wrapper = LambdaTaskWrapper(_make_func(), delay=900)
    assert wrapper._delay == 900


# ---------------------------------------------------------------------------
# Unit tests: retry_delay init and _validate_retry_delay (Requirements 1.1, 1.3, 1.4, 2.2, 2.4)
# ---------------------------------------------------------------------------


def test_retry_delay_defaults_to_zero():
    """retry_delay defaults to 0 when not supplied. Requirement 1.1"""
    wrapper = LambdaTaskWrapper(_make_func())
    assert wrapper._retry_delay == 0


def test_retry_delay_zero_with_empty_retry_on_is_accepted():
    """retry_delay=0 with empty retry_on is accepted. Requirement 2.4"""
    wrapper = LambdaTaskWrapper(_make_func(), retry_delay=0)
    assert wrapper._retry_delay == 0


def test_retry_delay_positive_with_nonempty_retry_on_is_accepted():
    """retry_delay > 0 with non-empty retry_on is accepted. Requirement 1.3"""
    wrapper = LambdaTaskWrapper(_make_func(), retry_delay=30, retry_on=(ValueError,))
    assert wrapper._retry_delay == 30


def test_retry_delay_positive_with_empty_retry_on_raises_type_error():
    """retry_delay > 0 with empty retry_on raises TypeError. Requirement 1.4"""
    with pytest.raises(TypeError):
        LambdaTaskWrapper(_make_func(), retry_delay=30)


def test_retry_delay_negative_raises_value_error():
    """retry_delay < 0 raises ValueError. Requirement 2.2"""
    with pytest.raises(ValueError):
        LambdaTaskWrapper(_make_func(), retry_delay=-1, retry_on=(ValueError,))


def test_retry_delay_above_max_raises_value_error():
    """retry_delay > 900 raises ValueError. Requirement 2.2"""
    with pytest.raises(ValueError):
        LambdaTaskWrapper(_make_func(), retry_delay=901, retry_on=(ValueError,))


# ---------------------------------------------------------------------------
# Unit tests: retry_delay property (Requirements 1.2)
# ---------------------------------------------------------------------------


def test_retry_delay_property_returns_value_passed_at_construction():
    """retry_delay property returns the value passed at construction. Requirement 1.2"""
    wrapper = LambdaTaskWrapper(_make_func(), retry_delay=30, retry_on=(ValueError,))
    assert wrapper.retry_delay == 30


def test_retry_delay_property_defaults_to_zero():
    """retry_delay property defaults to 0 when not supplied. Requirement 1.2"""
    wrapper = LambdaTaskWrapper(_make_func())
    assert wrapper.retry_delay == 0


# ---------------------------------------------------------------------------
# Unit tests: queue property
# ---------------------------------------------------------------------------


def test_queue_property_returns_value_passed_at_construction():
    """queue property returns the queue name passed at construction."""
    wrapper = LambdaTaskWrapper(_make_func(), queue="high-priority")
    assert wrapper.queue == "high-priority"


def test_queue_property_defaults_to_default():
    """queue property defaults to 'default' when not supplied."""
    wrapper = LambdaTaskWrapper(_make_func())
    assert wrapper.queue == "default"


# ---------------------------------------------------------------------------
# Unit tests: _build_task behaviour (Requirements 4.1, 4.2, 4.3)
# ---------------------------------------------------------------------------


def test_passing_delay_kwarg_to_execute_on_commit_raises_validation_error():
    """Passing _delay to execute_on_commit raises pydantic.ValidationError. Requirement 4.1, 4.2"""
    wrapper = LambdaTaskWrapper(_make_func(), delay=10)
    with pytest.raises(pydantic.ValidationError):
        wrapper._build_task(kwargs={"x": 1, "_delay": 5})


def test_build_task_uses_decorator_delay_not_call_time_override():
    """_build_task uses the decorator delay, not a call-time override. Requirement 4.3"""
    decorator_delay = 42
    wrapper = LambdaTaskWrapper(_make_func(), delay=decorator_delay)
    task = wrapper._build_task(kwargs={"x": 1})
    assert task.delay == decorator_delay


# ---------------------------------------------------------------------------
# Unit tests: lambda_task forwarding retry_delay (Requirements 1.1, 1.2)
# ---------------------------------------------------------------------------


def test_lambda_task_forwards_retry_delay():
    """lambda_task forwards retry_delay to LambdaTaskWrapper. Requirements 1.1, 1.2"""

    @lambda_task(retry_delay=30, retry_on=(ValueError,))
    def _task(*, x: int) -> None:
        pass

    assert _task.retry_delay == 30


def test_lambda_task_retry_delay_defaults_to_zero():
    """lambda_task without retry_delay produces wrapper.retry_delay == 0. Requirements 1.1, 1.2"""

    @lambda_task
    def _task(*, x: int) -> None:
        pass

    assert _task.retry_delay == 0


# ---------------------------------------------------------------------------
# retry-delay property-based tests
# ---------------------------------------------------------------------------


# Feature: retry-delay, Property 1: retry_delay storage round-trip
# Validates: Requirements 1.2
@given(retry_delay=st.integers(min_value=0, max_value=900))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_retry_delay_1_storage_round_trip(retry_delay):
    """Property 1: for any retry_delay in [0, 900], wrapper.retry_delay returns the same value.
    Validates: Requirements 1.2"""
    if retry_delay == 0:
        wrapper = LambdaTaskWrapper(_make_func(), retry_delay=retry_delay)
    else:
        wrapper = LambdaTaskWrapper(
            _make_func(), retry_delay=retry_delay, retry_on=(ValueError,)
        )
    assert wrapper.retry_delay == retry_delay


# Feature: retry-delay, Property 2: retry_delay requires retry_on
# Validates: Requirements 1.4
@given(retry_delay=st.integers(min_value=1, max_value=900))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_retry_delay_2_requires_retry_on(retry_delay):
    """Property 2: for any retry_delay in [1, 900], constructing with empty retry_on raises TypeError.
    Validates: Requirements 1.4"""
    with pytest.raises(TypeError):
        LambdaTaskWrapper(_make_func(), retry_delay=retry_delay)


# Feature: retry-delay, Property 3: out-of-range delay and retry_delay raise ValueError
# Validates: Requirements 2.1, 2.2
@given(value=st.integers().filter(lambda x: x < 0 or x > 900))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_retry_delay_3_out_of_range_raises_value_error(value):
    """Property 3: for any integer outside [0, 900], both delay and retry_delay raise ValueError.
    Validates: Requirements 2.1, 2.2"""
    with pytest.raises(ValueError):
        LambdaTaskWrapper(_make_func(), delay=value)
    with pytest.raises(ValueError):
        LambdaTaskWrapper(_make_func(), retry_delay=value, retry_on=(ValueError,))


# ---------------------------------------------------------------------------
# Unit tests: singleton defaults and forwarding (Requirements 1.1, 1.2, 1.3)
# ---------------------------------------------------------------------------


def test_singleton_defaults_to_false() -> None:
    """singleton defaults to False when not supplied. Requirement 1.1"""
    wrapper = LambdaTaskWrapper(_make_func())
    assert wrapper.singleton is False


def test_singleton_true_is_stored_and_exposed_via_property() -> None:
    """singleton=True is stored and exposed via property. Requirement 1.2"""
    wrapper = LambdaTaskWrapper(_make_func(), singleton=True)
    assert wrapper.singleton is True


def test_lambda_task_forwards_singleton_true() -> None:
    """lambda_task(singleton=True) forwards to wrapper. Requirements 1.1, 1.2"""

    @lambda_task(singleton=True)
    def _task(*, x: int) -> None:
        pass

    assert _task.singleton is True


def test_lambda_task_without_singleton_defaults_to_false() -> None:
    """@lambda_task without singleton produces wrapper.singleton == False. Requirements 1.1, 1.3"""

    @lambda_task
    def _task(*, x: int) -> None:
        pass

    assert _task.singleton is False


# ---------------------------------------------------------------------------
# singleton property-based tests
# ---------------------------------------------------------------------------


# Feature: singleton-task, Property 1: Singleton storage round-trip
# Validates: Requirements 1.1, 1.2
@given(singleton=st.booleans())
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_singleton_1_storage_round_trip(singleton: bool) -> None:
    """Property 1: for any boolean b, LambdaTaskWrapper(func, singleton=b).singleton == b.
    Validates: Requirements 1.1, 1.2"""
    wrapper = LambdaTaskWrapper(_make_func(), singleton=singleton)
    assert wrapper.singleton is singleton


# ---------------------------------------------------------------------------
# singleton validation: LockError must not be in retry_on
# ---------------------------------------------------------------------------

from redis.exceptions import LockError


def test_singleton_with_lock_error_in_retry_on_raises_type_error() -> None:
    """singleton=True with LockError in retry_on raises TypeError at decoration time."""
    with pytest.raises(TypeError, match="retry_on must not include LockError"):
        LambdaTaskWrapper(_make_func(), singleton=True, retry_on=(LockError,))


def test_singleton_with_lock_error_superclass_in_retry_on_raises_type_error() -> None:
    """singleton=True with a superclass of LockError in retry_on raises TypeError."""
    # LockError inherits from Exception via redis.exceptions.RedisError
    with pytest.raises(TypeError, match="retry_on must not include LockError"):
        LambdaTaskWrapper(_make_func(), singleton=True, retry_on=(Exception,))


def test_singleton_false_with_lock_error_in_retry_on_is_allowed() -> None:
    """singleton=False with LockError in retry_on is fine — no implicit conflict."""
    wrapper = LambdaTaskWrapper(_make_func(), singleton=False, retry_on=(LockError,))
    assert wrapper.singleton is False
    assert LockError in wrapper.retry_on
