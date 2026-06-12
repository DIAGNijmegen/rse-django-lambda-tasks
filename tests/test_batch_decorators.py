"""Tests for lambda_tasks.batch_decorators — BatchTaskWrapper and batch_task decorator."""

import pytest
from redis.exceptions import LockError

from lambda_tasks.decorators import BaseTaskWrapper, BatchTaskWrapper, batch_task
from lambda_tasks.settings import MAX_BATCH_TIMEOUT, TaskBackend


def _make_func():
    def task(*, x: int) -> str:
        return f"x={x}"

    return task


class TestBatchTaskWrapperIdentity:
    def test_preserves_name(self):
        wrapper = BatchTaskWrapper(_make_func())
        assert wrapper.__name__ == "task"

    def test_preserves_wrapped(self):
        func = _make_func()
        wrapper = BatchTaskWrapper(func)
        assert wrapper.__wrapped__ is func

    def test_is_base_task_wrapper(self):
        wrapper = BatchTaskWrapper(_make_func())
        assert isinstance(wrapper, BaseTaskWrapper)

    def test_backend_is_batch(self):
        wrapper = BatchTaskWrapper(_make_func())
        assert wrapper.backend == TaskBackend.BATCH


class TestBatchTaskWrapperDirectCall:
    def test_direct_call_executes_function(self):
        wrapper = BatchTaskWrapper(_make_func())
        assert wrapper(x=42) == "x=42"

    def test_direct_call_validates_kwargs(self):
        wrapper = BatchTaskWrapper(_make_func())
        with pytest.raises(Exception):
            wrapper(x="not_an_int")


class TestBatchTaskDecoratorFactory:
    def test_bare_decorator(self):
        @batch_task
        def my_task(*, value: int) -> None:
            pass

        assert isinstance(my_task, BatchTaskWrapper)
        assert my_task.__name__ == "my_task"

    def test_decorator_with_options(self):
        @batch_task(soft_timeout=1800, hard_timeout=3500, retry_on=(ConnectionError,))
        def my_task(*, value: int) -> None:
            pass

        assert isinstance(my_task, BatchTaskWrapper)
        assert my_task.retry_on == (ConnectionError,)


class TestBatchTaskWrapperValidation:
    def test_positional_args_raise_type_error(self):
        with pytest.raises(
            TypeError, match="batch_task functions must use keyword-only"
        ):

            @batch_task
            def bad_task(x: int) -> None:
                pass

    def test_var_kwargs_raise_type_error(self):
        with pytest.raises(TypeError, match="batch_task functions must not use"):

            @batch_task
            def bad_task(**kwargs: int) -> None:
                pass

    def test_underscore_param_raises_type_error(self):
        with pytest.raises(TypeError, match="must not start with '_'"):

            @batch_task
            def bad_task(*, _secret: int) -> None:
                pass

    def test_unannotated_param_raises_type_error(self):
        with pytest.raises(TypeError, match="must be type-annotated"):

            @batch_task
            def bad_task(*, x) -> None:
                pass


class TestBatchTaskTimeoutValidation:
    def test_timeout_at_3600_accepted(self):
        @batch_task(soft_timeout=3599, hard_timeout=3600)
        def my_task(*, x: int) -> None:
            pass

        assert my_task is not None

    def test_timeout_exceeding_3600_raises(self):
        with pytest.raises(ValueError, match=f"{MAX_BATCH_TIMEOUT}"):

            @batch_task(hard_timeout=3601)
            def my_task(*, x: int) -> None:
                pass

    def test_soft_equals_hard_raises(self):
        with pytest.raises(ValueError, match="must be strictly less than"):

            @batch_task(soft_timeout=100, hard_timeout=100)
            def my_task(*, x: int) -> None:
                pass

    def test_soft_greater_than_hard_raises(self):
        with pytest.raises(ValueError, match="must be strictly less than"):

            @batch_task(soft_timeout=200, hard_timeout=100)
            def my_task(*, x: int) -> None:
                pass

    def test_zero_timeout_raises(self):
        with pytest.raises(ValueError, match="must be greater than zero"):

            @batch_task(soft_timeout=0, hard_timeout=100)
            def my_task(*, x: int) -> None:
                pass


class TestBatchTaskRetryOnValidation:
    def test_non_exception_type_raises(self):
        with pytest.raises(
            TypeError, match="retry_on must contain only exception types"
        ):

            @batch_task(retry_on=(str,))
            def my_task(*, x: int) -> None:
                pass

    def test_retry_delay_without_retry_on_raises(self):
        with pytest.raises(TypeError, match="requires retry_on to be non-empty"):

            @batch_task(retry_delay=30)
            def my_task(*, x: int) -> None:
                pass


class TestBatchTaskSingletonValidation:
    def test_singleton_with_lock_error_in_retry_on_raises(self):
        with pytest.raises(TypeError, match="retry_on must not include LockError"):

            @batch_task(singleton=True, retry_on=(LockError,))
            def my_task(*, x: int) -> None:
                pass
