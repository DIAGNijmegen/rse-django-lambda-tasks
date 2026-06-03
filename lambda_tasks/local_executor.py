"""Process pool executor for async local task execution."""

import atexit
import logging
import signal
import uuid
from concurrent.futures import Future, ProcessPoolExecutor

from lambda_tasks.settings import LambdaTasksSettings

logger = logging.getLogger(__name__)

_pool: ProcessPoolExecutor | None = None


def _pool_initializer() -> None:
    """Run once per worker process.

    Ignores SIGINT so that Ctrl+C is handled exclusively by the parent
    process, which shuts down the pool cleanly via atexit. This prevents
    workers from being killed mid-operation and leaking semaphores.

    Then sets up Django for task execution.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    import django

    django.setup()


def _shutdown_pool() -> None:
    """Shut down the pool at interpreter exit to release semaphores."""
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=True, cancel_futures=True)
        _pool = None


def get_pool() -> ProcessPoolExecutor:
    """Return the shared ProcessPoolExecutor, creating it on first call."""
    global _pool
    if _pool is None:
        conf = LambdaTasksSettings()
        _pool = ProcessPoolExecutor(
            max_workers=conf.LOCAL_WORKERS,
            initializer=_pool_initializer,
        )
        atexit.register(_shutdown_pool)
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
