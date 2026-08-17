"""Tests for the django.contrib.postgres system check."""

from lambda_tasks.checks import check_contrib_postgres_installed


def test_check_passes_when_contrib_postgres_installed(settings):
    settings.INSTALLED_APPS = [
        "django.contrib.postgres",
        "lambda_tasks",
    ]
    errors = check_contrib_postgres_installed(app_configs=None)
    assert errors == []


def test_check_errors_when_contrib_postgres_missing(settings):
    settings.INSTALLED_APPS = [
        "lambda_tasks",
    ]
    errors = check_contrib_postgres_installed(app_configs=None)
    assert len(errors) == 1
    assert errors[0].id == "lambda_tasks.E001"
