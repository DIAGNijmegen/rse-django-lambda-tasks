from lambda_tasks.decorators import lambda_task


@lambda_task
def greet(*, name: str) -> str:
    return f"Hello, {name}!"
