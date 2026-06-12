import enum
from typing import Final

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured

MAX_DELAY: Final = 900
MAX_TIMEOUT: Final = 900
MAX_BATCH_TIMEOUT: Final = 3600


class TaskBackend(str, enum.Enum):
    LAMBDA = "lambda"
    BATCH = "batch"


class LambdaTasksSettings:
    @property
    def QUEUES(self) -> dict[str, str]:
        queues = getattr(django_settings, "LAMBDA_TASKS_QUEUES")

        if not queues:
            raise ImproperlyConfigured(
                "LAMBDA_TASKS_QUEUES must be defined in Django settings."
            )

        if "default" not in queues:
            raise ImproperlyConfigured(
                "LAMBDA_TASKS_QUEUES must contain a 'default' key."
            )

        return queues

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
    def BATCH_QUEUES(self) -> dict[str, dict[str, str]]:
        queues = getattr(django_settings, "LAMBDA_TASKS_BATCH_QUEUES", None)

        if queues is None:
            return {}

        if "default" not in queues:
            raise ImproperlyConfigured(
                "LAMBDA_TASKS_BATCH_QUEUES must contain a 'default' key."
            )

        for name, config in queues.items():
            if not isinstance(config, dict):
                raise ImproperlyConfigured(
                    f"LAMBDA_TASKS_BATCH_QUEUES['{name}'] must be a dict."
                )
            for key in ("job_queue", "job_definition"):
                if key not in config or not isinstance(config[key], str):
                    raise ImproperlyConfigured(
                        f"LAMBDA_TASKS_BATCH_QUEUES['{name}'] must contain a '{key}' string."
                    )

        return queues

    @property
    def LOCAL_WORKERS(self) -> int:
        value = int(getattr(django_settings, "LAMBDA_TASKS_LOCAL_WORKERS", 0))
        if value < 0:
            raise ImproperlyConfigured(
                "LAMBDA_TASKS_LOCAL_WORKERS must be a non-negative integer."
            )
        if value > 0 and self.EAGER:
            raise ImproperlyConfigured(
                "LAMBDA_TASKS_LOCAL_WORKERS and LAMBDA_TASKS_EAGER are mutually exclusive. "
                "Set one or the other, not both."
            )
        return value
