from django.http import HttpRequest, HttpResponse
from example_app.tasks import greet


def trigger(request: HttpRequest) -> HttpResponse:
    name = request.GET.get("name", "world")
    greet.execute_on_commit(name=name)
    return HttpResponse(f"Task triggered for: {name}")
