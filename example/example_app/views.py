from django.http import HttpRequest, HttpResponse
from example_app.tasks import boom, greet


def trigger(request: HttpRequest) -> HttpResponse:
    """Enqueue the successful task — produces a per-task Sentry transaction."""
    name = request.GET.get("name", "world")
    greet.execute_on_commit(name=name)
    return HttpResponse(f"greet task triggered for: {name}")


def trigger_error(request: HttpRequest) -> HttpResponse:
    """Enqueue the failing task — produces a Sentry error attributed to the
    task name, inside its own per-task trace."""
    name = request.GET.get("name", "world")
    boom.execute_on_commit(name=name)
    return HttpResponse(f"boom task triggered for: {name}")
