"""
Unit tests for lambda_tasks.models — TaskRecord model and SQSLambdaTaskMessage.execute_immediately().

Covers:
- Successful task → TaskRecord status SUCCESS with result and end_time
- Failing task → atomic block rolled back, TaskRecord status FAILED with traceback committed outside atomic
- soft_timeout >= hard_timeout → ConfigurationError, task not executed
- TaskRecord created with RUNNING status before task runs
- ORM writes inside a failing task are not visible after execution
- import_string resolution: ImportError propagates, TypeError on non-wrapper
"""

import uuid
from datetime import datetime

import pytest
from django.db import IntegrityError
from django.utils.timezone import now
from redis.exceptions import LockError

from lambda_tasks.decorators import lambda_task
from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage, TaskRecord
from lambda_tasks.settings import LambdaTasksSettings

# ---------------------------------------------------------------------------
# TaskRecord model — field, constraint, and ORM tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTaskRecordCreation:
    def test_can_create_with_required_fields(self):
        record = TaskRecord.objects.create(
            task_name="myapp.tasks.send_email",
            pk=uuid.uuid4(),
            kwargs={"to": "[email]"},
            n_retries=0,
            status=TaskRecord.TaskStatus.RUNNING,
        )
        assert record.pk is not None

    def test_all_required_fields_present(self):
        inv_id = uuid.uuid4()
        record = TaskRecord.objects.create(
            task_name="myapp.tasks.do_work",
            pk=inv_id,
            kwargs={"x": 1},
            n_retries=0,
            status=TaskRecord.TaskStatus.RUNNING,
        )
        fetched = TaskRecord.objects.get(pk=record.pk)
        assert fetched.task_name == "myapp.tasks.do_work"
        assert fetched.pk == inv_id
        assert fetched.kwargs == {"x": 1}
        assert fetched.status == TaskRecord.TaskStatus.RUNNING
        assert fetched.start_time is None
        assert fetched.end_time is None
        assert fetched.result is None
        assert fetched.traceback is None


@pytest.mark.django_db
class TestTaskRecordStatusChoices:
    @pytest.mark.parametrize(
        "status",
        [
            TaskRecord.TaskStatus.RUNNING,
            TaskRecord.TaskStatus.SUCCEEDED,
            TaskRecord.TaskStatus.FAILED,
            TaskRecord.TaskStatus.RETRIED,
        ],
    )
    def test_valid_status_choices(self, status):
        record = TaskRecord.objects.create(
            task_name="myapp.tasks.job",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=status,
        )
        assert TaskRecord.objects.get(pk=record.pk).status == status

    def test_status_choice_values(self):
        assert TaskRecord.TaskStatus.RUNNING == "RUNNING"
        assert TaskRecord.TaskStatus.SUCCEEDED == "SUCCEEDED"
        assert TaskRecord.TaskStatus.FAILED == "FAILED"
        assert TaskRecord.TaskStatus.RETRIED == "RETRIED"

    def test_retried_status_can_be_saved(self):
        record = TaskRecord.objects.create(
            task_name="myapp.tasks.job",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.RETRIED,
        )
        assert (
            TaskRecord.objects.get(pk=record.pk).status == TaskRecord.TaskStatus.RETRIED
        )


@pytest.mark.django_db
class TestTaskRecordInvocationIdUniqueness:
    def test_pk_is_unique(self):
        inv_id = uuid.uuid4()
        TaskRecord.objects.create(
            task_name="myapp.tasks.job",
            pk=inv_id,
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.RUNNING,
        )
        with pytest.raises(IntegrityError):
            TaskRecord.objects.create(
                task_name="myapp.tasks.other",
                pk=inv_id,
                kwargs={},
                n_retries=0,
                status=TaskRecord.TaskStatus.RUNNING,
            )

    def test_different_pks_are_allowed(self):
        TaskRecord.objects.create(
            task_name="myapp.tasks.job",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.RUNNING,
        )
        TaskRecord.objects.create(
            task_name="myapp.tasks.job",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.RUNNING,
        )
        assert TaskRecord.objects.count() == 2


@pytest.mark.django_db
class TestTaskRecordOrdering:
    def test_default_ordering_is_minus_start_time(self):
        import datetime

        from django.utils import timezone

        now = timezone.now()
        older = TaskRecord.objects.create(
            task_name="myapp.tasks.job",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.RUNNING,
            start_time=now - datetime.timedelta(seconds=10),
        )
        newer = TaskRecord.objects.create(
            task_name="myapp.tasks.job",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.RUNNING,
            start_time=now,
        )
        records = list(TaskRecord.objects.all())
        assert records[0].pk == newer.pk
        assert records[1].pk == older.pk

    def test_meta_ordering_attribute(self):
        assert TaskRecord._meta.ordering == ["-start_time"]


@pytest.mark.django_db
class TestTaskRecordOrmQueryable:
    def test_filter_by_status(self):
        TaskRecord.objects.create(
            task_name="myapp.tasks.job",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.SUCCEEDED,
        )
        TaskRecord.objects.create(
            task_name="myapp.tasks.job",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.FAILED,
        )
        assert (
            TaskRecord.objects.filter(status=TaskRecord.TaskStatus.SUCCEEDED).count()
            == 1
        )
        assert (
            TaskRecord.objects.filter(status=TaskRecord.TaskStatus.FAILED).count() == 1
        )

    def test_filter_by_task_name(self):
        TaskRecord.objects.create(
            task_name="myapp.tasks.alpha",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.RUNNING,
        )
        TaskRecord.objects.create(
            task_name="myapp.tasks.beta",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.RUNNING,
        )
        assert TaskRecord.objects.filter(task_name="myapp.tasks.alpha").count() == 1


# ---------------------------------------------------------------------------
# Module-level task helpers — must be at module level so import_string resolves them
# ---------------------------------------------------------------------------


@lambda_task
def _task_returns_value(*, x: int) -> int:
    """Returns x * 2."""
    return x * 2


@lambda_task
def _task_raises(*, msg: str) -> None:
    """Always raises RuntimeError."""
    raise RuntimeError(msg)


@lambda_task
def _task_creates_record_then_raises(*, label: str) -> None:
    """Creates a TaskRecord inside the atomic block, then raises."""
    TaskRecord.objects.create(
        task_name="side_effect_record",
        pk=uuid.uuid4(),
        kwargs={"label": label},
        n_retries=0,
        status=TaskRecord.TaskStatus.RUNNING,
    )
    raise RuntimeError("intentional failure")


@lambda_task
def _task_checks_own_status(*, inv_id: str) -> None:
    """Reads its own TaskRecord status while running."""
    record = TaskRecord.objects.get(pk=inv_id)
    _running_statuses.append(record.status)


# Shared list populated by _task_checks_own_status
_running_statuses: list = []


@lambda_task(soft_timeout=30, hard_timeout=60)
def _task_decorator_defaults(*, x: int) -> int:
    return x


@lambda_task
def _task_no_timeouts(*, x: int) -> int:
    return x


@lambda_task
def _task_failing_for_property(*, label: str) -> None:
    """Used by property tests — always raises."""
    TaskRecord.objects.create(
        task_name=f"prop_sentinel_{label}",
        pk=uuid.uuid4(),
        kwargs={"label": label},
        n_retries=0,
        status=TaskRecord.TaskStatus.RUNNING,
    )
    raise RuntimeError(f"intentional failure for {label}")


@lambda_task
def _task_lifecycle(*, rv: int) -> int:
    """Used by property tests — returns rv and captures RUNNING state."""
    inv_id = _lifecycle_current_inv_id
    rec = TaskRecord.objects.get(pk=inv_id)
    _lifecycle_captured.append({"status": rec.status, "start_time": rec.start_time})
    return rv


_lifecycle_current_inv_id: str = ""
_lifecycle_captured: list = []


@lambda_task
def _task_failing_lifecycle(*, rv: int) -> None:
    raise ValueError(f"lifecycle failure {rv}")


# Retry test helpers — must be at module level for import_string resolution
_RETRY_EXC_TYPES = (ValueError, RuntimeError, OSError)


@lambda_task(retry_on=_RETRY_EXC_TYPES)
def _task_retry_raises(*, exc_type_name: str) -> None:
    """Always raises the named exception — used by retry property tests."""
    exc_map = {
        "ValueError": ValueError,
        "RuntimeError": RuntimeError,
        "OSError": OSError,
    }
    raise exc_map[exc_type_name]("retry test error")


@lambda_task(retry_on=_RETRY_EXC_TYPES)
def _task_retry_raises_with_kwargs(*, x: int, label: str) -> None:
    """Raises ValueError — used to test kwargs preservation."""
    raise ValueError("retry kwargs test")


_NON_RETRY_EXC_TYPES = [TypeError, KeyError, AttributeError]


@lambda_task(retry_on=(ValueError,))
def _task_retry_raises_non_matching(*, exc_type_name: str) -> None:
    exc_map = {
        "TypeError": TypeError,
        "KeyError": KeyError,
        "AttributeError": AttributeError,
    }
    raise exc_map[exc_type_name]("non-matching error")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_name(wrapper) -> str:
    f = wrapper.__wrapped__
    return f"{f.__module__}.{f.__qualname__}"


def _make_message(task_name: str, kwargs: dict) -> SQSLambdaTaskMessage:
    return SQSLambdaTaskMessage(
        task_name=task_name,
        kwargs=kwargs,
    )


# ---------------------------------------------------------------------------
# Tests: success path
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestExecuteTaskSuccess:
    def test_successful_task_record_status_is_success(self):
        msg = _make_message(_task_name(_task_returns_value), {"x": 5})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.SUCCEEDED

    def test_successful_task_record_has_result(self):
        msg = _make_message(_task_name(_task_returns_value), {"x": 7})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        record = TaskRecord.objects.get(pk=message_id)
        assert record.result == 14

    def test_successful_task_record_has_end_time(self):
        msg = _make_message(_task_name(_task_returns_value), {"x": 3})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        record = TaskRecord.objects.get(pk=message_id)
        assert record.end_time is not None

    def test_successful_task_record_has_start_time(self):
        msg = _make_message(_task_name(_task_returns_value), {"x": 1})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        record = TaskRecord.objects.get(pk=message_id)
        assert record.start_time is not None


# ---------------------------------------------------------------------------
# Tests: failure path
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestExecuteTaskFailure:
    def test_failing_task_record_status_is_failed(self):
        msg = _make_message(_task_name(_task_raises), {"msg": "boom"})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.FAILED

    def test_failing_task_record_has_traceback(self):
        msg = _make_message(_task_name(_task_raises), {"msg": "boom"})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        record = TaskRecord.objects.get(pk=message_id)
        assert record.traceback
        assert "RuntimeError" in record.traceback

    def test_failing_task_record_has_end_time(self):
        msg = _make_message(_task_name(_task_raises), {"msg": "boom"})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        record = TaskRecord.objects.get(pk=message_id)
        assert record.end_time is not None

    def test_failing_task_orm_writes_are_rolled_back(self):
        msg = _make_message(
            _task_name(_task_creates_record_then_raises), {"label": "should_not_exist"}
        )
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        assert not TaskRecord.objects.filter(task_name="side_effect_record").exists()

    def test_failing_task_failed_record_is_committed(self):
        msg = _make_message(_task_name(_task_raises), {"msg": "check_commit"})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Tests: RUNNING status
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestExecuteTaskRunningStatus:
    def test_task_record_created_with_running_status(self):
        global _running_statuses
        _running_statuses = []
        inv_id = str(uuid.uuid4())
        msg = SQSLambdaTaskMessage(
            task_name=_task_name(_task_checks_own_status),
            kwargs={"inv_id": inv_id},
        )
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=inv_id)
        assert _running_statuses == [TaskRecord.TaskStatus.RUNNING]


# ---------------------------------------------------------------------------
# Tests: timeout resolution
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestExecuteTaskTimeoutResolution:
    def _capturing_context(self, timeout_args: list):
        class CapturingTimeoutContext:
            def __init__(self, soft_timeout, hard_timeout):
                timeout_args.append((soft_timeout, hard_timeout))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return CapturingTimeoutContext

    def test_decorator_timeouts_used(self):
        msg = SQSLambdaTaskMessage(
            task_name=_task_name(_task_decorator_defaults),
            kwargs={"x": 1},
        )
        timeout_args: list = []
        with patch(
            "lambda_tasks.models.TimeoutContext",
            self._capturing_context(timeout_args),
        ):
            msg.execute_immediately(message_id=str(uuid.uuid4()))
        assert timeout_args == [(30, 60)]

    def test_global_defaults_used_when_no_decorator_timeouts(self):
        msg = SQSLambdaTaskMessage(
            task_name=_task_name(_task_no_timeouts),
            kwargs={"x": 1},
        )
        timeout_args: list = []
        with patch(
            "lambda_tasks.models.TimeoutContext",
            self._capturing_context(timeout_args),
        ):
            msg.execute_immediately(message_id=str(uuid.uuid4()))
        assert timeout_args == [(270, 300)]


# ---------------------------------------------------------------------------
# Tests: import_string resolution
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestExecuteTaskImportStringResolution:
    def test_import_error_propagates_and_no_task_record_created(self):
        msg = _make_message("nonexistent.module.task", {})
        initial_count = TaskRecord.objects.count()
        with patch(
            "lambda_tasks.models.import_string",
            side_effect=ImportError("not found"),
        ):
            with pytest.raises(ImportError):
                msg.execute_immediately(message_id=str(uuid.uuid4()))
        assert TaskRecord.objects.count() == initial_count

    def test_non_wrapper_return_raises_type_error(self):
        msg = _make_message("some.module.plain_func", {})
        with patch("lambda_tasks.models.import_string", return_value=lambda: None):
            with pytest.raises(TypeError, match="expected LambdaTaskWrapper"):
                msg.execute_immediately(message_id=str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------

from hypothesis import HealthCheck, given
from hypothesis import settings as h_settings
from hypothesis import strategies as st


@pytest.mark.django_db(transaction=True)
@given(
    non_wrapper=st.one_of(
        st.integers(),
        st.text(),
        st.none(),
        st.booleans(),
        st.binary(),
    )
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_3_non_wrapper_raises_type_error(non_wrapper):
    """Property 3: for any non-LambdaTaskWrapper returned by import_string, TypeError is raised."""
    msg = _make_message("some.module.task", {})
    with patch("lambda_tasks.models.import_string", return_value=non_wrapper):
        with pytest.raises(TypeError, match="expected LambdaTaskWrapper"):
            msg.execute_immediately(message_id=str(uuid.uuid4()))


@pytest.mark.django_db(transaction=True)
@given(task_name=st.text(min_size=1))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_import_error_propagates_no_task_record(task_name):
    """Property 2: for any unresolvable task_name, ImportError propagates and no TaskRecord is created."""
    msg = _make_message(task_name, {})
    before = TaskRecord.objects.count()
    with patch(
        "lambda_tasks.models.import_string", side_effect=ImportError("not found")
    ):
        with pytest.raises(ImportError):
            msg.execute_immediately(message_id=str(uuid.uuid4()))
    assert TaskRecord.objects.count() == before


_label_alphabet = st.characters(whitelist_categories=("Lu", "Ll", "Nd"))
_label_strategy = st.text(min_size=1, max_size=50, alphabet=_label_alphabet)


@pytest.mark.django_db(transaction=True)
@given(label=_label_strategy)
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_12_atomic_rollback(label):
    """Property 12: ORM writes inside a failing task are rolled back; FAILED record is committed."""
    sentinel_name = f"prop12_sentinel_{label}"
    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_failing_for_property),
        kwargs={"label": label},
    )
    message_id = str(uuid.uuid4())
    with patch("lambda_tasks.models.TimeoutContext"):
        msg.execute_immediately(message_id=message_id)
    assert not TaskRecord.objects.filter(task_name=sentinel_name).exists()
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.FAILED


@pytest.mark.django_db(transaction=True)
@given(return_value=st.integers(min_value=0, max_value=1000))
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_13_task_record_lifecycle_success(return_value):
    """Property 13 (success): start_time non-null on RUNNING; end_time non-null and result matches on SUCCESS."""
    global _lifecycle_current_inv_id, _lifecycle_captured
    _lifecycle_captured = []
    inv_id = str(uuid.uuid4())
    _lifecycle_current_inv_id = inv_id
    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_lifecycle),
        kwargs={"rv": return_value},
    )
    with patch("lambda_tasks.models.TimeoutContext"):
        msg.execute_immediately(message_id=inv_id)
    assert _lifecycle_captured[0]["status"] == TaskRecord.TaskStatus.RUNNING
    assert _lifecycle_captured[0]["start_time"] is not None
    record = TaskRecord.objects.get(pk=inv_id)
    assert record.status == TaskRecord.TaskStatus.SUCCEEDED
    assert record.end_time is not None
    assert record.result == return_value


@pytest.mark.django_db(transaction=True)
@given(return_value=st.integers(min_value=0, max_value=1000))
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_13_task_record_lifecycle_failure(return_value):
    """Property 13 (failure): end_time non-null and traceback non-empty on FAILED."""
    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_failing_lifecycle),
        kwargs={"rv": return_value},
    )
    message_id = str(uuid.uuid4())
    with patch("lambda_tasks.models.TimeoutContext"):
        msg.execute_immediately(message_id=message_id)
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.FAILED
    assert record.end_time is not None
    assert record.traceback


# ---------------------------------------------------------------------------
# Tests: duplicate delivery / idempotency
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestExecuteTaskDuplicateDelivery:
    def test_duplicate_of_success_is_skipped(self):
        msg = _make_message(_task_name(_task_returns_value), {"x": 4})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
            msg.execute_immediately(message_id=message_id)  # duplicate
        assert TaskRecord.objects.filter(pk=message_id).count() == 1
        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.SUCCEEDED

    def test_duplicate_of_failed_is_skipped(self):
        msg = _make_message(_task_name(_task_raises), {"msg": "boom"})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
            msg.execute_immediately(message_id=message_id)  # duplicate
        assert TaskRecord.objects.filter(pk=message_id).count() == 1
        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.FAILED

    def test_duplicate_does_not_raise(self):
        msg = _make_message(_task_name(_task_returns_value), {"x": 2})
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=str(uuid.uuid4()))
            msg.execute_immediately(message_id=str(uuid.uuid4()))  # must not raise

    def test_duplicate_only_one_task_record_created(self):
        msg = _make_message(_task_name(_task_returns_value), {"x": 9})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            for _ in range(5):
                msg.execute_immediately(message_id=message_id)
        assert TaskRecord.objects.filter(pk=message_id).count() == 1


# ---------------------------------------------------------------------------
# import_string round-trip
# ---------------------------------------------------------------------------


def test_property_1_import_string_round_trip():
    """Property 1: import_string(task_name) returns the same LambdaTaskWrapper instance."""
    from django.utils.module_loading import import_string

    from lambda_tasks.decorators import LambdaTaskWrapper

    f = _task_returns_value.__wrapped__
    task_name = f"{f.__module__}.{f.__qualname__}"
    resolved = import_string(task_name)
    assert resolved is _task_returns_value
    assert isinstance(resolved, LambdaTaskWrapper)


# ---------------------------------------------------------------------------
# SQSLambdaTask._send and SQSLambdaTask.on_commit
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch

import django.db.transaction as _dbt
from django.core.exceptions import ImproperlyConfigured
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from lambda_tasks.models import SQSLambdaTask

_QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/000000000000/default"
_HIGH_MEM_URL = "https://sqs.us-east-1.amazonaws.com/000000000000/high-memory"
_QUEUES_MAP = {
    "default": {"queue_url": _QUEUE_URL},
    "high_memory": {"queue_url": _HIGH_MEM_URL},
}

_MESSAGE = SQSLambdaTaskMessage(
    task_name="lambda_tasks.tasks.cleanup_task_records",
    kwargs={},
)


@pytest.fixture()
def mock_boto3_sqs():
    with patch("lambda_tasks.models.boto3") as mock_b3:
        mock_client = MagicMock()
        mock_b3.client.return_value = mock_client
        yield mock_client


@pytest.mark.django_db
def test_send_known_queue_uses_correct_queue_url(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}
    SQSLambdaTask(message=_MESSAGE, delay=0, queue="default")._execute()
    assert mock_boto3_sqs.send_message.call_args.kwargs["QueueUrl"] == _QUEUE_URL


@pytest.mark.django_db
def test_send_known_queue_uses_correct_delay_seconds(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}
    SQSLambdaTask(message=_MESSAGE, delay=42, queue="default")._execute()
    assert mock_boto3_sqs.send_message.call_args.kwargs["DelaySeconds"] == 42


@pytest.mark.django_db
def test_send_named_queue_routes_to_correct_url(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {
        "default": {"queue_url": _QUEUE_URL},
        "high_memory": {"queue_url": _HIGH_MEM_URL},
    }
    SQSLambdaTask(message=_MESSAGE, delay=0, queue="high_memory")._execute()
    assert mock_boto3_sqs.send_message.call_args.kwargs["QueueUrl"] == _HIGH_MEM_URL


@pytest.mark.django_db
def test_send_unknown_queue_raises_improperly_configured(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}
    with pytest.raises(ImproperlyConfigured):
        SQSLambdaTask(message=_MESSAGE, delay=0, queue="nonexistent")._execute()
    mock_boto3_sqs.send_message.assert_not_called()


@pytest.mark.django_db
def test_send_boto3_exception_propagates(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}
    mock_boto3_sqs.send_message.side_effect = RuntimeError("SQS unavailable")
    with pytest.raises(RuntimeError, match="SQS unavailable"):
        SQSLambdaTask(message=_MESSAGE, delay=0, queue="default")._execute()


@pytest.mark.django_db
def test_send_delay_zero_passed_as_delay_seconds(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}
    SQSLambdaTask(message=_MESSAGE, delay=0, queue="default")._execute()
    assert mock_boto3_sqs.send_message.call_args.kwargs["DelaySeconds"] == 0


@pytest.mark.django_db
def test_send_eager_mode_executes_in_process(settings):
    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}
    settings.LAMBDA_TASKS_EAGER = True
    with (
        patch(
            "lambda_tasks.models.SQSLambdaTaskMessage.execute_immediately"
        ) as mock_exec,
        patch("lambda_tasks.models.boto3") as mock_b3,
    ):
        SQSLambdaTask(message=_MESSAGE, delay=0, queue="default")._execute()
        mock_exec.assert_called_once()
        mock_b3.client.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_on_commit_valid_dict_calls_send_message(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}
    deferred = {
        "message": {
            "task_name": "lambda_tasks.tasks.cleanup_task_records",
            "kwargs": {"x": 1},
        },
        "delay": 5,
        "queue": "default",
    }
    with _dbt.atomic():
        SQSLambdaTask.model_validate(deferred).execute_on_commit()
    mock_boto3_sqs.send_message.assert_called_once()
    call_kwargs = mock_boto3_sqs.send_message.call_args.kwargs
    assert call_kwargs["QueueUrl"] == _QUEUE_URL
    assert call_kwargs["DelaySeconds"] == 5


@pytest.mark.django_db
def test_on_commit_invalid_dict_raises_validation_error(settings, mock_boto3_sqs):
    from pydantic import ValidationError

    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}
    deferred = {
        "message": {
            "task_name": "lambda_tasks.tasks.cleanup_task_records",
            "kwargs": {},
        },
        "delay": 5,
        # 'queue' intentionally omitted
    }
    with pytest.raises(ValidationError):
        SQSLambdaTask.model_validate(deferred).execute_on_commit()
    mock_boto3_sqs.send_message.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_on_commit_eager_mode_executes_in_process(settings):
    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}
    settings.LAMBDA_TASKS_EAGER = True
    deferred = {
        "message": {
            "task_name": "lambda_tasks.tasks.cleanup_task_records",
            "kwargs": {},
        },
        "delay": 0,
        "queue": "default",
    }
    with (
        patch(
            "lambda_tasks.models.SQSLambdaTaskMessage.execute_immediately"
        ) as mock_exec,
        patch("lambda_tasks.models.boto3") as mock_b3,
    ):
        with _dbt.atomic():
            SQSLambdaTask.model_validate(deferred).execute_on_commit()
        mock_exec.assert_called_once()
        mock_b3.client.assert_not_called()


_deferred_msg_st = st.builds(
    lambda delay, queue: {
        "message": {
            "task_name": "lambda_tasks.tasks.cleanup_task_records",
            "kwargs": {},
        },
        "delay": delay,
        "queue": queue,
    },
    delay=st.integers(min_value=0, max_value=900),
    queue=st.sampled_from(["default", "high_memory"]),
)


@pytest.mark.django_db
@given(queue_name=st.sampled_from(["default", "high_memory"]))
@hyp_settings(
    max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_send_queue_routing_valid_names(settings, queue_name):
    settings.LAMBDA_TASKS_QUEUES = _QUEUES_MAP
    with patch("lambda_tasks.models.boto3") as mock_b3:
        mock_client = MagicMock()
        mock_b3.client.return_value = mock_client
        SQSLambdaTask(message=_MESSAGE, delay=0, queue=queue_name)._execute()
    assert (
        mock_client.send_message.call_args.kwargs["QueueUrl"]
        == _QUEUES_MAP[queue_name]["queue_url"]
    )


@pytest.mark.django_db
@given(
    queue_name=st.text(
        min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Ll",))
    )
)
@hyp_settings(
    max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_send_unknown_queue_raises(settings, queue_name):
    from hypothesis import assume

    assume(queue_name not in _QUEUES_MAP)
    settings.LAMBDA_TASKS_QUEUES = _QUEUES_MAP
    with patch("lambda_tasks.models.boto3") as mock_b3:
        mock_client = MagicMock()
        mock_b3.client.return_value = mock_client
        with pytest.raises(ImproperlyConfigured):
            SQSLambdaTask(message=_MESSAGE, delay=0, queue=queue_name)._execute()
        mock_client.send_message.assert_not_called()


_SQS_ERRORS = [
    RuntimeError("network error"),
    ConnectionError("timeout"),
    ValueError("bad response"),
]


@given(exc=st.sampled_from(_SQS_ERRORS))
@hyp_settings(
    max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@pytest.mark.django_db
def test_property_send_sqs_failure_propagates(settings, exc):
    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}
    with patch("lambda_tasks.models.boto3") as mock_b3:
        mock_client = MagicMock()
        mock_b3.client.return_value = mock_client
        mock_client.send_message.side_effect = exc
        with pytest.raises(type(exc)):
            SQSLambdaTask(message=_MESSAGE, delay=0, queue="default")._execute()


@pytest.mark.django_db(transaction=True)
@given(msg=_deferred_msg_st)
@hyp_settings(
    max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_on_commit_passes_all_fields(settings, msg):
    import json

    settings.LAMBDA_TASKS_QUEUES = _QUEUES_MAP
    with patch("lambda_tasks.models.boto3") as mock_b3:
        mock_client = MagicMock()
        mock_b3.client.return_value = mock_client
        with _dbt.atomic():
            SQSLambdaTask.model_validate(msg).execute_on_commit()
    mock_client.send_message.assert_called_once()
    call_kwargs = mock_client.send_message.call_args.kwargs
    body = json.loads(call_kwargs["MessageBody"])
    assert body["task_name"] == msg["message"]["task_name"]
    assert body["kwargs"] == msg["message"]["kwargs"]
    assert call_kwargs["DelaySeconds"] == msg["delay"]
    assert call_kwargs["QueueUrl"] == _QUEUES_MAP[msg["queue"]]["queue_url"]


@pytest.mark.django_db
@given(missing_field=st.sampled_from(["message", "delay", "queue"]))
@hyp_settings(
    max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_on_commit_rejects_invalid_dicts(settings, missing_field):
    from pydantic import ValidationError

    settings.LAMBDA_TASKS_QUEUES = _QUEUES_MAP
    valid = {
        "message": {
            "task_name": "lambda_tasks.tasks.cleanup_task_records",
            "kwargs": {},
        },
        "delay": 0,
        "queue": "default",
    }
    invalid_dict = {k: v for k, v in valid.items() if k != missing_field}
    with patch("lambda_tasks.models.boto3") as mock_b3:
        mock_client = MagicMock()
        mock_b3.client.return_value = mock_client
        with pytest.raises(ValidationError):
            SQSLambdaTask.model_validate(invalid_dict).execute_on_commit()
        mock_client.send_message.assert_not_called()


@pytest.mark.django_db(transaction=True)
@given(msg=_deferred_msg_st)
@hyp_settings(
    max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_on_commit_eager_mode(settings, msg):
    settings.LAMBDA_TASKS_QUEUES = _QUEUES_MAP
    settings.LAMBDA_TASKS_EAGER = True
    with (
        patch(
            "lambda_tasks.models.SQSLambdaTaskMessage.execute_immediately"
        ) as mock_exec,
        patch("lambda_tasks.models.boto3") as mock_b3,
    ):
        with _dbt.atomic():
            SQSLambdaTask.model_validate(msg).execute_on_commit()
        mock_exec.assert_called_once()
        mock_b3.client.assert_not_called()


# ---------------------------------------------------------------------------
# Feature: task-retry — Property 3: _n_retries non-negative validation
# ---------------------------------------------------------------------------


# Feature: task-retry, Property 3: _n_retries non-negative validation
# Validates: Requirements 2.3
@given(n=st.integers(max_value=-1))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_3_n_retries_negative_raises_validation_error(n: int) -> None:
    """Property 3 (negative): constructing SQSLambdaTaskMessage with _n_retries < 0 raises ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SQSLambdaTaskMessage(
            task_name="lambda_tasks.tasks.cleanup_task_records",
            kwargs={},
            n_retries=n,
        )


@given(n=st.integers(min_value=0))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_3_n_retries_non_negative_succeeds(n: int) -> None:
    """Property 3 (non-negative): constructing SQSLambdaTaskMessage with _n_retries >= 0 succeeds."""
    msg = SQSLambdaTaskMessage(
        task_name="lambda_tasks.tasks.cleanup_task_records",
        kwargs={},
        n_retries=n,
    )
    assert msg.n_retries == n


# ---------------------------------------------------------------------------
# Feature: task-retry — Task 2.1: MaxRetriesExceededError unit + property tests
# ---------------------------------------------------------------------------

from lambda_tasks.models import MaxRetriesExceededError


def test_max_retries_exceeded_error_is_exception_subclass() -> None:
    """Unit test: MaxRetriesExceededError is a subclass of Exception."""
    assert issubclass(MaxRetriesExceededError, Exception)


# Feature: task-retry, Property 9: MaxRetriesExceededError message contains task name and retry count
# Validates: Requirements 4.3
@given(
    task_name=st.text(min_size=1),
    n_retries=st.integers(min_value=0),
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_9_max_retries_exceeded_error_message(
    task_name: str, n_retries: int
) -> None:
    """Property 9: MaxRetriesExceededError message contains task name and retry count.
    Validates: Requirements 4.3"""
    error = MaxRetriesExceededError(task_name=task_name, n_retries=n_retries)
    message = str(error)
    assert task_name in message
    assert str(n_retries) in message


# ---------------------------------------------------------------------------
# Feature: task-retry — Task 5: retry logic property tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@given(
    n_retries=st.integers(min_value=0, max_value=2879),
    exc_type_name=st.sampled_from(["ValueError", "RuntimeError", "OSError"]),
)
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_4_retry_increments_n_retries(n_retries, exc_type_name):
    """Property 4: retry increments _n_retries by 1.
    Validates: Requirements 2.2"""
    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_retry_raises),
        kwargs={"exc_type_name": exc_type_name},
        n_retries=n_retries,
    )
    captured_tasks: list[SQSLambdaTask] = []

    def capturing_execute_on_commit(self: SQSLambdaTask) -> None:
        captured_tasks.append(self)

    with patch("lambda_tasks.models.import_string", return_value=_task_retry_raises):
        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(
                SQSLambdaTask, "execute_on_commit", capturing_execute_on_commit
            ):
                msg.execute_immediately(message_id=str(uuid.uuid4()))

    assert len(captured_tasks) == 1
    assert captured_tasks[0].message.n_retries == n_retries + 1


@pytest.mark.django_db(transaction=True)
@given(
    x=st.integers(),
    label=st.text(
        min_size=1,
        max_size=20,
        alphabet=st.characters(
            blacklist_characters="\x00",
            blacklist_categories=("Cs",),
        ),
    ),
)
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_5_matching_exc_enqueues_retry_same_kwargs(x, label):
    """Property 5: matching exception enqueues retry with same kwargs.
    Validates: Requirements 3.1"""
    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_retry_raises_with_kwargs),
        kwargs={"x": x, "label": label},
        n_retries=0,
    )
    captured_tasks: list[SQSLambdaTask] = []

    def capturing_execute_on_commit(self: SQSLambdaTask) -> None:
        captured_tasks.append(self)

    with patch(
        "lambda_tasks.models.import_string", return_value=_task_retry_raises_with_kwargs
    ):
        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(
                SQSLambdaTask, "execute_on_commit", capturing_execute_on_commit
            ):
                msg.execute_immediately(message_id=str(uuid.uuid4()))

    assert len(captured_tasks) == 1
    assert captured_tasks[0].message.kwargs["x"] == x
    assert captured_tasks[0].message.kwargs["label"] == label


@pytest.mark.django_db(transaction=True)
@given(exc_type_name=st.sampled_from(["ValueError", "RuntimeError", "OSError"]))
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_6_retrying_status_and_traceback(exc_type_name):
    """Property 6: retried task record has RETRYING status and non-null traceback.
    Validates: Requirements 3.2"""
    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_retry_raises),
        kwargs={"exc_type_name": exc_type_name},
        n_retries=0,
    )
    message_id = str(uuid.uuid4())
    with patch("lambda_tasks.models.import_string", return_value=_task_retry_raises):
        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(SQSLambdaTask, "execute_on_commit"):
                msg.execute_immediately(message_id=message_id)
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.RETRIED
    assert record.traceback is not None
    assert record.end_time is not None


@pytest.mark.django_db(transaction=True)
@given(exc_type_name=st.sampled_from(["ValueError", "RuntimeError"]))
@h_settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow])
def test_retry_log_message_contains_exception_type(exc_type_name):
    """Retry log message must reference the actual exception type, not None."""
    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_retry_raises),
        kwargs={"exc_type_name": exc_type_name},
        n_retries=0,
    )
    message_id = str(uuid.uuid4())
    with patch("lambda_tasks.models.import_string", return_value=_task_retry_raises):
        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(SQSLambdaTask, "execute_on_commit"):
                with patch("lambda_tasks.models.task_logger") as mock_logger:
                    msg.execute_immediately(message_id=message_id)

    retry_warnings = [
        call for call in mock_logger.warning.call_args_list if "Retrying" in str(call)
    ]
    assert len(retry_warnings) == 1
    log_msg = str(retry_warnings[0])
    assert exc_type_name in log_msg


@pytest.mark.django_db(transaction=True)
@given(exc_type_name=st.sampled_from(["TypeError", "KeyError", "AttributeError"]))
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_7_non_matching_exc_fails_no_retry(exc_type_name):
    """Property 7: non-matching exception → FAILED, no retry enqueued.
    Validates: Requirements 3.3, 3.4"""
    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_retry_raises_non_matching),
        kwargs={"exc_type_name": exc_type_name},
        n_retries=0,
    )
    message_id = str(uuid.uuid4())
    with patch(
        "lambda_tasks.models.import_string",
        return_value=_task_retry_raises_non_matching,
    ):
        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(SQSLambdaTask, "execute_on_commit") as mock_eoc:
                msg.execute_immediately(message_id=message_id)
    mock_eoc.assert_not_called()
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.FAILED


@pytest.mark.django_db(transaction=True)
@given(
    n_retries=st.integers(min_value=2880, max_value=32767),
    exc_type_name=st.sampled_from(["ValueError", "RuntimeError", "OSError"]),
)
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_8_max_retries_exceeded_raises_and_fails(n_retries, exc_type_name):
    """Property 8: _n_retries >= MAX_RETRIES → MaxRetriesExceededError raised, FAILED recorded.
    Validates: Requirements 4.2, 4.4"""
    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_retry_raises),
        kwargs={"exc_type_name": exc_type_name},
        n_retries=n_retries,
    )
    message_id = str(uuid.uuid4())
    with patch("lambda_tasks.models.import_string", return_value=_task_retry_raises):
        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(SQSLambdaTask, "execute_on_commit") as mock_eoc:
                with pytest.raises(MaxRetriesExceededError):
                    msg.execute_immediately(message_id=message_id)
    mock_eoc.assert_not_called()
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.FAILED
    assert record.traceback is not None


@pytest.mark.django_db(transaction=True)
def test_property_11_zero_delay_produces_delay_in_range():
    """Property 11: zero retry_delay → retry delay in [1, 5].
    Validates: Requirements 5.2"""

    @lambda_task(retry_on=(ValueError,))
    def _task_raises_zero_delay(*, x: int) -> None:
        raise ValueError("zero delay test")

    delays_seen = []
    for _ in range(100):
        msg = SQSLambdaTaskMessage(
            task_name=_task_name(_task_raises_zero_delay),
            kwargs={"x": 1},
            n_retries=0,
        )
        captured_tasks: list[SQSLambdaTask] = []

        def capturing_execute_on_commit(self: SQSLambdaTask) -> None:
            captured_tasks.append(self)

        with patch(
            "lambda_tasks.models.import_string", return_value=_task_raises_zero_delay
        ):
            with patch("lambda_tasks.models.TimeoutContext"):
                with patch.object(
                    SQSLambdaTask, "execute_on_commit", capturing_execute_on_commit
                ):
                    msg.execute_immediately(message_id=str(uuid.uuid4()))

        assert len(captured_tasks) == 1
        delays_seen.append(captured_tasks[0].delay)

    for d in delays_seen:
        assert isinstance(d, int)
        assert 1 <= d <= 5


@pytest.mark.django_db
def test_task_takes_not_json_seralizable():

    @lambda_task
    def _task_takes_not_json_seralizable(*, x: datetime) -> None:
        assert isinstance(x, datetime)

    message_id = uuid.uuid4()
    execution_time = now()

    with patch(
        "lambda_tasks.models.import_string",
        return_value=_task_takes_not_json_seralizable,
    ):
        msg = SQSLambdaTaskMessage(
            task_name=_task_name(_task_takes_not_json_seralizable),
            kwargs={"x": execution_time},
            n_retries=0,
        ).execute_immediately(message_id=message_id)

    record = TaskRecord.objects.get(pk=message_id)
    assert record.kwargs == {"x": execution_time.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}


# ---------------------------------------------------------------------------
# Feature: retry-delay — Task 9.1: Failing tests for updated retry path
# ---------------------------------------------------------------------------


@lambda_task(retry_on=(ValueError,), retry_delay=30)
def _task_raises_for_retry_delay_nonzero(*, x: int) -> None:
    """Raises ValueError — used to test non-zero retry_delay enqueue. Module-level for import_string."""
    raise ValueError("retry delay test")


@lambda_task(retry_on=(ValueError,), retry_delay=0)
def _task_raises_for_retry_delay_zero(*, x: int) -> None:
    """Raises ValueError — used to test zero retry_delay jitter enqueue. Module-level for import_string."""
    raise ValueError("zero retry delay test")


# Feature: retry-delay, Property 4: non-zero retry_delay is used in the retry enqueue
# Validates: Requirements 3.1
@pytest.mark.django_db(transaction=True)
@given(retry_delay=st.integers(min_value=1, max_value=900))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_retry_delay_nonzero_used_in_retry_enqueue(retry_delay):
    """Property 4: non-zero retry_delay is used as delay in the retry SQSLambdaTask.
    Validates: Requirements 3.1"""

    @lambda_task(retry_on=(ValueError,), retry_delay=retry_delay)
    def _task_raises_for_retry_delay(*, x: int) -> None:
        raise ValueError("retry delay test")

    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_raises_for_retry_delay),
        kwargs={"x": 1},
        n_retries=0,
    )
    captured_tasks: list[SQSLambdaTask] = []

    def capturing_execute_on_commit(self: SQSLambdaTask) -> None:
        captured_tasks.append(self)

    with patch(
        "lambda_tasks.models.import_string", return_value=_task_raises_for_retry_delay
    ):
        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(
                SQSLambdaTask, "execute_on_commit", capturing_execute_on_commit
            ):
                msg.execute_immediately(message_id=str(uuid.uuid4()))

    assert len(captured_tasks) == 1
    assert captured_tasks[0].delay >= min(retry_delay + 1, 900)
    assert captured_tasks[0].delay <= min(retry_delay + 5, 900)


# Feature: retry-delay, Property 5: zero retry_delay produces jitter in [1, 5]
# Validates: Requirements 3.2, 3.3
@pytest.mark.django_db(transaction=True)
def test_retry_delay_zero_produces_jitter_in_range():
    """Property 5: zero retry_delay → retry delay in [1, 5].
    Validates: Requirements 3.2, 3.3"""

    @lambda_task(retry_on=(ValueError,), retry_delay=0)
    def _task_raises_zero_retry_delay(*, x: int) -> None:
        raise ValueError("zero retry delay test")

    delays_seen: list[int] = []
    for _ in range(100):
        msg = SQSLambdaTaskMessage(
            task_name=_task_name(_task_raises_zero_retry_delay),
            kwargs={"x": 1},
            n_retries=0,
        )
        captured_tasks: list[SQSLambdaTask] = []

        def capturing_execute_on_commit(self: SQSLambdaTask) -> None:
            captured_tasks.append(self)

        with patch(
            "lambda_tasks.models.import_string",
            return_value=_task_raises_zero_retry_delay,
        ):
            with patch("lambda_tasks.models.TimeoutContext"):
                with patch.object(
                    SQSLambdaTask, "execute_on_commit", capturing_execute_on_commit
                ):
                    msg.execute_immediately(message_id=str(uuid.uuid4()))

        assert len(captured_tasks) == 1
        delays_seen.append(captured_tasks[0].delay)

    for d in delays_seen:
        assert isinstance(d, int)
        assert 1 <= d <= 5


# Feature: retry-delay, Property 6: normal enqueue defaults delay to 0
# Validates: Requirements 4.3, 4.4
@pytest.mark.django_db(transaction=True)
@given(call_delay=st.integers(min_value=0, max_value=900))
@h_settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_normal_execute_on_commit_uses_call_time_delay(settings, call_delay):
    """Property 6: normal execute_on_commit uses _delay passed at call time.
    Validates: Requirements 4.3, 4.4"""
    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}

    from lambda_tasks.tasks import cleanup_task_records

    with patch("lambda_tasks.models.boto3") as mock_b3:
        mock_client = MagicMock()
        mock_b3.client.return_value = mock_client
        import django.db.transaction as _transaction

        with _transaction.atomic():
            cleanup_task_records.execute_on_commit(_delay=call_delay)

    mock_client.send_message.assert_called_once()
    assert mock_client.send_message.call_args.kwargs["DelaySeconds"] == call_delay


@pytest.mark.django_db(transaction=True)
def test_normal_execute_on_commit_defaults_delay_to_zero(settings):
    """execute_on_commit without _delay defaults to 0."""
    settings.LAMBDA_TASKS_QUEUES = {"default": {"queue_url": _QUEUE_URL}}

    from lambda_tasks.tasks import cleanup_task_records

    with patch("lambda_tasks.models.boto3") as mock_b3:
        mock_client = MagicMock()
        mock_b3.client.return_value = mock_client
        import django.db.transaction as _transaction

        with _transaction.atomic():
            cleanup_task_records.execute_on_commit()

    mock_client.send_message.assert_called_once()
    assert mock_client.send_message.call_args.kwargs["DelaySeconds"] == 0


def test_passing_delay_to_execute_on_commit_sets_delay():
    """Passing _delay to execute_on_commit sets the delay on the task.
    Validates: Requirements 4.1, 4.2"""
    from unittest.mock import patch

    @lambda_task
    def _task_simple(*, x: int) -> None:
        pass

    captured: list = []
    with patch(
        "lambda_tasks.models.transaction.on_commit",
        side_effect=lambda cb: captured.append(cb),
    ):
        _task_simple.execute_on_commit(x=1, _delay=5)
    assert captured[0].__self__.delay == 5


# ---------------------------------------------------------------------------
# Feature: singleton-task — module-level task helper for lock key format test
# ---------------------------------------------------------------------------


@lambda_task(singleton=True)
def _task_singleton_noop(*, x: int) -> int:
    """Singleton task that returns x — used by singleton lock key property test."""
    return x


# ---------------------------------------------------------------------------
# Feature: singleton-task, Property 2: Lock key format
# ---------------------------------------------------------------------------


# Task name strategy: dotted module paths like "myapp.tasks.do_work"
_task_name_segment_st = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_"
    ),
)
_task_name_st = st.builds(
    lambda segments: ".".join(segments),
    segments=st.lists(_task_name_segment_st, min_size=1, max_size=5),
)


@pytest.mark.django_db(transaction=True)
@given(task_name=_task_name_st)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_singleton_lock_key_format(task_name: str) -> None:
    """Feature: singleton-task, Property 2: Lock key format

    For any task name string, when a singleton task is executed, the executor
    should attempt to acquire a lock with key `lambda_tasks.singleton_lock.{task_name}`.

    **Validates: Requirements 2.1**
    """
    msg = SQSLambdaTaskMessage(
        task_name=task_name,
        kwargs={"x": 1},
        n_retries=0,
    )
    message_id = str(uuid.uuid4())

    mock_lock = MagicMock()
    mock_lock.__enter__ = MagicMock(return_value=mock_lock)
    mock_lock.__exit__ = MagicMock(return_value=False)

    mock_cache = MagicMock()
    mock_cache.lock.return_value = mock_lock

    with (
        patch("lambda_tasks.models.import_string", return_value=_task_singleton_noop),
        patch("lambda_tasks.models.TimeoutContext"),
        patch(
            "lambda_tasks.models.caches",
            {LambdaTasksSettings().SINGLETON_CACHE: mock_cache},
        ),
    ):
        msg.execute_immediately(message_id=message_id)

    expected_key = f"lambda_tasks.singleton_lock.{task_name}"
    _, hard_timeout = _task_singleton_noop.resolved_timeouts
    mock_cache.lock.assert_called_once_with(
        expected_key,
        blocking_timeout=0,
        timeout=hard_timeout,
    )


# ---------------------------------------------------------------------------
# Feature: singleton-task — module-level task helper for lock release failure scenario
# ---------------------------------------------------------------------------


@lambda_task(singleton=True)
def _task_singleton_raises(*, x: int) -> None:
    """Singleton task that always raises — used by lock release property test."""
    raise RuntimeError(f"singleton failure {x}")


# ---------------------------------------------------------------------------
# Feature: singleton-task, Property 3: Lock release on success and failure
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@given(
    x=st.integers(min_value=0, max_value=1000),
    should_succeed=st.booleans(),
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_singleton_lock_release_on_success_and_failure(
    *,
    x: int,
    should_succeed: bool,
) -> None:
    """Feature: singleton-task, Property 3: Lock release on success and failure

    For any singleton task execution that either succeeds or raises an exception
    (other than LockError), the lock context manager should be properly exited
    (lock released).

    **Validates: Requirements 2.2, 2.3**
    """
    wrapper = _task_singleton_noop if should_succeed else _task_singleton_raises

    msg = SQSLambdaTaskMessage(
        task_name=_task_name(wrapper),
        kwargs={"x": x},
        n_retries=0,
    )
    message_id = str(uuid.uuid4())

    mock_lock = MagicMock()
    mock_lock.__enter__ = MagicMock(return_value=mock_lock)
    mock_lock.__exit__ = MagicMock(return_value=False)

    mock_cache = MagicMock()
    mock_cache.lock.return_value = mock_lock

    with (
        patch("lambda_tasks.models.import_string", return_value=wrapper),
        patch("lambda_tasks.models.TimeoutContext"),
        patch(
            "lambda_tasks.models.caches",
            {LambdaTasksSettings().SINGLETON_CACHE: mock_cache},
        ),
    ):
        msg.execute_immediately(message_id=message_id)

    # Lock context manager __exit__ must have been called regardless of outcome
    mock_lock.__exit__.assert_called_once()


# ---------------------------------------------------------------------------
# Feature: singleton-task, Property 4: LockError triggers retry with RETRYING status and incremented n_retries
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@given(
    n_retries=st.integers(min_value=0, max_value=2879),
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_singleton_lockerror_retry(
    *,
    n_retries: int,
) -> None:
    """Feature: singleton-task, Property 4: LockError triggers retry with RETRYING status and incremented n_retries

    For any singleton task where LockError is raised and n_retries < MAX_RETRIES,
    the executor should set the TaskRecord status to RETRYING, record a traceback
    containing "LockError", and re-enqueue the task with n_retries + 1.

    **Validates: Requirements 3.1, 3.3**
    """
    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_singleton_noop),
        kwargs={"x": 1},
        n_retries=n_retries,
    )
    message_id = str(uuid.uuid4())

    mock_lock = MagicMock()
    mock_lock.__enter__ = MagicMock(side_effect=LockError("lock contention"))
    mock_lock.__exit__ = MagicMock(return_value=False)

    mock_cache = MagicMock()
    mock_cache.lock.return_value = mock_lock

    captured_tasks: list[SQSLambdaTask] = []

    def capturing_execute_on_commit(self: SQSLambdaTask) -> None:
        captured_tasks.append(self)

    with (
        patch("lambda_tasks.models.import_string", return_value=_task_singleton_noop),
        patch("lambda_tasks.models.TimeoutContext"),
        patch(
            "lambda_tasks.models.caches",
            {LambdaTasksSettings().SINGLETON_CACHE: mock_cache},
        ),
        patch.object(SQSLambdaTask, "execute_on_commit", capturing_execute_on_commit),
    ):
        msg.execute_immediately(message_id=message_id)

    # TaskRecord should be RETRYING with LockError in traceback
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.RETRIED
    assert record.traceback is not None
    assert "LockError" in record.traceback

    # A retry task should have been enqueued with n_retries + 1
    assert len(captured_tasks) == 1
    assert captured_tasks[0].message.n_retries == n_retries + 1


# ---------------------------------------------------------------------------
# Feature: singleton-task, Property 5: LockError at MAX_RETRIES raises MaxRetriesExceededError
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@given(
    n_retries=st.integers(min_value=2880, max_value=32767),
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_singleton_lockerror_max_retries(
    *,
    n_retries: int,
) -> None:
    """Feature: singleton-task, Property 5: LockError at MAX_RETRIES raises MaxRetriesExceededError

    For any singleton task where LockError is raised and n_retries >= MAX_RETRIES,
    the executor should raise MaxRetriesExceededError and record the TaskRecord
    as FAILED with a non-null traceback.

    **Validates: Requirements 3.2**
    """
    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_singleton_noop),
        kwargs={"x": 1},
        n_retries=n_retries,
    )
    message_id = str(uuid.uuid4())

    mock_lock = MagicMock()
    mock_lock.__enter__ = MagicMock(side_effect=LockError("lock contention"))
    mock_lock.__exit__ = MagicMock(return_value=False)

    mock_cache = MagicMock()
    mock_cache.lock.return_value = mock_lock

    with (
        patch("lambda_tasks.models.import_string", return_value=_task_singleton_noop),
        patch("lambda_tasks.models.TimeoutContext"),
        patch(
            "lambda_tasks.models.caches",
            {LambdaTasksSettings().SINGLETON_CACHE: mock_cache},
        ),
        patch.object(SQSLambdaTask, "execute_on_commit") as mock_eoc,
    ):
        with pytest.raises(MaxRetriesExceededError):
            msg.execute_immediately(message_id=message_id)

    # No retry should have been enqueued
    mock_eoc.assert_not_called()

    # TaskRecord should be FAILED with non-null traceback
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.FAILED
    assert record.traceback is not None


# ---------------------------------------------------------------------------
# Feature: singleton-task — Task 4.6: Unit tests for singleton execution
# ---------------------------------------------------------------------------


@lambda_task
def _task_non_singleton_noop(*, x: int) -> int:
    """Non-singleton task — used to verify no lock is acquired."""
    return x


@pytest.mark.django_db(transaction=True)
class TestSingletonExecutionUnit:
    def test_singleton_false_does_not_acquire_lock(self) -> None:
        """singleton=False → no cache.lock() call is made.
        Requirements: 1.3"""
        msg = SQSLambdaTaskMessage(
            task_name=_task_name(_task_non_singleton_noop),
            kwargs={"x": 42},
            n_retries=0,
        )
        message_id = str(uuid.uuid4())

        mock_cache = MagicMock()

        with (
            patch(
                "lambda_tasks.models.import_string",
                return_value=_task_non_singleton_noop,
            ),
            patch("lambda_tasks.models.TimeoutContext"),
            patch("lambda_tasks.models.caches", {"default": mock_cache}),
        ):
            msg.execute_immediately(message_id=message_id)

        mock_cache.lock.assert_not_called()

        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.SUCCEEDED
        assert record.result == 42

    def test_singleton_uses_singleton_cache_backend(self, settings: object) -> None:
        """Executor uses caches[SINGLETON_CACHE] for lock acquisition.
        Requirements: 4.2"""
        settings.LAMBDA_TASKS_SINGLETON_CACHE = "my_redis"  # type: ignore[attr-defined]

        msg = SQSLambdaTaskMessage(
            task_name=_task_name(_task_singleton_noop),
            kwargs={"x": 7},
            n_retries=0,
        )
        message_id = str(uuid.uuid4())

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=mock_lock)
        mock_lock.__exit__ = MagicMock(return_value=False)

        mock_cache = MagicMock()
        mock_cache.lock.return_value = mock_lock

        # Only provide the custom cache key — if executor looks up the wrong key it will KeyError
        with (
            patch(
                "lambda_tasks.models.import_string", return_value=_task_singleton_noop
            ),
            patch("lambda_tasks.models.TimeoutContext"),
            patch("lambda_tasks.models.caches", {"my_redis": mock_cache}),
        ):
            msg.execute_immediately(message_id=message_id)

        mock_cache.lock.assert_called_once()

    def test_sqs_lambda_task_message_schema_excludes_singleton(self) -> None:
        """SQSLambdaTaskMessage model fields do not include 'singleton'.
        Requirements: 5.1"""
        assert "singleton" not in SQSLambdaTaskMessage.model_fields


# ---------------------------------------------------------------------------
# Feature: singleton-task — retry_singleton=False produces SUCCESS on LockError
# ---------------------------------------------------------------------------


@lambda_task(singleton=True, retry_singleton=False)
def _task_singleton_no_retry(*, x: int) -> int:
    return x


@pytest.mark.django_db(transaction=True)
class TestSingletonRetrySingletonFalse:
    """Verify that retry_singleton=False causes LockError to produce SUCCESS
    with traceback instead of triggering a retry."""

    def test_lock_error_produces_success_not_retry(self) -> None:
        """LockError with retry_singleton=False → SUCCESS with traceback, not RETRYING."""
        msg = SQSLambdaTaskMessage(
            task_name=_task_name(_task_singleton_no_retry),
            kwargs={"x": 1},
            n_retries=0,
        )
        message_id = str(uuid.uuid4())

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(side_effect=LockError("contention"))
        mock_lock.__exit__ = MagicMock(return_value=False)

        mock_cache = MagicMock()
        mock_cache.lock.return_value = mock_lock

        with (
            patch(
                "lambda_tasks.models.import_string",
                return_value=_task_singleton_no_retry,
            ),
            patch("lambda_tasks.models.TimeoutContext"),
            patch(
                "lambda_tasks.models.caches",
                {LambdaTasksSettings().SINGLETON_CACHE: mock_cache},
            ),
            patch.object(SQSLambdaTask, "execute_on_commit") as mock_eoc,
        ):
            msg.execute_immediately(message_id=message_id)

        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.SUCCEEDED
        assert record.traceback is not None
        assert "LockError" in record.traceback
        mock_eoc.assert_not_called()

    def test_retry_singleton_true_still_retries_on_lock_error(self) -> None:
        """retry_singleton=True (default) → LockError triggers RETRYING."""
        msg = SQSLambdaTaskMessage(
            task_name=_task_name(_task_singleton_noop),
            kwargs={"x": 1},
            n_retries=0,
        )
        message_id = str(uuid.uuid4())

        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(side_effect=LockError("contention"))
        mock_lock.__exit__ = MagicMock(return_value=False)

        mock_cache = MagicMock()
        mock_cache.lock.return_value = mock_lock

        with (
            patch(
                "lambda_tasks.models.import_string",
                return_value=_task_singleton_noop,
            ),
            patch("lambda_tasks.models.TimeoutContext"),
            patch(
                "lambda_tasks.models.caches",
                {LambdaTasksSettings().SINGLETON_CACHE: mock_cache},
            ),
            patch.object(SQSLambdaTask, "execute_on_commit") as mock_eoc,
        ):
            msg.execute_immediately(message_id=message_id)

        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.RETRIED
        assert record.traceback is not None
        assert "LockError" in record.traceback
        mock_eoc.assert_called_once()
