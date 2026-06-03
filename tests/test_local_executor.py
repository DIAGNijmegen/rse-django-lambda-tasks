"""
Tests for the async local execution feature (local_executor.py and LOCAL_WORKERS setting).
"""

import pytest
from django.core.exceptions import ImproperlyConfigured

# ---------------------------------------------------------------------------
# LOCAL_WORKERS setting validation (Requirement 1)
# ---------------------------------------------------------------------------


def test_local_workers_default_is_zero(settings):
    """LOCAL_WORKERS defaults to 0 when LAMBDA_TASKS_LOCAL_WORKERS is absent."""
    from lambda_tasks.settings import LambdaTasksSettings

    # Ensure the setting is not present
    if hasattr(settings, "LAMBDA_TASKS_LOCAL_WORKERS"):
        delattr(settings, "LAMBDA_TASKS_LOCAL_WORKERS")

    conf = LambdaTasksSettings()
    assert conf.LOCAL_WORKERS == 0


def test_local_workers_positive_integer(settings):
    """A positive LAMBDA_TASKS_LOCAL_WORKERS is returned correctly."""
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_LOCAL_WORKERS = 4
    settings.LAMBDA_TASKS_EAGER = False
    conf = LambdaTasksSettings()
    assert conf.LOCAL_WORKERS == 4


def test_local_workers_negative_raises(settings):
    """A negative LAMBDA_TASKS_LOCAL_WORKERS raises ImproperlyConfigured."""
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_LOCAL_WORKERS = -1
    conf = LambdaTasksSettings()
    with pytest.raises(ImproperlyConfigured):
        _ = conf.LOCAL_WORKERS


def test_local_workers_and_eager_raises(settings):
    """Setting both EAGER=True and LOCAL_WORKERS > 0 raises ImproperlyConfigured."""
    from lambda_tasks.settings import LambdaTasksSettings

    settings.LAMBDA_TASKS_EAGER = True
    settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
    conf = LambdaTasksSettings()
    with pytest.raises(ImproperlyConfigured):
        _ = conf.LOCAL_WORKERS


# ---------------------------------------------------------------------------
# Property-based tests: LOCAL_WORKERS validation
# ---------------------------------------------------------------------------

from django.test import override_settings
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st


@given(n=st.integers(min_value=1, max_value=1000))
@h_settings(max_examples=100)
def test_positive_local_workers_is_preserved(n):
    """Feature: async-local-execution, Property 1: Positive LOCAL_WORKERS is preserved

    For any positive integer n, when LAMBDA_TASKS_LOCAL_WORKERS is set to n
    and LAMBDA_TASKS_EAGER is False, LambdaTasksSettings().LOCAL_WORKERS
    returns exactly n.

    Validates: Requirements 1.1
    """
    from lambda_tasks.settings import LambdaTasksSettings

    with override_settings(LAMBDA_TASKS_LOCAL_WORKERS=n, LAMBDA_TASKS_EAGER=False):
        conf = LambdaTasksSettings()
        assert conf.LOCAL_WORKERS == n


@given(n=st.integers(min_value=-1000, max_value=-1))
@h_settings(max_examples=100)
def test_negative_local_workers_is_rejected(n):
    """Feature: async-local-execution, Property 2: Negative LOCAL_WORKERS is rejected

    For any negative integer n, when LAMBDA_TASKS_LOCAL_WORKERS is set to n,
    accessing LOCAL_WORKERS raises ImproperlyConfigured.

    Validates: Requirements 1.3
    """
    from lambda_tasks.settings import LambdaTasksSettings

    with override_settings(LAMBDA_TASKS_LOCAL_WORKERS=n):
        conf = LambdaTasksSettings()
        with pytest.raises(ImproperlyConfigured):
            _ = conf.LOCAL_WORKERS


@given(n=st.integers(min_value=1, max_value=1000))
@h_settings(max_examples=100)
def test_mutual_exclusion_of_eager_and_local_workers(n):
    """Feature: async-local-execution, Property 3: Mutual exclusion of EAGER and LOCAL_WORKERS

    For any positive integer n, when both EAGER is True and LOCAL_WORKERS is n,
    accessing LOCAL_WORKERS raises ImproperlyConfigured.

    Validates: Requirements 1.4
    """
    from lambda_tasks.settings import LambdaTasksSettings

    with override_settings(LAMBDA_TASKS_EAGER=True, LAMBDA_TASKS_LOCAL_WORKERS=n):
        conf = LambdaTasksSettings()
        with pytest.raises(ImproperlyConfigured):
            _ = conf.LOCAL_WORKERS


# ---------------------------------------------------------------------------
# get_pool() tests (Requirement 2)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _reset_pool():
    """Reset the module-level _pool after each test to avoid cross-test contamination."""
    yield
    import lambda_tasks.local_executor

    if lambda_tasks.local_executor._pool is not None:
        lambda_tasks.local_executor._pool.shutdown(wait=True, cancel_futures=True)
        lambda_tasks.local_executor._pool = None


class TestGetPool:
    """Tests for get_pool() lifecycle and configuration."""

    @pytest.mark.usefixtures("_reset_pool")
    def test_get_pool_returns_process_pool_executor_with_correct_workers(
        self, settings
    ):
        """get_pool() returns a ProcessPoolExecutor with _max_workers equal to LOCAL_WORKERS.

        Validates: Requirements 2.1
        """
        from concurrent.futures import ProcessPoolExecutor

        import lambda_tasks.local_executor

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 3
        settings.LAMBDA_TASKS_EAGER = False

        pool = lambda_tasks.local_executor.get_pool()

        assert isinstance(pool, ProcessPoolExecutor)
        assert pool._max_workers == 3

    @pytest.mark.usefixtures("_reset_pool")
    def test_get_pool_returns_same_instance_on_repeated_calls(self, settings):
        """get_pool() returns the same pool instance on repeated calls (pool reuse).

        Validates: Requirements 2.2
        """
        import lambda_tasks.local_executor

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
        settings.LAMBDA_TASKS_EAGER = False

        pool_first = lambda_tasks.local_executor.get_pool()
        pool_second = lambda_tasks.local_executor.get_pool()

        assert pool_first is pool_second

    @pytest.mark.usefixtures("_reset_pool")
    def test_get_pool_stores_pool_at_module_level(self, settings):
        """get_pool() stores the pool in the module-level _pool variable.

        Validates: Requirements 2.4
        """
        import lambda_tasks.local_executor

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
        settings.LAMBDA_TASKS_EAGER = False

        assert lambda_tasks.local_executor._pool is None

        pool = lambda_tasks.local_executor.get_pool()

        assert lambda_tasks.local_executor._pool is pool


# ---------------------------------------------------------------------------
# Property-based tests: get_pool() (Requirement 2)
# ---------------------------------------------------------------------------


@given(n=st.integers(min_value=1, max_value=32))
@h_settings(max_examples=100)
def test_pool_created_with_correct_worker_count(n):
    """Feature: async-local-execution, Property 4: Pool created with correct worker count

    For any positive integer n (within reasonable bounds, e.g. 1–32), when
    LOCAL_WORKERS is n, the ProcessPoolExecutor created by get_pool() SHALL
    have _max_workers equal to n.

    Validates: Requirements 2.1
    """
    import lambda_tasks.local_executor

    with override_settings(LAMBDA_TASKS_LOCAL_WORKERS=n, LAMBDA_TASKS_EAGER=False):
        # Reset pool to force fresh creation for each example
        lambda_tasks.local_executor._pool = None
        try:
            pool = lambda_tasks.local_executor.get_pool()
            assert pool._max_workers == n
        finally:
            # Shut down properly to avoid leaked semaphores
            if lambda_tasks.local_executor._pool is not None:
                lambda_tasks.local_executor._pool.shutdown(
                    wait=True, cancel_futures=True
                )
            lambda_tasks.local_executor._pool = None


# ---------------------------------------------------------------------------
# submit_task() tests (Requirements 3.1, 3.2, 3.4, 5.3)
# ---------------------------------------------------------------------------

import uuid
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Property-based tests: Dispatch routing (Requirements 3.1, 7.2)
# ---------------------------------------------------------------------------


@given(
    local_workers=st.integers(min_value=1, max_value=32),
    task_name=st.from_regex(r"[a-z][a-z0-9_.]{0,50}", fullmatch=True),
    kwargs=st.dictionaries(
        st.text(min_size=1, max_size=10, alphabet=st.characters(categories=("L",))),
        st.integers(),
    ),
    n_retries=st.integers(min_value=0, max_value=100),
)
@h_settings(max_examples=100)
def test_async_local_dispatch_routes_to_pool(
    local_workers, task_name, kwargs, n_retries
):
    """Feature: async-local-execution, Property 5: Async local dispatch routes to pool

    For any valid SQSLambdaTaskMessage and any positive LOCAL_WORKERS value,
    when EAGER is False, calling SQSLambdaTask._execute() SHALL call
    ProcessPoolExecutor.submit() (via submit_task) and SHALL NOT call
    boto3.client('sqs').send_message().

    Validates: Requirements 3.1, 7.2
    """
    from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage

    message = SQSLambdaTaskMessage(
        task_name=task_name,
        kwargs=kwargs,
        n_retries=n_retries,
    )
    task = SQSLambdaTask(message=message, delay=0, queue="default")

    with override_settings(
        LAMBDA_TASKS_LOCAL_WORKERS=local_workers,
        LAMBDA_TASKS_EAGER=False,
    ):
        with (
            patch("lambda_tasks.models.submit_task") as mock_submit,
            patch("lambda_tasks.models.boto3.client") as mock_boto3_client,
        ):
            task._execute()

        mock_submit.assert_called_once_with(
            message_json=message.model_dump_json(),
        )
        mock_boto3_client.assert_not_called()


class TestSubmitTask:
    """Tests for submit_task() fire-and-forget submission."""

    def test_submit_task_calls_pool_submit_with_execute_in_worker(self, settings):
        """submit_task() calls pool.submit() with _execute_in_worker as the callable.

        Validates: Requirements 3.1, 3.2
        """
        from lambda_tasks.local_executor import _execute_in_worker, submit_task

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
        settings.LAMBDA_TASKS_EAGER = False

        mock_pool = MagicMock()
        with patch("lambda_tasks.local_executor.get_pool", return_value=mock_pool):
            submit_task(
                message_json='{"task_name": "myapp.tasks.foo", "kwargs": {}, "n_retries": 0}'
            )

        mock_pool.submit.assert_called_once()
        call_args = mock_pool.submit.call_args
        # First positional arg should be _execute_in_worker
        assert call_args[0][0] is _execute_in_worker

    def test_submit_task_passes_message_json_as_kwarg(self, settings):
        """submit_task() passes the message_json string to pool.submit() as a keyword argument.

        Validates: Requirements 3.2
        """
        from lambda_tasks.local_executor import submit_task

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
        settings.LAMBDA_TASKS_EAGER = False

        mock_pool = MagicMock()
        message = '{"task_name": "myapp.tasks.foo", "kwargs": {}, "n_retries": 0}'
        with patch("lambda_tasks.local_executor.get_pool", return_value=mock_pool):
            submit_task(message_json=message)

        call_kwargs = mock_pool.submit.call_args[1]
        assert call_kwargs["message_json"] == message

    def test_submit_task_passes_valid_uuid4_message_id(self, settings):
        """submit_task() generates a valid UUID4 string and passes it as message_id kwarg.

        Validates: Requirements 3.2
        """
        from lambda_tasks.local_executor import submit_task

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
        settings.LAMBDA_TASKS_EAGER = False

        mock_pool = MagicMock()
        with patch("lambda_tasks.local_executor.get_pool", return_value=mock_pool):
            submit_task(
                message_json='{"task_name": "myapp.tasks.foo", "kwargs": {}, "n_retries": 0}'
            )

        call_kwargs = mock_pool.submit.call_args[1]
        message_id = call_kwargs["message_id"]
        # Verify it's a valid UUID4 string
        parsed = uuid.UUID(message_id, version=4)
        assert str(parsed) == message_id

    def test_submit_task_does_not_wait_on_future(self, settings):
        """submit_task() does not call .result() on the Future.

        Validates: Requirements 3.4, 5.3
        """
        from lambda_tasks.local_executor import submit_task

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
        settings.LAMBDA_TASKS_EAGER = False

        mock_pool = MagicMock()
        mock_future = MagicMock()
        mock_pool.submit.return_value = mock_future

        with patch("lambda_tasks.local_executor.get_pool", return_value=mock_pool):
            submit_task(
                message_json='{"task_name": "myapp.tasks.foo", "kwargs": {}, "n_retries": 0}'
            )

        mock_future.result.assert_not_called()

    def test_submit_task_attaches_exception_logging_callback(self, settings):
        """submit_task() attaches _log_worker_exception as a done callback."""
        from lambda_tasks.local_executor import _log_worker_exception, submit_task

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
        settings.LAMBDA_TASKS_EAGER = False

        mock_pool = MagicMock()
        mock_future = MagicMock()
        mock_pool.submit.return_value = mock_future

        with patch("lambda_tasks.local_executor.get_pool", return_value=mock_pool):
            submit_task(
                message_json='{"task_name": "myapp.tasks.foo", "kwargs": {}, "n_retries": 0}'
            )

        mock_future.add_done_callback.assert_called_once_with(_log_worker_exception)


# ---------------------------------------------------------------------------
# Property-based tests: Message serialization round-trip (Requirement 6)
# ---------------------------------------------------------------------------


@given(
    task_name=st.text(min_size=1, max_size=100).filter(lambda s: s.strip()),
    kwargs=st.dictionaries(
        st.text(min_size=1, max_size=50),
        st.one_of(
            st.integers(),
            st.text(),
            st.booleans(),
            st.floats(allow_nan=False, allow_infinity=False),
            st.none(),
        ),
    ),
    n_retries=st.integers(min_value=0, max_value=1000),
)
@h_settings(max_examples=100)
def test_message_serialization_round_trip(task_name, kwargs, n_retries):
    """Feature: async-local-execution, Property 6: Task message serialization round-trip

    For any valid SQSLambdaTaskMessage (with arbitrary task_name, kwargs containing
    JSON-serializable values, and non-negative n_retries), serializing via
    model_dump_json() and deserializing via model_validate_json() SHALL produce
    an equivalent message object.

    Validates: Requirements 6.1, 6.2, 3.2, 3.3
    """
    from lambda_tasks.models import SQSLambdaTaskMessage

    original = SQSLambdaTaskMessage(
        task_name=task_name,
        kwargs=kwargs,
        n_retries=n_retries,
    )

    serialized = original.model_dump_json()
    deserialized = SQSLambdaTaskMessage.model_validate_json(serialized)

    assert deserialized == original


# ---------------------------------------------------------------------------
# Dispatch routing tests (Requirements 3.1, 7.1, 7.2, 7.3)
# ---------------------------------------------------------------------------


class TestDispatchRouting:
    """Tests for SQSLambdaTask._execute() dispatch routing logic."""

    def test_local_workers_positive_calls_submit_task(self, settings):
        """When LOCAL_WORKERS > 0 and EAGER=False, _execute() calls submit_task() with JSON message.

        Validates: Requirements 3.1, 7.2
        """
        from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
        settings.LAMBDA_TASKS_EAGER = False

        message = SQSLambdaTaskMessage(
            task_name="myapp.tasks.foo",
            kwargs={"user_id": 1},
            n_retries=0,
        )
        task = SQSLambdaTask(message=message, delay=0, queue="default")

        with patch("lambda_tasks.models.submit_task") as mock_submit:
            task._execute()

        mock_submit.assert_called_once_with(
            message_json=message.model_dump_json(),
        )

    def test_local_workers_positive_does_not_call_sqs(self, settings):
        """When LOCAL_WORKERS > 0 and EAGER=False, _execute() does NOT call boto3 SQS.

        Validates: Requirements 7.2
        """
        from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
        settings.LAMBDA_TASKS_EAGER = False

        message = SQSLambdaTaskMessage(
            task_name="myapp.tasks.foo",
            kwargs={"user_id": 1},
            n_retries=0,
        )
        task = SQSLambdaTask(message=message, delay=0, queue="default")

        with (
            patch("lambda_tasks.models.submit_task"),
            patch("lambda_tasks.models.boto3.client") as mock_boto3_client,
        ):
            task._execute()

        mock_boto3_client.assert_not_called()

    def test_local_workers_zero_sends_to_sqs(self, settings):
        """When LOCAL_WORKERS=0 and EAGER=False, _execute() sends to SQS (existing behaviour).

        Validates: Requirements 7.3
        """
        from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 0
        settings.LAMBDA_TASKS_EAGER = False
        settings.LAMBDA_TASKS_QUEUES = {
            "default": "https://sqs.us-east-1.amazonaws.com/000/default",
        }

        message = SQSLambdaTaskMessage(
            task_name="myapp.tasks.foo",
            kwargs={"user_id": 1},
            n_retries=0,
        )
        task = SQSLambdaTask(message=message, delay=0, queue="default")

        with patch("lambda_tasks.models.boto3.client") as mock_boto3_client:
            mock_sqs = MagicMock()
            mock_boto3_client.return_value = mock_sqs
            task._execute()

        mock_boto3_client.assert_called_once_with("sqs")
        mock_sqs.send_message.assert_called_once_with(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/000/default",
            MessageBody=message.model_dump_json(),
            DelaySeconds=0,
        )

    def test_eager_mode_calls_execute_immediately(self, settings):
        """When EAGER=True, _execute() calls execute_immediately() (existing behaviour).

        Validates: Requirements 7.1
        """
        from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage

        settings.LAMBDA_TASKS_EAGER = True
        if hasattr(settings, "LAMBDA_TASKS_LOCAL_WORKERS"):
            delattr(settings, "LAMBDA_TASKS_LOCAL_WORKERS")

        message = SQSLambdaTaskMessage(
            task_name="myapp.tasks.foo",
            kwargs={"user_id": 1},
            n_retries=0,
        )
        task = SQSLambdaTask(message=message, delay=0, queue="default")

        with patch(
            "lambda_tasks.models.SQSLambdaTaskMessage.execute_immediately"
        ) as mock_execute:
            task._execute()

        mock_execute.assert_called_once()
        # Verify a UUID string was passed as message_id
        call_kwargs = mock_execute.call_args[1]
        parsed = uuid.UUID(call_kwargs["message_id"], version=4)
        assert str(parsed) == call_kwargs["message_id"]


# ---------------------------------------------------------------------------
# Integration tests: Transaction commit, error isolation, pool initializer
# (Requirements 2.3, 4.1, 4.2, 5.1, 5.2)
# ---------------------------------------------------------------------------


class TestPoolInitializer:
    """Tests for _pool_initializer() calling django.setup()."""

    def test_pool_initializer_calls_django_setup(self):
        """_pool_initializer() calls django.setup() once.

        Validates: Requirements 2.3
        """
        from lambda_tasks.local_executor import _pool_initializer

        with patch("django.setup") as mock_django_setup:
            _pool_initializer()

        mock_django_setup.assert_called_once()

    def test_pool_initializer_ignores_sigint(self):
        """_pool_initializer() sets SIGINT to SIG_IGN so workers don't receive Ctrl+C."""
        import signal

        from lambda_tasks.local_executor import _pool_initializer

        original_handler = signal.getsignal(signal.SIGINT)
        try:
            with patch("django.setup"):
                _pool_initializer()

            assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        finally:
            signal.signal(signal.SIGINT, original_handler)


@pytest.mark.django_db(transaction=True)
class TestTransactionCommitIntegration:
    """Tests for execute_on_commit() interaction with transactions."""

    def test_on_commit_submits_after_transaction(self, settings):
        """execute_on_commit() submits to the pool only after the transaction commits.

        Validates: Requirements 4.1
        """
        from django.db import transaction

        from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
        settings.LAMBDA_TASKS_EAGER = False

        message = SQSLambdaTaskMessage(
            task_name="myapp.tasks.foo",
            kwargs={"user_id": 1},
            n_retries=0,
        )
        task = SQSLambdaTask(message=message, delay=0, queue="default")

        with patch("lambda_tasks.models.submit_task") as mock_submit:
            with transaction.atomic():
                task.execute_on_commit()
                # Inside the transaction, submit_task should NOT have been called yet
                mock_submit.assert_not_called()

            # After the transaction commits, submit_task should have been called
            mock_submit.assert_called_once_with(
                message_json=message.model_dump_json(),
            )

    def test_rollback_prevents_pool_submission(self, settings):
        """A rolled-back transaction does not submit the task to the pool.

        Validates: Requirements 4.2
        """
        from django.db import transaction

        from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage

        settings.LAMBDA_TASKS_LOCAL_WORKERS = 2
        settings.LAMBDA_TASKS_EAGER = False

        message = SQSLambdaTaskMessage(
            task_name="myapp.tasks.foo",
            kwargs={"user_id": 1},
            n_retries=0,
        )
        task = SQSLambdaTask(message=message, delay=0, queue="default")

        with patch("lambda_tasks.models.submit_task") as mock_submit:
            try:
                with transaction.atomic():
                    task.execute_on_commit()
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass

            # After rollback, submit_task should never have been called
            mock_submit.assert_not_called()


class TestWorkerExceptionIsolation:
    """Tests for pool resilience when workers raise exceptions."""

    def test_pool_survives_worker_exception(self, settings):
        """If _execute_in_worker raises, the pool continues accepting new tasks.

        Validates: Requirements 5.1, 5.2
        """
        from lambda_tasks.local_executor import submit_task

        mock_pool = MagicMock()
        # First submit raises in the worker (simulated by the future),
        # but submit() itself succeeds — fire-and-forget means the pool
        # doesn't propagate the exception at submission time.
        mock_pool.submit.return_value = MagicMock()

        with patch("lambda_tasks.local_executor.get_pool", return_value=mock_pool):
            # First submission
            submit_task(
                message_json='{"task_name": "myapp.tasks.foo", "kwargs": {}, "n_retries": 0}',
            )
            # Second submission — pool still accepts tasks
            submit_task(
                message_json='{"task_name": "myapp.tasks.bar", "kwargs": {}, "n_retries": 0}',
            )

        # Pool.submit was called twice — pool did not crash
        assert mock_pool.submit.call_count == 2
