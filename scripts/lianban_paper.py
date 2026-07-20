# -*- coding: utf-8 -*-
"""
连板策略 · 模拟盘机器人

按连板晋级概率 + 封板质量 + 题材主线自动买卖，落盘操作记录。

产出（docs/03-智能策略/连板数据/模拟盘/）：
  - 持仓.json          当前资金与持仓
  - 成交记录.json      全量成交
  - 操作记录.md        汇总日志
  - 每日/YYYYMMDD.md   当日观察与决策

用法：
  python scripts/lianban_paper.py run              # 跑最近交易日
  python scripts/lianban_paper.py run --date 20260716
  python scripts/lianban_paper.py backfill --from 20260710 --to 20260716
  python scripts/lianban_paper.py status           # 查看持仓
  python scripts/lianban_paper.py reset            # 重置为初始资金
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lianban_paths import (
    PAPER_CONFIG,
    PAPER_DAILY_DIR,
    PAPER_LOG_MD,
    PAPER_STATE_JSON,
    PAPER_TRADES_JSON,
    daily_json,
    ensure_paper_dir,
    list_daily_dates,
    paper_daily_md,
)
from lianban_today import resolve_trade_date

DEFAULT_CONFIG: dict[str, Any] = {
    "initial_cash": 20000.0,
    "max_positions": 2,
    "single_position_pct": 0.4,
    "cold_pool_max_pct": 0.5,
    "warm_pool_max_pct": 0.8,
    "cold_pool_threshold": 6,
    "min_buy_score": 55,
    "min_promote_prob_pct": 28.0,
    "max_boards_buy": 4,
    "max_zhaban_buy": 2,
    "min_turnover_pct": 0.5,
    "hard_block_turnover_pct": 0.3,
    "min_fill_prob_to_buy": 0.22,
    "max_order_vs_amount_ratio": 0.03,
    "sh_min_amount_yi": 9000,
    "sh_warn_amount_yi": 10000,
    "below_sh_min_no_buy": True,
    "asphyxia_max_exposure_pct": 0.1,
    "asphyxia_single_position_pct": 0.1,
    "commission_rate": 0.0003,
    "stamp_tax_sell": 0.001,
    "min_commission": 5.0,
    "bot_name": "连板模拟盘机器人",
    "feishu_chat_id": "oc_b082d116980b38638fd17cf4807be8d0",
    "feishu_notify_enabled": True,
    "lark_cli_bin": "lark-cli",
    "auction_end_time": "09:25",
    "auction_retry_seconds": 30,
    "auction_max_retries": 6,
}


@dataclass
class FillResult:
    can_buy: bool
    fill_prob: float
    fill_ratio: float
    note: str


@dataclass
class Position:
    code: str
    name: str
    shares: int
    cost_price: float
    buy_date: str
    boards_at_buy: int
    buy_reason: str = ""
    theme: str = ""


@dataclass
class Portfolio:
    initial_cash: float
    cash: float
    positions: list[Position] = field(default_factory=list)
    last_trade_date: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def position_codes(self) -> set[str]:
        return {p.code for p in self.positions}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if PAPER_CONFIG.is_file():
        try:
            cfg.update(json.loads(PAPER_CONFIG.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config_template() -> None:
    ensure_paper_dir()
    if not PAPER_CONFIG.is_file():
        PAPER_CONFIG.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def parse_pct(s: str | float | int | None) -> float:
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"([\d.]+)", str(s))
    return float(m.group(1)) if m else 0.0


def parse_seal_time_seconds(t: str | None) -> int | None:
    """首次封板时间 HHMMSS → 当日秒数。"""
    if not t:
        return None
    digits = re.sub(r"\D", "", str(t))
    if len(digits) < 4:
        return None
    h = int(digits[0:2])
    m = int(digits[2:4])
    s = int(digits[4:6]) if len(digits) >= 6 else 0
    return h * 3600 + m * 60 + s


def deterministic_roll(trade_date: str, code: str) -> float:
    """可复现的排板摇点，用于模拟排队是否轮到。"""
    raw = f"{trade_date}:{code}".encode()
    digest = hashlib.md5(raw).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def trade_date_to_iso(trade_date: str) -> str:
    return f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"


def fetch_sh_amount_yi(trade_date: str) -> float | None:
    """上证指数当日成交额（亿元）。"""
    try:
        import time

        import akshare as ak
    except ImportError:
        return None

    iso = trade_date_to_iso(trade_date)
    for attempt in range(3):
        try:
            df = ak.stock_zh_index_daily_em(symbol="sh000001")
            if df is None or df.empty:
                continue
            df = df.copy()
            df["date"] = df["date"].astype(str).str[:10]
            row = df[df["date"] == iso]
            if row.empty:
                return None
            amount = float(row.iloc[-1]["amount"])
            return round(amount / 1e8, 0)
        except Exception:
            if attempt < 2:
                time.sleep(1.5)
            continue
    return None


def assess_market_volume(
    trade_date: str,
    cfg: dict[str, Any],
    *,
    phase: str = "full_day",
) -> dict[str, Any]:
    """全市场量能评估（以上证成交额为锚）。

    phase:
      - full_day: 收盘后模拟盘，使用全天阈值（如 9000/10000 亿）
      - auction: 竞价结束后盘前推送，全天阈值不适用，仅作参考展示
    """
    min_yi = float(cfg.get("sh_min_amount_yi", 9000))
    warn_yi = float(cfg.get("sh_warn_amount_yi", 10000))
    below_no_buy = bool(cfg.get("below_sh_min_no_buy", True))

    if phase == "auction":
        sh_yi = fetch_sh_amount_yi(trade_date)
        if sh_yi is None:
            note = (
                f"竞价阶段不适用全天量能阈值（{min_yi:.0f}/{warn_yi:.0f} 亿），"
                "收盘后再评估是否窒息"
            )
        else:
            note = (
                f"当前上证成交额约 {sh_yi:.0f} 亿（盘中累计，非全天），"
                f"全天阈值 {min_yi:.0f}/{warn_yi:.0f} 亿收盘后再评估"
            )
        return {
            "sh_amount_yi": sh_yi,
            "volume_state": "竞价暂不评估",
            "is_asphyxia": False,
            "block_new_buy": False,
            "max_exposure_pct": None,
            "single_position_pct": None,
            "note": note,
            "phase": "auction",
        }

    sh_yi = fetch_sh_amount_yi(trade_date)

    if sh_yi is None:
        return {
            "sh_amount_yi": None,
            "volume_state": "未知",
            "is_asphyxia": False,
            "block_new_buy": False,
            "max_exposure_pct": None,
            "single_position_pct": None,
            "note": "未获取到上证成交额，不触发量能风控",
        }

    if sh_yi < min_yi:
        return {
            "sh_amount_yi": sh_yi,
            "volume_state": "窒息/冰点",
            "is_asphyxia": True,
            "block_new_buy": below_no_buy,
            "max_exposure_pct": float(cfg.get("asphyxia_max_exposure_pct", 0.1)),
            "single_position_pct": float(cfg.get("asphyxia_single_position_pct", 0.1)),
            "note": f"上证成交额{sh_yi:.0f}亿<{min_yi:.0f}亿，量能窒息" + (
                "，禁止新开仓" if below_no_buy else "，仅轻仓"
            ),
        }

    if sh_yi < warn_yi:
        return {
            "sh_amount_yi": sh_yi,
            "volume_state": "缩量观望",
            "is_asphyxia": False,
            "block_new_buy": False,
            "max_exposure_pct": float(cfg.get("cold_pool_max_pct", 0.5)),
            "single_position_pct": float(cfg.get("single_position_pct", 0.4)) * 0.75,
            "note": f"上证成交额{sh_yi:.0f}亿<{warn_yi:.0f}亿，缩量观望",
        }

    return {
        "sh_amount_yi": sh_yi,
        "volume_state": "正常",
        "is_asphyxia": False,
        "block_new_buy": False,
        "max_exposure_pct": None,
        "single_position_pct": None,
        "note": f"上证成交额{sh_yi:.0f}亿",
    }


def simulate_limit_up_fill(
    stock: dict,
    trade_date: str,
    target_amount: float,
    cfg: dict[str, Any],
) -> FillResult:
    """
    模拟涨停封板时能否买进（排板成交）。

    因子：换手率、封单占比、炸板回封次数、早盘秒板、委托占成交额。
    最后以可复现摇点判定是否轮到成交。
    """
    turnover = parse_pct(stock.get("换手率"))
    seal_pct = parse_pct(stock.get("封单占比"))
    zhaban = int(stock.get("炸板次数", 0))
    day_amount = float(stock.get("成交额") or 0)
    first_seal = str(stock.get("首次封板时间", ""))
    hard_block = float(cfg.get("hard_block_turnover_pct", 0.3))
    min_prob = float(cfg.get("min_fill_prob_to_buy", 0.22))
    max_order_ratio = float(cfg.get("max_order_vs_amount_ratio", 0.03))
    notes: list[str] = []

    if turnover < hard_block:
        return FillResult(
            False,
            0.0,
            0.0,
            f"一字板换手{turnover:.2f}%<{hard_block}%，封死难成交",
        )

    # 日内实际换手越高，排板机会越多
    liquidity = min(0.88, 0.04 + turnover * 0.065)

    if seal_pct >= 500:
        seal_factor = 0.02
        notes.append(f"封单极强{seal_pct:.0f}%")
    elif seal_pct >= 100:
        seal_factor = 0.08
        notes.append(f"封单过厚{seal_pct:.0f}%")
    elif seal_pct >= 50:
        seal_factor = 0.22
        notes.append(f"封单偏厚{seal_pct:.0f}%")
    elif seal_pct >= 30:
        seal_factor = 0.48
    elif seal_pct >= 15:
        seal_factor = 0.68
    else:
        seal_factor = 0.92

    reopen_bonus = min(0.38, zhaban * 0.14)
    if zhaban > 0:
        notes.append(f"炸板回封{zhaban}次")

    early_penalty = 0.0
    seal_sec = parse_seal_time_seconds(first_seal)
    market_open = 9 * 3600 + 30 * 60
    if (
        seal_sec is not None
        and seal_sec <= market_open + 5 * 60
        and turnover < 2.0
        and zhaban == 0
    ):
        early_penalty = 0.28
        notes.append("早盘秒板未开板")

    fill_prob = min(0.98, max(0.0, liquidity * seal_factor + reopen_bonus - early_penalty))

    if day_amount > 0 and target_amount > 0:
        order_ratio = target_amount / day_amount
        if order_ratio > max_order_ratio:
            shrink = max(0.08, max_order_ratio / order_ratio)
            fill_prob *= shrink
            notes.append(f"委托占成交额{order_ratio * 100:.1f}%")

    code = str(stock["代码"]).zfill(6)
    roll = deterministic_roll(trade_date, code)

    if fill_prob < min_prob:
        detail = "；".join(notes) if notes else "流动性不足"
        return FillResult(
            False,
            fill_prob,
            0.0,
            f"成交概率{fill_prob * 100:.0f}%过低（{detail}）",
        )

    if roll > fill_prob:
        detail = "；".join(notes) if notes else "排板未轮到"
        return FillResult(
            False,
            fill_prob,
            0.0,
            f"排板未成交（概率{fill_prob * 100:.0f}%/摇点{roll * 100:.0f}%）| {detail}",
        )

    fill_ratio = 1.0 if fill_prob >= 0.82 else max(0.35, fill_prob)
    detail = "；".join(notes) if notes else "流动性尚可"
    if fill_ratio < 1.0:
        detail += f"；部分成交{fill_ratio * 100:.0f}%"
    return FillResult(
        True,
        fill_prob,
        fill_ratio,
        f"排板成交（概率{fill_prob * 100:.0f}%/摇点{roll * 100:.0f}%）| {detail}",
    )


def copy_portfolio(pf: Portfolio) -> Portfolio:
    return Portfolio(
        initial_cash=pf.initial_cash,
        cash=pf.cash,
        positions=[Position(**asdict(p)) for p in pf.positions],
        last_trade_date=pf.last_trade_date,
        created_at=pf.created_at,
        updated_at=pf.updated_at,
    )


def load_portfolio(cfg: dict[str, Any]) -> Portfolio:
    if PAPER_STATE_JSON.is_file():
        data = json.loads(PAPER_STATE_JSON.read_text(encoding="utf-8"))
        positions = [Position(**p) for p in data.get("positions", [])]
        return Portfolio(
            initial_cash=float(data.get("initial_cash", cfg["initial_cash"])),
            cash=float(data.get("cash", cfg["initial_cash"])),
            positions=positions,
            last_trade_date=str(data.get("last_trade_date", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return Portfolio(
        initial_cash=float(cfg["initial_cash"]),
        cash=float(cfg["initial_cash"]),
        created_at=now,
        updated_at=now,
    )


def save_portfolio(pf: Portfolio) -> None:
    ensure_paper_dir()
    pf.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = {
        "initial_cash": pf.initial_cash,
        "cash": pf.cash,
        "positions": [asdict(p) for p in pf.positions],
        "last_trade_date": pf.last_trade_date,
        "created_at": pf.created_at,
        "updated_at": pf.updated_at,
    }
    PAPER_STATE_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_trades() -> list[dict]:
    if PAPER_TRADES_JSON.is_file():
        return json.loads(PAPER_TRADES_JSON.read_text(encoding="utf-8"))
    return []


def save_trades(trades: list[dict]) -> None:
    ensure_paper_dir()
    PAPER_TRADES_JSON.write_text(
        json.dumps(trades, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def append_trade(trades: list[dict], record: dict) -> None:
    record["id"] = len(trades) + 1
    record["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trades.append(record)
    save_trades(trades)


def calc_commission(amount: float, cfg: dict[str, Any], is_sell: bool) -> float:
    comm = max(amount * cfg["commission_rate"], cfg["min_commission"])
    if is_sell:
        comm += amount * cfg["stamp_tax_sell"]
    return comm


def load_daily_data(date: str) -> dict | None:
    p = daily_json(date)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def stocks_to_df(stocks: list[dict]) -> Any:
    import pandas as pd

    if not stocks:
        return pd.DataFrame()
    rows = []
    for s in stocks:
        rows.append(
            {
                "code": str(s["代码"]).zfill(6),
                "name": s["名称"],
                "boards": int(s["连板数"]),
                "price": float(s["最新价"]),
            }
        )
    return pd.DataFrame(rows)


def cluster_sizes(stocks: list[dict]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for s in stocks:
        theme = s.get("题材簇") or "其他"
        sizes[theme] = sizes.get(theme, 0) + 1
    return sizes


def market_emotion_label(pool_size: int, cfg: dict[str, Any]) -> str:
    if pool_size <= cfg["cold_pool_threshold"]:
        return "冰点/谨慎"
    if pool_size <= 10:
        return "修复/分化"
    return "偏暖/积极"


def max_total_exposure_pct(pool_size: int, cfg: dict[str, Any]) -> float:
    if pool_size <= cfg["cold_pool_threshold"]:
        return cfg["cold_pool_max_pct"]
    return cfg["warm_pool_max_pct"]


def score_stock(stock: dict, sizes: dict[str, int], cfg: dict[str, Any]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0

    prob = float(stock.get("晋级概率_pct") or stock.get("晋级概率", 0) * 100)
    score += prob * 1.5
    reasons.append(f"晋级概率{prob:.1f}%")

    boards = int(stock["连板数"])
    zhaban = int(stock.get("炸板次数", 0))
    turnover = parse_pct(stock.get("换手率"))
    seal_pct = parse_pct(stock.get("封单占比"))
    big_seal = str(stock.get("大单封板", ""))
    theme = stock.get("题材簇") or "其他"
    cluster_n = sizes.get(theme, 0)

    if big_seal == "强封":
        score += 15
        reasons.append("强封")
    if zhaban == 0:
        score += 10
        reasons.append("零炸板")
    if seal_pct >= 20:
        score += 8
        reasons.append(f"封单{seal_pct:.0f}%")

    if cluster_n >= 3:
        score += 12
        reasons.append(f"主线簇({theme}×{cluster_n})")
    elif cluster_n >= 2:
        score += 6
        reasons.append(f"同线簇({theme}×{cluster_n})")

    if boards >= 5:
        score -= 8
        reasons.append("高位5板+")
    if boards > cfg["max_boards_buy"]:
        score -= 50
        reasons.append(f"超最高买入板数{cfg['max_boards_buy']}")

    if zhaban >= 3:
        score -= 12
        reasons.append(f"炸板{zhaban}次")
    if zhaban > cfg["max_zhaban_buy"]:
        score -= 50
        reasons.append("烂板排除")

    if prob < cfg["min_promote_prob_pct"]:
        score -= 30
        reasons.append("晋级率不足")

    if str(stock.get("弱转强")) == "是" and zhaban >= 2:
        score -= 5
        reasons.append("弱转强但质量一般")

    return score, reasons


def rank_candidates(
    stocks: list[dict],
    held: set[str],
    cfg: dict[str, Any],
    trade_date: str = "",
    sample_budget: float = 8000.0,
) -> list[dict]:
    sizes = cluster_sizes(stocks)
    ranked = []
    for s in stocks:
        code = str(s["代码"]).zfill(6)
        if code in held:
            continue
        sc, reasons = score_stock(s, sizes, cfg)
        fill = simulate_limit_up_fill(s, trade_date, sample_budget, cfg) if trade_date else None
        ranked.append(
            {
                "stock": s,
                "code": code,
                "score": sc,
                "reasons": reasons,
                "theme": s.get("题材簇") or "其他",
                "fill_prob": round(fill.fill_prob * 100, 1) if fill else None,
                "can_buy": fill.can_buy if fill else None,
                "fill_note": fill.note if fill else "",
            }
        )
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked


def fetch_close_price(code: str, date: str) -> float | None:
    """断板卖出价：取当日收盘价。"""
    try:
        import akshare as ak
    except ImportError:
        return None

    sym = code
    if code.startswith("6"):
        sym = f"sh{code}"
    elif code.startswith(("0", "3")):
        sym = f"sz{code}"
    else:
        sym = f"bj{code}"

    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=date,
            end_date=date,
            adjust="",
        )
        if df is not None and not df.empty:
            return float(df.iloc[-1]["收盘"])
    except Exception:
        pass

    try:
        df = ak.stock_zh_a_daily(symbol=sym, adjust="qfq")
        if df is not None and not df.empty:
            df = df.reset_index()
            col_date = "date" if "date" in df.columns else df.columns[0]
            df[col_date] = df[col_date].astype(str).str.replace("-", "")
            row = df[df[col_date] == date]
            if not row.empty:
                return float(row.iloc[-1]["close"])
    except Exception:
        pass
    return None


def resolve_sell_price(
    code: str, date: str, next_data: dict | None, fallback_buy: float
) -> tuple[float, str]:
    if next_data:
        for s in next_data.get("stocks", []):
            if str(s["代码"]).zfill(6) == code:
                price = float(s["最新价"])
                boards = int(s["连板数"])
                return price, f"涨停池收盘 {price:.2f}（{boards}板）"

    close = fetch_close_price(code, date)
    if close is not None:
        return close, f"断板收盘价 {close:.2f}"

    # 无法取价时按 -5% 估算
    est = fallback_buy * 0.95
    return est, f"无行情数据，按买入价-5%估算 {est:.2f}"


def portfolio_market_value(pf: Portfolio, prices: dict[str, float]) -> float:
    total = pf.cash
    for p in pf.positions:
        px = prices.get(p.code, p.cost_price)
        total += p.shares * px
    return total


def process_sells(
    pf: Portfolio,
    trade_date: str,
    prev_date: str,
    prev_data: dict,
    today_data: dict,
    trades: list[dict],
    cfg: dict[str, Any],
    journal: dict,
) -> None:
    if not pf.positions:
        return

    prev_data = load_daily_data(prev_date)
    today_stocks = today_data.get("stocks", [])
    today_df = stocks_to_df(today_stocks)
    sells = []

    for pos in list(pf.positions):
        if pos.buy_date >= trade_date:
            continue

        code = pos.code
        in_pool = code in today_df.set_index("code").index if not today_df.empty else False

        if in_pool:
            cur_boards = int(today_df.set_index("code").loc[code, "boards"])
            if cur_boards == pos.boards_at_buy + 1:
                pos.boards_at_buy = cur_boards
                journal["hold"].append(
                    {
                        "code": pos.code,
                        "name": pos.name,
                        "boards": cur_boards,
                        "note": "成功晋级，继续持有",
                    }
                )
                continue
            if cur_boards >= pos.boards_at_buy:
                journal["hold"].append(
                    {
                        "code": pos.code,
                        "name": pos.name,
                        "boards": cur_boards,
                        "note": f"维持{cur_boards}板，继续持有",
                    }
                )
                continue

        sell_price, price_note = resolve_sell_price(
            pos.code, trade_date, today_data, pos.cost_price
        )
        amount = pos.shares * sell_price
        commission = calc_commission(amount, cfg, is_sell=True)
        proceeds = amount - commission
        pnl = proceeds - pos.shares * pos.cost_price
        pnl_pct = (sell_price / pos.cost_price - 1) * 100

        pf.cash += proceeds
        pf.positions.remove(pos)

        record = {
            "date": trade_date,
            "action": "卖出",
            "code": pos.code,
            "name": pos.name,
            "shares": pos.shares,
            "price": round(sell_price, 2),
            "amount": round(amount, 2),
            "commission": round(commission, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "reason": f"未晋级卖出（买入时{pos.boards_at_buy}板）| {price_note}",
            "theme": pos.theme,
        }
        append_trade(trades, record)
        sells.append(record)

    journal["sells"] = sells


def process_buys(
    pf: Portfolio,
    trade_date: str,
    today_data: dict,
    trades: list[dict],
    cfg: dict[str, Any],
    journal: dict,
    *,
    volume_phase: str = "full_day",
) -> None:
    stocks = today_data.get("stocks", [])
    pool_size = len(stocks)
    emotion = market_emotion_label(pool_size, cfg)
    volume = assess_market_volume(trade_date, cfg, phase=volume_phase)

    pool_exposure = max_total_exposure_pct(pool_size, cfg)
    max_exposure = pool_exposure
    if volume.get("max_exposure_pct") is not None:
        max_exposure = min(max_exposure, volume["max_exposure_pct"])

    journal["market"] = {
        "pool_size": pool_size,
        "emotion": emotion,
        "max_exposure_pct": max_exposure,
        "sh_amount_yi": volume.get("sh_amount_yi"),
        "volume_state": volume.get("volume_state"),
        "is_asphyxia": volume.get("is_asphyxia"),
        "volume_note": volume.get("note"),
    }

    total_mv = portfolio_market_value(pf, {})
    single_pct = volume.get("single_position_pct") or cfg["single_position_pct"]
    per_position_cap = total_mv * single_pct

    ranked = rank_candidates(
        stocks,
        pf.position_codes,
        cfg,
        trade_date=trade_date,
        sample_budget=per_position_cap,
    )
    journal["candidates"] = [
        {
            "code": r["code"],
            "name": r["stock"]["名称"],
            "boards": r["stock"]["连板数"],
            "score": round(r["score"], 1),
            "prob_pct": r["stock"].get("晋级概率_pct"),
            "theme": r["theme"],
            "reasons": r["reasons"],
            "fill_prob": r.get("fill_prob"),
            "can_buy": r.get("can_buy"),
            "fill_note": r.get("fill_note", ""),
        }
        for r in ranked[:8]
    ]

    max_positions = int(cfg["max_positions"])
    slots = max_positions - len(pf.positions)
    if slots <= 0 or pf.cash < 1000:
        journal["buys"] = []
        journal["buy_failed"] = []
        journal["buy_skip"] = "持仓已满或现金不足"
        return

    if volume.get("block_new_buy"):
        journal["buys"] = []
        journal["buy_failed"] = []
        journal["buy_skip"] = volume.get("note", "量能窒息，禁止新开仓")
        return

    max_invest = total_mv * max_exposure
    current_invested = sum(p.shares * p.cost_price for p in pf.positions)
    budget = min(pf.cash, max(0, max_invest - current_invested))

    buys = []
    buy_failed = []
    for cand in ranked:
        if slots <= 0 or budget < 1000:
            break
        if cand["score"] < cfg["min_buy_score"]:
            continue

        s = cand["stock"]
        price = float(s["最新价"])
        if price <= 0:
            continue

        alloc = min(budget, per_position_cap, pf.cash)
        intended_shares = int(alloc / price / 100) * 100
        if intended_shares < 100:
            continue

        intended_amount = intended_shares * price
        fill = simulate_limit_up_fill(s, trade_date, intended_amount, cfg)
        if not fill.can_buy:
            buy_failed.append(
                {
                    "code": cand["code"],
                    "name": s["名称"],
                    "boards": int(s["连板数"]),
                    "score": round(cand["score"], 1),
                    "intended_shares": intended_shares,
                    "fill_prob": round(fill.fill_prob * 100, 1),
                    "reason": fill.note,
                }
            )
            continue

        shares = int(intended_shares * fill.fill_ratio / 100) * 100
        if shares < 100:
            buy_failed.append(
                {
                    "code": cand["code"],
                    "name": s["名称"],
                    "boards": int(s["连板数"]),
                    "score": round(cand["score"], 1),
                    "intended_shares": intended_shares,
                    "fill_prob": round(fill.fill_prob * 100, 1),
                    "reason": f"部分成交后不足1手 | {fill.note}",
                }
            )
            continue

        amount = shares * price
        commission = calc_commission(amount, cfg, is_sell=False)
        total_cost = amount + commission
        if total_cost > pf.cash:
            shares = int((pf.cash - cfg["min_commission"]) / price / 100) * 100
            if shares < 100:
                continue
            amount = shares * price
            commission = calc_commission(amount, cfg, is_sell=False)
            total_cost = amount + commission

        pf.cash -= total_cost
        budget -= total_cost
        slots -= 1

        pos = Position(
            code=cand["code"],
            name=s["名称"],
            shares=shares,
            cost_price=price,
            buy_date=trade_date,
            boards_at_buy=int(s["连板数"]),
            buy_reason="; ".join(cand["reasons"]) + f" | {fill.note}",
            theme=cand["theme"],
        )
        pf.positions.append(pos)

        record = {
            "date": trade_date,
            "action": "买入",
            "code": cand["code"],
            "name": s["名称"],
            "shares": shares,
            "price": round(price, 2),
            "amount": round(amount, 2),
            "commission": round(commission, 2),
            "boards": int(s["连板数"]),
            "prob_pct": s.get("晋级概率_pct"),
            "score": round(cand["score"], 1),
            "fill_prob": round(fill.fill_prob * 100, 1),
            "fill_ratio": round(fill.fill_ratio * 100, 1),
            "reason": "; ".join(cand["reasons"]) + f" | {fill.note}",
            "theme": cand["theme"],
            "emotion": emotion,
        }
        append_trade(trades, record)
        buys.append(record)

    journal["buys"] = buys
    journal["buy_failed"] = buy_failed
    if not buys and not journal.get("buy_skip"):
        if buy_failed:
            journal["buy_skip"] = f"有{len(buy_failed)}只意向标的但封板未成交"
        else:
            journal["buy_skip"] = f"无评分≥{cfg['min_buy_score']}的可买标的"


def write_daily_journal(trade_date: str, journal: dict, pf: Portfolio, prices: dict[str, float]) -> None:
    ensure_paper_dir()
    mv = portfolio_market_value(pf, prices)
    ret = (mv / pf.initial_cash - 1) * 100

    lines = [
        f"# 模拟盘日报（{trade_date}）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 机器人：连板策略模拟盘（晋级概率 + 封板质量 + 主线簇 + **排板成交模拟**）",
        "",
        "## 盘面观察",
        "",
        f"- 连板池（≥2）：**{journal['market']['pool_size']}** 只",
        f"- 情绪判断：**{journal['market']['emotion']}**",
        f"- 上证成交额：**{journal['market'].get('sh_amount_yi', '—')}** 亿元",
        f"- 量能状态：**{journal['market'].get('volume_state', '—')}**",
        f"- 最大仓位上限：{journal['market']['max_exposure_pct']*100:.0f}%",
    ]
    if journal["market"].get("volume_note"):
        lines.append(f"- 量能说明：{journal['market']['volume_note']}")
    lines += [
        "",
        "## 候选评分（Top）",
        "",
        "| 代码 | 名称 | 板数 | 评分 | 晋级率 | 成交概率 | 可买 | 题材 |",
        "|------|------|------|------|--------|----------|------|------|",
    ]

    for c in journal.get("candidates", [])[:6]:
        can_buy = "是" if c.get("can_buy") else ("否" if c.get("can_buy") is False else "-")
        lines.append(
            f"| {c['code']} | {c['name']} | {c['boards']} | {c['score']} | "
            f"{c.get('prob_pct', '-')} | {c.get('fill_prob', '-')} | {can_buy} | {c.get('theme', '-')} |"
        )

    lines += ["", "## 今日操作", ""]

    if journal.get("sells"):
        lines.append("### 卖出")
        lines.append("")
        for s in journal["sells"]:
            lines.append(
                f"- **卖出** {s['name']}（{s['code']}）{s['shares']}股 @ {s['price']} "
                f"盈亏 {s['pnl']:+.2f}（{s['pnl_pct']:+.2f}%）— {s['reason']}"
            )
        lines.append("")
    else:
        lines.append("- 无卖出")
        lines.append("")

    if journal.get("hold"):
        lines.append("### 继续持有")
        lines.append("")
        for h in journal["hold"]:
            lines.append(f"- {h['name']}（{h['code']}）→ {h['boards']}板：{h['note']}")
        lines.append("")

    if journal.get("buy_failed"):
        lines.append("### 想买但未成交（封板排队）")
        lines.append("")
        for f in journal["buy_failed"]:
            lines.append(
                f"- **未成交** {f['name']}（{f['code']}）{f['boards']}板 "
                f"意向{f['intended_shares']}股 成交概率{f['fill_prob']}% — {f['reason']}"
            )
        lines.append("")

    if journal.get("buys"):
        lines.append("### 买入")
        lines.append("")
        for b in journal["buys"]:
            fill_info = ""
            if b.get("fill_prob") is not None:
                fill_info = f" 成交概率{b['fill_prob']}%"
                if b.get("fill_ratio", 100) < 100:
                    fill_info += f"（部分成交{b['fill_ratio']}%）"
            lines.append(
                f"- **买入** {b['name']}（{b['code']}）{b['boards']}板 {b['shares']}股 @ {b['price']} "
                f"评分{b['score']}{fill_info} | {b['reason']}"
            )
        lines.append("")
    elif journal.get("buy_skip"):
        lines.append(f"- 买入：{journal['buy_skip']}")
        lines.append("")

    lines += [
        "## 收盘持仓",
        "",
        f"- 现金：{pf.cash:,.2f} 元",
        f"- 总资产：{mv:,.2f} 元（收益率 {ret:+.2f}%）",
        "",
    ]

    if pf.positions:
        lines.append("| 代码 | 名称 | 股数 | 成本 | 买入日 | 买入板数 | 题材 |")
        lines.append("|------|------|------|------|--------|----------|------|")
        for p in pf.positions:
            px = prices.get(p.code, p.cost_price)
            lines.append(
                f"| {p.code} | {p.name} | {p.shares} | {p.cost_price:.2f} | "
                f"{p.buy_date} | {p.boards_at_buy} | {p.theme} |"
            )
    else:
        lines.append("- 空仓")

    lines.append("")
    paper_daily_md(trade_date).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_master_log(pf: Portfolio, trades: list[dict], prices: dict[str, float]) -> None:
    mv = portfolio_market_value(pf, prices)
    ret = (mv / pf.initial_cash - 1) * 100

    lines = [
        "# 连板模拟盘 · 操作记录",
        "",
        f"> 最后更新：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 初始资金：**{pf.initial_cash:,.0f}** 元",
        "",
        "## 当前状态",
        "",
        f"| 项目 | 数值 |",
        f"|------|------|",
        f"| 现金 | {pf.cash:,.2f} 元 |",
        f"| 总资产 | {mv:,.2f} 元 |",
        f"| 总收益率 | {ret:+.2f}% |",
        f"| 持仓数 | {len(pf.positions)} |",
        f"| 最近交易日 | {pf.last_trade_date or '-'} |",
        "",
    ]

    if pf.positions:
        lines += ["## 持仓明细", ""]
        for p in pf.positions:
            px = prices.get(p.code, p.cost_price)
            mv_p = p.shares * px
            lines.append(
                f"- **{p.name}**（{p.code}）{p.shares}股 成本{p.cost_price:.2f} "
                f"市值{mv_p:,.2f} | {p.boards_at_buy}板买入 {p.buy_date} | {p.theme}"
            )
        lines.append("")

    lines += ["## 成交流水", ""]
    if trades:
        lines.append("| # | 日期 | 买卖 | 名称 | 代码 | 股数 | 价格 | 金额 | 盈亏 | 说明 |")
        lines.append("|---|------|------|------|------|------|------|------|------|------|")
        for t in trades[-30:]:
            pnl = f"{t.get('pnl', 0):+.2f}" if t["action"] == "卖出" else "-"
            lines.append(
                f"| {t.get('id', '-')} | {t['date']} | {t['action']} | {t['name']} | {t['code']} | "
                f"{t['shares']} | {t['price']} | {t['amount']} | {pnl} | {t.get('reason', '')[:40]} |"
            )
    else:
        lines.append("- 暂无成交")
    lines.append("")

    PAPER_LOG_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_prices_from_pool(data: dict | None, pf: Portfolio) -> dict[str, float]:
    prices: dict[str, float] = {}
    if data:
        for s in data.get("stocks", []):
            prices[str(s["代码"]).zfill(6)] = float(s["最新价"])
    for p in pf.positions:
        if p.code not in prices:
            prices[p.code] = p.cost_price
    return prices


def run_single_day(
    trade_date: str,
    pf: Portfolio,
    trades: list[dict],
    cfg: dict[str, Any],
    prev_date: str | None = None,
) -> dict:
    today_data = load_daily_data(trade_date)
    if not today_data:
        raise FileNotFoundError(f"缺少当日连板数据: {daily_json(trade_date)}")

    if pf.last_trade_date and trade_date <= pf.last_trade_date:
        return {"skipped": True, "reason": f"已处理过 {trade_date}"}

    if prev_date is None:
        dates = sorted(list_daily_dates())
        if trade_date in dates:
            idx = dates.index(trade_date)
            if idx + 1 < len(dates):
                prev_date = dates[idx + 1]

    journal: dict[str, Any] = {
        "date": trade_date,
        "market": {},
        "candidates": [],
        "sells": [],
        "buys": [],
        "buy_failed": [],
        "hold": [],
    }

    if prev_date:
        prev_data = load_daily_data(prev_date)
        if prev_data and pf.positions:
            process_sells(pf, trade_date, prev_date, prev_data, today_data, trades, cfg, journal)

    process_buys(pf, trade_date, today_data, trades, cfg, journal)

    prices = get_prices_from_pool(today_data, pf)
    write_daily_journal(trade_date, journal, pf, prices)
    pf.last_trade_date = trade_date
    save_portfolio(pf)
    write_master_log(pf, trades, prices)
    return journal


def resolve_pool_date(trade_date: str) -> str | None:
    if daily_json(trade_date).is_file():
        return trade_date
    for d in list_daily_dates():
        if d <= trade_date:
            return d
    return None


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.strip().split(":", 1)
    return int(hour), int(minute)


def wait_until_auction_end(trade_date: str, cfg: dict[str, Any]) -> None:
    """竞价结束（默认 09:25 北京时间）后再继续分析。"""
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    hour, minute = _parse_hhmm(str(cfg.get("auction_end_time", "09:25")))
    target = datetime.strptime(trade_date, "%Y%m%d").replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=tz
    )
    if now < target:
        wait_s = (target - now).total_seconds()
        print(f"等待竞价结束 {cfg.get('auction_end_time', '09:25')}（约 {wait_s:.0f}s）...")
        time.sleep(wait_s)


def refresh_today_pool(trade_date: str, cfg: dict[str, Any]) -> tuple[bool, str]:
    """竞价结束后拉取当日涨停池并落盘，供预期操作分析使用。"""
    scripts_dir = Path(__file__).resolve().parent
    root = scripts_dir.parent
    python = sys.executable
    retries = int(cfg.get("auction_max_retries", 6))
    interval = int(cfg.get("auction_retry_seconds", 30))
    last_err = ""

    for attempt in range(1, retries + 1):
        print(f"拉取当日连板池 {trade_date}（第 {attempt}/{retries} 次）...")
        proc = subprocess.run(
            [python, str(scripts_dir / "lianban_today.py"), "--date", trade_date],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            last_err = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
            print(last_err, file=sys.stderr)
        elif daily_json(trade_date).is_file():
            data = load_daily_data(trade_date)
            if data is not None:
                pool_size = len(data.get("stocks", []))
                print(f"连板池已更新：{trade_date}，样本 {pool_size} 只")
                return True, f"{trade_date} 竞价后连板池（{pool_size} 只）"
        else:
            last_err = f"未生成 {daily_json(trade_date)}"

        if attempt < retries:
            print(f"数据未就绪，{interval}s 后重试...")
            time.sleep(interval)

    return False, last_err or "竞价后连板池拉取失败"


def prepare_auction_analysis(
    trade_date: str,
    cfg: dict[str, Any],
    *,
    skip_wait: bool = False,
) -> tuple[bool, str]:
    if not skip_wait:
        wait_until_auction_end(trade_date, cfg)
    return refresh_today_pool(trade_date, cfg)


def preview_expected_operations(
    trade_date: str,
    pf: Portfolio,
    cfg: dict[str, Any],
    *,
    pool_note: str = "",
) -> dict[str, Any]:
    """竞价结束后预判：不修改真实持仓，基于当日连板池模拟买卖意向。"""
    pool_date = resolve_pool_date(trade_date)
    if not pool_date:
        return {"error": "无连板池数据，请先运行 python scripts/lianban.py today"}

    today_data = load_daily_data(pool_date)
    if not today_data:
        return {"error": f"缺少连板数据: {daily_json(pool_date)}"}

    pf_copy = copy_portfolio(pf)
    trades_copy: list[dict] = []
    journal: dict[str, Any] = {
        "trade_date": trade_date,
        "pool_date": pool_date,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        "data_phase": "竞价结束后",
        "pool_note": pool_note,
        "market": {},
        "candidates": [],
        "sell_watch": [],
        "sells": [],
        "buys": [],
        "buy_failed": [],
        "hold": [],
        "buy_skip": "",
    }

    for pos in pf_copy.positions:
        target = pos.boards_at_buy + 1
        journal["sell_watch"].append(
            {
                "code": pos.code,
                "name": pos.name,
                "boards_at_buy": pos.boards_at_buy,
                "target_boards": target,
                "shares": pos.shares,
                "note": f"若今日收盘未晋级至{target}板，将卖出",
            }
        )

    dates = sorted(list_daily_dates())
    prev_date = None
    if pool_date in dates:
        idx = dates.index(pool_date)
        if idx + 1 < len(dates):
            prev_date = dates[idx + 1]

    if pool_date == trade_date and prev_date and pf_copy.positions:
        prev_data = load_daily_data(prev_date)
        if prev_data:
            process_sells(
                pf_copy,
                trade_date,
                prev_date,
                prev_data,
                today_data,
                trades_copy,
                cfg,
                journal,
            )
            journal["sell_watch"] = []

    process_buys(
        pf_copy, trade_date, today_data, trades_copy, cfg, journal, volume_phase="auction"
    )

    prices = get_prices_from_pool(today_data, pf)
    journal["portfolio"] = {
        "cash": pf.cash,
        "market_value": portfolio_market_value(pf, prices),
        "return_pct": (portfolio_market_value(pf, prices) / pf.initial_cash - 1) * 100,
        "positions": len(pf.positions),
    }
    return journal


def format_feishu_expect(journal: dict[str, Any], cfg: dict[str, Any]) -> str:
    if journal.get("error"):
        return f"⚠️ 连板模拟盘竞价后推送失败\n\n{journal['error']}"

    trade_date = journal["trade_date"]
    pool_date = journal["pool_date"]
    bot_name = cfg.get("bot_name", "连板模拟盘机器人")
    market = journal.get("market") or {}
    pf = journal.get("portfolio") or {}

    lines = [
        f"**{bot_name} · 竞价结束后预期操作（{trade_date}）**",
        "",
        f"生成时间：{journal['generated_at']}",
        f"数据基准：{journal.get('pool_note') or f'{pool_date} 连板池'}",
        "",
        "**盘面**",
        f"- 连板池：{market.get('pool_size', '—')} 只",
        f"- 情绪：{market.get('emotion', '—')}",
        f"- 仓位上限：{market.get('max_exposure_pct', 0) * 100:.0f}%",
    ]
    if market.get("volume_note"):
        lines.append(f"- 量能：{market['volume_note']}")
    min_yi = cfg.get("sh_min_amount_yi", 9000)
    warn_yi = cfg.get("sh_warn_amount_yi", 10000)
    lines.append(
        f"- 量能风控：竞价阶段**不启用**全天阈值（{min_yi}/{warn_yi} 亿），收盘模拟盘再判定"
    )

    lines += ["", "**持仓监控（卖出预期）**"]
    if journal.get("sells"):
        for s in journal["sells"]:
            lines.append(
                f"- 🔴 预期卖出 {s['name']}（{s['code']}）{s['shares']}股 — {s['reason']}"
            )
    elif journal.get("sell_watch"):
        for w in journal["sell_watch"]:
            lines.append(f"- 👀 {w['name']}（{w['code']}）{w['boards_at_buy']}板 — {w['note']}")
    else:
        lines.append("- 无持仓")

    lines += ["", "**预期买入**"]
    if journal.get("buys"):
        for b in journal["buys"]:
            fill = f" 成交概率{b['fill_prob']}%" if b.get("fill_prob") is not None else ""
            lines.append(
                f"- 🟢 拟买入 {b['name']}（{b['code']}）{b['boards']}板 "
                f"{b['shares']}股 @ {b['price']} 评分{b['score']}{fill}"
            )
    elif journal.get("buy_failed"):
        for f in journal["buy_failed"][:4]:
            lines.append(
                f"- 🟡 想买未成交 {f['name']}（{f['code']}）{f['boards']}板 "
                f"评分{f['score']} 成交概率{f['fill_prob']}% — {f['reason']}"
            )
    elif journal.get("buy_skip"):
        lines.append(f"- {journal['buy_skip']}")
    else:
        lines.append("- 暂无买入意向")

    top = journal.get("candidates", [])[:3]
    if top:
        lines += ["", "**候选关注 Top3**"]
        for c in top:
            can_buy = "可买" if c.get("can_buy") else "难成交"
            lines.append(
                f"- {c['name']}（{c['code']}）{c['boards']}板 "
                f"评分{c['score']} 晋级率{c.get('prob_pct', '-')} {can_buy}"
            )

    lines += [
        "",
        "**当前模拟盘**",
        f"- 现金：{pf.get('cash', 0):,.2f} 元",
        f"- 总资产：{pf.get('market_value', 0):,.2f} 元（{pf.get('return_pct', 0):+.2f}%）",
        f"- 持仓：{pf.get('positions', 0)} 只",
    ]
    return "\n".join(lines)


def send_feishu_message(text: str, cfg: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    chat_id = cfg.get("feishu_chat_id", "")
    if not chat_id:
        return {"ok": False, "error": "未配置 feishu_chat_id"}
    if not cfg.get("feishu_notify_enabled", True):
        return {"ok": False, "error": "feishu_notify_enabled=false"}

    import os
    import shutil
    import subprocess

    lark_cli = cfg.get("lark_cli_bin") or shutil.which("lark-cli") or "lark-cli"
    cmd = [lark_cli, "im", "+messages-send", "--chat-id", chat_id, "--markdown", text]
    if dry_run:
        return {"ok": True, "dry_run": True, "cmd": cmd, "text": text}

    env = os.environ.copy()
    npm_global = os.path.expanduser("~/.npm-global/bin")
    if npm_global not in env.get("PATH", ""):
        env["PATH"] = f"{npm_global}:{env.get('PATH', '')}"

    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or proc.stdout.strip()}
    try:
        return {"ok": True, "response": json.loads(proc.stdout)}
    except json.JSONDecodeError:
        return {"ok": True, "response": proc.stdout.strip()}


def cmd_expect(args: argparse.Namespace) -> None:
    cfg = load_config()
    pf = load_portfolio(cfg)
    trade_date = args.date or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    pool_note = ""
    if not getattr(args, "skip_refresh", False):
        ok, note = prepare_auction_analysis(
            trade_date, cfg, skip_wait=getattr(args, "skip_wait", False)
        )
        if not ok:
            journal = {"error": note}
            if args.format == "json":
                print(json.dumps(journal, ensure_ascii=False, indent=2))
                return
            print(format_feishu_expect(journal, cfg))
            raise SystemExit(1)
        pool_note = note
    journal = preview_expected_operations(trade_date, pf, cfg, pool_note=pool_note)
    if args.format == "json":
        print(json.dumps(journal, ensure_ascii=False, indent=2))
        return
    print(format_feishu_expect(journal, cfg))


def cmd_notify(args: argparse.Namespace) -> None:
    cfg = load_config()
    pf = load_portfolio(cfg)
    trade_date = args.date or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")

    ok, pool_note = prepare_auction_analysis(
        trade_date,
        cfg,
        skip_wait=getattr(args, "skip_wait", False),
    )
    if not ok:
        text = format_feishu_expect({"error": pool_note}, cfg)
        if getattr(args, "print_only", False):
            print(text)
            raise SystemExit(1)
        result = send_feishu_message(text, cfg, dry_run=getattr(args, "dry_run", False))
        if not result.get("ok"):
            print(f"飞书推送失败: {result.get('error')}", file=sys.stderr)
        raise SystemExit(1)

    journal = preview_expected_operations(trade_date, pf, cfg, pool_note=pool_note)
    text = format_feishu_expect(journal, cfg)
    if args.print_only:
        print(text)
        return
    result = send_feishu_message(text, cfg, dry_run=args.dry_run)
    if not result.get("ok"):
        print(f"飞书推送失败: {result.get('error')}", file=sys.stderr)
        raise SystemExit(1)
    if result.get("dry_run"):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"已推送竞价后预期操作到飞书群 {cfg.get('feishu_chat_id')}")


def cmd_run(args: argparse.Namespace) -> None:
    cfg = load_config()
    save_config_template()
    pf = load_portfolio(cfg)
    trades = load_trades()

    trade_date = args.date or resolve_trade_date(None, max_scan=30)
    if not trade_date:
        print("无法确定交易日", file=sys.stderr)
        raise SystemExit(2)

    daily_path = daily_json(trade_date)
    if not daily_path.is_file():
        print(f"缺少 {daily_path}，请先运行: python scripts/lianban.py today --date {trade_date}")
        raise SystemExit(2)

    journal = run_single_day(trade_date, pf, trades, cfg, prev_date=args.prev_date)
    if journal.get("skipped"):
        print(journal["reason"])
        return

    mv = portfolio_market_value(pf, get_prices_from_pool(load_daily_data(trade_date), pf))
    print(f"模拟盘 {trade_date} 完成 | 总资产 {mv:,.2f} 元 | 持仓 {len(pf.positions)} 只")
    print(f"日报: {paper_daily_md(trade_date)}")
    print(f"汇总: {PAPER_LOG_MD}")


def cmd_backfill(args: argparse.Namespace) -> None:
    cfg = load_config()
    if args.reset:
        pf = Portfolio(
            initial_cash=float(cfg["initial_cash"]),
            cash=float(cfg["initial_cash"]),
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        save_portfolio(pf)
        save_trades([])
    else:
        pf = load_portfolio(cfg)
    trades = load_trades()

    dates = sorted(d for d in list_daily_dates() if d.isdigit())
    start = args.date_from or dates[0] if dates else None
    end = args.date_to or dates[-1] if dates else None
    if not start or not end:
        print("无可用交易日数据", file=sys.stderr)
        raise SystemExit(2)

    run_dates = [d for d in dates if start <= d <= end]
    print(f"回测模拟 {len(run_dates)} 日: {start} ~ {end}")

    for i, d in enumerate(run_dates):
        prev = run_dates[i - 1] if i > 0 else None
        try:
            journal = run_single_day(d, pf, trades, cfg, prev_date=prev)
            if journal.get("skipped"):
                continue
            mv = portfolio_market_value(pf, get_prices_from_pool(load_daily_data(d), pf))
            print(f"  {d} 总资产 {mv:,.2f} 持仓{len(pf.positions)}")
        except FileNotFoundError as e:
            print(f"  [跳过] {d}: {e}")

    mv = portfolio_market_value(pf, get_prices_from_pool(load_daily_data(end), pf))
    ret = (mv / pf.initial_cash - 1) * 100
    print(f"\n回测结束: 总资产 {mv:,.2f} 元 收益率 {ret:+.2f}%")


def cmd_status(_: argparse.Namespace) -> None:
    cfg = load_config()
    pf = load_portfolio(cfg)
    trades = load_trades()
    latest = list_daily_dates()
    data = load_daily_data(latest[0]) if latest else None
    prices = get_prices_from_pool(data, pf)
    mv = portfolio_market_value(pf, prices)
    ret = (mv / pf.initial_cash - 1) * 100
    print(f"初始资金: {pf.initial_cash:,.2f}")
    print(f"现金:     {pf.cash:,.2f}")
    print(f"总资产:   {mv:,.2f}  ({ret:+.2f}%)")
    print(f"持仓:     {len(pf.positions)} 只")
    print(f"成交:     {len(trades)} 笔")
    for p in pf.positions:
        print(f"  - {p.name}({p.code}) {p.shares}股 @{p.cost_price}")


def cmd_reset(_: argparse.Namespace) -> None:
    cfg = load_config()
    pf = Portfolio(
        initial_cash=float(cfg["initial_cash"]),
        cash=float(cfg["initial_cash"]),
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    save_portfolio(pf)
    save_trades([])
    if PAPER_LOG_MD.is_file():
        PAPER_LOG_MD.unlink()
    for f in PAPER_DAILY_DIR.glob("*.md"):
        f.unlink()
    print(f"已重置模拟盘，初始资金 {cfg['initial_cash']:,.0f} 元")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="连板策略模拟盘机器人")
    sub = p.add_subparsers(dest="command", required=True)

    sp_run = sub.add_parser("run", help="运行单日模拟（默认最近交易日）")
    sp_run.add_argument("--date", help="交易日 YYYYMMDD")
    sp_run.add_argument("--prev-date", help="上一交易日（自动推断）")
    sp_run.set_defaults(func=cmd_run)

    sp_bf = sub.add_parser("backfill", help="按历史每日数据批量模拟")
    sp_bf.add_argument("--from", dest="date_from", help="起始 YYYYMMDD")
    sp_bf.add_argument("--to", dest="date_to", help="结束 YYYYMMDD")
    sp_bf.add_argument("--reset", action="store_true", help="回测前重置资金")
    sp_bf.set_defaults(func=cmd_backfill)

    sub.add_parser("status", help="查看持仓").set_defaults(func=cmd_status)
    sub.add_parser("reset", help="重置模拟盘").set_defaults(func=cmd_reset)

    sp_expect = sub.add_parser("expect", help="竞价结束后生成预期操作（不修改持仓）")
    sp_expect.add_argument("--date", help="交易日 YYYYMMDD")
    sp_expect.add_argument("--format", choices=["text", "json"], default="text")
    sp_expect.add_argument("--skip-wait", action="store_true", help="不等待竞价结束时刻")
    sp_expect.add_argument("--skip-refresh", action="store_true", help="不拉取当日连板池，用本地缓存")
    sp_expect.set_defaults(func=cmd_expect)

    sp_notify = sub.add_parser("notify", help="竞价结束后分析并推送预期操作到飞书群")
    sp_notify.add_argument("--date", help="交易日 YYYYMMDD")
    sp_notify.add_argument("--skip-wait", action="store_true", help="不等待竞价结束时刻")
    sp_notify.add_argument("--print-only", action="store_true", help="仅打印，不发送")
    sp_notify.add_argument("--dry-run", action="store_true", help="打印 lark-cli 命令，不发送")
    sp_notify.set_defaults(func=cmd_notify)
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    ns.func(ns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
