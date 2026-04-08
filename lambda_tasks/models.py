"""Django model for persisting task execution records."""

import contextlib
import datetime
import random
import traceback
import uuid

import boto3
from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured
from django.db import models, transaction
from django.db.models import Q
from django.utils.module_loading import import_string
from django.utils.timezone import now
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import LockError

from lambda_tasks.logging import task_logger
from lambda_tasks.settings import LambdaTasksSettings
from lambda_tasks.timeouts import TimeoutContext


class MaxRetriesExceededError(Exception):
    def __init__(self, *, task_name: str, n_retries: int) -> None:
        super().__init__(
            f"Task '{task_name}' exceeded the maximum retry limit ({n_retries} retries)."
        )


class TaskStatus(models.TextChoices):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


class TaskRecord(models.Model):
    TaskStatus = TaskStatus

    id = models.UUIDField(primary_key=True, editable=False)
    task_name = models.CharField(max_length=255, editable=False)
    kwargs = models.JSONField(editable=False)
    n_retries = models.PositiveSmallIntegerField(editable=False)
    status = models.CharField(
        max_length=10,
        choices=TaskStatus,
        editable=False,
    )
    start_time = models.DateTimeField(null=True, editable=False)
    end_time = models.DateTimeField(null=True, editable=False)
    result = models.JSONField(null=True, editable=False)
    traceback = models.TextField(null=True, editable=False)

    @property
    def duration(self) -> datetime.timedelta | None:
        try:
            return self.end_time - self.start_time  # ty: ignore[unsupported-operator]
        except TypeError:
            return None

    class Meta:
        ordering = ["-start_time"]
        indexes = [
            models.Index(fields=["task_name"]),
            models.Index(fields=["status"]),
            models.Index(fields=["-start_time"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=TaskStatus.values),
                name="taskrecord_status_valid",
            ),
        ]


class SQSLambdaTaskMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_name: str
    kwargs: dict
    n_retries: int = Field(default=0, ge=0)

    def execute_immediately(self, *, message_id: str) -> None:
        """Execute a background task described.

        Creates a TaskRecord, resolves timeouts, validates configuration,
        runs the task inside an atomic block with timeout enforcement, and
        persists the outcome.
        """
        task_logger.message_id = message_id

        try:
            task_logger.info(f"Received {self.task_name}")

            # Local import to avoid circular dependency
            from lambda_tasks.decorators import LambdaTaskWrapper

            wrapper = import_string(self.task_name)

            if not isinstance(wrapper, LambdaTaskWrapper):
                raise TypeError(
                    f"import_string('{self.task_name}') returned {type(wrapper)!r}, "
                    f"expected LambdaTaskWrapper."
                )

            # Resolve before creating the TaskRecord — ConfigurationError here means
            # misconfigured settings, not a task failure, so no record should be written.
            soft_timeout, hard_timeout = wrapper.resolved_timeouts

            record, created = (
                TaskRecord.objects.get_or_create(  # ty: ignore[unresolved-attribute]
                    pk=message_id,
                    defaults={
                        "task_name": self.task_name,
                        "kwargs": self.model_dump(mode="json")["kwargs"],
                        "n_retries": self.n_retries,
                        "status": TaskRecord.TaskStatus.RUNNING,
                        "start_time": now(),
                    },
                )
            )

            if not created:
                task_logger.warning(
                    f"Skipping duplicate delivery (existing record status={record.status})"
                )
                return

            ignored_exception: BaseException | None = None
            ignored_traceback: str | None = None
            result = None

            if wrapper.singleton:
                cache = caches[LambdaTasksSettings().SINGLETON_CACHE]
                lock_key = f"lambda_tasks.singleton_lock.{self.task_name}"
                lock_ctx: contextlib.AbstractContextManager = (
                    cache.lock(  # ty: ignore[assignment]
                        lock_key,
                        blocking_timeout=0,
                        timeout=hard_timeout,
                    )
                )
                effective_retry_on = (LockError, *wrapper.retry_on)
            else:
                lock_ctx = contextlib.nullcontext()
                effective_retry_on = wrapper.retry_on

            try:
                with lock_ctx:
                    with transaction.atomic():
                        with TimeoutContext(
                            soft_timeout=soft_timeout, hard_timeout=hard_timeout
                        ):
                            result = wrapper(**self.kwargs)
            except Exception as error:
                if wrapper.ignore_errors and isinstance(error, wrapper.ignore_errors):
                    ignored_exception = error
                    ignored_traceback = traceback.format_exc()
                elif effective_retry_on and isinstance(error, effective_retry_on):
                    conf = LambdaTasksSettings()
                    if self.n_retries >= conf.MAX_RETRIES:
                        record.status = TaskRecord.TaskStatus.FAILED
                        record.traceback = traceback.format_exc()
                        record.end_time = now()
                        record.save(update_fields=["status", "traceback", "end_time"])

                        task_logger.warning(
                            f"Failed due to MaxRetriesExceededError in {record.duration}"
                        )

                        raise MaxRetriesExceededError(
                            task_name=self.task_name, n_retries=self.n_retries
                        )
                    else:
                        record.status = TaskRecord.TaskStatus.RETRYING
                        record.traceback = traceback.format_exc()
                        record.end_time = now()
                        record.save(update_fields=["status", "traceback", "end_time"])

                        task_logger.warning(
                            f"Retrying (due to {type(ignored_exception).__name__}) after {record.duration}"
                        )

                        delay = min(
                            wrapper.retry_delay + round(random.uniform(1, 5)), 900
                        )

                        retry_task = SQSLambdaTask(
                            message=SQSLambdaTaskMessage(
                                task_name=self.task_name,
                                kwargs=self.kwargs,
                                n_retries=self.n_retries + 1,
                            ),
                            delay=delay,
                            queue=wrapper.queue,
                        )
                        retry_task.execute_on_commit()

                        return
                else:
                    task_logger.error(error, exc_info=True)

                    record.status = TaskRecord.TaskStatus.FAILED
                    record.traceback = traceback.format_exc()
                    record.end_time = now()
                    record.save(update_fields=["status", "traceback", "end_time"])

                    task_logger.warning(f"Failed in {record.duration}")
                    return

            if ignored_exception is None:
                record.status = TaskRecord.TaskStatus.SUCCESS
                record.result = result
                record.end_time = now()
                record.save(update_fields=["status", "result", "end_time"])

                task_logger.info(f"Succeeded in {record.duration}")
            else:
                record.status = TaskRecord.TaskStatus.SUCCESS
                record.traceback = ignored_traceback
                record.end_time = now()
                record.save(update_fields=["status", "traceback", "end_time"])

                task_logger.info(
                    f"Succeeded (ignored {type(ignored_exception).__name__}) in {record.duration}"
                )

        finally:
            task_logger.message_id = None


class SQSLambdaTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: SQSLambdaTaskMessage
    delay: int
    queue: str

    def execute_on_commit(self) -> None:
        """Enqueue this task after the current transaction commits."""
        transaction.on_commit(self._execute)

    def _execute(self) -> None:
        """Send this task to SQS (or execute eagerly).

        Raises:
            ImproperlyConfigured: if the queue name is not found in settings.
            Any boto3 exception: propagated directly to the caller.
        """
        conf = LambdaTasksSettings()

        if conf.EAGER:
            self.message.execute_immediately(message_id=str(uuid.uuid4()))
        else:
            try:
                queue_url = conf.QUEUES[self.queue]
            except KeyError:
                raise ImproperlyConfigured(
                    f"Queue '{self.queue}' is not defined in LAMBDA_TASKS_QUEUES."
                )
            client = boto3.client("sqs")
            client.send_message(
                QueueUrl=queue_url,
                MessageBody=self.message.model_dump_json(),
                DelaySeconds=self.delay,
            )
