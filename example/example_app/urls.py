from django.urls import path
from example_app.views import trigger, trigger_error

urlpatterns = [
    path("trigger/", trigger),
    path("trigger-error/", trigger_error),
]
