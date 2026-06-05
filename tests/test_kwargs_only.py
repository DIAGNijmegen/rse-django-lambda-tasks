"""
Task 5 — kwargs-only signatures across all library functions.

TDD: test written first (red), then implementation makes it green.

Property: every non-dunder callable in lambda_tasks.* must have all
parameters as KEYWORD_ONLY (inspect.Parameter.KEYWORD_ONLY).

Exempt: __call__, __enter__, __exit__, __init_subclass__, __class_getitem__,
and any other dunder that must match a fixed Python protocol signature.
Also exempt: class objects themselves (we inspect their methods separately).
"""

import pytest

from lambda_tasks.decorators import lambda_task

# ---------------------------------------------------------------------------
# Underscore-prefixed parameter validation
# ---------------------------------------------------------------------------


class TestUnderscorePrefixRejected:
    def test_underscore_kwonly_param_raises_type_error(self):
        def bad(*, _name: str) -> None:
            pass

        with pytest.raises(TypeError, match="reserved parameter '_name'"):
            lambda_task(bad)

    def test_underscore_prefix_with_parens_raises_type_error(self):
        def bad(*, _delay: int) -> None:
            pass

        with pytest.raises(TypeError, match="reserved parameter '_delay'"):
            lambda_task(soft_timeout=60, hard_timeout=120)(bad)

    def test_multiple_underscore_params_raises_on_first(self):
        def bad(*, _a: int, _b: str) -> None:
            pass

        with pytest.raises(TypeError, match="reserved parameter"):
            lambda_task(bad)

    def test_non_underscore_kwonly_param_is_accepted(self):
        def good(*, name: str, value: int = 0) -> None:
            pass

        wrapper = lambda_task(good)
        assert wrapper.__name__ == "good"

    def test_double_underscore_param_raises_type_error(self):
        """Dunder-style params are also rejected — the rule is any leading underscore."""

        def bad(*, __internal: str) -> None:
            pass

        with pytest.raises(TypeError, match="reserved parameter"):
            lambda_task(bad)
