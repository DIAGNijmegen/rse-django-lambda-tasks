DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "lambda_tasks_test",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "localhost",
        "PORT": "5432",
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
