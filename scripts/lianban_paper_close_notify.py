#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
连板模拟盘 · 交易日收盘后飞书推送入口（供 cron / GitHub Actions 调用）。

流程：
  1. 等到 15:00（收盘，可 --skip-wait 跳过）
  2. 拉取当日连板池（lianban_today）
  3. 执行当日模拟盘结算
  4. 推送收盘总结到飞书群

示例：
  python scripts/lianban_paper_close_notify.py
  python scripts/lianban_paper_close_notify.py --date 20260718
  python scripts/lianban_paper_close_notify.py --skip-wait --print-only
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lianban_paper import cmd_close_notify, load_config  # noqa: E402

HOLIDAY_CSV = ROOT / "qbot" / "config" / "national_holidays.csv"
TZ_SH = ZoneInfo("Asia/Shanghai")


def now_shanghai() -> datetime:
    return datetime.now(TZ_SH)


def is_weekday(date_str: str) -> bool:
    return datetime.strptime(date_str, "%Y%m%d").weekday() < 5


def is_statutory_holiday(date_str: str) -> bool:
    if not HOLIDAY_CSV.is_file():
        return False
    dt = datetime.strptime(date_str, "%Y%m%d")
    year = dt.year
    md = dt.strftime("%m-%d")
    with HOLIDAY_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if str(row.get("year", "")) != str(year):
                continue
            start = row.get("startDate", "")
            end = row.get("endDate", "")
            if start and end and start <= md <= end:
                return True
    return False


def is_trading_day(date_str: str) -> bool:
    if not is_weekday(date_str):
        return False
    if is_statutory_holiday(date_str):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="连板模拟盘收盘后飞书推送")
    parser.add_argument("--date", help="交易日 YYYYMMDD，默认今天（北京时间）")
    parser.add_argument("--force", action="store_true", help="非交易日也执行（调试用）")
    parser.add_argument("--skip-wait", action="store_true", help="不等待 15:00，立即分析")
    parser.add_argument("--print-only", action="store_true", help="仅打印，不发送")
    parser.add_argument("--dry-run", action="store_true", help="仅打印 lark-cli 命令")
    args = parser.parse_args(argv)

    trade_date = args.date or now_shanghai().strftime("%Y%m%d")
    if not args.force and not is_trading_day(trade_date):
        print(f"跳过：{trade_date} 非 A 股交易日")
        return 0

    cfg = load_config()
    if not cfg.get("feishu_notify_enabled", True):
        print("跳过：feishu_notify_enabled=false")
        return 0

    ns = argparse.Namespace(
        date=trade_date,
        print_only=args.print_only,
        dry_run=args.dry_run,
        skip_wait=args.skip_wait,
    )
    cmd_close_notify(ns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
