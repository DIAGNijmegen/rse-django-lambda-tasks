"""Tests for lambda_tasks.tracing.

These paths are driven entirely by standard OTEL environment variables and do
not read Django settings, so environment is manipulated with monkeypatch and
override_settings is never used.

Each test resets the global tracer provider so installation is observable and
isolated. The OTEL SDK forbids overwriting a real TracerProvider once set, so
the suite swaps in a fresh default proxy provider before each test.
"""

import builtins
import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from lambda_tasks import tracing

_ENDPOINT = "https://xray.us-east-1.amazonaws.com/v1/traces"


@pytest.fixture(autouse=True)
def _reset_global_tracer_provider():
    """Reset OTEL global state so each test starts from a clean default provider."""
    import opentelemetry.trace as trace_module

    original_provider = trace_module._TRACER_PROVIDER
    original_set = trace_module._TRACER_PROVIDER_SET_ONCE
    trace_module._TRACER_PROVIDER = None
    trace_module._TRACER_PROVIDER_SET_ONCE = trace_module.Once()
    try:
        yield
    finally:
        trace_module._TRACER_PROVIDER = original_provider
        trace_module._TRACER_PROVIDER_SET_ONCE = original_set


def _clear_otel_env(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)


def _library_instrumentors():
    """The five instrumentor instances configure_tracing() should install."""
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor

    return {
        "Psycopg": PsycopgInstrumentor(),
        "Botocore": BotocoreInstrumentor(),
        "Requests": RequestsInstrumentor(),
        "HTTPXClient": HTTPXClientInstrumentor(),
        "Redis": RedisInstrumentor(),
    }


@pytest.fixture
def _uninstrument_libraries():
    """Uninstrument the library instrumentors after a test.

    The instrumentors are process-global singletons; leaving them instrumented
    would leak into other tests. Uninstrument before and after so the test
    starts from a clean slate regardless of ordering.
    """
    instrumentors = _library_instrumentors()
    for instrumentor in instrumentors.values():
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    try:
        yield instrumentors
    finally:
        for instrumentor in instrumentors.values():
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()


class TestTracingIsEnabled:
    def test_disabled_when_no_endpoint(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        assert tracing.tracing_is_enabled() is False

    def test_enabled_by_base_endpoint(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        assert tracing.tracing_is_enabled() is True

    def test_enabled_by_per_signal_endpoint(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _ENDPOINT)
        assert tracing.tracing_is_enabled() is True

    def test_disabled_when_endpoint_blank(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
        assert tracing.tracing_is_enabled() is False


class TestConfigureTracing:
    def test_noop_when_disabled(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        result = tracing.configure_tracing()
        assert result is None
        assert not isinstance(trace.get_tracer_provider(), TracerProvider)

    def test_installs_provider_when_enabled(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        result = tracing.configure_tracing()

        assert isinstance(result, TracerProvider)
        assert trace.get_tracer_provider() is result

    def test_idempotent_on_second_call(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        first = tracing.configure_tracing()
        second = tracing.configure_tracing()

        assert isinstance(first, TracerProvider)
        assert second is None
        assert trace.get_tracer_provider() is first


class TestInstrumentLibraries:
    def test_instruments_all_libraries_when_enabled(
        self, monkeypatch, _uninstrument_libraries
    ):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        tracing.configure_tracing()

        for name, instrumentor in _uninstrument_libraries.items():
            assert instrumentor.is_instrumented_by_opentelemetry is True, name

    def test_noop_when_disabled(self, monkeypatch, _uninstrument_libraries):
        _clear_otel_env(monkeypatch)

        result = tracing.configure_tracing()

        assert result is None
        for name, instrumentor in _uninstrument_libraries.items():
            assert instrumentor.is_instrumented_by_opentelemetry is False, name

    def test_one_failing_instrumentor_does_not_block_the_others(
        self, monkeypatch, caplog, _uninstrument_libraries
    ):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

        def _boom(self, **kwargs):
            raise RuntimeError("botocore instrumentation failed")

        monkeypatch.setattr(BotocoreInstrumentor, "_instrument", _boom)

        with caplog.at_level("WARNING", logger="lambda_tasks.tracing"):
            tracing.configure_tracing()

        instrumentors = _uninstrument_libraries
        assert instrumentors["Botocore"].is_instrumented_by_opentelemetry is False
        for name in ("Psycopg", "Requests", "HTTPXClient", "Redis"):
            assert instrumentors[name].is_instrumented_by_opentelemetry is True, name

        assert any(
            "Failed to instrument botocore" in record.message
            for record in caplog.records
        )

    def test_requests_instrumentor_excludes_xray_endpoint(
        self, monkeypatch, _uninstrument_libraries
    ):
        """A POST to the X-Ray OTLP endpoint must produce no span.

        The requests instrumentor is configured with an excluded_urls regex
        matching the X-Ray host so the exporter's own export calls are not
        traced (self-referential noise).
        """
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        import requests
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        with patch.object(tracing, "_build_tracer_provider", return_value=provider):
            tracing.configure_tracing()

        # Issue a request to the excluded X-Ray endpoint; the underlying
        # socket is patched so no real network call is made. The instrumentor
        # decides whether to create a span before the transport runs.
        with patch.object(
            requests.adapters.HTTPAdapter,
            "send",
            side_effect=requests.exceptions.ConnectionError("blocked"),
        ):
            with pytest.raises(requests.exceptions.ConnectionError):
                requests.post(_ENDPOINT, data=b"body")

        assert exporter.get_finished_spans() == ()


class TestInstrumentLambdaHandler:
    def test_noop_off_lambda(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)

        from opentelemetry.instrumentation.aws_lambda import AwsLambdaInstrumentor

        instrumentor = AwsLambdaInstrumentor()
        assert instrumentor.is_instrumented_by_opentelemetry is False
        tracing.instrument_lambda_handler()
        assert instrumentor.is_instrumented_by_opentelemetry is False

    def test_noop_when_disabled(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")

        from opentelemetry.instrumentation.aws_lambda import AwsLambdaInstrumentor

        instrumentor = AwsLambdaInstrumentor()
        assert instrumentor.is_instrumented_by_opentelemetry is False
        tracing.instrument_lambda_handler()
        assert instrumentor.is_instrumented_by_opentelemetry is False

    def test_instruments_on_lambda_when_enabled(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")
        monkeypatch.setenv("ORIG_HANDLER", "lambda_tasks.handler.handler")

        from opentelemetry.instrumentation.aws_lambda import AwsLambdaInstrumentor

        instrumentor = AwsLambdaInstrumentor()
        try:
            tracing.instrument_lambda_handler()
            assert instrumentor.is_instrumented_by_opentelemetry is True
        finally:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()

    def test_defaults_orig_handler_when_unset(self, monkeypatch):
        """With ORIG_HANDLER unset the package defaults it to its own handler
        so container-image deployments need not set the env var."""
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")
        monkeypatch.delenv("ORIG_HANDLER", raising=False)
        monkeypatch.delenv("_HANDLER", raising=False)

        from opentelemetry.instrumentation.aws_lambda import AwsLambdaInstrumentor

        instrumentor = AwsLambdaInstrumentor()
        try:
            tracing.instrument_lambda_handler()
            assert os.environ["ORIG_HANDLER"] == "lambda_tasks.handler.handler"
        finally:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()

    def test_leaves_explicit_orig_handler_unchanged(self, monkeypatch):
        """An explicitly configured ORIG_HANDLER wins — setdefault must not
        overwrite a consumer that invokes the handler at a different path."""
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")
        monkeypatch.setenv("ORIG_HANDLER", "custom.module.my_handler")

        from opentelemetry.instrumentation.aws_lambda import AwsLambdaInstrumentor

        instrumentor = AwsLambdaInstrumentor()
        try:
            tracing.instrument_lambda_handler()
            assert os.environ["ORIG_HANDLER"] == "custom.module.my_handler"
        finally:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()


class TestOptionalDependency:
    """The OTEL packages are a genuinely optional extra.

    Importing lambda_tasks.handler / lambda_tasks.tracing must not require
    opentelemetry or requests to be installed, and the disabled no-op path
    must never reach those imports.
    """

    @staticmethod
    def _block_otel_and_requests():
        """Return an __import__ replacement that hides OTEL and requests."""
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.split(".")[0] in ("opentelemetry", "requests"):
                raise ModuleNotFoundError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        return fake_import

    def test_handler_imports_without_otel_installed(self):
        """lambda_tasks.handler imports cleanly with OTEL/requests absent.

        Run in a subprocess so blocking the imports cannot perturb module
        singletons the rest of the suite relies on.
        """
        program = (
            "import builtins\n"
            "_real = builtins.__import__\n"
            "def _fake(name, *a, **k):\n"
            "    if name.split('.')[0] in ('opentelemetry', 'requests'):\n"
            "        raise ModuleNotFoundError('No module named ' + name)\n"
            "    return _real(name, *a, **k)\n"
            "builtins.__import__ = _fake\n"
            "import os\n"
            "os.environ.pop('OTEL_EXPORTER_OTLP_ENDPOINT', None)\n"
            "os.environ.pop('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', None)\n"
            "import lambda_tasks.handler as h\n"
            "assert h.configure_tracing() is None\n"
            "assert h.instrument_lambda_handler() is None\n"
            "assert h.flush_tracing() is None\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            env={**os.environ, "DJANGO_SETTINGS_MODULE": ""},
        )
        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout

    def test_disabled_entry_points_do_not_import_otel(self, monkeypatch):
        """With tracing disabled, configure/instrument are no-ops that never
        touch opentelemetry — so they work with the packages absent."""
        _clear_otel_env(monkeypatch)
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
        monkeypatch.setattr(builtins, "__import__", self._block_otel_and_requests())

        assert tracing.tracing_is_enabled() is False
        assert tracing.configure_tracing() is None
        assert tracing.instrument_lambda_handler() is None
        assert tracing.flush_tracing() is None

    def test_enabled_but_otel_missing_logs_and_does_not_raise(
        self, monkeypatch, caplog
    ):
        """configure_tracing() is enabled but OTEL is absent → logged no-op,
        not an ImportError that would crash handler import."""
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        monkeypatch.setattr(builtins, "__import__", self._block_otel_and_requests())

        with caplog.at_level("WARNING", logger="lambda_tasks.tracing"):
            result = tracing.configure_tracing()

        assert result is None
        assert any(
            "OpenTelemetry packages are not installed" in record.message
            for record in caplog.records
        )


class TestLoggingDiagnostics:
    def test_missing_credentials_logs_warning(self, monkeypatch, caplog):
        """A span export with no resolvable AWS credentials logs a warning
        (otherwise the unsigned request is silently rejected by X-Ray)."""
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        session = tracing._build_signing_session()

        prepared = (
            __import__("requests")
            .Request(method="POST", url=_ENDPOINT, data=b"body")
            .prepare()
        )

        with (
            patch(
                "botocore.session.Session.get_credentials",
                return_value=None,
            ),
            caplog.at_level("WARNING", logger="lambda_tasks.tracing"),
        ):
            session.auth(prepared)

        assert any("unsigned" in record.message for record in caplog.records)

    def test_missing_region_logs_warning(self, monkeypatch, caplog):
        """Building the signing session with no region resolvable logs a warning."""
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        with caplog.at_level("WARNING", logger="lambda_tasks.tracing"):
            tracing._build_signing_session()

        assert any(
            "No AWS region resolvable" in record.message for record in caplog.records
        )


class TestHandlerImportOrderingRegression:
    """Regression test for the import-time ordering bug.

    `instrument_lambda_handler()` must run AFTER `def handler` executes, because
    AwsLambdaInstrumentor wraps the handler via wrapt attribute lookup on the
    module. When the setup calls sat at the top of the module (before the def),
    the lookup raised AttributeError, the best-effort wrapper swallowed it and
    logged "Failed to instrument the Lambda handler...", and the handler was
    never wrapped — so no SERVER span was produced.

    Run in a subprocess so the fresh, fully-instrumented import cannot perturb
    the module singletons the rest of the suite relies on.
    """

    def test_fresh_import_wraps_handler_without_warning(self):
        program = (
            "import logging\n"
            "records = []\n"
            "class _Capture(logging.Handler):\n"
            "    def emit(self, record):\n"
            "        records.append(record.getMessage())\n"
            "logging.getLogger('lambda_tasks.tracing').addHandler(_Capture())\n"
            "logging.getLogger('lambda_tasks.tracing').setLevel(logging.DEBUG)\n"
            "import lambda_tasks.handler as h\n"
            "from opentelemetry.instrumentation.aws_lambda import "
            "AwsLambdaInstrumentor\n"
            "assert AwsLambdaInstrumentor().is_instrumented_by_opentelemetry, "
            "'handler was not wrapped'\n"
            "assert not any('Failed to instrument the Lambda handler' in m "
            "for m in records), records\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "OTEL_EXPORTER_OTLP_ENDPOINT": _ENDPOINT,
                "AWS_LAMBDA_FUNCTION_NAME": "my-fn",
                "AWS_REGION": "us-east-1",
                "ORIG_HANDLER": "lambda_tasks.handler.handler",
                "DJANGO_SETTINGS_MODULE": "",
            },
        )
        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout


class TestHandlerSpanParenting:
    """An invocation of the wrapped handler produces a SERVER span whose trace
    contains the task's inner spans as descendants (single connected trace)."""

    def test_inner_span_descends_from_handler_server_span(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _ENDPOINT)
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("ORIG_HANDLER", "lambda_tasks.handler.handler")

        from opentelemetry import trace as trace_api
        from opentelemetry.instrumentation.aws_lambda import AwsLambdaInstrumentor
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace_api.set_tracer_provider(provider)

        instrumentor = AwsLambdaInstrumentor()

        def _inner_handler(event, context):
            tracer = trace_api.get_tracer("test")
            with tracer.start_as_current_span("inner-db-span"):
                pass
            return {"batchItemFailures": []}

        monkeypatch.setattr(
            "lambda_tasks.handler.handler", _inner_handler, raising=False
        )

        class _Context:
            function_name = "my-fn"
            function_version = "$LATEST"
            invoked_function_arn = (
                "arn:aws:lambda:us-east-1:123456789012:function:my-fn"
            )
            memory_limit_in_mb = 128
            aws_request_id = "req-1"

        try:
            instrumentor.instrument()
            wrapped = __import__("lambda_tasks.handler", fromlist=["handler"]).handler
            wrapped({"Records": []}, _Context())
        finally:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()

        spans = exporter.get_finished_spans()
        server_spans = [s for s in spans if s.kind == trace_api.SpanKind.SERVER]
        inner_spans = [s for s in spans if s.name == "inner-db-span"]

        assert server_spans, f"no SERVER span produced; got {[s.name for s in spans]}"
        assert inner_spans, "inner task span was not recorded"

        server = server_spans[0]
        inner = inner_spans[0]

        assert inner.context.trace_id == server.context.trace_id
        assert inner.parent is not None
        assert inner.parent.span_id == server.context.span_id
