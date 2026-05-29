"""
Unit tests for memory limit enforcement in the Lambda handler.

The handler sets resource.RLIMIT_AS during cold start so that runaway
memory allocation raises MemoryError instead of triggering the Lambda OOM
killer.
"""

import resource
from unittest.mock import patch

import lambda_tasks.handler as handler_module


class TestMemoryLimitSetDuringColdStart:
    def test_rlimit_as_set_from_env_var(self, monkeypatch):
        """When AWS_LAMBDA_FUNCTION_MEMORY_SIZE is set, RLIMIT_AS is configured with 128 MB reserved."""
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "512")
        monkeypatch.setattr("lambda_tasks.handler.resolve_environment", lambda: None)
        monkeypatch.setattr(
            "lambda_tasks.handler.resolve_secrets_into_env", lambda: None
        )

        calls: list[tuple] = []

        def fake_setrlimit(which: int, limits: tuple[int, int]) -> None:
            calls.append((which, limits))

        monkeypatch.setattr(resource, "setrlimit", fake_setrlimit)

        handler_module._cold_start_done = False
        handler_module.handler(event={"Records": []}, context=None)

        expected = (512 - 128) * 1024 * 1024
        assert calls == [(resource.RLIMIT_AS, (expected, expected))]

    def test_rlimit_as_not_set_when_env_var_missing(self, monkeypatch):
        """When AWS_LAMBDA_FUNCTION_MEMORY_SIZE is not set, RLIMIT_AS is unchanged."""
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", raising=False)
        monkeypatch.setattr("lambda_tasks.handler.resolve_environment", lambda: None)
        monkeypatch.setattr(
            "lambda_tasks.handler.resolve_secrets_into_env", lambda: None
        )

        calls: list[tuple] = []

        def fake_setrlimit(which: int, limits: tuple[int, int]) -> None:
            calls.append((which, limits))

        monkeypatch.setattr(resource, "setrlimit", fake_setrlimit)

        handler_module._cold_start_done = False
        handler_module.handler(event={"Records": []}, context=None)

        assert calls == []

    def test_memory_limit_set_before_loaders(self, monkeypatch):
        """The memory limit is set before resolve_environment runs."""
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "256")

        call_order: list[str] = []

        def fake_setrlimit(which: int, limits: tuple[int, int]) -> None:
            call_order.append("setrlimit")

        def fake_resolve_environment() -> None:
            call_order.append("resolve_environment")

        def fake_resolve_secrets() -> None:
            call_order.append("resolve_secrets_into_env")

        monkeypatch.setattr(resource, "setrlimit", fake_setrlimit)
        monkeypatch.setattr(
            "lambda_tasks.handler.resolve_environment", fake_resolve_environment
        )
        monkeypatch.setattr(
            "lambda_tasks.handler.resolve_secrets_into_env", fake_resolve_secrets
        )

        handler_module._cold_start_done = False
        handler_module.handler(event={"Records": []}, context=None)

        assert call_order == [
            "setrlimit",
            "resolve_environment",
            "resolve_secrets_into_env",
        ]

    def test_memory_limit_is_logged(self, monkeypatch, caplog):
        """Setting the memory limit emits an info log."""
        import logging

        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "1024")
        monkeypatch.setattr("lambda_tasks.handler.resolve_environment", lambda: None)
        monkeypatch.setattr(
            "lambda_tasks.handler.resolve_secrets_into_env", lambda: None
        )
        monkeypatch.setattr(resource, "setrlimit", lambda *args: None)

        handler_module._cold_start_done = False

        with caplog.at_level(logging.INFO, logger="lambda_tasks.handler"):
            handler_module.handler(event={"Records": []}, context=None)

        assert any("RLIMIT_AS" in msg for msg in caplog.messages)

    def test_rlimit_floored_at_minimum_when_memory_too_small(self, monkeypatch):
        """When AWS_LAMBDA_FUNCTION_MEMORY_SIZE <= reserved, RLIMIT_AS is set to the 64 MB minimum."""
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "128")
        monkeypatch.setattr("lambda_tasks.handler.resolve_environment", lambda: None)
        monkeypatch.setattr(
            "lambda_tasks.handler.resolve_secrets_into_env", lambda: None
        )

        calls: list[tuple] = []

        def fake_setrlimit(which: int, limits: tuple[int, int]) -> None:
            calls.append((which, limits))

        monkeypatch.setattr(resource, "setrlimit", fake_setrlimit)

        handler_module._cold_start_done = False
        handler_module.handler(event={"Records": []}, context=None)

        expected = 64 * 1024 * 1024
        assert calls == [(resource.RLIMIT_AS, (expected, expected))]
