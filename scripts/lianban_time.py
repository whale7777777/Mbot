# -*- coding: utf-8 -*-
"""连板脚本统一使用北京时间（Asia/Shanghai）。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def today_beijing() -> date:
    return now_beijing().date()


def beijing_now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return now_beijing().strftime(fmt)


def beijing_today_str() -> str:
    return today_beijing().strftime("%Y%m%d")


def beijing_now_iso(timespec: str = "seconds") -> str:
    return now_beijing().isoformat(timespec=timespec)
