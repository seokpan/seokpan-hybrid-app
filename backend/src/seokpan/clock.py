"""Read-only clocks shared by application services and provider adapters."""

import time
from typing import Protocol


class MillisecondClock(Protocol):
    @property
    def now_ms(self) -> int: ...


class SystemClock:
    """UTC epoch clock for production deadlines; node synchronization is an Infra gate."""

    @property
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000
