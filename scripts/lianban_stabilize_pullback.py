# -*- coding: utf-8 -*-
"""
连板回踩企稳扫描

数据源：
- 连板历史：docs/03-智能策略/连板数据/每日/*.json
- 日 K：东方财富 push2his

产出：
- docs/03-智能策略/连板数据/连板回踩企稳扫描.md
- docs/03-智能策略/连板数据/连板回踩企稳扫描.json

筛选五步（默认参数）：
1. 三板以上 — 近 22 日连板 JSON 峰值 ≥ 3
2. 连续绿K — 近 2 日全部为阴线
3. 企稳信号 — 绿K 段最后一根满足 ≥1 项企稳特征
4. 排除涨停 — 最新日涨跌幅 < 9.5%
5. 股价 ≥20 — 最新收盘价 ≥ 20 元
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lianban_jinji_weekly import is_st_or_delist
from lianban_paths import DAILY_DIR, STABILIZE_MD, ensure_doc_dir, list_daily_dates
from lianban_similar import assign_theme

OUT_MD = STABILIZE_MD
OUT_JSON = OUT_MD.with_suffix(".json")

EM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ── 默认扫描参数 ──────────────────────────────────────────────────────────────

DEFAULT_TRADE_DAYS = 22
DEFAULT_GREEN_DAYS = 2
DEFAULT_MIN_BOARDS = 3
DEFAULT_MIN_SIGNALS = 1
DEFAULT_MIN_PRICE = 20.0
DEFAULT_SLEEP_MS = 80
LIMIT_UP_PCT = 9.5

# ── 文档用：筛选步骤说明 ──────────────────────────────────────────────────────

FILTER_LOGIC: list[tuple[str, str, str]] = [
    (
        "1. 三板以上",
        "近 {trade_days} 日连板 JSON 中，峰值连板数 ≥ {min_boards}",
        "确保曾是高标/题材龙，而非普通首板或二板跟风。",
    ),
    (
        "2. 连续绿K",
        "最近 {green_days} 个交易日 **全部为阴线**（收<开，或涨跌幅<0）",
        "处于连板断板后的调整段，尚未重新涨停。",
    ),
    (
        "3. 企稳信号",
        "绿K段最后一根 K 线，满足 ≥{min_signals} 项企稳特征（见下表）",
        "下跌动能减弱，可能出现「龙回头/回踩再启动」的前奏。",
    ),
    (
        "4. 排除涨停",
        "最新一日涨跌幅 < 9.5%",
        "仍在涨停的票不算「回踩」，排除加速段。",
    ),
    (
        "5. 股价 ≥20",
        "最新收盘价 ≥ {min_price} 元",
        "过滤低价股，聚焦中高价高标回踩（默认 20 元）。",
    ),
]

SIGNAL_LOGIC: dict[str, tuple[str, str]] = {
    "跌幅收窄": (
        "最新绿K跌幅，比前一绿K绝对值小 ≥0.3pp",
        "杀跌力度减弱，恐慌盘减少。",
    ),
    "缩量": (
        "最新绿K成交量 < 前一绿K × 90%",
        "抛压减轻，筹码趋于稳定。",
    ),
    "低点抬高": (
        "最新绿K最低价 ≥ 前一绿K最低价",
        "不再创新低，有横向支撑。",
    ),
    "小阴线": (
        "最新绿K涨跌幅绝对值 ≤ 2.5%",
        "小幅整理，多空暂时平衡。",
    ),
    "下影偏长/收偏上": (
        "收盘位于当日振幅的上 45% 区域",
        "盘中下探后有承接，收在相对高位。",
    ),
    "未破调整前低": (
        "最新绿K最低价 ≥ 绿K段前一日最低价",
        "调整未加深，结构未被破坏。",
    ),
}

PATTERN_HINTS: list[tuple[set[str], str]] = [
    ({"跌幅收窄", "缩量"}, "杀跌衰竭型：急跌后量缩价稳，常见于龙头第一次回踩。"),
    ({"低点抬高", "小阴线"}, "横向企稳型：不再创新低，小幅阴跌磨底。"),
    ({"下影偏长/收偏上"}, "下探回升型：盘中有承接，尾段拉回。"),
    ({"缩量", "未破调整前低"}, "缩量守前低型：调整幅度可控，等待板块再启动。"),
]

NEAR_MISS_CODES = ("600500", "002584")  # 中化国际、西陇科学


@dataclass(frozen=True)
class ScanParams:
    trade_days: int = DEFAULT_TRADE_DAYS
    green_days: int = DEFAULT_GREEN_DAYS
    min_boards: int = DEFAULT_MIN_BOARDS
    min_signals: int = DEFAULT_MIN_SIGNALS
    min_price: float = DEFAULT_MIN_PRICE
    sleep_ms: int = DEFAULT_SLEEP_MS

    def as_dict(self) -> dict:
        return {
            "trade_days": self.trade_days,
            "green_days": self.green_days,
            "min_boards": self.min_boards,
            "min_signals": self.min_signals,
            "min_price": self.min_price,
        }


@dataclass
class BoardRecord:
    date: str
    boards: int
    reason: str = ""
    zhaban: int = 0
    change_pct: float | None = None
    turnover: float | None = None


@dataclass
class LianbanHist:
    code: str
    name: str
    peak_boards: int = 0
    last_lb_date: str = ""
    last_reason: str = ""
    industry: str = ""
    dates: list[str] = field(default_factory=list)
    records: list[BoardRecord] = field(default_factory=list)


class FilterResult(NamedTuple):
    passed: bool
    signals: list[str]
    reasons: list[str]


# ── 数据获取 ──────────────────────────────────────────────────────────────────


def _parse_pct(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).replace("%", "").strip()
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_secid(code: str) -> str:
    c = str(code).zfill(6)
    return f"1.{c}" if c.startswith("6") else f"0.{c}"


def fetch_kline_em(code: str, bars: int = 20) -> pd.DataFrame | None:
    """日 K，列：日期,开,收,高,低,量,额,振幅,涨跌幅,涨跌额,换手"""
    secid = to_secid(code)
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "end": "20500101",
        "lmt": str(bars),
    }
    try:
        resp = requests.get(url, params=params, headers=EM_HEADERS, timeout=20)
        resp.raise_for_status()
        klines = (resp.json().get("data") or {}).get("klines") or []
    except Exception:
        return None
    if not klines:
        return None
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) < 11:
            continue
        rows.append(
            {
                "日期": p[0].replace("-", ""),
                "开": float(p[1]),
                "收": float(p[2]),
                "高": float(p[3]),
                "低": float(p[4]),
                "量": float(p[5]),
                "额": float(p[6]),
                "振幅": float(p[7]),
                "涨跌幅": float(p[8]),
                "涨跌额": float(p[9]),
                "换手": float(p[10]),
            }
        )
    if not rows:
        return None
    return pd.DataFrame(rows).sort_values("日期").reset_index(drop=True)


def load_lianban_universe(
    trade_days: int = DEFAULT_TRADE_DAYS,
    min_boards: int = DEFAULT_MIN_BOARDS,
) -> dict[str, LianbanHist]:
    dates = sorted(list_daily_dates())
    if not dates:
        dates = sorted(
            p.stem
            for p in DAILY_DIR.glob("*.json")
            if len(p.stem) == 8 and p.stem.isdigit()
        )
    window = dates[-trade_days:] if len(dates) > trade_days else dates
    out: dict[str, LianbanHist] = {}
    for d in window:
        p = DAILY_DIR / f"{d}.json"
        if not p.is_file():
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        stocks = payload.get("stocks") or []
        if not stocks and payload.get("by_boards"):
            for items in payload["by_boards"].values():
                stocks.extend(items)
        for s in stocks:
            name = str(s.get("名称", ""))
            if is_st_or_delist(name):
                continue
            boards = int(s.get("连板数", 0) or 0)
            if boards < min_boards:
                continue
            code = str(s.get("代码", "")).zfill(6)
            h = out.get(code)
            if not h:
                h = LianbanHist(code=code, name=name)
                out[code] = h
            h.name = name
            h.peak_boards = max(h.peak_boards, boards)
            h.last_lb_date = max(h.last_lb_date, d) if h.last_lb_date else d
            h.last_reason = s.get("涨停原因") or h.last_reason
            h.industry = s.get("所属行业") or h.industry
            if d not in h.dates:
                h.dates.append(d)
            rec = BoardRecord(
                date=d,
                boards=boards,
                reason=str(s.get("涨停原因") or "").strip(),
                zhaban=int(s.get("炸板次数", 0) or 0),
                change_pct=_parse_pct(s.get("涨跌幅")),
                turnover=_parse_pct(s.get("换手率")),
            )
            existing = next((r for r in h.records if r.date == d), None)
            if existing:
                if boards >= existing.boards:
                    h.records.remove(existing)
                    h.records.append(rec)
            else:
                h.records.append(rec)
    for h in out.values():
        h.records.sort(key=lambda r: r.date)
    return out


# ── K 线指标 ──────────────────────────────────────────────────────────────────


def is_green_bar(row: pd.Series) -> bool:
    """绿K = 阴线：收 < 开，或涨跌幅 < 0"""
    if row["收"] < row["开"]:
        return True
    return row["涨跌幅"] < -0.01


def stabilization_signals(df: pd.DataFrame, green_n: int) -> list[str]:
    if len(df) < green_n + 1:
        return []
    tail = df.iloc[-green_n:]
    if not all(is_green_bar(tail.iloc[i]) for i in range(green_n)):
        return []
    last = tail.iloc[-1]
    prev = tail.iloc[-2] if green_n >= 2 else None
    sig: list[str] = []

    if prev is not None:
        if abs(last["涨跌幅"]) < abs(prev["涨跌幅"]) - 0.3:
            sig.append("跌幅收窄")
        if last["量"] < prev["量"] * 0.9:
            sig.append("缩量")
        if last["低"] >= prev["低"] - 0.01:
            sig.append("低点抬高")

    if abs(last["涨跌幅"]) <= 2.5:
        sig.append("小阴线")

    span = last["高"] - last["低"]
    if span > 1e-6 and (last["收"] - last["低"]) / span >= 0.55:
        sig.append("下影偏长/收偏上")

    before = df.iloc[-(green_n + 1)]
    if last["低"] >= before["低"]:
        sig.append("未破调整前低")

    return sig


# ── 五步筛选 ──────────────────────────────────────────────────────────────────


def _step1_min_boards(hist: LianbanHist, params: ScanParams) -> str | None:
    if hist.peak_boards < params.min_boards:
        return f"峰值仅 {hist.peak_boards} 板，低于 {params.min_boards} 板门槛。"
    return None


def _step2_green_k(df: pd.DataFrame, params: ScanParams) -> str | None:
    tail = df.iloc[-params.green_days :]
    greens = [is_green_bar(tail.iloc[i]) for i in range(params.green_days)]
    if all(greens):
        return None
    bad = []
    for i, ok in enumerate(greens):
        r = tail.iloc[i]
        tag = "阴" if ok else "阳/非绿K"
        bad.append(f"{r['日期'][4:6]}.{r['日期'][6:8]}:{r['涨跌幅']:+.1f}%({tag})")
    return (
        f"近 {params.green_days} 日未连续绿K：{' → '.join(bad)}。"
        " （例如最新一日收阳反弹，则不算仍在绿K调整中）"
    )


def _step3_stabilize(df: pd.DataFrame, params: ScanParams) -> tuple[str | None, list[str]]:
    sig = stabilization_signals(df, params.green_days)
    if len(sig) >= params.min_signals:
        return None, sig
    return (
        f"企稳信号不足（仅 {len(sig)} 项：{('、'.join(sig) if sig else '无')}），"
        f"需要 ≥{params.min_signals} 项。",
        sig,
    )


def _step4_not_limit_up(latest: pd.Series) -> str | None:
    if latest["涨跌幅"] >= LIMIT_UP_PCT:
        return f"最新日仍涨停（{latest['涨跌幅']:+.2f}%），属于加速段非回踩。"
    return None


def _step5_min_price(latest: pd.Series, params: ScanParams) -> str | None:
    price = float(latest["收"])
    if price < params.min_price:
        return f"最新价 {price:.2f} 元，低于 {params.min_price} 元门槛。"
    return None


def apply_filters(
    hist: LianbanHist,
    df: pd.DataFrame | None,
    params: ScanParams,
) -> FilterResult:
    """按五步顺序检查，返回是否命中及未命中原因。"""
    reasons: list[str] = []

    miss = _step1_min_boards(hist, params)
    if miss:
        return FilterResult(False, [], [miss])

    if df is None or len(df) < params.green_days + 1:
        return FilterResult(False, [], ["K 线数据不足，无法判断绿K与企稳。"])

    latest = df.iloc[-1]

    for step in (
        lambda: _step5_min_price(latest, params),
        lambda: _step4_not_limit_up(latest),
        lambda: _step2_green_k(df, params),
    ):
        miss = step()
        if miss:
            reasons.append(miss)

    miss, signals = _step3_stabilize(df, params)
    if miss:
        reasons.append(miss)

    if reasons:
        return FilterResult(False, signals, reasons)
    return FilterResult(True, signals, [])


# ── 标注与备注 ────────────────────────────────────────────────────────────────


def signal_logic_notes(signals: list[str]) -> list[dict[str, str]]:
    return [
        {
            "信号": s,
            "计算逻辑": SIGNAL_LOGIC.get(s, ("—", "—"))[0],
            "含义": SIGNAL_LOGIC.get(s, ("—", "—"))[1],
        }
        for s in signals
    ]


def classify_pattern(signals: list[str]) -> str:
    sig_set = set(signals)
    for need, label in PATTERN_HINTS:
        if need.issubset(sig_set):
            return label
    if len(signals) >= 3:
        return "综合企稳型：多项特征同时出现，回踩质量相对较好。"
    if signals:
        return "单项企稳型：仅部分特征成立，需结合板块强度验证。"
    return "—"


def build_filter_notes(
    hist: LianbanHist,
    params: ScanParams,
    latest_price: float | None = None,
    passed: bool = False,
) -> list[str]:
    notes = [
        f"【1 三板以上】峰值 {hist.peak_boards} 板（最近连板日 {hist.last_lb_date}）"
        + (" ✓" if hist.peak_boards >= params.min_boards else " ✗"),
        f"【2 连续绿K】要求近 {params.green_days} 日全阴",
        f"【3 企稳信号】要求 ≥{params.min_signals} 项",
        f"【4 排除涨停】最新日涨幅 < {LIMIT_UP_PCT}%",
        f"【5 股价门槛】最新价 ≥ {params.min_price} 元"
        + (
            f"（当前 {latest_price:.2f} 元 ✓）"
            if latest_price is not None and latest_price >= params.min_price
            else (
                f"（当前 {latest_price:.2f} 元 ✗）"
                if latest_price is not None
                else ""
            )
        ),
    ]
    if passed:
        notes[0] += (
            f" — 近 {params.trade_days} 日内曾达 {hist.peak_boards} 连板，符合高标回踩池。"
        )
    return notes


def explain_miss_reasons(
    hist: LianbanHist,
    df: pd.DataFrame | None,
    params: ScanParams,
) -> list[str]:
    result = apply_filters(hist, df, params)
    if result.passed:
        return ["已满足条件（应出现在命中列表）。"]
    return result.reasons


# ── 断板识别 ──────────────────────────────────────────────────────────────────


def find_peak_record(hist: LianbanHist) -> BoardRecord | None:
    candidates = [r for r in hist.records if r.boards == hist.peak_boards]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.date)


def find_break_row(
    df: pd.DataFrame, peak_date: str
) -> tuple[pd.Series | None, pd.Series | None, pd.Series | None]:
    """峰值断板日 K 线、峰值日 K 线、断板前一日 K 线。"""
    peak_row = prev_row = None
    for i in range(len(df)):
        row = df.iloc[i]
        if row["日期"] == peak_date:
            peak_row = row
            if i > 0:
                prev_row = df.iloc[i - 1]
            break
    for i in range(len(df)):
        row = df.iloc[i]
        if row["日期"] <= peak_date:
            continue
        if row["涨跌幅"] < LIMIT_UP_PCT:
            before = df.iloc[i - 1] if i > 0 else None
            return row, peak_row, before
    return None, peak_row, prev_row


def describe_break_reason(
    break_row: pd.Series,
    peak_rec: BoardRecord | None,
    prev_row: pd.Series | None,
) -> str:
    date_fmt = f"{break_row['日期'][4:6]}.{break_row['日期'][6:8]}"
    pct = float(break_row["涨跌幅"])
    hs = float(break_row["换手"])
    tags: list[str] = []

    if peak_rec and peak_rec.zhaban >= 3:
        tags.append(f"峰值日炸板{peak_rec.zhaban}次")

    if prev_row is not None and float(prev_row["收"]) > 0:
        open_pct = (float(break_row["开"]) / float(prev_row["收"]) - 1) * 100
        if open_pct >= 2 and break_row["收"] < break_row["开"]:
            tags.append(f"高开低走(开{open_pct:+.1f}%)")
        elif open_pct >= 5:
            tags.append(f"高开后回落(开{open_pct:+.1f}%)")

    if pct <= -7:
        tags.append(f"大阴断板{pct:+.1f}%")
    elif pct < 0:
        tags.append(f"断板阴线{pct:+.1f}%")
    elif pct < 5:
        tags.append(f"断板震荡{pct:+.1f}%")
    else:
        tags.append(f"未封板{pct:+.1f}%")

    if hs >= 35:
        tags.append(f"天量换手{hs:.1f}%")
    elif hs >= 20:
        tags.append(f"放量换手{hs:.1f}%")
    elif hs >= 8:
        tags.append(f"换手{hs:.1f}%")

    morph = "，".join(tags)
    theme = peak_rec.reason if peak_rec and peak_rec.reason and peak_rec.reason != "-" else ""
    if theme:
        return f"{theme} → {date_fmt} {morph}"
    return f"{date_fmt} {morph}"


def resolve_break_info(hist: LianbanHist, df: pd.DataFrame | None) -> dict[str, str]:
    peak_rec = find_peak_record(hist)
    peak_date = peak_rec.date if peak_rec else hist.last_lb_date
    if not peak_date or df is None or df.empty:
        return {"断板日": "-", "断板原因": "-"}

    break_row, _peak_row, prev_row = find_break_row(df, peak_date)
    if break_row is None:
        return {"断板日": "-", "断板原因": "-"}

    return {
        "断板日": str(break_row["日期"]),
        "断板原因": describe_break_reason(break_row, peak_rec, prev_row),
    }


# ── 单股分析 & 扫描 ──────────────────────────────────────────────────────────


def analyze_stock(hist: LianbanHist, params: ScanParams) -> dict | None:
    df = fetch_kline_em(hist.code, bars=25)
    result = apply_filters(hist, df, params)
    if not result.passed or df is None:
        return None

    latest = df.iloc[-1]
    tail = df.iloc[-params.green_days :]
    drop_sum = tail["涨跌幅"].sum()
    vol_ratio = (
        round(tail.iloc[-1]["量"] / tail.iloc[-2]["量"], 2)
        if params.green_days >= 2 and tail.iloc[-2]["量"] > 0
        else None
    )
    k_desc = " → ".join(
        f"{r['日期'][4:6]}.{r['日期'][6:8]}:{r['涨跌幅']:+.1f}%"
        for _, r in tail.iterrows()
    )
    sig = result.signals
    pattern = classify_pattern(sig)
    filter_notes = build_filter_notes(
        hist,
        params,
        latest_price=float(latest["收"]),
        passed=True,
    )
    logic_summary = (
        f"{pattern} "
        f"近{params.green_days}日绿K累计{drop_sum:+.2f}%；"
        f"信号：{'、'.join(sig)}。"
    )
    theme = assign_theme(hist.last_reason, hist.industry)
    break_info = resolve_break_info(hist, df)

    return {
        "代码": hist.code,
        "名称": hist.name,
        "峰值连板": hist.peak_boards,
        "最近连板日": hist.last_lb_date,
        "断板日": break_info["断板日"],
        "断板原因": break_info["断板原因"],
        "涨停原因": hist.last_reason,
        "所属行业": hist.industry or "-",
        "题材板块": theme,
        "K线日期": str(latest["日期"]),
        "最新价": latest["收"],
        "最新涨跌": latest["涨跌幅"],
        "绿K天数": params.green_days,
        "近段绿K": k_desc,
        "累计跌幅": round(drop_sum, 2),
        "企稳信号": sig,
        "信号逻辑": signal_logic_notes(sig),
        "形态备注": pattern,
        "筛选逻辑": filter_notes,
        "逻辑备注": logic_summary,
        "信号数": len(sig),
        "量比昨": vol_ratio,
        "换手": latest["换手"],
    }


def run_scan(params: ScanParams) -> tuple[list[dict], dict[str, LianbanHist]]:
    universe = load_lianban_universe(params.trade_days, params.min_boards)
    hits: list[dict] = []

    for i, (_code, hist) in enumerate(sorted(universe.items())):
        if params.sleep_ms and i:
            time.sleep(params.sleep_ms / 1000.0)
        row = analyze_stock(hist, params)
        if row:
            hits.append(row)

    hits.sort(key=lambda x: (-x["信号数"], -x["峰值连板"], x["累计跌幅"]))
    return hits, universe


def build_near_miss(
    universe: dict[str, LianbanHist],
    codes: tuple[str, ...],
    params: ScanParams,
) -> list[dict]:
    wide = load_lianban_universe(params.trade_days, min_boards=2)
    out = []
    for code in codes:
        c = code.zfill(6)
        hist = universe.get(c) or wide.get(c)
        if not hist:
            continue
        df = fetch_kline_em(hist.code, bars=12)
        reasons = explain_miss_reasons(hist, df, params)
        if hist.peak_boards < params.min_boards:
            reasons.insert(
                0,
                f"峰值仅 {hist.peak_boards} 板，低于当前三板门槛（仍列出供对照）。",
            )
        out.append(
            {
                "代码": hist.code,
                "名称": hist.name,
                "峰值连板": hist.peak_boards,
                "最近连板日": hist.last_lb_date,
                "涨停原因": hist.last_reason,
                "所属行业": hist.industry or "-",
                "题材板块": assign_theme(hist.last_reason, hist.industry),
                "未命中原因": reasons,
            }
        )
    return out


# ── Markdown 报告 ─────────────────────────────────────────────────────────────


def build_markdown(
    results: list[dict],
    params: ScanParams,
    scan_date: str,
    near_miss: list[dict] | None = None,
) -> str:
    lines = [
        "# 连板回踩企稳扫描",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"> 扫描日 K 截止：**{scan_date}**  ",
        f"> 连板历史窗口：近 **{params.trade_days}** 个交易日（本地 `每日/` JSON）  ",
        (
            f"> 条件：≥{params.min_boards} 连板 + 近 {params.green_days} 日绿K "
            f"+ 企稳≥{params.min_signals} + **股价≥{params.min_price:g}元**"
        ),
        "",
        "## 一、筛选逻辑（五步）",
        "",
        "| 步骤 | 规则 | 逻辑说明 |",
        "|------|------|----------|",
    ]
    for step, rule_tpl, note in FILTER_LOGIC:
        rule = rule_tpl.format(
            trade_days=params.trade_days,
            min_boards=params.min_boards,
            green_days=params.green_days,
            min_signals=params.min_signals,
            min_price=params.min_price,
        )
        lines.append(f"| {step} | {rule} | {note} |")

    lines.extend(
        [
            "",
            "## 二、企稳信号含义",
            "",
            "| 信号 | 计算逻辑 | 盘面含义 |",
            "|------|----------|----------|",
        ]
    )
    for name, (calc, meaning) in SIGNAL_LOGIC.items():
        lines.append(f"| {name} | {calc} | {meaning} |")

    lines.extend(
        [
            "",
            "## 三、形态归类（自动备注）",
            "",
            "| 形态 | 触发条件 | 备注 |",
            "|------|----------|------|",
        ]
    )
    for need, label in PATTERN_HINTS:
        lines.append(
            f"| {label.split('：')[0]} | 同时出现：{'、'.join(sorted(need))} | "
            f"{label.split('：', 1)[-1]} |"
        )

    lines.extend(["", f"## 四、命中列表（共 {len(results)} 只）", ""])
    if not results:
        lines.append("_当前无符合条件的标的。_")
    else:
        lines.extend(
            [
                "| 代码 | 名称 | 最新价 | 题材板块 | 峰值 | 断板原因 | 形态备注 | 近段绿K | 企稳信号 |",
                "|------|------|--------|----------|------|----------|----------|---------|----------|",
            ]
        )
        for r in results:
            sig = "、".join(r["企稳信号"])
            pattern_short = (r.get("形态备注") or "—").split("：")[0]
            break_reason = str(r.get("断板原因", "-")).replace("|", "\\|")
            lines.append(
                f"| {r['代码']} | {r['名称']} | {r['最新价']} | {r.get('题材板块', '-')} | "
                f"{r['峰值连板']} | {break_reason} | {pattern_short} | "
                f"{r['近段绿K']} | {sig} |"
            )

        by_theme: dict[str, list[dict]] = {}
        for r in results:
            t = r.get("题材板块") or "其他"
            by_theme.setdefault(t, []).append(r)
        lines.extend(["", "## 五、按题材板块归类", ""])
        for theme in sorted(by_theme, key=lambda t: (-len(by_theme[t]), t)):
            items = by_theme[theme]
            names = "、".join(f"{x['名称']}({x['峰值连板']})" for x in items)
            lines.append(f"- **{theme}**（{len(items)} 只）：{names}")

        lines.extend(["", "## 六、个股明细（含逻辑备注）", ""])
        for r in results:
            lines.extend(
                [
                    f"### {r['名称']}（{r['代码']}）",
                    "",
                    f"- **形态备注**：{r.get('形态备注', '—')}",
                    f"- **题材板块**：{r.get('题材板块', '—')}（同花顺涨停原因 + 行业归类）",
                    f"- **所属行业**：{r.get('所属行业', '—')}",
                    f"- **逻辑摘要**：{r.get('逻辑备注', '—')}",
                f"- **峰值连板**：{r['峰值连板']} 板（最近出现在 {r['最近连板日']}）",
                f"- **断板日**：{r.get('断板日', '-')}",
                f"- **断板原因**：{r.get('断板原因', '-')}",
                f"- **涨停原因**：{r['涨停原因']}",
                    f"- **近段绿K**：{r['近段绿K']}（累计 {r['累计跌幅']}%）",
                    f"- **最新**：收 {r['最新价']}，涨跌 {r['最新涨跌']:+.2f}%，换手 {r['换手']:.2f}%",
                    "",
                    "**筛选逻辑**",
                    "",
                ]
            )
            for note in r.get("筛选逻辑") or []:
                lines.append(f"- {note}")
            lines.extend(["", "**企稳信号逻辑**", ""])
            for sn in r.get("信号逻辑") or []:
                lines.append(f"- **{sn['信号']}**：{sn['计算逻辑']} → {sn['含义']}")
            lines.append("")

    if near_miss:
        lines.extend(["", "## 七、未命中示例（对照理解）", ""])
        for nm in near_miss:
            lines.extend(
                [
                    f"### {nm['名称']}（{nm['代码']}）— 未入选",
                    "",
                    f"- **题材板块**：{nm.get('题材板块', '—')}",
                    f"- **所属行业**：{nm.get('所属行业', '—')}",
                    f"- **峰值连板**：{nm.get('峰值连板')} 板（{nm.get('最近连板日')}）",
                    f"- **涨停原因**：{nm.get('涨停原因', '—')}",
                    "",
                    "**未命中原因**",
                    "",
                ]
            )
            for reason in nm.get("未命中原因") or []:
                lines.append(f"- {reason}")
            lines.append("")

    lines.extend(
        [
            "## 维护",
            "",
            "```bash",
            "python scripts/lianban_stabilize_pullback.py",
            "python scripts/lianban.py stabilize",
            "python scripts/lianban.py stabilize --green-days 3 --min-price 20",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> ScanParams:
    ap = argparse.ArgumentParser(description="连板回踩绿K企稳扫描（五步筛选）")
    ap.add_argument("--trade-days", type=int, default=DEFAULT_TRADE_DAYS)
    ap.add_argument("--green-days", type=int, default=DEFAULT_GREEN_DAYS, choices=[2, 3])
    ap.add_argument("--min-boards", type=int, default=DEFAULT_MIN_BOARDS)
    ap.add_argument("--min-signals", type=int, default=DEFAULT_MIN_SIGNALS)
    ap.add_argument("--min-price", type=float, default=DEFAULT_MIN_PRICE)
    ap.add_argument("--sleep-ms", type=int, default=DEFAULT_SLEEP_MS)
    args = ap.parse_args(argv)
    return ScanParams(
        trade_days=args.trade_days,
        green_days=args.green_days,
        min_boards=args.min_boards,
        min_signals=args.min_signals,
        min_price=args.min_price,
        sleep_ms=args.sleep_ms,
    )


def main(argv: list[str] | None = None) -> int:
    params = parse_args(argv)
    ensure_doc_dir()

    hits, universe = run_scan(params)
    near_miss = build_near_miss(universe, NEAR_MISS_CODES, params)
    scan_date = max((h["K线日期"] for h in hits), default=datetime.now().strftime("%Y%m%d"))

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scan_date": scan_date,
        "params": params.as_dict(),
        "count": len(hits),
        "stocks": hits,
        "near_miss_examples": near_miss,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(
        build_markdown(hits, params, scan_date, near_miss),
        encoding="utf-8",
    )

    print(f"命中 {len(hits)} 只 → {OUT_MD}")
    for r in hits[:15]:
        print(
            f"  {r['代码']} {r['名称']} 峰值{r['峰值连板']}板 "
            f"{r['近段绿K']} | {'、'.join(r['企稳信号'])}"
        )
    if len(hits) > 15:
        print(f"  … 共 {len(hits)} 只，见 {OUT_MD.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
