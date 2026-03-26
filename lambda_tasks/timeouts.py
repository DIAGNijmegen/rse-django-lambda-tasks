"""Timeout enforcement for lambda tasks using Unix SIGALRM."""

import signal

from lambda_tasks.settings import LambdaTasksSettings


class SoftTimeLimitExceeded(Exception):
    """Raised inside a running task when the soft timeout is exceeded.

    The task may catch this exception to perform cleanup before the hard
    timeout forcibly terminates execution.
    """


class HardTimeLimitExceeded(Exception):
    """Raised by the executor when the hard timeout is exceeded.

    The task is terminated immediately; this exception is not intended to
    be caught by task code.
    """


class TimeoutContext:
    """Context manager that enforces soft and hard timeouts via SIGALRM.

    Two-phase approach:
    1. Arm soft_timeout seconds. On SIGALRM, raise SoftTimeLimitExceeded
       inside the task and re-arm for (hard_timeout - soft_timeout) seconds.
    2. Second SIGALRM raises HardTimeLimitExceeded.

    Any pre-existing alarm is saved on enter and restored on exit.

    Usage::

        with TimeoutContext(soft_timeout=270, hard_timeout=300):
            run_task()
    """

    def __init__(self, soft_timeout: int, hard_timeout: int) -> None:
        self.soft_timeout = soft_timeout
        self.hard_timeout = hard_timeout
        self._previous_alarm: int = 0
        self._previous_handler = None
        self._in_hard_phase: bool = False

    def _handler(self, signum: int, frame) -> None:
        if not self._in_hard_phase:
            # Phase 1 → Phase 2: raise soft, re-arm for remaining hard window
            self._in_hard_phase = True
            remaining = self.hard_timeout - self.soft_timeout
            signal.alarm(max(1, remaining))
            raise SoftTimeLimitExceeded()
        else:
            # Phase 2: hard timeout expired
            raise HardTimeLimitExceeded()

    def __enter__(self) -> "TimeoutContext":
        conf = LambdaTasksSettings()

        if not conf.EAGER:
            self._in_hard_phase = False
            self._previous_handler = signal.signal(signal.SIGALRM, self._handler)
            self._previous_alarm = signal.alarm(self.soft_timeout)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        conf = LambdaTasksSettings()

        if not conf.EAGER:
            # Cancel any pending alarm we armed
            signal.alarm(0)
            # Restore the original signal handler
            signal.signal(signal.SIGALRM, self._previous_handler)
            # Restore any pre-existing alarm
            if self._previous_alarm > 0:
                signal.alarm(self._previous_alarm)
