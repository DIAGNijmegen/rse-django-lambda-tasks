from time import sleep

from lambda_tasks.decorators import lambda_task
from lambda_tasks.logging import task_logger


@lambda_task()
def greet(*, name: str) -> str:
    """A successful task — produces one per-task Sentry transaction named
    ``example_app.tasks.greet``."""
    task_logger.info(f"Running greet with: {name=}")

    for ii in range(3):
        task_logger.info(f"Sleeping: {ii=}")
        sleep(1)

    try:
        raise RuntimeError("testing")
    except Exception:
        task_logger.error("Raising", exc_info=True)

    return f"Hello, {name}!"


@lambda_task()
def boom(*, name: str) -> str:
    """A failing task — the executor records it as a FAILED TaskRecord and the
    error is reported to Sentry attributed to ``example_app.tasks.boom`` (not
    the Lambda function name), inside its own per-task trace."""
    task_logger.info(f"Running boom with: {name=}")
    raise RuntimeError(f"boom for {name}")
