"""Optional Sentry initialisation for the example app.

Demonstrates how a host application wires Sentry so that
``django-lambda-tasks`` can attribute per-task traces and errors. This mirrors
a typical production ``sentry_sdk.init(...)`` (Django + AWS Lambda integrations
and tracing), but is driven entirely by environment variables so the example
runs with or without Sentry configured.

``init_sentry()`` is called from ``settings.py`` at import time. Because the
local process-pool workers call ``django.setup()`` (which imports settings) in
each child process, this runs in the web process *and* in every task worker —
which is exactly what is needed for the per-task trace to be captured where the
task actually executes.
"""

import logging
import os


def init_sentry() -> None:
    """Initialise Sentry if ``SENTRY_DSN`` is set; otherwise do nothing.

    Idempotent enough for the example's purposes: ``sentry_sdk.init`` is safe to
    call once per process, and this is only invoked from settings import.
    """
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return

    import sentry_sdk
    from sentry_sdk.integrations.aws_lambda import AwsLambdaIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=dsn,
        integrations=[
            DjangoIntegration(),
            AwsLambdaIntegration(),
            # On sentry-sdk 2.x the logging integration defaulted to
            # event_level=ERROR, so logger.error(...) calls (e.g. task_logger)
            # became Sentry events automatically. On 3.0 the default is None, so
            # error logs no longer surface as events. Set it explicitly so
            # task_logger.error output appears in Sentry on both SDK versions.
            LoggingIntegration(event_level=logging.ERROR),
        ],
        # Sample every transaction so the per-task trace is always sent while
        # you are testing. Lower this in a real deployment.
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
    )
