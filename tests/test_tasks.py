"""Tests for lambda_tasks.tasks — built-in maintenance tasks."""

import uuid

import pytest
from django.utils.timezone import now, timedelta

from lambda_tasks.decorators import LambdaTaskWrapper
from lambda_tasks.models import TaskRecord, TaskStatus
from lambda_tasks.tasks import cleanup_task_records


@pytest.mark.django_db
class TestCleanupTaskRecordsDeletesOldRecords:
    def _create_record(
        self, *, age_days: int, status: str = TaskStatus.SUCCESS
    ) -> uuid.UUID:
        record_id = uuid.uuid4()
        TaskRecord.objects.create(
            id=record_id,
            task_name="some.task",
            kwargs={},
            n_retries=0,
            status=status,
            start_time=now() - timedelta(days=age_days),
            end_time=now() - timedelta(days=age_days) + timedelta(seconds=1),
        )
        return record_id

    def test_deletes_records_older_than_retention_days(self) -> None:
        old_id = self._create_record(age_days=8)
        recent_id = self._create_record(age_days=3)

        cleanup_task_records(retention_days=7)

        assert not TaskRecord.objects.filter(pk=old_id).exists()
        assert TaskRecord.objects.filter(pk=recent_id).exists()

    def test_default_retention_is_seven_days(self) -> None:
        old_id = self._create_record(age_days=8)
        recent_id = self._create_record(age_days=6)

        cleanup_task_records()

        assert not TaskRecord.objects.filter(pk=old_id).exists()
        assert TaskRecord.objects.filter(pk=recent_id).exists()

    def test_record_just_inside_retention_is_not_deleted(self) -> None:
        """A record aged slightly less than retention_days survives."""
        record_id = uuid.uuid4()
        cutoff_time = now() - timedelta(days=7)
        TaskRecord.objects.create(
            id=record_id,
            task_name="some.task",
            kwargs={},
            n_retries=0,
            status=TaskStatus.SUCCESS,
            start_time=cutoff_time + timedelta(seconds=1),
            end_time=cutoff_time + timedelta(seconds=2),
        )

        cleanup_task_records(retention_days=7)

        assert TaskRecord.objects.filter(pk=record_id).exists()

    def test_deletes_all_statuses(self) -> None:
        ids = [
            self._create_record(age_days=10, status=TaskStatus.SUCCESS),
            self._create_record(age_days=10, status=TaskStatus.FAILED),
            self._create_record(age_days=10, status=TaskStatus.RUNNING),
            self._create_record(age_days=10, status=TaskStatus.RETRYING),
        ]

        cleanup_task_records(retention_days=7)

        for record_id in ids:
            assert not TaskRecord.objects.filter(pk=record_id).exists()

    def test_returns_deleted_count(self) -> None:
        self._create_record(age_days=10)
        self._create_record(age_days=10)
        self._create_record(age_days=3)

        result = cleanup_task_records(retention_days=7)

        assert result == 2

    def test_no_records_to_delete_returns_zero(self) -> None:
        self._create_record(age_days=1)

        result = cleanup_task_records(retention_days=7)

        assert result == 0


class TestCleanupTaskRecordsIsLambdaTask:
    def test_is_lambda_task_wrapper(self) -> None:
        assert isinstance(cleanup_task_records, LambdaTaskWrapper)

    def test_uses_default_queue(self) -> None:
        assert cleanup_task_records.queue == "default"
