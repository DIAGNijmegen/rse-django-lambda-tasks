from typing import Final

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from pydantic import BaseModel

MAX_DELAY: Final = 900
MAX_TIMEOUT: Final = 900
MAX_BATCH_TIMEOUT: Final = 3600


class SQSQueueConfig(BaseModel):
    model_config = {"extra": "forbid"}
    queue_url: str


class BatchQueueConfig(BaseModel):
    model_config = {"extra": "forbid"}
    job_queue_arn: str
    job_definition_arn: str


QueueConfig = SQSQueueConfig | BatchQueueConfig


def _parse_queue_config(*, name: str, raw: object) -> QueueConfig:
    if not isinstance(raw, dict):
        raise ImproperlyConfigured(f"LAMBDA_TASKS_QUEUES['{name}'] must be a dict.")
    try:
        return SQSQueueConfig.model_validate(raw)
    except Exception:
        pass
    try:
        return BatchQueueConfig.model_validate(raw)
    except Exception:
        pass
    raise ImproperlyConfigured(
        f"LAMBDA_TASKS_QUEUES['{name}'] must contain either 'queue_url' or both 'job_queue_arn' and 'job_definition_arn'."
    )


class LambdaTasksSettings:
    @property
    def QUEUES(self) -> dict[str, QueueConfig]:
        queues = getattr(django_settings, "LAMBDA_TASKS_QUEUES")

        if not queues:
            raise ImproperlyConfigured(
                "LAMBDA_TASKS_QUEUES must be defined in Django settings."
            )

        if "default" not in queues:
            raise ImproperlyConfigured(
                "LAMBDA_TASKS_QUEUES must contain a 'default' key."
            )

        parsed: dict[str, QueueConfig] = {}
        for name, raw in queues.items():
            parsed[name] = _parse_queue_config(name=name, raw=raw)

        if not isinstance(parsed["default"], SQSQueueConfig):
            raise ImproperlyConfigured(
                "LAMBDA_TASKS_QUEUES['default'] must be an SQS queue (must contain 'queue_url')."
            )

        return parsed

    def queue_max_timeout(self, *, queue: str) -> int:
        config = self.QUEUES[queue]
        if isinstance(config, BatchQueueConfig):
            return MAX_BATCH_TIMEOUT
        else:
            return MAX_TIMEOUT

    @property
    def EAGER(self) -> bool:
        return bool(getattr(django_settings, "LAMBDA_TASKS_EAGER", False))

    @property
    def DEFAULT_SOFT_TIMEOUT(self) -> int:
        return int(getattr(django_settings, "LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT", 270))

    @property
    def DEFAULT_HARD_TIMEOUT(self) -> int:
        return int(getattr(django_settings, "LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT", 300))

    @property
    def MAX_RETRIES(self) -> int:
        return int(getattr(django_settings, "LAMBDA_TASKS_MAX_RETRIES", 2880))

    @property
    def SINGLETON_CACHE(self) -> str:
        return str(getattr(django_settings, "LAMBDA_TASKS_SINGLETON_CACHE", "default"))

    @property
    def LOCAL_WORKERS(self) -> int:
        value = int(getattr(django_settings, "LAMBDA_TASKS_LOCAL_WORKERS", 0))
        if value < 0:
            raise ImproperlyConfigured(
                "LAMBDA_TASKS_LOCAL_WORKERS must be a non-negative integer."
            )
        elif value > 0 and self.EAGER:
            raise ImproperlyConfigured(
                "LAMBDA_TASKS_LOCAL_WORKERS and LAMBDA_TASKS_EAGER are mutually exclusive. "
                "Set one or the other, not both."
            )
        else:
            return value
