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
