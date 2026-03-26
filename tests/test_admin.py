from django.contrib import admin

import lambda_tasks.admin  # noqa: F401 — triggers @admin.register side-effect
from lambda_tasks.models import TaskRecord


def test_task_record_registered_in_admin():
    assert TaskRecord in admin.site._registry


def test_task_record_admin_list_display():
    assert admin.site._registry[TaskRecord].list_display == (
        "task_name",
        "status",
        "start_time",
        "end_time",
        "n_retries",
        "duration",
        "result",
    )
