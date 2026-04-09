"""
Decorator and wrapper for lambda tasks.

Provides LambdaTaskWrapper, which wraps a callable to expose:
- direct __call__(**kwargs) — synchronous invocation
- on_commit(**kwargs) — enqueue via django.db.transaction.on_commit

Also provides the lambda_task decorator factory.
"""

import functools
import inspect
import types
import uuid
from collections.abc import Callable
from typing import Any, overload

import pydantic
from redis.exceptions import LockError

from lambda_tasks.models import SQSLambdaTask, SQSLambdaTaskMessage
from lambda_tasks.settings import MAX_TIMEOUT, LambdaTasksSettings


def _build_kwargs_model(func: types.FunctionType) -> type[pydantic.BaseModel]:
    """Build a Pydantic model matching the keyword-only parameters of *func*.

    The model uses strict mode to prevent silent coercion (e.g. "1" → int),
    with the exception that bool is accepted for int-annotated fields since
    bool is a legitimate int subclass in Python.
    """
    sig = inspect.signature(func)
    hints = func.__annotations__.copy()
    hints.pop("return", None)

    fields: dict[str, Any] = {}

    for name, param in sig.parameters.items():
        annotation = hints.get(name, Any)

        if param.default is inspect.Parameter.empty:
            fields[name] = (annotation, ...)
        else:
            fields[name] = (annotation, param.default)

    return pydantic.create_model(
        f"_{func.__name__}_kwargs",
        __config__=pydantic.ConfigDict(strict=True, extra="forbid"),
        **fields,
    )


class LambdaTaskWrapper:
    """Wraps a function to be executed as a background task.

    Preserves __name__, __doc__, and __wrapped__ via functools.wraps.
    """

    # Declared explicitly so type checkers can see them (set by functools.update_wrapper)
    __wrapped__: Callable[..., Any]
    __name__: str
    __doc__: str | None

    def __init__(
        self,
        func: types.FunctionType,
        *,
        delay: int = 0,
        retry_delay: int = 0,
        soft_timeout: int | None = None,
        hard_timeout: int | None = None,
        queue: str = "default",
        ignore_errors: tuple[type[BaseException], ...] = (),
        retry_on: tuple[type[BaseException], ...] = (),
        singleton: bool = False,
    ) -> None:
        self._validate_func(func=func)
        self._validate_timeouts(soft_timeout=soft_timeout, hard_timeout=hard_timeout)
        self._validate_delay(delay=delay)
        self._validate_retry_delay(retry_delay=retry_delay, retry_on=retry_on)
        self._validate_ignore_errors(ignore_errors=ignore_errors)
        self._validate_retry_on(retry_on=retry_on)
        self._validate_no_overlap(retry_on=retry_on, ignore_errors=ignore_errors)
        self._validate_singleton(singleton=singleton, retry_on=retry_on)

        functools.update_wrapper(self, func)

        self._func: types.FunctionType = func
        self._delay = delay
        self._retry_delay = retry_delay
        self._soft_timeout = soft_timeout
        self._hard_timeout = hard_timeout
        self._queue = queue
        self._ignore_errors = ignore_errors
        self._retry_on = retry_on
        self._singleton = singleton
        self._kwargs_model: type[pydantic.BaseModel] = _build_kwargs_model(func)

    @property
    def resolved_timeouts(self) -> tuple[int, int]:
        """Return (soft_timeout, hard_timeout) resolved against settings defaults.

        Merges decorator-supplied values with settings defaults, then validates
        the final pair.  Each resolved value must be greater than zero and at
        most ``MAX_TIMEOUT`` (900), and ``soft_timeout`` must be strictly less
        than ``hard_timeout``.  Result is cached after first access.
        """
        try:
            return self._resolved_timeouts_cache
        except AttributeError:
            pass

        conf = LambdaTasksSettings()
        soft = (
            self._soft_timeout
            if self._soft_timeout is not None
            else conf.DEFAULT_SOFT_TIMEOUT
        )
        hard = (
            self._hard_timeout
            if self._hard_timeout is not None
            else conf.DEFAULT_HARD_TIMEOUT
        )

        # Validate settings-sourced values (decorator values are already checked at decoration time)
        for name, value, source in (
            ("soft_timeout", soft, self._soft_timeout),
            ("hard_timeout", hard, self._hard_timeout),
        ):
            if source is None and value <= 0:
                raise ValueError(
                    f"{name} ({value}) from settings must be greater than zero."
                )
            if source is None and value > MAX_TIMEOUT:
                raise ValueError(
                    f"{name} ({value}) from settings exceeds the maximum allowed value of {MAX_TIMEOUT} seconds."
                )

        # Cross-validate the resolved pair (decorator values may mix with settings defaults)
        if soft >= hard:
            raise ValueError(
                f"Resolved soft_timeout ({soft}) must be strictly less than "
                f"hard_timeout ({hard}) for task '{self.__name__}'."
            )

        self._resolved_timeouts_cache: tuple[int, int] = (soft, hard)
        return self._resolved_timeouts_cache

    def __call__(self, **kwargs: Any) -> Any:
        """Directly invoke the wrapped function."""
        self._kwargs_model.model_validate(kwargs)
        return self._func(**kwargs)

    def _build_task(self, *, kwargs: dict[str, Any]) -> SQSLambdaTask:
        """Pop overrides, validate kwargs, and build a SQSLambdaTask.

        Mutates *kwargs* in-place (pops ``_n_retries``).
        Returns ``SQSLambdaTask``.

        Raises:
            pydantic.ValidationError: if the remaining kwargs fail type validation.
        """
        n_retries = kwargs.pop("_n_retries", 0)

        self._kwargs_model.model_validate(kwargs)

        message = SQSLambdaTaskMessage(
            task_name=f"{self._func.__module__}.{self._func.__qualname__}",
            kwargs=dict(kwargs),
            n_retries=n_retries,
        )

        return SQSLambdaTask(
            message=message,
            delay=self._delay,
            queue=self._queue,
        )

    def serialize(self, **kwargs: Any) -> dict:
        """Serialize this task invocation to a JSON-compatible dict for deferred enqueuing.

        Returns a dict matching SQSLambdaTaskMessage schema

        Raises:
            pydantic.ValidationError: if kwargs fail the task's declared type annotations.
        """
        task = self._build_task(kwargs=kwargs)
        return task.model_dump()

    def execute_on_commit(self, **kwargs: Any) -> None:
        """Enqueue the task to run after the current transaction commits."""
        task = self._build_task(kwargs=kwargs)
        task.execute_on_commit()

    @staticmethod
    def _validate_func(*, func: types.FunctionType) -> None:
        """Raise TypeError if *func* has positional, **kwargs, underscore-prefixed, or unannotated parameters."""
        sig = inspect.signature(func)
        hints = func.__annotations__.copy()
        hints.pop("return", None)
        name: str = func.__name__

        for param in sig.parameters.values():
            if param.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                raise TypeError(
                    f"lambda_task functions must use keyword-only arguments. "
                    f"'{name}' has positional parameter '{param.name}'."
                )
            if param.kind is inspect.Parameter.VAR_KEYWORD:
                raise TypeError(
                    f"lambda_task functions must not use **kwargs. "
                    f"'{name}' has a **{param.name} parameter — declare all parameters explicitly."
                )
            if param.name.startswith("_"):
                raise TypeError(
                    f"lambda_task function parameters must not start with '_' "
                    f"(reserved for on_commit overrides). "
                    f"'{name}' has reserved parameter '{param.name}'."
                )
            if param.kind is inspect.Parameter.KEYWORD_ONLY and param.name not in hints:
                raise TypeError(
                    f"lambda_task function parameters must be type-annotated. "
                    f"'{name}' has unannotated parameter '{param.name}'."
                )

    @property
    def retry_delay(self) -> int:
        """Delay in seconds used when enqueuing a retry. 0 means use jitter."""
        return self._retry_delay

    @property
    def queue(self) -> str:
        """The SQS queue name this task is routed to."""
        return self._queue

    @property
    def ignore_errors(self) -> tuple[type[BaseException], ...]:
        """Exception types that are treated as non-fatal during task execution."""
        return self._ignore_errors

    @property
    def retry_on(self) -> tuple[type[BaseException], ...]:
        """Exception types that trigger an automatic retry."""
        return self._retry_on

    @property
    def singleton(self) -> bool:
        """Whether this task enforces single-concurrency via a Redis lock."""
        return self._singleton

    @staticmethod
    def _validate_delay(*, delay: int) -> None:
        """Raise ValueError if delay is outside the allowed range [0, 900]."""
        if delay < 0 or delay > 900:
            raise ValueError(f"delay ({delay}) must be in the range [0, 900].")

    @staticmethod
    def _validate_retry_delay(
        *,
        retry_delay: int,
        retry_on: tuple[type[BaseException], ...],
    ) -> None:
        """Raise ValueError if retry_delay is outside [0, 900], or TypeError if retry_delay != 0 and retry_on is empty."""
        if retry_delay < 0 or retry_delay > 900:
            raise ValueError(
                f"retry_delay ({retry_delay}) must be in the range [0, 900]."
            )

        if retry_delay != 0 and not retry_on:
            raise TypeError(
                f"retry_delay ({retry_delay}) requires retry_on to be non-empty — "
                f"retry_delay only applies when the task is configured to retry."
            )

    @staticmethod
    def _validate_ignore_errors(
        *, ignore_errors: tuple[type[BaseException], ...]
    ) -> None:
        """Raise TypeError if any element is not a subclass of BaseException."""
        for item in ignore_errors:
            if not (isinstance(item, type) and issubclass(item, BaseException)):
                raise TypeError(
                    f"ignore_errors must contain only exception types (subclasses of "
                    f"BaseException); got {item!r}."
                )

    @staticmethod
    def _validate_retry_on(*, retry_on: tuple[type[BaseException], ...]) -> None:
        """Raise TypeError if any element is not a subclass of BaseException."""
        for item in retry_on:
            if not (isinstance(item, type) and issubclass(item, BaseException)):
                raise TypeError(
                    f"retry_on must contain only exception types (subclasses of "
                    f"BaseException); got {item!r}."
                )

    @staticmethod
    def _validate_no_overlap(
        *,
        retry_on: tuple[type[BaseException], ...],
        ignore_errors: tuple[type[BaseException], ...],
    ) -> None:
        """Raise TypeError if any type in retry_on and ignore_errors overlap via subclass."""
        for retry_type in retry_on:
            for ignore_type in ignore_errors:
                if issubclass(retry_type, ignore_type) or issubclass(
                    ignore_type, retry_type
                ):
                    raise TypeError(
                        f"retry_on and ignore_errors must not overlap: "
                        f"{retry_type.__name__!r} and {ignore_type.__name__!r} conflict "
                        f"(one is a subclass of the other)."
                    )

    @staticmethod
    def _validate_timeouts(
        *, soft_timeout: int | None, hard_timeout: int | None
    ) -> None:
        """Raise ValueError if any timeout is not in (0, 900] or soft_timeout >= hard_timeout.

        Each timeout, when supplied, must be greater than zero and at most
        ``MAX_TIMEOUT`` (900).  When both are supplied, ``soft_timeout`` must
        be strictly less than ``hard_timeout``.
        """
        for name, value in (
            ("soft_timeout", soft_timeout),
            ("hard_timeout", hard_timeout),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} ({value}) must be greater than zero.")
            if value is not None and value > MAX_TIMEOUT:
                raise ValueError(
                    f"{name} ({value}) exceeds the maximum allowed value of {MAX_TIMEOUT} seconds."
                )

        if soft_timeout is not None and hard_timeout is not None:
            if soft_timeout >= hard_timeout:
                raise ValueError(
                    f"soft_timeout ({soft_timeout}) must be strictly less than "
                    f"hard_timeout ({hard_timeout})."
                )

    @staticmethod
    def _validate_singleton(
        *,
        singleton: bool,
        retry_on: tuple[type[BaseException], ...],
    ) -> None:
        """Raise TypeError if singleton=True and LockError is in retry_on."""
        if singleton:
            for exc_type in retry_on:
                if issubclass(exc_type, LockError) or issubclass(LockError, exc_type):
                    raise TypeError(
                        f"retry_on must not include LockError (or a superclass of it) "
                        f"when singleton=True — LockError is retried automatically. "
                        f"Got {exc_type.__name__!r}."
                    )


@overload
def lambda_task(
    func: types.FunctionType,
    *,
    delay: int = ...,
    retry_delay: int = ...,
    soft_timeout: int | None = ...,
    hard_timeout: int | None = ...,
    queue: str = ...,
    ignore_errors: tuple[type[BaseException], ...] = ...,
    retry_on: tuple[type[BaseException], ...] = ...,
    singleton: bool = ...,
) -> LambdaTaskWrapper: ...


@overload
def lambda_task(
    func: None = None,
    *,
    delay: int = ...,
    retry_delay: int = ...,
    soft_timeout: int | None = ...,
    hard_timeout: int | None = ...,
    queue: str = ...,
    ignore_errors: tuple[type[BaseException], ...] = ...,
    retry_on: tuple[type[BaseException], ...] = ...,
    singleton: bool = ...,
) -> Callable[[types.FunctionType], LambdaTaskWrapper]: ...


def lambda_task(
    func: types.FunctionType | None = None,
    *,
    delay: int = 0,
    retry_delay: int = 0,
    soft_timeout: int | None = None,
    hard_timeout: int | None = None,
    queue: str = "default",
    ignore_errors: tuple[type[BaseException], ...] = (),
    retry_on: tuple[type[BaseException], ...] = (),
    singleton: bool = False,
) -> LambdaTaskWrapper | Callable[[types.FunctionType], LambdaTaskWrapper]:
    """Decorator factory that registers a function as a background task.

    Can be used with or without parentheses::

        @lambda_task
        def my_task(*, x: int): ...

        @lambda_task(delay=5, soft_timeout=60, hard_timeout=120)
        def my_task(*, x: int): ...
    """

    def _decorate(f: types.FunctionType) -> LambdaTaskWrapper:
        wrapper = LambdaTaskWrapper(
            f,
            delay=delay,
            retry_delay=retry_delay,
            soft_timeout=soft_timeout,
            hard_timeout=hard_timeout,
            queue=queue,
            ignore_errors=ignore_errors,
            retry_on=retry_on,
            singleton=singleton,
        )
        return wrapper

    if func is not None:
        # Called as @lambda_task (no parentheses)
        return _decorate(func)

    # Called as @lambda_task(...) — return the decorator
    return _decorate
