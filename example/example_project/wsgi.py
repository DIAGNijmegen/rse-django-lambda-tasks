"""WSGI config for example_project."""

import os

from django.core.handlers.wsgi import WSGIHandler

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "example_project.settings")

application: WSGIHandler = WSGIHandler()
