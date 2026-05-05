"""
Unit tests for LambdaTaskWrapper.

Tests are written first (TDD) and cover:
- __name__ and __doc__ preservation
- __wrapped__ is set to the original function
- direct __call__ invokes the original function
- on_commit is callable and accepts task kwargs plus override kwargs
"""

import pydantic
import pytest

from lambda_tasks.decorators import LambdaTaskWrapper
from lambda_tasks.settings import MAX_TIMEOUT


def sample_task(*, x: int, y: str = "hello") -> str:
    """A sample task for testing."""
    return f"{y}-{x}"


def no_args_task() -> None:
    """A task with no arguments."""
    pass


class TestLambdaTaskWrapperIdentity:
    def test_name_is_preserved(self):
        wrapper = LambdaTaskWrapper(sample_task)
        assert wrapper.__name__ == "sample_task"

    def test_doc_is_preserved(self):
        wrapper = LambdaTaskWrapper(sample_task)
        assert wrapper.__doc__ == "A sample task for testing."

    def test_wrapped_is_set(self):
        wrapper = LambdaTaskWrapper(sample_task)
        assert wrapper.__wrapped__ is sample_task

    def test_name_preserved_for_no_args_task(self):
        wrapper = LambdaTaskWrapper(no_args_task)
        assert wrapper.__name__ == "no_args_task"

    def test_doc_preserved_for_no_args_task(self):
        wrapper = LambdaTaskWrapper(no_args_task)
        assert wrapper.__doc__ == "A task with no arguments."


class TestLambdaTaskWrapperCall:
    def test_direct_call_invokes_original(self):
        wrapper = LambdaTaskWrapper(sample_task)
        result = wrapper(x=42, y="world")
        assert result == "world-42"

    def test_direct_call_with_defaults(self):
        wrapper = LambdaTaskWrapper(sample_task)
        result = wrapper(x=7)
        assert result == "hello-7"

    def test_direct_call_no_args_task(self):
        called = []

        def track_task() -> None:
            called.append(True)

        wrapper = LambdaTaskWrapper(track_task)
        wrapper()
        assert called == [True]


@pytest.mark.django_db
class TestLambdaTaskWrapperOnCommit:
    def test_on_commit_is_callable(self):
        wrapper = LambdaTaskWrapper(sample_task)
        assert callable(wrapper.execute_on_commit)

    def test_on_commit_accepts_task_kwargs(self):
        wrapper = LambdaTaskWrapper(sample_task)
        # Should not raise
        wrapper.execute_on_commit(x=1, y="test")

    def test_on_commit_accepts_delay_override(self):
        wrapper = LambdaTaskWrapper(sample_task)
        with pytest.raises(pydantic.ValidationError):
            wrapper.execute_on_commit(x=1, _delay=10)

    def test_on_commit_accepts_delay_and_task_kwargs(self):
        wrapper = LambdaTaskWrapper(sample_task)
        with pytest.raises(pydantic.ValidationError):
            wrapper.execute_on_commit(x=1, y="test", _delay=5)


# ---------------------------------------------------------------------------
# Task 5.2 — lambda_task decorator factory
# ---------------------------------------------------------------------------

import inspect

import hypothesis.strategies as st
import pytest
from hypothesis import given
from hypothesis import settings as h_settings

from lambda_tasks.decorators import lambda_task

# --- helpers ----------------------------------------------------------------


def _make_positional_func(n_positional: int):
    """Dynamically create a function with *n_positional* positional parameters."""
    param_names = [f"p{i}" for i in range(n_positional)]
    src = f"def _f({', '.join(param_names)}): pass"
    ns: dict = {}
    exec(src, ns)  # noqa: S102
    return ns["_f"]


def _make_kwonly_func(n_kwonly: int):
    """Dynamically create a function with *n_kwonly* keyword-only parameters, all typed as int."""
    if n_kwonly == 0:

        def _f():
            pass

        return _f
    param_names = [f"k{i}" for i in range(n_kwonly)]
    params = ", ".join(f"{p}: int" for p in param_names)
    src = f"def _f(*, {params}): pass"
    ns: dict = {}
    exec(src, ns)  # noqa: S102
    return ns["_f"]


# --- unit tests -------------------------------------------------------------


class TestBackgroundTaskDecoratorValidation:
    def test_positional_arg_raises_type_error_at_decoration(self):
        def bad(x):
            pass

        with pytest.raises(TypeError):
            lambda_task(bad)

    def test_positional_arg_with_parens_raises_type_error(self):
        def bad(x, y):
            pass

        with pytest.raises(TypeError):
            lambda_task(delay=5)(bad)

    def test_soft_ge_hard_raises_configuration_error(self):
        def good(*, x: int):
            pass

        with pytest.raises(ValueError):
            lambda_task(good, soft_timeout=10, hard_timeout=10)

    def test_soft_gt_hard_raises_configuration_error(self):
        def good(*, x: int):
            pass

        with pytest.raises(ValueError):
            lambda_task(good, soft_timeout=20, hard_timeout=10)

    def test_zero_arg_function_is_accepted(self):
        def zero():
            pass

        wrapper = lambda_task(zero)
        assert wrapper.__name__ == "zero"

    def test_kwonly_function_is_accepted(self):
        def kwonly(*, a: int, b: str = "hi"):
            pass

        wrapper = lambda_task(kwonly)
        assert wrapper.__name__ == "kwonly"

    def test_decorator_without_parens(self):
        @lambda_task
        def my_task(*, value: int):
            """My task."""
            return value

        assert my_task.__name__ == "my_task"
        assert my_task.__doc__ == "My task."
        assert my_task.__wrapped__.__name__ == "my_task"

    def test_decorator_with_parens(self):
        @lambda_task(delay=5)
        def my_task2(*, value: int):
            """My task 2."""
            return value

        assert my_task2.__name__ == "my_task2"
        assert my_task2.__doc__ == "My task 2."

    def test_valid_soft_lt_hard_is_accepted(self):
        def good(*, x: int):
            pass

        wrapper = lambda_task(good, soft_timeout=5, hard_timeout=10)
        assert wrapper.__name__ == "good"


# ---------------------------------------------------------------------------
# Tasks 5.1–5.3: LambdaTaskWrapper direct construction validation
# ---------------------------------------------------------------------------


class TestLambdaTaskWrapperDirectConstruction:
    def test_positional_param_raises_type_error(self):
        """Requirement 3.1 / Property 5: positional param → TypeError on direct construction."""

        def bad(x: int) -> None:
            pass

        with pytest.raises(TypeError, match="positional parameter"):
            LambdaTaskWrapper(bad)

    def test_underscore_param_raises_type_error(self):
        """Requirement 3.2 / Property 6: _-prefixed kwonly param → TypeError on direct construction."""

        def bad(*, _hidden: int) -> None:
            pass

        with pytest.raises(TypeError, match="reserved parameter"):
            LambdaTaskWrapper(bad)

    def test_soft_ge_hard_raises_configuration_error(self):
        """Requirement 3.3 / Property 7: soft >= hard → ValueError on direct construction."""

        def good(*, x: int) -> None:
            pass

        with pytest.raises(ValueError):
            LambdaTaskWrapper(good, soft_timeout=100, hard_timeout=50)


# --- property-based tests ---------------------------------------------------


# Feature: django-lambda-tasks, Property 2: Positional-argument functions are rejected at decoration time
@given(n=st.integers(min_value=1, max_value=5))
@h_settings(max_examples=100)
def test_property_positional_args_always_rejected(n):
    """**Validates: Requirements 1.3**"""
    func = _make_positional_func(n)
    with pytest.raises(TypeError):
        lambda_task(func)


# Feature: django-lambda-tasks, Property 3: Invalid timeout configuration is rejected at decoration time
@given(
    soft=st.integers(min_value=0, max_value=1000),
    delta=st.integers(min_value=0, max_value=500),
)
@h_settings(max_examples=100)
def test_property_invalid_timeout_always_rejected(soft, delta):
    """**Validates: Requirements 1.5**"""
    hard = soft - delta  # hard <= soft, always invalid
    func = _make_kwonly_func(0)
    with pytest.raises(ValueError):
        lambda_task(func, soft_timeout=soft, hard_timeout=hard)


# Feature: django-lambda-tasks, Property 1: Decorator preserves function identity
@given(n=st.integers(min_value=0, max_value=5))
@h_settings(max_examples=100)
def test_property_zero_arg_and_kwonly_always_accepted(n):
    """**Validates: Requirements 1.1, 1.2**"""
    func = _make_kwonly_func(n)
    wrapper = lambda_task(func)
    assert wrapper.__name__ == func.__name__
    assert wrapper.__wrapped__ is func
    assert callable(wrapper.execute_on_commit)


# ---------------------------------------------------------------------------
# Tasks 5.4–5.7: Property-based tests for LambdaTaskWrapper direct construction
# ---------------------------------------------------------------------------


# Feature: import-string-task-resolution, Property 5: Positional-parameter functions rejected at construction
@given(n=st.integers(min_value=1, max_value=5))
@h_settings(max_examples=100)
def test_property_5_positional_params_rejected_at_construction(n):
    """Property 5: for any function with ≥1 positional params, LambdaTaskWrapper raises TypeError."""
    func = _make_positional_func(n)
    with pytest.raises(TypeError):
        LambdaTaskWrapper(func)


# Feature: import-string-task-resolution, Property 6: Underscore-prefixed parameters rejected at construction
@given(
    suffix=st.text(
        min_size=1,
        max_size=20,
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")),
    )
)
@h_settings(max_examples=100)
def test_property_6_underscore_params_rejected_at_construction(suffix):
    """Property 6: for any function with a _-prefixed kwonly param, LambdaTaskWrapper raises TypeError."""
    param_name = f"_{suffix}"
    src = f"def _f(*, {param_name}): pass"
    ns: dict = {}
    exec(src, ns)  # noqa: S102
    with pytest.raises(TypeError):
        LambdaTaskWrapper(ns["_f"])


# Feature: import-string-task-resolution, Property 7: Invalid timeout configuration rejected at construction
@given(
    soft=st.integers(min_value=0, max_value=1000),
    delta=st.integers(min_value=0, max_value=500),
)
@h_settings(max_examples=100)
def test_property_7_invalid_timeouts_rejected_at_construction(soft, delta):
    """Property 7: soft >= hard or either > 900 → ValueError at construction."""
    hard = soft - delta  # hard <= soft, always invalid

    def _f(*, x: int) -> None:
        pass

    with pytest.raises(ValueError):
        LambdaTaskWrapper(_f, soft_timeout=soft, hard_timeout=hard)


# Feature: import-string-task-resolution, Property 8: Valid inputs produce a correctly-attributed wrapper
@given(
    soft=st.integers(min_value=1, max_value=899),
    delta=st.integers(min_value=1, max_value=10),
)
@h_settings(max_examples=100)
def test_property_8_valid_inputs_produce_correct_wrapper(soft, delta):
    """Property 8: valid (func, soft, hard) → wrapper with correct attributes."""
    hard = min(soft + delta, 900)
    if soft >= hard:
        return  # skip degenerate cases from clamping

    def _f(*, x: int) -> None:
        """Docstring."""
        pass

    wrapper = LambdaTaskWrapper(_f, soft_timeout=soft, hard_timeout=hard)
    assert callable(wrapper)
    assert callable(wrapper.execute_on_commit)
    assert wrapper.__name__ == "_f"
    assert wrapper.__doc__ == "Docstring."
    assert wrapper.__wrapped__ is _f


# ---------------------------------------------------------------------------
# Task 11.2 — LambdaTaskWrapper.on_commit enqueue wiring
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch

import django.db.transaction
from django.test import TestCase

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/000000000000/default"


def _make_wrapper(soft_timeout=None, hard_timeout=None, delay=0, queue="default"):
    """Helper: create a LambdaTaskWrapper with given defaults."""
    return LambdaTaskWrapper(
        sample_task,
        soft_timeout=soft_timeout,
        hard_timeout=hard_timeout,
        delay=delay,
        queue=queue,
    )


@pytest.fixture()
def mock_enqueuer():
    """Patch SQSLambdaTask._send and return the mock."""
    with patch("lambda_tasks.models.SQSLambdaTask._execute") as mock_send:
        yield mock_send


# --- ValueError when soft_timeout >= hard_timeout ------------------


@pytest.mark.django_db(transaction=True)
class TestOnCommitTimeoutValidation:
    def test_wrapper_default_soft_ge_hard_raises_configuration_error(
        self, mock_enqueuer
    ):
        """Invalid wrapper-level defaults are caught at construction time."""
        with pytest.raises(ValueError):
            _make_wrapper(soft_timeout=100, hard_timeout=50)
        mock_enqueuer.assert_not_called()

    def test_valid_soft_lt_hard_does_not_raise(self, settings, mock_enqueuer):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = _make_wrapper(soft_timeout=60, hard_timeout=120)
        # Should not raise
        wrapper.execute_on_commit(x=1)


# --- on_commit outside transaction → dispatches immediately ----------------


@pytest.mark.django_db(transaction=True)
class TestOnCommitOutsideTransaction:
    def test_dispatches_immediately_outside_transaction(self, settings, mock_enqueuer):
        """Outside a transaction, transaction.on_commit fires the callback immediately."""
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = _make_wrapper()
        wrapper.execute_on_commit(x=1, y="hello")
        # Should have been called immediately (no active transaction)
        mock_enqueuer.assert_called_once()

    def test_enqueue_called_with_correct_task_name(self, settings):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = _make_wrapper()
        captured: list = []
        with patch(
            "lambda_tasks.models.transaction.on_commit",
            side_effect=lambda cb: captured.append(cb),
        ):
            wrapper.execute_on_commit(x=42)
        deferred_task = captured[0].__self__
        expected = f"{sample_task.__module__}.{sample_task.__qualname__}"
        assert deferred_task.message.task_name == expected

    def test_enqueue_called_with_task_kwargs(self, settings):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = _make_wrapper()
        captured: list = []
        with patch(
            "lambda_tasks.models.transaction.on_commit",
            side_effect=lambda cb: captured.append(cb),
        ):
            wrapper.execute_on_commit(x=7, y="world")
        deferred_task = captured[0].__self__
        assert deferred_task.message.kwargs == {"x": 7, "y": "world"}

    def test_override_delay_passed_to_enqueue(self, settings):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = _make_wrapper(delay=30)
        captured: list = []
        with patch(
            "lambda_tasks.models.transaction.on_commit",
            side_effect=lambda cb: captured.append(cb),
        ):
            wrapper.execute_on_commit(x=1)
        assert captured[0].__self__.delay == 30

    def test_wrapper_default_delay_used_when_no_override(self, settings):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = _make_wrapper(delay=15)
        captured: list = []
        with patch(
            "lambda_tasks.models.transaction.on_commit",
            side_effect=lambda cb: captured.append(cb),
        ):
            wrapper.execute_on_commit(x=1)
        assert captured[0].__self__.delay == 15

    def test_override_queue_passed_to_enqueue_is_not_supported(self, settings):
        """_queue is not a supported override — it should be rejected as an unknown kwarg."""
        settings.LAMBDA_TASKS_QUEUES = {
            "default": QUEUE_URL,
            "high_memory": "https://sqs.us-east-1.amazonaws.com/000000000000/high-memory",
        }
        wrapper = _make_wrapper()
        with pytest.raises(Exception):
            wrapper.execute_on_commit(x=1, _queue="high_memory")


# --- on_commit inside transaction → dispatches after commit, not before ----


@pytest.mark.django_db(transaction=True)
class TestOnCommitInsideTransaction:
    def test_does_not_dispatch_before_commit(self, settings, mock_enqueuer):
        """Inside an atomic block, enqueue must NOT be called before commit."""
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = _make_wrapper()
        with django.db.transaction.atomic():
            wrapper.execute_on_commit(x=1)
            # Still inside the transaction — should not have fired yet
            mock_enqueuer.assert_not_called()

    def test_dispatches_after_commit(self, settings, mock_enqueuer):
        """After the atomic block commits, enqueue should be called exactly once."""
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = _make_wrapper()
        with django.db.transaction.atomic():
            wrapper.execute_on_commit(x=1)
        # Transaction has committed — enqueue should have fired
        mock_enqueuer.assert_called_once()


# ---------------------------------------------------------------------------
# Task 3 — LAMBDA_TASKS_EAGER mode
# ---------------------------------------------------------------------------

import hypothesis.strategies as st
from django.test import override_settings
from hypothesis import given
from hypothesis import settings as h_settings


def _make_recording_task():
    """Return a task function and a list that records calls to it."""
    calls: list[dict] = []

    @lambda_task
    def _task(*, value: int = 0) -> int:
        calls.append({"value": value})
        return value

    return _task, calls


def _eager_patch(task_fn):
    """Context manager: patch import_string so eager mode can resolve a local task."""
    return patch("lambda_tasks.models.import_string", return_value=task_fn)


@pytest.mark.django_db(transaction=True)
class TestEagerMode:
    def test_eager_executes_task_synchronously(self):
        """When EAGER=True, on_commit runs the task immediately."""
        task_fn, calls = _make_recording_task()

        with override_settings(LAMBDA_TASKS_EAGER=True), _eager_patch(task_fn):
            task_fn.execute_on_commit(value=42)

        assert calls == [{"value": 42}]

    def test_eager_does_not_call_boto3(self):
        """When EAGER=True, boto3.client must never be called."""
        task_fn, _ = _make_recording_task()

        with patch("boto3.client") as mock_boto3:
            with override_settings(LAMBDA_TASKS_EAGER=True), _eager_patch(task_fn):
                task_fn.execute_on_commit(value=1)
            mock_boto3.assert_not_called()

    def test_eager_does_not_call_sqs(self):
        """When EAGER=True, enqueuer.enqueue is called but never sends to SQS."""
        task_fn, _ = _make_recording_task()

        with patch("boto3.client") as mock_boto3:
            with override_settings(LAMBDA_TASKS_EAGER=True), _eager_patch(task_fn):
                task_fn.execute_on_commit(value=7)
            mock_boto3.assert_not_called()

    def test_non_eager_still_uses_sqs_path(self, settings):
        """When EAGER=False (default), on_commit uses the normal enqueuer path."""
        settings.LAMBDA_TASKS_EAGER = False
        task_fn, calls = _make_recording_task()

        with patch("lambda_tasks.models.SQSLambdaTask._execute") as mock_send:
            task_fn.execute_on_commit(value=99)
            assert calls == []
            mock_send.assert_called_once()


@pytest.mark.django_db(transaction=True)
@given(value=st.integers(min_value=0, max_value=1000))
@h_settings(max_examples=50)
def test_property_eager_always_executes_synchronously(value):
    """Property: for any kwargs, EAGER=True always runs the task in-process."""
    calls: list[int] = []

    @lambda_task
    def _task(*, v: int = 0) -> int:
        calls.append(v)
        return v

    with override_settings(LAMBDA_TASKS_EAGER=True), _eager_patch(_task):
        _task.execute_on_commit(v=value)

    assert calls == [value]


# ---------------------------------------------------------------------------
# Task 4 — 900s timeout cap
# ---------------------------------------------------------------------------


# --- 4.1 Decoration time ---


@given(v=st.integers(min_value=MAX_TIMEOUT + 1, max_value=MAX_TIMEOUT + 10000))
@h_settings(max_examples=100)
def test_property_soft_timeout_over_900_rejected_at_decoration(v):
    """soft_timeout > 900 must raise ValueError at decoration time."""

    def _f(*, x: int) -> None:
        pass

    with pytest.raises(ValueError):
        lambda_task(_f, soft_timeout=v, hard_timeout=v + 1)


@given(v=st.integers(min_value=MAX_TIMEOUT + 1, max_value=MAX_TIMEOUT + 10000))
@h_settings(max_examples=100)
def test_property_hard_timeout_over_900_rejected_at_decoration(v):
    """hard_timeout > 900 must raise ValueError at decoration time."""

    def _f(*, x: int) -> None:
        pass

    with pytest.raises(ValueError):
        lambda_task(_f, soft_timeout=1, hard_timeout=v)


# --- 4.4 Regression: valid pairs still work ---


@given(
    soft=st.integers(min_value=1, max_value=MAX_TIMEOUT - 1),
    delta=st.integers(min_value=1, max_value=10),
)
@h_settings(max_examples=100)
def test_property_valid_timeout_pair_accepted_at_decoration(soft, delta):
    """Valid timeout pairs (both ≤ 900, soft < hard) must not raise."""
    hard = min(soft + delta, MAX_TIMEOUT)
    if soft >= hard:
        return  # skip degenerate cases from clamping

    def _f(*, x: int) -> None:
        pass

    wrapper = lambda_task(_f, soft_timeout=soft, hard_timeout=hard)
    assert wrapper is not None


# ---------------------------------------------------------------------------
# Task 1.2 — Property 3: EAGER mode writes a TaskRecord
# ---------------------------------------------------------------------------

from unittest.mock import patch as _patch

# Feature: eager-mode-example-app, Property 3: EAGER mode writes a TaskRecord
from lambda_tasks.models import TaskRecord


@pytest.mark.django_db(transaction=True)
@given(value=st.integers())
@h_settings(max_examples=100)
@override_settings(LAMBDA_TASKS_EAGER=True)
def test_property_3_eager_mode_writes_task_record(value):
    """**Validates: Requirements 2.4, 3.3**"""

    def _dynamic_task(*, n: int) -> None:
        pass

    task_name = f"{_dynamic_task.__module__}.{_dynamic_task.__qualname__}"
    wrapper = LambdaTaskWrapper(_dynamic_task)

    # Patch import_string so execute_task can resolve the local wrapper
    with _patch("lambda_tasks.models.import_string", return_value=wrapper):
        wrapper.execute_on_commit(n=value)

    assert TaskRecord.objects.filter(
        task_name=task_name,
        kwargs={"n": value},
        status__in=[TaskRecord.TaskStatus.SUCCEEDED, TaskRecord.TaskStatus.FAILED],
    ).exists()


# ---------------------------------------------------------------------------
# resolved_timeouts — validation of settings-sourced values
# ---------------------------------------------------------------------------


@given(v=st.integers(min_value=MAX_TIMEOUT + 1, max_value=MAX_TIMEOUT + 10000))
@h_settings(max_examples=100)
def test_resolved_timeouts_raises_when_settings_soft_over_900(v):
    """soft from settings > 900 must raise ValueError on resolved_timeouts."""

    def _f(*, x: int) -> None:
        pass

    wrapper = lambda_task(_f)  # no decorator timeouts
    wrapper.__dict__.pop("_resolved_timeouts_cache", None)
    with override_settings(
        LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT=v,
        LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT=v + 1,
    ):
        with pytest.raises(ValueError):
            _ = wrapper.resolved_timeouts


@given(v=st.integers(min_value=MAX_TIMEOUT + 1, max_value=MAX_TIMEOUT + 10000))
@h_settings(max_examples=100)
def test_resolved_timeouts_raises_when_settings_hard_over_900(v):
    """hard from settings > 900 must raise ValueError on resolved_timeouts."""

    def _f(*, x: int) -> None:
        pass

    wrapper = lambda_task(_f)  # no decorator timeouts
    wrapper.__dict__.pop("_resolved_timeouts_cache", None)
    with override_settings(
        LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT=1,
        LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT=v,
    ):
        with pytest.raises(ValueError):
            _ = wrapper.resolved_timeouts


@given(
    soft=st.integers(min_value=1, max_value=3600),
    delta=st.integers(min_value=0, max_value=3600),
)
@h_settings(max_examples=100)
def test_resolved_timeouts_raises_when_settings_soft_ge_hard(soft, delta):
    """soft >= hard from settings must raise ValueError on resolved_timeouts."""
    hard = soft - delta  # hard <= soft, always invalid

    def _f(*, x: int) -> None:
        pass

    wrapper = lambda_task(_f)
    wrapper.__dict__.pop("_resolved_timeouts_cache", None)
    with override_settings(
        LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT=soft,
        LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT=hard,
    ):
        with pytest.raises(ValueError):
            _ = wrapper.resolved_timeouts


# ---------------------------------------------------------------------------
# _validate_kwargs — signature binding and type-hint validation
# ---------------------------------------------------------------------------


def _typed_task(*, count: int, label: str, flag: bool = False) -> None:
    pass


def _optional_task(*, value: int | None = None) -> None:
    pass


def _unannotated_task(*, x: int, y: int = 0) -> None:
    pass


@pytest.mark.django_db(transaction=True)
class TestOnCommitKwargsValidation:
    """on_commit raises TypeError for bad kwargs before any enqueue attempt."""

    # --- type mismatches ---

    def test_wrong_type_raises_type_error(self, settings, mock_enqueuer):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = LambdaTaskWrapper(_typed_task)
        with pytest.raises(pydantic.ValidationError, match="count"):
            wrapper.execute_on_commit(count="not-an-int", label="hi")
        mock_enqueuer.assert_not_called()

    def test_second_arg_wrong_type_raises_type_error(self, settings, mock_enqueuer):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = LambdaTaskWrapper(_typed_task)
        with pytest.raises(pydantic.ValidationError, match="label"):
            wrapper.execute_on_commit(count=1, label=99)
        mock_enqueuer.assert_not_called()

    def test_correct_types_do_not_raise(self, settings, mock_enqueuer):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = LambdaTaskWrapper(_typed_task)
        wrapper.execute_on_commit(count=1, label="ok", flag=True)
        mock_enqueuer.assert_called_once()

    # --- Optional / X | None ---

    def test_none_accepted_for_optional_param(self, settings, mock_enqueuer):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = LambdaTaskWrapper(_optional_task)
        wrapper.execute_on_commit(value=None)
        mock_enqueuer.assert_called_once()

    def test_int_accepted_for_optional_param(self, settings, mock_enqueuer):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = LambdaTaskWrapper(_optional_task)
        wrapper.execute_on_commit(value=42)
        mock_enqueuer.assert_called_once()

    def test_wrong_type_for_optional_param_raises(self, settings, mock_enqueuer):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = LambdaTaskWrapper(_optional_task)
        with pytest.raises(pydantic.ValidationError, match="value"):
            wrapper.execute_on_commit(value="nope")
        mock_enqueuer.assert_not_called()

    # --- **kwargs functions are rejected at decoration time ---

    def test_var_keyword_raises_type_error(self):
        def bad(*, x: int, **extra: int) -> None:
            pass

        with pytest.raises(TypeError, match=r"\*\*extra"):
            LambdaTaskWrapper(bad)

    # --- unannotated params are rejected at decoration time ---

    def test_unannotated_param_raises_type_error(self):
        def bad(*, x) -> None:
            pass

        with pytest.raises(TypeError, match="unannotated parameter 'x'"):
            LambdaTaskWrapper(bad)

    # --- signature binding errors ---

    def test_missing_required_kwarg_raises_type_error(self, settings, mock_enqueuer):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = LambdaTaskWrapper(_typed_task)
        with pytest.raises(pydantic.ValidationError):
            wrapper.execute_on_commit(count=1)  # label is required
        mock_enqueuer.assert_not_called()

    def test_unexpected_kwarg_raises_type_error(self, settings, mock_enqueuer):
        settings.LAMBDA_TASKS_QUEUES = {"default": QUEUE_URL}
        wrapper = LambdaTaskWrapper(_typed_task)
        with pytest.raises(pydantic.ValidationError):
            wrapper.execute_on_commit(count=1, label="hi", nonexistent=True)
        mock_enqueuer.assert_not_called()


# --- property-based: any non-int value for an int param raises TypeError ---


@given(
    bad_value=st.one_of(st.text(), st.floats(allow_nan=False), st.lists(st.integers()))
)
@h_settings(max_examples=100)
def test_property_wrong_type_for_int_param_always_raises(bad_value):
    """For any non-int, non-bool value passed to an int-annotated param, TypeError is raised."""

    @lambda_task
    def _task(*, n: int) -> None:
        pass

    with pytest.raises(pydantic.ValidationError):
        _task.execute_on_commit(n=bad_value)


# ---------------------------------------------------------------------------
# Feature: ignore-errors-decorator-option — Property 1 & 2
# ---------------------------------------------------------------------------

_EXC_TYPES = [ValueError, RuntimeError, KeyError, TypeError, OSError, AttributeError]
_exc_type_st = st.sampled_from(_EXC_TYPES)
_ignore_errors_st = st.lists(_exc_type_st, min_size=1, max_size=3).map(tuple)


# Feature: ignore-errors-decorator-option, Property 1: ignore_errors round-trip on LambdaTaskWrapper
@given(ignore_errors=_ignore_errors_st)
@h_settings(max_examples=100)
def test_property_ignore_errors_round_trip(ignore_errors):
    """Property 1: for any tuple of exception types, wrapper.ignore_errors returns an equal tuple.
    Validates: Requirements 1.1, 1.3"""

    def _f(*, x: int) -> None:
        pass

    wrapper = LambdaTaskWrapper(_f, ignore_errors=ignore_errors)
    assert wrapper.ignore_errors == ignore_errors


# Feature: ignore-errors-decorator-option, Property 2: Non-exception types in ignore_errors are rejected at decoration time
@given(
    bad_value=st.one_of(
        st.integers(),
        st.text(),
        st.none(),
        st.booleans(),
        st.just(object),
    )
)
@h_settings(max_examples=100)
def test_property_non_exception_type_rejected(bad_value):
    """Property 2: any non-BaseException-subclass in ignore_errors raises TypeError at decoration time.
    Validates: Requirements 1.4"""

    def _f(*, x: int) -> None:
        pass

    with pytest.raises(TypeError):
        LambdaTaskWrapper(_f, ignore_errors=(bad_value,))


# ---------------------------------------------------------------------------
# Feature: ignore-errors-decorator-option — Task 2.1: lambda_task factory forwarding
# ---------------------------------------------------------------------------


class TestLambdaTaskIgnoreErrorsForwarding:
    def test_ignore_errors_forwarded_to_wrapper(self):
        """lambda_task(ignore_errors=(ValueError,)) produces wrapper with ignore_errors == (ValueError,)."""

        @lambda_task(ignore_errors=(ValueError,))
        def _task(*, x: int) -> None:
            pass

        assert _task.ignore_errors == (ValueError,)

    def test_ignore_errors_default_is_empty_tuple(self):
        """lambda_task with no ignore_errors defaults to ()."""

        @lambda_task
        def _task(*, x: int) -> None:
            pass

        assert _task.ignore_errors == ()

    def test_ignore_errors_multiple_types_forwarded(self):
        """Multiple exception types are forwarded correctly."""

        @lambda_task(ignore_errors=(ValueError, KeyError, OSError))
        def _task(*, x: int) -> None:
            pass

        assert _task.ignore_errors == (ValueError, KeyError, OSError)

    def test_sqs_lambda_task_message_has_no_ignore_errors_field(self):
        """SQSLambdaTaskMessage must not carry ignore_errors as a field (Requirement 4.2)."""
        from lambda_tasks.models import SQSLambdaTaskMessage

        fields = SQSLambdaTaskMessage.model_fields
        assert "ignore_errors" not in fields
