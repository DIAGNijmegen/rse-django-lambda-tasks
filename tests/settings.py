DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "lambda_tasks",
]

USE_TZ = True

LAMBDA_TASKS_QUEUES = {
    "default": {"queue_url": "https://sqs.localhost/default"},
}
