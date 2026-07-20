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
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

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
    "commission_rate": 0.0003,
    "stamp_tax_sell": 0.001,
    "min_commission": 5.0,
    "bot_name": "连板模拟盘机器人",
    # 量能风控（上证成交额，亿元）；below_sh_min_no_buy=false 时不因量能禁止开仓
    "sh_min_amount_yi": 9000,
    "sh_warn_amount_yi": 10000,
    "below_sh_min_no_buy": False,
    "asphyxia_max_exposure_pct": 0.1,
    "asphyxia_single_position_pct": 0.1,
    # 指数退潮 + 缩量 → 强制冰点情绪
    "index_5d_drop_ice_pct": 3.0,
    "index_vol_shrink_ratio": 0.85,
    # 风格/主线：连板池内同题材簇至少 N 只才买
    "min_cluster_size_buy": 3,
    "require_mainline_cluster": True,
    # 冰点期不买高位板（≥此板数则跳过）
    "cold_pool_max_boards_buy": 2,
}


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


def trade_date_to_iso(trade_date: str) -> str:
    return f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"


def fetch_sh_index_series(trade_date: str, lookback: int = 10) -> list[dict[str, float]]:
    """上证指数近 N 日收盘与成交额（亿元），含 trade_date 当日。"""
    try:
        import akshare as ak
    except ImportError:
        return []

    iso = trade_date_to_iso(trade_date)
    try:
        df = ak.stock_zh_index_daily_em(symbol="sh000001")
        if df is None or df.empty:
            return []
        df = df.copy()
        df["date"] = df["date"].astype(str).str[:10]
        df = df[df["date"] <= iso].tail(lookback)
        rows: list[dict[str, float]] = []
        for _, r in df.iterrows():
            rows.append(
                {
                    "close": float(r["close"]),
                    "amount_yi": float(r["amount"]) / 1e8,
                }
            )
        return rows
    except Exception:
        return []


def fetch_sh_amount_yi(trade_date: str) -> float | None:
    series = fetch_sh_index_series(trade_date, lookback=1)
    return series[-1]["amount_yi"] if series else None


def assess_market_volume(trade_date: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """全市场量能评估（以上证成交额为锚）。"""
    sh_yi = fetch_sh_amount_yi(trade_date)
    min_yi = float(cfg.get("sh_min_amount_yi", 9000))
    warn_yi = float(cfg.get("sh_warn_amount_yi", 10000))
    below_no_buy = bool(cfg.get("below_sh_min_no_buy", False))

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
            "max_exposure_pct": (
                float(cfg.get("asphyxia_max_exposure_pct", 0.1)) if below_no_buy else None
            ),
            "single_position_pct": (
                float(cfg.get("asphyxia_single_position_pct", 0.1)) if below_no_buy else None
            ),
            "note": f"上证成交额{sh_yi:.0f}亿<{min_yi:.0f}亿，量能偏低"
            + ("，禁止新开仓" if below_no_buy else "（不限制开仓）"),
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


def assess_index_trend(trade_date: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """近 5 日指数涨跌与量能是否萎缩。"""
    series = fetch_sh_index_series(trade_date, lookback=6)
    if len(series) < 2:
        return {
            "index_5d_pct": None,
            "vol_shrinking": False,
            "force_ice": False,
            "note": "指数数据不足",
        }

    start_close = series[max(0, len(series) - 6)]["close"]
    end_close = series[-1]["close"]
    idx_5d_pct = (end_close / start_close - 1) * 100

    amounts = [r["amount_yi"] for r in series]
    today_amt = amounts[-1]
    avg_amt = sum(amounts[:-1]) / max(1, len(amounts) - 1)
    shrink_ratio = float(cfg.get("index_vol_shrink_ratio", 0.85))
    vol_shrinking = today_amt < avg_amt * shrink_ratio

    drop_thr = float(cfg.get("index_5d_drop_ice_pct", 3.0))
    force_ice = idx_5d_pct <= -drop_thr and vol_shrinking
    note = f"上证5日{idx_5d_pct:+.1f}%"
    if vol_shrinking:
        note += f"，量能较均量萎缩（{today_amt:.0f}/{avg_amt:.0f}亿）"
    if force_ice:
        note += "，退潮+缩量→强制冰点"

    return {
        "index_5d_pct": round(idx_5d_pct, 2),
        "vol_shrinking": vol_shrinking,
        "force_ice": force_ice,
        "note": note,
    }


def assess_market_context(
    trade_date: str, pool_size: int, cfg: dict[str, Any]
) -> dict[str, Any]:
    """综合情绪：连板宽度 + 量能 + 指数趋势。"""
    volume = assess_market_volume(trade_date, cfg)
    index_trend = assess_index_trend(trade_date, cfg)

    if volume.get("is_asphyxia") or index_trend.get("force_ice"):
        emotion = "冰点/谨慎"
    elif pool_size <= cfg["cold_pool_threshold"]:
        emotion = "冰点/谨慎"
    elif pool_size <= 10:
        emotion = "修复/分化"
    else:
        emotion = "偏暖/积极"

    if index_trend.get("force_ice") and emotion == "修复/分化":
        emotion = "冰点/谨慎"

    if volume.get("max_exposure_pct") is not None:
        max_exposure = volume["max_exposure_pct"]
    elif pool_size <= cfg["cold_pool_threshold"]:
        max_exposure = cfg["cold_pool_max_pct"]
    else:
        max_exposure = cfg["warm_pool_max_pct"]

    if index_trend.get("force_ice"):
        max_exposure = min(max_exposure, float(cfg.get("asphyxia_max_exposure_pct", 0.1)))

    single_pct = volume.get("single_position_pct") or cfg["single_position_pct"]

    return {
        "pool_size": pool_size,
        "emotion": emotion,
        "max_exposure_pct": max_exposure,
        "single_position_pct": single_pct,
        "volume": volume,
        "index_trend": index_trend,
        "block_new_buy": bool(volume.get("block_new_buy")),
    }


def passes_buy_filters(
    stock: dict,
    sizes: dict[str, int],
    ctx: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[bool, str]:
    """风格转换 + 冰点高位板过滤。"""
    boards = int(stock["连板数"])
    theme = stock.get("题材簇") or "其他"
    cluster_n = sizes.get(theme, 0)
    pool_size = int(ctx.get("pool_size", 0))

    if cfg.get("require_mainline_cluster", True):
        min_cluster = int(cfg.get("min_cluster_size_buy", 3))
        if cluster_n < min_cluster:
            return False, f"非主线簇({theme}×{cluster_n}<{min_cluster})"

    cold_thr = int(cfg.get("cold_pool_threshold", 6))
    max_boards_cold = int(cfg.get("cold_pool_max_boards_buy", 2))
    if pool_size <= cold_thr and boards > max_boards_cold:
        return False, f"冰点期不买{boards}板(上限{max_boards_cold}板)"

    return True, ""


def resolve_prev_trade_date(trade_date: str) -> str | None:
    """已落盘交易日中，trade_date 的上一交易日（新到旧列表）。"""
    dates = list_daily_dates()
    if trade_date not in dates:
        return None
    idx = dates.index(trade_date)
    if idx + 1 < len(dates):
        return dates[idx + 1]
    return None


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

    if turnover < cfg["min_turnover_pct"]:
        score -= 40
        reasons.append(f"换手{turnover:.2f}%一字难买")

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
    ctx: dict[str, Any] | None = None,
) -> list[dict]:
    sizes = cluster_sizes(stocks)
    ranked = []
    for s in stocks:
        code = str(s["代码"]).zfill(6)
        if code in held:
            continue
        sc, reasons = score_stock(s, sizes, cfg)
        can_buy = True
        filter_note = ""
        if ctx is not None:
            can_buy, filter_note = passes_buy_filters(s, sizes, ctx, cfg)
            if not can_buy and filter_note:
                reasons.append(filter_note)
        ranked.append(
            {
                "stock": s,
                "code": code,
                "score": sc,
                "reasons": reasons,
                "theme": s.get("题材簇") or "其他",
                "can_buy": can_buy,
                "filter_note": filter_note,
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
) -> None:
    stocks = today_data.get("stocks", [])
    pool_size = len(stocks)
    ctx = assess_market_context(trade_date, pool_size, cfg)
    emotion = ctx["emotion"]
    journal["market"] = {
        "pool_size": pool_size,
        "emotion": emotion,
        "max_exposure_pct": ctx["max_exposure_pct"],
        "sh_amount_yi": ctx["volume"].get("sh_amount_yi"),
        "volume_state": ctx["volume"].get("volume_state"),
        "index_5d_pct": ctx["index_trend"].get("index_5d_pct"),
        "block_new_buy": ctx["block_new_buy"],
        "volume_note": ctx["volume"].get("note"),
        "index_note": ctx["index_trend"].get("note"),
    }

    ranked = rank_candidates(stocks, pf.position_codes, cfg, ctx)
    journal["candidates"] = [
        {
            "code": r["code"],
            "name": r["stock"]["名称"],
            "boards": r["stock"]["连板数"],
            "score": round(r["score"], 1),
            "prob_pct": r["stock"].get("晋级概率_pct"),
            "theme": r["theme"],
            "reasons": r["reasons"],
            "can_buy": r.get("can_buy", True),
        }
        for r in ranked[:8]
    ]

    if ctx["block_new_buy"]:
        journal["buys"] = []
        journal["buy_skip"] = (
            f"量能风控禁止新开仓（{ctx['volume'].get('note', '')}）"
        )
        return

    max_positions = int(cfg["max_positions"])
    slots = max_positions - len(pf.positions)
    if slots <= 0 or pf.cash < 1000:
        journal["buys"] = []
        journal["buy_skip"] = "持仓已满或现金不足"
        return

    total_mv = portfolio_market_value(pf, {})
    max_invest = total_mv * ctx["max_exposure_pct"]
    current_invested = sum(p.shares * p.cost_price for p in pf.positions)
    budget = min(pf.cash, max(0, max_invest - current_invested))
    per_position_cap = total_mv * ctx["single_position_pct"]

    buys = []
    for cand in ranked:
        if slots <= 0 or budget < 1000:
            break
        if not cand.get("can_buy", True):
            continue
        if cand["score"] < cfg["min_buy_score"]:
            continue

        s = cand["stock"]
        price = float(s["最新价"])
        if price <= 0:
            continue

        alloc = min(budget, per_position_cap, pf.cash)
        shares = int(alloc / price / 100) * 100
        if shares < 100:
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
            buy_reason="; ".join(cand["reasons"]),
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
            "reason": "; ".join(cand["reasons"]),
            "theme": cand["theme"],
            "emotion": emotion,
        }
        append_trade(trades, record)
        buys.append(record)

    journal["buys"] = buys
    if not buys and not journal.get("buy_skip"):
        journal["buy_skip"] = f"无评分≥{cfg['min_buy_score']}且通过风控的可买标的"


def write_daily_journal(trade_date: str, journal: dict, pf: Portfolio, prices: dict[str, float]) -> None:
    ensure_paper_dir()
    mv = portfolio_market_value(pf, prices)
    ret = (mv / pf.initial_cash - 1) * 100

    lines = [
        f"# 模拟盘日报（{trade_date}）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 机器人：连板策略模拟盘（晋级概率 + 封板质量 + 主线簇 + **量能/风格风控**）",
        "",
        "## 盘面观察",
        "",
        f"- 连板池（≥2）：**{journal['market']['pool_size']}** 只",
        f"- 情绪判断：**{journal['market']['emotion']}**",
    ]
    mkt = journal["market"]
    if mkt.get("sh_amount_yi") is not None:
        lines.append(f"- 上证成交额：**{mkt['sh_amount_yi']:.0f}** 亿元（{mkt.get('volume_state', '-')}）")
    if mkt.get("index_5d_pct") is not None:
        lines.append(f"- 上证5日涨跌：**{mkt['index_5d_pct']:+.2f}%**")
    if mkt.get("block_new_buy"):
        lines.append("- 量能风控：**禁止新开仓**")
    lines += [
        f"- 最大仓位上限：{journal['market']['max_exposure_pct']*100:.0f}%",
        "",
        "## 候选评分（Top）",
        "",
        "| 代码 | 名称 | 板数 | 评分 | 晋级率 | 可买 | 题材 | 因子 |",
        "|------|------|------|------|--------|------|------|------|",
    ]

    for c in journal.get("candidates", [])[:6]:
        can = "是" if c.get("can_buy", True) else "否"
        lines.append(
            f"| {c['code']} | {c['name']} | {c['boards']} | {c['score']} | "
            f"{c.get('prob_pct', '-')} | {can} | {c.get('theme', '-')} | "
            f"{'; '.join(c.get('reasons', [])[:3])} |"
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

    if journal.get("buys"):
        lines.append("### 买入")
        lines.append("")
        for b in journal["buys"]:
            lines.append(
                f"- **买入** {b['name']}（{b['code']}）{b['boards']}板 {b['shares']}股 @ {b['price']} "
                f"评分{b['score']} | {b['reason']}"
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
        prev_date = resolve_prev_trade_date(trade_date)

    journal: dict[str, Any] = {
        "date": trade_date,
        "market": {},
        "candidates": [],
        "sells": [],
        "buys": [],
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
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    ns.func(ns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
