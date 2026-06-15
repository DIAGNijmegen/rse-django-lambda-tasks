"""Process pool executor for async local task execution."""

import atexit
import logging
import signal
import threading
import uuid
from concurrent.futures import Future, ProcessPoolExecutor

from lambda_tasks.settings import LambdaTasksSettings

logger = logging.getLogger(__name__)

_pool: ProcessPoolExecutor | None = None
_handlers_installed: bool = False


def _pool_initializer() -> None:
    """Run once per worker process.

    Ignores SIGINT so that Ctrl+C is handled exclusively by the parent
    process, which releases the pool via its SIGINT/SIGTERM handler (and
    atexit as a fallback). This prevents workers from being killed
    mid-operation and leaking semaphores.

    Closes all inherited database connections before setting up Django.
    On Linux (fork-based spawning), child processes inherit copies of the
    parent's DB connections which are in an inconsistent state.

    Then sets up Django for task execution.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    import django
    from django.db import connections

    connections.close_all()

    django.setup()


def _shutdown_pool() -> None:
    """Shut down the pool, releasing its POSIX semaphores promptly.

    On shutdown we must unlink the pool's semaphores quickly, because under
    ``runserver`` the autoreloader parent SIGKILLs this child almost immediately
    after Ctrl+C (see ``_install_shutdown_handlers``). ``pool.shutdown(wait=True)``
    blocks on joining the worker processes — if a worker is slow to exit (e.g.
    it ran a heavy ``django.setup()``), the semaphores are not unlinked until
    the worker dies, and the SIGKILL wins the race, leaking the semaphores.

    To avoid that, terminate the worker processes first (a near-instant
    SIGTERM/SIGKILL to children we own), then shut the pool down without
    waiting. With the workers already gone, ``concurrent.futures`` releases the
    queue semaphores immediately.
    """
    global _pool
    if _pool is not None:
        pool = _pool
        _pool = None
        processes = getattr(pool, "_processes", None)
        if processes:
            for process in list(processes.values()):
                process.terminate()
        pool.shutdown(wait=False, cancel_futures=True)


def _install_shutdown_handlers() -> None:
    """Install SIGINT/SIGTERM handlers that release the pool promptly.

    Under Django's ``runserver`` autoreloader the development server runs in a
    child process spawned by ``subprocess.run()``. On Ctrl+C the terminal
    delivers SIGINT to the whole process group; the autoreloader parent unwinds
    out of ``subprocess.run`` and immediately calls ``process.kill()``
    (SIGKILL) on this child. That is a race: this process must unlink the
    pool's POSIX semaphores before the SIGKILL lands, otherwise multiprocessing's
    ``resource_tracker`` reports them as leaked at shutdown.

    Relying on ``atexit`` loses that race in applications with a heavy shutdown
    sequence, because ``atexit`` runs only after the full interpreter unwind.
    Instead we shut the pool down as the very first action of the signal
    handler, then chain to the previously installed handler so normal shutdown
    behaviour (KeyboardInterrupt, autoreloader exit) is preserved.

    Idempotent and only effective on the main thread — ``signal.signal`` raises
    ``ValueError`` off the main thread, in which case this is a no-op.
    """
    global _handlers_installed
    if _handlers_installed:
        return

    if threading.current_thread() is not threading.main_thread():
        return

    def make_handler(previous):  # type: ignore[no-untyped-def]
        def handler(signum, frame):  # type: ignore[no-untyped-def]
            # Release the pool's semaphores first, before the autoreloader
            # parent can SIGKILL us.
            _shutdown_pool()
            if callable(previous):
                previous(signum, frame)
            elif previous == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)
            # previous == SIG_IGN: nothing to chain to.

        return handler

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(signum)
            signal.signal(signum, make_handler(previous))
    except ValueError:
        # Not on the main thread; cannot install signal handlers here.
        return

    _handlers_installed = True


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
