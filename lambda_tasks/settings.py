"""
Lazy settings object for lambda_tasks.
Reads from django.conf.settings on first attribute access.
"""

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured

_UNSET = object()

MAX_TIMEOUT = 900


class LambdaTasksSettings:
    """Lazy wrapper around Django settings for the lambda_tasks library."""

    def _get(self, *, name: str, default: object = _UNSET) -> object:
        val = getattr(django_settings, name, _UNSET)
        if val is _UNSET:
            if default is _UNSET:
                return None
            return default
        if val is None:
            if default is _UNSET:
                return None
            return None
        return val

    def _resolve_queues(self) -> dict[str, str]:
        queues = self._get(name="LAMBDA_TASKS_QUEUES")

        if queues:
            if "default" not in queues:  # type: ignore[operator]
                raise ImproperlyConfigured(
                    "LAMBDA_TASKS_QUEUES must contain a 'default' key."
                )
            return queues  # type: ignore[return-value]

        raise ImproperlyConfigured(
            "LAMBDA_TASKS_QUEUES must be defined in Django settings."
        )

    @property
    def QUEUES(self) -> dict[str, str]:
        return self._resolve_queues()

    @property
    def EAGER(self) -> bool:
        val = self._get(name="LAMBDA_TASKS_EAGER", default=False)
        return bool(val)

    @property
    def DEFAULT_SOFT_TIMEOUT(self) -> int:
        default = 270
        val = self._get(name="LAMBDA_TASKS_DEFAULT_SOFT_TIMEOUT", default=default)
        if val is None:
            return default
        else:
            return int(val)  # type: ignore[return-value]

    @property
    def DEFAULT_HARD_TIMEOUT(self) -> int:
        default = 300
        val = self._get(name="LAMBDA_TASKS_DEFAULT_HARD_TIMEOUT", default=default)
        if val is None:
            return default
        else:
            return int(val)  # type: ignore[return-value]

    @property
    def MAX_RETRIES(self) -> int:
        default = 2880  # 60 * 24 * 2
        val = self._get(name="LAMBDA_TASKS_MAX_RETRIES", default=default)
        if val is None:
            return default
        return int(val)  # type: ignore[return-value]
