"""Django deployment checks for lambda_tasks."""

from django.conf import settings as django_settings
from django.core.checks import Warning, register


@register("lambda_tasks", deploy=True)
def check_noop_not_in_production(
    *, app_configs: object, **kwargs: object
) -> list[Warning]:
    """Warn if any non-production execution mode is enabled."""
    warnings: list[Warning] = []

    if bool(getattr(django_settings, "LAMBDA_TASKS_NOOP_EXECUTION", False)):
        warnings.append(
            Warning(
                "LAMBDA_TASKS_NOOP_EXECUTION is enabled. "
                "Tasks will be silently dropped.",
                hint="Set LAMBDA_TASKS_NOOP_EXECUTION = False for production.",
                id="lambda_tasks.W001",
            )
        )

    if bool(getattr(django_settings, "LAMBDA_TASKS_EAGER", False)):
        warnings.append(
            Warning(
                "LAMBDA_TASKS_EAGER is enabled. "
                "Tasks will run synchronously in-process.",
                hint="Set LAMBDA_TASKS_EAGER = False for production.",
                id="lambda_tasks.W002",
            )
        )

    local_workers = int(getattr(django_settings, "LAMBDA_TASKS_LOCAL_WORKERS", 0))
    if local_workers > 0:
        warnings.append(
            Warning(
                "LAMBDA_TASKS_LOCAL_WORKERS is set to a positive value. "
                "Tasks will run in a local process pool.",
                hint="Set LAMBDA_TASKS_LOCAL_WORKERS = 0 for production.",
                id="lambda_tasks.W003",
            )
        )

    return warnings
