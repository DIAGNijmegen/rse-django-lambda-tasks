from time import sleep

from lambda_tasks.decorators import lambda_task
from lambda_tasks.logging import task_logger


@lambda_task()
def greet(*, name: str) -> str:
    task_logger.info(f"Running greet with: {name=}")

    ii = 0

    while ii < 10:
        task_logger.info(f"Sleeping: {ii=}")
        sleep(1)
        ii += 1

    return f"Hello, {name}!"
