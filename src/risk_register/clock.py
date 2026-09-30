"""可控时间源。

定时复查依赖时钟。生产使用 :class:`SystemClock`，测试与演示使用
:class:`FixedClock`，保证复查到期判断完全可复现。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def today(self) -> date: ...


class SystemClock:
    """系统当前日期（UTC，服务端统一以日期为复查粒度）。"""

    def today(self) -> date:
        return datetime.now(timezone.utc).date()


class FixedClock:
    """测试用固定时钟，可手动推进。"""

    def __init__(self, current: date | str) -> None:
        self._current = date.fromisoformat(current) if isinstance(current, str) else current

    def today(self) -> date:
        return self._current

    def advance(self, days: int = 0, *, to: date | str | None = None) -> date:
        if to is not None:
            target = date.fromisoformat(to) if isinstance(to, str) else to
            if target < self._current:
                raise ValueError("固定时钟不能回拨")
            self._current = target
        else:
            self._current = self._current.fromordinal(self._current.toordinal() + days)
        return self._current
