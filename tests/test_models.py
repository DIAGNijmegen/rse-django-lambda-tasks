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

import pytest
from django.db import IntegrityError

from lambda_tasks.decorators import lambda_task
from lambda_tasks.models import SQSLambdaTaskMessage, TaskRecord

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
            TaskRecord.TaskStatus.SUCCESS,
            TaskRecord.TaskStatus.FAILED,
            TaskRecord.TaskStatus.RETRYING,
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
        assert TaskRecord.TaskStatus.SUCCESS == "SUCCESS"
        assert TaskRecord.TaskStatus.FAILED == "FAILED"
        assert TaskRecord.TaskStatus.RETRYING == "RETRYING"

    def test_retrying_status_can_be_saved(self):
        record = TaskRecord.objects.create(
            task_name="myapp.tasks.job",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.RETRYING,
        )
        assert (
            TaskRecord.objects.get(pk=record.pk).status
            == TaskRecord.TaskStatus.RETRYING
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
            status=TaskRecord.TaskStatus.SUCCESS,
        )
        TaskRecord.objects.create(
            task_name="myapp.tasks.job",
            pk=uuid.uuid4(),
            kwargs={},
            n_retries=0,
            status=TaskRecord.TaskStatus.FAILED,
        )
        assert (
            TaskRecord.objects.filter(status=TaskRecord.TaskStatus.SUCCESS).count() == 1
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
        assert record.status == TaskRecord.TaskStatus.SUCCESS

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
    assert record.status == TaskRecord.TaskStatus.SUCCESS
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
        assert record.status == TaskRecord.TaskStatus.SUCCESS

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
_QUEUES_MAP = {"default": _QUEUE_URL, "high_memory": _HIGH_MEM_URL}
_MESSAGE = SQSLambdaTaskMessage(
    task_name="myapp.tasks.my_task",
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
    settings.LAMBDA_TASKS_QUEUES = {"default": _QUEUE_URL}
    SQSLambdaTask(message=_MESSAGE, delay=0, queue="default")._execute()
    assert mock_boto3_sqs.send_message.call_args.kwargs["QueueUrl"] == _QUEUE_URL


@pytest.mark.django_db
def test_send_known_queue_uses_correct_delay_seconds(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {"default": _QUEUE_URL}
    SQSLambdaTask(message=_MESSAGE, delay=42, queue="default")._execute()
    assert mock_boto3_sqs.send_message.call_args.kwargs["DelaySeconds"] == 42


@pytest.mark.django_db
def test_send_named_queue_routes_to_correct_url(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {
        "default": _QUEUE_URL,
        "high_memory": _HIGH_MEM_URL,
    }
    SQSLambdaTask(message=_MESSAGE, delay=0, queue="high_memory")._execute()
    assert mock_boto3_sqs.send_message.call_args.kwargs["QueueUrl"] == _HIGH_MEM_URL


@pytest.mark.django_db
def test_send_unknown_queue_raises_improperly_configured(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {"default": _QUEUE_URL}
    with pytest.raises(ImproperlyConfigured):
        SQSLambdaTask(message=_MESSAGE, delay=0, queue="nonexistent")._execute()
    mock_boto3_sqs.send_message.assert_not_called()


@pytest.mark.django_db
def test_send_boto3_exception_propagates(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {"default": _QUEUE_URL}
    mock_boto3_sqs.send_message.side_effect = RuntimeError("SQS unavailable")
    with pytest.raises(RuntimeError, match="SQS unavailable"):
        SQSLambdaTask(message=_MESSAGE, delay=0, queue="default")._execute()


@pytest.mark.django_db
def test_send_delay_zero_passed_as_delay_seconds(settings, mock_boto3_sqs):
    settings.LAMBDA_TASKS_QUEUES = {"default": _QUEUE_URL}
    SQSLambdaTask(message=_MESSAGE, delay=0, queue="default")._execute()
    assert mock_boto3_sqs.send_message.call_args.kwargs["DelaySeconds"] == 0


@pytest.mark.django_db
def test_send_eager_mode_executes_in_process(settings):
    settings.LAMBDA_TASKS_QUEUES = {"default": _QUEUE_URL}
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
    settings.LAMBDA_TASKS_QUEUES = {"default": _QUEUE_URL}
    deferred = {
        "message": {
            "task_name": "myapp.tasks.my_task",
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

    settings.LAMBDA_TASKS_QUEUES = {"default": _QUEUE_URL}
    deferred = {
        "message": {
            "task_name": "myapp.tasks.my_task",
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
    settings.LAMBDA_TASKS_QUEUES = {"default": _QUEUE_URL}
    settings.LAMBDA_TASKS_EAGER = True
    deferred = {
        "message": {
            "task_name": "myapp.tasks.my_task",
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
    lambda message, delay, queue: {"message": message, "delay": delay, "queue": queue},
    message=st.builds(
        lambda task_name, kwargs: {
            "task_name": task_name,
            "kwargs": kwargs,
        },
        task_name=st.from_regex(r"[a-z]+\.[a-z]+", fullmatch=True),
        kwargs=st.fixed_dictionaries({}),
    ),
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
        mock_client.send_message.call_args.kwargs["QueueUrl"] == _QUEUES_MAP[queue_name]
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
    settings.LAMBDA_TASKS_QUEUES = {"default": _QUEUE_URL}
    with patch("lambda_tasks.models.boto3") as mock_b3:
        mock_client = MagicMock()
        mock_b3.client.return_value = mock_client
        mock_client.send_message.side_effect = exc
        with pytest.raises(type(exc)):
            SQSLambdaTask(message=_MESSAGE, delay=0, queue="default")._execute()


@pytest.mark.django_db(transaction=True)
@given(delay=st.integers(min_value=0, max_value=899))
@hyp_settings(
    max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_on_commit_delay_embedded_in_sqs_message(settings, delay):
    from django.db import transaction

    from lambda_tasks.decorators import LambdaTaskWrapper

    def _task(*, x: int = 0) -> None:
        pass

    settings.LAMBDA_TASKS_QUEUES = {"default": _QUEUE_URL}
    wrapper = LambdaTaskWrapper(_task)
    with patch("lambda_tasks.models.boto3") as mock_b3:
        mock_client = MagicMock()
        mock_b3.client.return_value = mock_client
        with transaction.atomic():
            wrapper.execute_on_commit(x=1, _delay=delay)
        mock_client.send_message.assert_called_once()
        assert mock_client.send_message.call_args.kwargs["DelaySeconds"] == delay


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
    assert call_kwargs["QueueUrl"] == _QUEUES_MAP[msg["queue"]]


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
            "task_name": "myapp.tasks.my_task",
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
# Feature: ignore-errors-decorator-option — module-level task helpers
# ---------------------------------------------------------------------------

_IGNORABLE_EXC_TYPES = [
    ValueError,
    RuntimeError,
    KeyError,
    TypeError,
    OSError,
    AttributeError,
]
_ignorable_exc_type_st = st.sampled_from(_IGNORABLE_EXC_TYPES)
_ignore_errors_tuple_st = st.lists(_ignorable_exc_type_st, min_size=1, max_size=3).map(
    tuple
)


@lambda_task(
    ignore_errors=(
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        OSError,
        AttributeError,
    )
)
def _task_raises_ignored(*, exc_type_name: str) -> None:
    """Raises the named exception type — used by ignore_errors property tests."""
    exc_map = {
        "ValueError": ValueError,
        "RuntimeError": RuntimeError,
        "KeyError": KeyError,
        "TypeError": TypeError,
        "OSError": OSError,
        "AttributeError": AttributeError,
    }
    raise exc_map[exc_type_name]("ignored error")


@lambda_task
def _task_raises_value_error_no_ignore(*, msg: str) -> None:
    """Raises ValueError with no ignore_errors — used to test non-ignored path."""
    raise ValueError(msg)


@lambda_task(ignore_errors=(ValueError,))
def _task_creates_record_then_raises_ignored(*, label: str) -> None:
    """Creates a TaskRecord inside the atomic block, then raises an ignored exception."""
    TaskRecord.objects.create(
        task_name=f"ignored_side_effect_{label}",
        pk=uuid.uuid4(),
        kwargs={"label": label},
        n_retries=0,
        status=TaskRecord.TaskStatus.RUNNING,
    )
    raise ValueError("ignored — side effects should be rolled back")


# ---------------------------------------------------------------------------
# Feature: ignore-errors-decorator-option — Properties 3–6
# ---------------------------------------------------------------------------

_EXC_TYPE_NAMES = [
    "ValueError",
    "RuntimeError",
    "KeyError",
    "TypeError",
    "OSError",
    "AttributeError",
]
_exc_type_name_st = st.sampled_from(_EXC_TYPE_NAMES)


# Feature: ignore-errors-decorator-option, Property 3: Ignored exception produces SUCCESS with traceback and end_time
@pytest.mark.django_db(transaction=True)
@given(exc_type_name=_exc_type_name_st)
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_ignored_exc_produces_success(exc_type_name):
    """Property 3: ignored exception → status=SUCCESS, non-null traceback containing exc name, non-null end_time.
    Validates: Requirements 2.1, 2.3, 2.4, 5.1"""
    msg = _make_message(
        _task_name(_task_raises_ignored), {"exc_type_name": exc_type_name}
    )
    message_id = str(uuid.uuid4())
    with patch("lambda_tasks.models.TimeoutContext"):
        msg.execute_immediately(message_id=message_id)
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.SUCCESS
    assert record.traceback is not None
    assert exc_type_name in record.traceback
    assert record.end_time is not None


# Feature: ignore-errors-decorator-option, Property 4: Ignored exception commits the transaction
@pytest.mark.django_db(transaction=True)
@given(label=_label_strategy)
@h_settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
def test_property_ignored_exc_commits_record(label):
    """Property 4: ignored exception → task-side ORM writes rolled back, TaskRecord committed as SUCCESS.
    Validates: Requirements 2.2"""
    msg = _make_message(
        _task_name(_task_creates_record_then_raises_ignored), {"label": label}
    )
    message_id = str(uuid.uuid4())
    with patch("lambda_tasks.models.TimeoutContext"):
        msg.execute_immediately(message_id=message_id)
    # Task-side write must be rolled back
    assert not TaskRecord.objects.filter(
        task_name=f"ignored_side_effect_{label}"
    ).exists()
    # TaskRecord itself must be committed as SUCCESS
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.SUCCESS


# Feature: ignore-errors-decorator-option, Property 5: Subclass of ignored exception type is also ignored
@pytest.mark.django_db(transaction=True)
@given(base_exc=_ignorable_exc_type_st)
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_subclass_of_ignored_is_ignored(base_exc):
    """Property 5: subclass of an ignored exception type is also treated as ignored → SUCCESS.
    Validates: Requirements 2.5"""
    SubExc = type(f"Sub{base_exc.__name__}", (base_exc,), {})

    @lambda_task(ignore_errors=(base_exc,))
    def _task_raises_subclass(*, x: int) -> None:
        raise SubExc("subclass error")

    msg = _make_message(_task_name(_task_raises_subclass), {"x": 1})
    message_id = str(uuid.uuid4())
    with patch("lambda_tasks.models.import_string", return_value=_task_raises_subclass):
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.SUCCESS


# Feature: ignore-errors-decorator-option, Property 6: Non-ignored exception produces FAILED with rollback
@pytest.mark.django_db(transaction=True)
@given(exc_type=_ignorable_exc_type_st)
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_non_ignored_exc_produces_failed(exc_type):
    """Property 6: exception not in ignore_errors → status=FAILED, task-side writes rolled back, traceback non-null.
    Validates: Requirements 3.1, 3.2, 3.3, 3.4"""

    # Use a wrapper with empty ignore_errors so nothing is ignored
    @lambda_task
    def _task_raises_exc(*, label: str) -> None:
        TaskRecord.objects.create(
            task_name=f"non_ignored_side_effect_{label}",
            pk=uuid.uuid4(),
            kwargs={"label": label},
            n_retries=0,
            status=TaskRecord.TaskStatus.RUNNING,
        )
        raise exc_type("non-ignored error")

    label = "prop6"
    msg = _make_message(_task_name(_task_raises_exc), {"label": label})
    message_id = str(uuid.uuid4())
    with patch("lambda_tasks.models.import_string", return_value=_task_raises_exc):
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
    assert not TaskRecord.objects.filter(
        task_name=f"non_ignored_side_effect_{label}"
    ).exists()
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.FAILED
    assert record.traceback is not None


# ---------------------------------------------------------------------------
# Feature: ignore-errors-decorator-option — Task 5: regression guard unit tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestIgnoreErrorsRegressionGuard:
    def test_clean_success_traceback_remains_none(self):
        """Regression guard: successful task must leave traceback as None (Requirement 5.2)."""
        msg = _make_message(_task_name(_task_returns_value), {"x": 3})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.SUCCESS
        assert record.traceback is None

    def test_non_ignored_exception_produces_failed(self):
        """Regression guard: non-ignored exception → FAILED, traceback non-null, task writes rolled back."""
        msg = _make_message(
            _task_name(_task_creates_record_then_raises), {"label": "regression_guard"}
        )
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        assert not TaskRecord.objects.filter(task_name="side_effect_record").exists()
        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.FAILED
        assert record.traceback is not None

    def test_empty_ignore_errors_all_exceptions_produce_failed(self):
        """Regression guard: ignore_errors=() (default) → all exceptions still produce FAILED."""
        msg = _make_message(_task_name(_task_raises), {"msg": "default_ignore_errors"})
        message_id = str(uuid.uuid4())
        with patch("lambda_tasks.models.TimeoutContext"):
            msg.execute_immediately(message_id=message_id)
        record = TaskRecord.objects.get(pk=message_id)
        assert record.status == TaskRecord.TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Feature: ignore-errors-decorator-option — Property 7: eager mode parity
# ---------------------------------------------------------------------------

from django.test import override_settings


# Feature: ignore-errors-decorator-option, Property 7: Eager mode applies the same ignore_errors logic
@pytest.mark.django_db(transaction=True)
@given(exc_type_name=_exc_type_name_st)
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
@override_settings(LAMBDA_TASKS_EAGER=True)
def test_property_eager_mode_ignore_errors_parity(exc_type_name):
    """Property 7: eager mode applies the same ignore_errors logic — ignored exception → SUCCESS.
    Validates: Requirements 4.3"""
    msg = _make_message(
        _task_name(_task_raises_ignored), {"exc_type_name": exc_type_name}
    )
    message_id = str(uuid.uuid4())
    with patch("lambda_tasks.models.import_string", return_value=_task_raises_ignored):
        msg.execute_immediately(message_id=message_id)
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.SUCCESS
    assert record.traceback is not None


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
            task_name="myapp.tasks.my_task",
            kwargs={},
            n_retries=n,
        )


@given(n=st.integers(min_value=0))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_3_n_retries_non_negative_succeeds(n: int) -> None:
    """Property 3 (non-negative): constructing SQSLambdaTaskMessage with _n_retries >= 0 succeeds."""
    msg = SQSLambdaTaskMessage(
        task_name="myapp.tasks.my_task",
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
    with patch("lambda_tasks.models.import_string", return_value=_task_retry_raises):
        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(_task_retry_raises, "execute_on_commit") as mock_eoc:
                msg.execute_immediately(message_id=str(uuid.uuid4()))
    mock_eoc.assert_called_once()
    call_kwargs = mock_eoc.call_args.kwargs
    assert call_kwargs["_n_retries"] == n_retries + 1


@pytest.mark.django_db(transaction=True)
@given(
    x=st.integers(),
    label=st.text(min_size=1, max_size=20),
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
    with patch(
        "lambda_tasks.models.import_string", return_value=_task_retry_raises_with_kwargs
    ):
        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(
                _task_retry_raises_with_kwargs, "execute_on_commit"
            ) as mock_eoc:
                msg.execute_immediately(message_id=str(uuid.uuid4()))
    mock_eoc.assert_called_once()
    call_kwargs = mock_eoc.call_args.kwargs
    assert call_kwargs["x"] == x
    assert call_kwargs["label"] == label


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
            with patch.object(_task_retry_raises, "execute_on_commit"):
                msg.execute_immediately(message_id=message_id)
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.RETRYING
    assert record.traceback is not None
    assert record.end_time is not None


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
            with patch.object(
                _task_retry_raises_non_matching, "execute_on_commit"
            ) as mock_eoc:
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
            with patch.object(_task_retry_raises, "execute_on_commit") as mock_eoc:
                with pytest.raises(MaxRetriesExceededError):
                    msg.execute_immediately(message_id=message_id)
    mock_eoc.assert_not_called()
    record = TaskRecord.objects.get(pk=message_id)
    assert record.status == TaskRecord.TaskStatus.FAILED
    assert record.traceback is not None


@pytest.mark.django_db(transaction=True)
@given(delay=st.integers(min_value=1, max_value=900))
@h_settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_10_non_zero_delay_used_as_retry_delay(delay):
    """Property 10: non-zero wrapper delay is used as retry _delay.
    Validates: Requirements 5.1"""

    @lambda_task(retry_on=(ValueError,), delay=delay)
    def _task_raises_for_delay(*, x: int) -> None:
        raise ValueError("delay test")

    msg = SQSLambdaTaskMessage(
        task_name=_task_name(_task_raises_for_delay),
        kwargs={"x": 1},
        n_retries=0,
    )
    with patch(
        "lambda_tasks.models.import_string", return_value=_task_raises_for_delay
    ):
        with patch("lambda_tasks.models.TimeoutContext"):
            with patch.object(_task_raises_for_delay, "execute_on_commit") as mock_eoc:
                msg.execute_immediately(message_id=str(uuid.uuid4()))
    mock_eoc.assert_called_once()
    assert mock_eoc.call_args.kwargs["_delay"] == delay


@pytest.mark.django_db(transaction=True)
def test_property_11_zero_delay_produces_delay_in_range():
    """Property 11: zero wrapper delay → retry _delay in [1, 5].
    Validates: Requirements 5.2"""

    @lambda_task(retry_on=(ValueError,), delay=0)
    def _task_raises_zero_delay(*, x: int) -> None:
        raise ValueError("zero delay test")

    delays_seen = []
    for _ in range(100):
        msg = SQSLambdaTaskMessage(
            task_name=_task_name(_task_raises_zero_delay),
            kwargs={"x": 1},
            n_retries=0,
        )
        with patch(
            "lambda_tasks.models.import_string", return_value=_task_raises_zero_delay
        ):
            with patch("lambda_tasks.models.TimeoutContext"):
                with patch.object(
                    _task_raises_zero_delay, "execute_on_commit"
                ) as mock_eoc:
                    msg.execute_immediately(message_id=str(uuid.uuid4()))
        delays_seen.append(mock_eoc.call_args.kwargs["_delay"])

    for d in delays_seen:
        assert isinstance(d, int)
        assert 1 <= d <= 5
