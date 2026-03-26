from django.urls import path
from example_app.views import trigger

urlpatterns = [
    path("trigger/", trigger),
]
