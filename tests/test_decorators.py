"""
Tests for lambda_tasks.decorators — LambdaTaskWrapper and lambda_task decorator.

Covers retry_on validation, no-overlap validation, and decorator forwarding.
"""

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
