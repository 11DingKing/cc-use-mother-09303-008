"""时钟抽象。

所有时间戳与“是否到期”的判断都从时钟获取，生产环境使用系统时钟，
测试与批量复查使用固定/可调时钟，从而让定时复查结果可复现。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区信息的当前时间。"""


class SystemClock:
    """生产环境时钟，始终返回 UTC 当前时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """固定时钟，返回构造时给定的时间，适合确定性测试。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = self._ensure_aware(moment)

    @staticmethod
    def _ensure_aware(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment

    def now(self) -> datetime:
        return self._moment


class MutableClock:
    """可调时钟，可显式设置或推进时间，用于模拟复查周期跨越。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = FixedClock._ensure_aware(moment)

    def now(self) -> datetime:
        return self._moment

    def set(self, moment: datetime) -> None:
        self._moment = FixedClock._ensure_aware(moment)

    def advance(self, **kwargs) -> datetime:
        """按 ``datetime.timedelta`` 支持的关键字推进时间。"""
        from datetime import timedelta

        self._moment = self._moment + timedelta(**kwargs)
        return self._moment
