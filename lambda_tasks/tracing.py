"""Optional OpenTelemetry tracing for the Lambda and Batch execution paths.

Initialised at import time of ``lambda_tasks.handler`` (before the Lambda
runtime invokes the handler, which is earlier than ``django.setup()``). Driven
entirely by standard OTEL environment variables so this package stays generic:
the consuming application declares the OTEL dependencies and sets
``OTEL_EXPORTER_OTLP_ENDPOINT`` to the regional AWS X-Ray OTLP traces endpoint
(``https://xray.<region>.amazonaws.com/v1/traces``).

Spans are exported directly to the X-Ray endpoint — no collector, no daemon,
no Lambda layer. Requests are SigV4-signed with the ambient AWS credentials
(the task / execution role), so there is no API key to manage. When no
credentials are resolvable (local dev / tests) the request is left unsigned.

The OpenTelemetry and ``requests`` imports are deferred into the functions
below so they are only reached when tracing is enabled (a non-empty
``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``).
This keeps the OTEL packages a genuinely optional extra: importing
``lambda_tasks.handler`` without them installed is a no-op, not an
``ImportError``.
"""

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    import requests
    from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger(__name__)

_XRAY_SIGV4_SERVICE_NAME = "xray"


def tracing_is_enabled() -> bool:
    """Whether an OTLP traces endpoint has been configured.

    Either the base ``OTEL_EXPORTER_OTLP_ENDPOINT`` or the per-signal
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` turns it on.
    """
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    )


def _running_on_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _build_signing_session() -> "requests.Session":
    import requests
    import requests.auth
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session as BotocoreSession

    class _SigV4Auth(requests.auth.AuthBase):
        """SigV4-signs OTLP exports to the X-Ray endpoint."""

        def __init__(
            self, *, service_name: str, region: str, session: BotocoreSession
        ) -> None:
            self._service_name = service_name
            self._region = region
            self._session = session

        def __call__(self, r: "requests.PreparedRequest") -> "requests.PreparedRequest":
            credentials = self._session.get_credentials()

            if credentials is None:
                logger.warning(
                    "No AWS credentials resolvable; sending OTLP span export to "
                    "%s unsigned. The X-Ray endpoint will reject unsigned "
                    "requests, so spans will be dropped.",
                    r.url,
                )
                return r

            aws_request = AWSRequest(
                method=r.method,
                url=r.url,
                data=r.body,
                headers={"Content-Type": "application/x-protobuf"},
            )
            SigV4Auth(credentials, self._service_name, self._region).add_auth(
                aws_request
            )

            r.headers.update(dict(aws_request.headers))

            return r

    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", ""))

    if not region:
        logger.warning(
            "No AWS region resolvable from AWS_REGION / AWS_DEFAULT_REGION; "
            "SigV4 signing of OTLP span exports may fail."
        )

    session = requests.Session()
    session.auth = _SigV4Auth(
        service_name=_XRAY_SIGV4_SERVICE_NAME,
        region=region,
        session=BotocoreSession(),
    )

    return session


def _build_tracer_provider() -> "TracerProvider":
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    service_name = os.environ.get(
        "OTEL_SERVICE_NAME",
        os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "lambda-tasks"),
    )

    tracer_provider = TracerProvider(
        resource=Resource.create(attributes={"service.name": service_name}),
        id_generator=AwsXRayIdGenerator(),
    )

    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            span_exporter=OTLPSpanExporter(session=_build_signing_session())
        )
    )
    return tracer_provider


def _set_xray_propagator() -> None:
    from opentelemetry import propagate
    from opentelemetry.propagators.aws import (
        AwsXRayLambdaPropagator,
        AwsXRayPropagator,
    )

    propagate.set_global_textmap(
        AwsXRayLambdaPropagator() if _running_on_lambda() else AwsXRayPropagator()
    )


def _instrument_libraries() -> None:
    """Instrument the libraries whose calls we want as spans.

    SQL (psycopg 3), AWS SDK (botocore → boto3/aioboto3), outbound HTTP
    (requests and httpx), and cache (redis). Each is wrapped independently and
    is best-effort: a missing or failing instrumentor is logged, not raised, so
    it cannot take down the worker. All are process-global and idempotent.

    The OTLP span export itself goes out over ``requests`` to the X-Ray
    endpoint; that URL is excluded from the requests instrumentor so the
    exporter's own calls are not traced (which would be self-referential noise).
    """

    def _try(name: str, instrument: "Callable[[], None]") -> None:
        try:
            instrument()
        except Exception:
            logger.warning(
                "Failed to instrument %s for tracing; continuing without it.",
                name,
                exc_info=True,
            )
        else:
            logger.info("%s instrumentation installed.", name)

    def _psycopg() -> None:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

        PsycopgInstrumentor().instrument()

    def _botocore() -> None:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

        BotocoreInstrumentor().instrument()

    def _requests() -> None:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        # Do not trace the exporter's own POST to the X-Ray OTLP endpoint.
        RequestsInstrumentor().instrument(excluded_urls="xray\\..*\\.amazonaws\\.com")

    def _httpx() -> None:
        from opentelemetry.instrumentation.httpx import (
            HTTPXClientInstrumentor,
        )

        HTTPXClientInstrumentor().instrument()

    def _redis() -> None:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()

    _try("psycopg", _psycopg)
    _try("botocore", _botocore)
    _try("requests", _requests)
    _try("httpx", _httpx)
    _try("redis", _redis)


def configure_tracing() -> "TracerProvider | None":
    """Install the global tracer provider. No-op unless enabled.

    Returns the provider so callers (e.g. the Batch entry point) can flush it
    on shutdown, or ``None`` when tracing is disabled or already configured.

    Tracing is best-effort: if it is enabled but the OTEL packages are missing
    or the provider cannot be built, the failure is logged and ``None`` is
    returned rather than propagated — a broken observability setup must not
    take down the worker (this runs at import time of the handler module).
    """
    if not tracing_is_enabled():
        logger.debug("OTEL tracing disabled (no OTLP endpoint configured).")
        return None

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        if isinstance(trace.get_tracer_provider(), TracerProvider):
            logger.info(
                "A TracerProvider is already installed; skipping configuration."
            )
            return None

        tracer_provider = _build_tracer_provider()
        trace.set_tracer_provider(tracer_provider)
        _set_xray_propagator()
        _instrument_libraries()
    except ImportError:
        logger.warning(
            "OTEL tracing is enabled but the OpenTelemetry packages are not "
            "installed; tracing is disabled. Install the 'otel' extra to "
            "enable it.",
            exc_info=True,
        )
        return None
    except Exception:
        logger.warning(
            "Failed to configure OTEL tracing; continuing without it.",
            exc_info=True,
        )
        return None

    logger.info("OTEL tracing configured (exporting spans to the OTLP endpoint).")
    return tracer_provider


def instrument_lambda_handler() -> None:
    """Wrap the Lambda handler for tracing. No-op unless enabled.

    Must be called at import time of the handler module, before the runtime
    invokes the handler.
    """
    if not tracing_is_enabled():
        return

    if not _running_on_lambda():
        logger.debug(
            "Not running on Lambda (AWS_LAMBDA_FUNCTION_NAME unset); "
            "skipping AwsLambdaInstrumentor."
        )
        return

    try:
        from opentelemetry.instrumentation.aws_lambda import (
            AwsLambdaInstrumentor,
        )

        AwsLambdaInstrumentor().instrument()
    except Exception:
        logger.warning(
            "Failed to instrument the Lambda handler for tracing; "
            "continuing without handler spans.",
            exc_info=True,
        )
        return

    logger.info("AwsLambdaInstrumentor installed; Lambda handler is traced.")


def flush_tracing() -> None:
    """Force-flush and shut down the global tracer provider. No-op unless enabled.

    Called at the end of the one-shot Batch entry point so short-lived jobs do
    not drop buffered spans. Gated on ``tracing_is_enabled()`` so the OTEL
    imports are never reached when tracing is disabled (or the packages are not
    installed).
    """
    if not tracing_is_enabled():
        return

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        logger.debug("Flushing and shutting down the TracerProvider before exit.")
        provider.force_flush()
        provider.shutdown()
    else:
        logger.debug(
            "Tracing is enabled but no TracerProvider is installed; "
            "nothing to flush."
        )
