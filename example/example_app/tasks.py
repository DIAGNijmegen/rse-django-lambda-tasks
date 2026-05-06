import logging
from time import sleep

from lambda_tasks.decorators import lambda_task

logger = logging.getLogger(__name__)


@lambda_task()
def greet(*, name: str) -> str:
    logger.warning(f"Running greet with: {name=}")

    ii = 0

    while ii < 10:
        logger.warning(f"Sleeping: {ii=}")
        sleep(1)
        ii += 1

    return f"Hello, {name}!"
