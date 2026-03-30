from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured

MAX_TIMEOUT = 900


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
