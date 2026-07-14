# -*- coding: utf-8 -*-
"""补全每日 JSON：涨停原因、封单占比（同花顺历史），并重写 Markdown。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lianban_lib import (
    CalibratedModel,
    factors_to_display,
    format_seal_ratio_pct,
    parse_seal_minutes,
    predict_prob,
    row_factors,
)
from lianban_paths import DAILY_DIR, daily_json, daily_md, ensure_daily_dir
from lianban_similar import attach_similar_fields
from lianban_today import _parse_amount_for_sort, build_markdown
from lianban_zt_reason import fetch_zt_reason_map


def _load_stocks(payload: dict) -> list[dict]:
    stocks = payload.get("stocks") or []
    if not stocks and payload.get("by_boards"):
        for items in payload["by_boards"].values():
            stocks.extend(items)
    return stocks


def _save_stocks(payload: dict, stocks: list[dict]) -> None:
    payload["stocks"] = stocks
    by_boards: dict[str, list[dict]] = {}
    for s in stocks:
        n = str(int(s.get("连板数", 0)))
        by_boards.setdefault(n, []).append(s)
    for n in by_boards:
        by_boards[n].sort(
            key=lambda x: float(x.get("晋级概率") or -1),
            reverse=True,
        )
    payload["by_boards"] = by_boards


def _ths_amounts(info: dict) -> tuple[float | None, float | None]:
    seal_f, turn_f = None, None
    seal = info.get("封板资金")
    turn = info.get("成交额")
    if seal not in (None, ""):
        try:
            seal_f = float(seal)
        except (TypeError, ValueError):
            pass
    if turn not in (None, ""):
        try:
            turn_f = float(turn)
        except (TypeError, ValueError):
            pass
    return seal_f, turn_f


def patch_stock_fields(
    payload: dict,
    reason_map: dict,
) -> tuple[int, int]:
    """补涨停原因、封单占比、大单封板；必要时按校准重算晋级概率。"""
    stocks = _load_stocks(payload)
    model = None
    cal = payload.get("calibration")
    if cal:
        model = CalibratedModel.from_dict(cal)

    reason_n, seal_n = 0, 0
    for s in stocks:
        code = str(s.get("代码", "")).zfill(6)
        info = reason_map.get(code) or {}

        reason = info.get("涨停原因") or "-"
        if reason != "-" and s.get("涨停原因") != reason:
            s["涨停原因"] = reason
            reason_n += 1

        need_seal = not s.get("封单占比") or s.get("封单占比") == "-"
        seal_amt, turnover = _ths_amounts(info)
        if seal_amt is None:
            parsed = _parse_amount_for_sort(s.get("封板资金"))
            seal_amt = parsed if parsed > 0 else None

        if need_seal and seal_amt is not None and turnover and turnover > 0:
            s["封单占比"] = format_seal_ratio_pct(seal_amt, turnover)
            factors = row_factors(
                pd.Series(
                    {
                        "_zhaban": s.get("炸板次数"),
                        "_seal_minutes": parse_seal_minutes(s.get("首次封板时间")),
                        "_last_seal_minutes": parse_seal_minutes(
                            s.get("最后封板时间")
                        ),
                        "_seal_amount": seal_amt,
                        "_turnover": turnover,
                    }
                )
            )
            disp = factors_to_display(factors)
            s["大单封板"] = disp["大单封板"]
            if s.get("最后封板时间"):
                s["弱转强"] = disp["弱转强"]
            if model:
                boards = int(s.get("连板数", 0))
                prob = predict_prob(boards, model, factors)
                if prob is not None:
                    s["晋级概率"] = prob
                    s["晋级概率_pct"] = round(prob * 100, 2)
            seal_n += 1

    _save_stocks(payload, stocks)
    return reason_n, seal_n


def rebuild_md(payload: dict, date: str) -> None:
    stocks = _load_stocks(payload)
    if not stocks:
        return
    stocks = attach_similar_fields(stocks)
    _save_stocks(payload, stocks)
    table = pd.DataFrame(stocks)
    if "晋级概率_pct" in table.columns and "晋级概率" not in table.columns:
        table["晋级概率"] = table["晋级概率_pct"].map(
            lambda x: float(x) / 100.0 if x is not None else None
        )
    rates = payload.get("rates_by_board") or {}
    rates_by_n = {int(k): v for k, v in rates.items()}
    md = build_markdown(
        date,
        table,
        rates_by_n,
        pairs_used=[],
        min_boards=int(payload.get("min_boards", 2)),
        hist_pairs=int(payload.get("hist_pairs", 20)),
        stock_records=stocks,
    )
    daily_md(date).write_text(md, encoding="utf-8")


def backfill_all(dates: list[str] | None = None) -> tuple[int, int]:
    ensure_daily_dir()
    paths = sorted(DAILY_DIR.glob("*.json"))
    if dates:
        want = set(dates)
        paths = [p for p in paths if p.stem in want]

    ok, skip = 0, 0
    for p in paths:
        date = p.stem
        payload = json.loads(p.read_text(encoding="utf-8"))
        reason_map = fetch_zt_reason_map(date)
        if not reason_map:
            skip += 1
            print(f"[跳过] {date} 无涨停原因数据源")
            continue
        reason_n, seal_n = patch_stock_fields(payload, reason_map)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        rebuild_md(payload, date)
        print(
            f"[完成] {date} 涨停原因 {reason_n} 只，封单占比 {seal_n} 只"
        )
        ok += 1
    return ok, skip


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="补全每日连板 JSON 涨停原因并重写 MD")
    ap.add_argument("--date", action="append", help="指定 YYYYMMDD，可多次")
    args = ap.parse_args()
    ok, skip = backfill_all(args.date)
    print(f"\n完成: 成功 {ok} 日，跳过 {skip} 日")


if __name__ == "__main__":
    main()
