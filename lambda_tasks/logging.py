"""
Provides task_logger — a LoggerAdapter that automatically includes the
current message_id on every log record while a task is executing.

Usage in task functions::

    from lambda_tasks.logging import task_logger

    @lambda_task(...)
    def my_task(*, user_id: int) -> None:
        task_logger.info("processing user %s", user_id)
        # → "processing user 42" tagged with the active message_id
"""

import logging
from collections.abc import MutableMapping
from typing import Any


class _TaskLogger(logging.LoggerAdapter):
    """LoggerAdapter that prepends [message_id] to every message."""

    def __init__(self) -> None:
        super().__init__(logging.getLogger(f"{__package__}.task"), extra={})
        self.message_id: str | None = None

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        prefix = f"[{self.message_id}] " if self.message_id else ""
        return f"{prefix}{msg}", kwargs


task_logger = _TaskLogger()
