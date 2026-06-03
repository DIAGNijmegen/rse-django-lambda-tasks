"""Django AppConfig for the lambda_tasks library."""

from django.apps import AppConfig


class LambdaTasksConfig(AppConfig):
    name = "lambda_tasks"
    verbose_name = "Lambda Tasks"

    def ready(self) -> None:
        """Install pool shutdown signal handlers in async-local mode.

        Only relevant when LOCAL_WORKERS > 0 (development async execution). In
        that mode the process pool's POSIX semaphores must be released promptly
        on Ctrl+C, before Django's autoreloader parent SIGKILLs this child. See
        ``local_executor._install_shutdown_handlers`` for the full rationale.
        """
        from lambda_tasks.local_executor import _install_shutdown_handlers
        from lambda_tasks.settings import LambdaTasksSettings

        if LambdaTasksSettings().LOCAL_WORKERS > 0:
            _install_shutdown_handlers()
