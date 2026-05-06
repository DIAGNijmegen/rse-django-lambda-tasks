"""Process pool executor for async local task execution."""

import logging
import uuid
from concurrent.futures import Future, ProcessPoolExecutor

from lambda_tasks.settings import LambdaTasksSettings

logger = logging.getLogger(__name__)

_pool: ProcessPoolExecutor | None = None


def _pool_initializer() -> None:
    """Run once per worker process. Sets up Django."""
    import django

    django.setup()


def get_pool() -> ProcessPoolExecutor:
    """Return the shared ProcessPoolExecutor, creating it on first call."""
    global _pool
    if _pool is None:
        conf = LambdaTasksSettings()
        _pool = ProcessPoolExecutor(
            max_workers=conf.LOCAL_WORKERS,
            initializer=_pool_initializer,
        )
    return _pool


def _execute_in_worker(*, message_json: str, message_id: str) -> None:
    """Worker entry point. Deserializes and executes the task.

    Runs in a child process. Django is already set up via the pool initializer.
    """
    from lambda_tasks.models import SQSLambdaTaskMessage

    message = SQSLambdaTaskMessage.model_validate_json(message_json)
    message.execute_immediately(message_id=message_id)


def _log_worker_exception(future: Future) -> None:  # type: ignore[type-arg]
    """Callback attached to each worker future. Logs unhandled exceptions."""
    exception = future.exception()
    if exception is not None:
        logger.error("Worker process raised an exception", exc_info=exception)


def submit_task(*, message_json: str) -> None:
    """Submit a task to the process pool. Fire-and-forget."""
    pool = get_pool()
    message_id = str(uuid.uuid4())
    future = pool.submit(
        _execute_in_worker, message_json=message_json, message_id=message_id
    )
    future.add_done_callback(_log_worker_exception)
