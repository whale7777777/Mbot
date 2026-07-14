# -*- coding: utf-8 -*-
"""连板晋级预测与校准的共享逻辑。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from lianban_jinji_weekly import (
    is_st_or_delist,
    pick_col,
)


@dataclass
class CalibratedModel:
    """滚动回测后写入的配置。"""

    lookback_pairs: int = 20
    min_boards: int = 2
    laplace_alpha: float = 1.0
    shrink_to_overall: float = 0.15
    board_rates: dict[int, float] = field(default_factory=dict)
    overall_ge2_rate: float | None = None
    factor_multipliers: dict[str, float] = field(default_factory=dict)
    backtest_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "lookback_pairs": self.lookback_pairs,
            "min_boards": self.min_boards,
            "laplace_alpha": self.laplace_alpha,
            "shrink_to_overall": self.shrink_to_overall,
            "board_rates": {str(k): v for k, v in self.board_rates.items()},
            "overall_ge2_rate": self.overall_ge2_rate,
            "factor_multipliers": self.factor_multipliers,
            "backtest_summary": self.backtest_summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CalibratedModel:
        return cls(
            lookback_pairs=int(d.get("lookback_pairs", 20)),
            min_boards=int(d.get("min_boards", 2)),
            laplace_alpha=float(d.get("laplace_alpha", 1.0)),
            shrink_to_overall=float(d.get("shrink_to_overall", 0.15)),
            board_rates={int(k): float(v) for k, v in d.get("board_rates", {}).items()},
            overall_ge2_rate=d.get("overall_ge2_rate"),
            factor_multipliers={
                str(k): float(v) for k, v in d.get("factor_multipliers", {}).items()
            },
            backtest_summary=d.get("backtest_summary", {}),
        )


def aggregate_rates_from_pairs(
    pair_stats: list[dict], min_boards: int = 2
) -> tuple[dict[int, dict], dict]:
    agg: dict[int, dict] = defaultdict(lambda: {"promoted": 0, "total": 0})
    ge2_prom, ge2_total = 0, 0
    for stats in pair_stats:
        for n, row in stats["by_boards"].items():
            agg[n]["promoted"] += row["promoted"]
            agg[n]["total"] += row["count_T"]
        ex = stats["overall_excluding_first"]
        ge2_prom += ex["promoted"]
        ge2_total += ex["denom"]
    rates: dict[int, dict] = {}
    for n, v in sorted(agg.items()):
        t, p = v["total"], v["promoted"]
        rates[n] = {"n": n, "total": t, "promoted": p, "rate": (p / t) if t else None}
    overall = (ge2_prom / ge2_total) if ge2_total else None
    return rates, {"promoted": ge2_prom, "total": ge2_total, "rate": overall}


def smooth_rate(
    promoted: int, total: int, prior_rate: float, alpha: float, shrink: float
) -> float:
    if total <= 0:
        return prior_rate
    raw = (promoted + alpha * prior_rate) / (total + alpha)
    return (1 - shrink) * raw + shrink * prior_rate


def build_board_rates(
    rates_raw: dict[int, dict],
    min_boards: int,
    overall_rate: float | None,
    alpha: float,
    shrink: float,
) -> dict[int, float]:
    prior = overall_rate if overall_rate is not None else 0.25
    out: dict[int, float] = {}
    for n, row in rates_raw.items():
        if n < min_boards and min_boards > 1:
            continue
        t, p = row["total"], row["promoted"]
        if t > 0:
            out[n] = smooth_rate(p, t, prior, alpha, shrink)
    if min_boards not in out and min_boards in rates_raw:
        row = rates_raw[min_boards]
        if row["total"] > 0:
            out[min_boards] = smooth_rate(
                row["promoted"], row["total"], prior, alpha, shrink
            )
    return out


def fallback_board_rate(boards: int, board_rates: dict[int, float]) -> float | None:
    if not board_rates:
        return None
    known = sorted(board_rates.keys())
    candidates = [n for n in known if n <= boards]
    if candidates:
        return board_rates[max(candidates)]
    return board_rates[max(known)]


def parse_seal_minutes(val) -> int | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip().replace(":", "")
    if len(s) < 4:
        return None
    try:
        hh, mm = int(s[:2]), int(s[2:4])
    except ValueError:
        return None
    return (hh - 9) * 60 + (mm - 30)


# 因子键（用于学习乘子与 predict_prob）
FACTOR_KEYS = (
    "early_seal",
    "zero_zhaban",
    "heavy_seal",
    "big_order_seal",
    "weak_to_strong",
)

FACTOR_LABELS: dict[str, str] = {
    "early_seal": "10:30前封板",
    "zero_zhaban": "炸板=0",
    "heavy_seal": "封单/成交额≥20%",
    "big_order_seal": "封单/成交额≥12%",
    "weak_to_strong": "弱转强",
}

# 封板资金相对当日成交额（个股口径，非固定金额）
SEAL_TURNOVER_RATIO_BIG = 0.12
SEAL_TURNOVER_RATIO_HEAVY = 0.20
# 弱转强：盘中炸板≥1 次，或最后封板较首次封板晚≥5分钟（开板后再封）
WEAK_TO_STRONG_RESEAL_GAP = 5


def seal_turnover_ratio(
    seal_amt: float | None, turnover: float | None
) -> float | None:
    """封板资金 / 当日成交额。"""
    if seal_amt is None or turnover is None:
        return None
    if turnover <= 0:
        return None
    return float(seal_amt) / float(turnover)


def is_big_order_seal(seal_amt: float | None, turnover: float | None) -> bool:
    ratio = seal_turnover_ratio(seal_amt, turnover)
    if ratio is None:
        return False
    return SEAL_TURNOVER_RATIO_BIG <= ratio < SEAL_TURNOVER_RATIO_HEAVY


def is_heavy_seal(seal_amt: float | None, turnover: float | None) -> bool:
    ratio = seal_turnover_ratio(seal_amt, turnover)
    return ratio is not None and ratio >= SEAL_TURNOVER_RATIO_HEAVY


def is_weak_to_strong(
    zhaban,
    first_seal_min: int | None,
    last_seal_min: int | None,
) -> bool:
    z = pd.to_numeric(zhaban, errors="coerce")
    if pd.notna(z) and int(z) >= 1:
        return True
    if (
        first_seal_min is not None
        and last_seal_min is not None
        and last_seal_min - first_seal_min >= WEAK_TO_STRONG_RESEAL_GAP
    ):
        return True
    return False


def row_factors(row: pd.Series) -> dict[str, bool]:
    zhaban = pd.to_numeric(row.get("_zhaban"), errors="coerce")
    seal_min = row.get("_seal_minutes")
    last_seal_min = row.get("_last_seal_minutes")
    seal_amt = pd.to_numeric(row.get("_seal_amount"), errors="coerce")
    turnover = pd.to_numeric(row.get("_turnover"), errors="coerce")
    return {
        "early_seal": seal_min is not None and seal_min <= 60,
        "zero_zhaban": pd.notna(zhaban) and int(zhaban) == 0,
        "heavy_seal": is_heavy_seal(
            float(seal_amt) if pd.notna(seal_amt) else None,
            float(turnover) if pd.notna(turnover) else None,
        ),
        "big_order_seal": is_big_order_seal(
            float(seal_amt) if pd.notna(seal_amt) else None,
            float(turnover) if pd.notna(turnover) else None,
        ),
        "weak_to_strong": is_weak_to_strong(zhaban, seal_min, last_seal_min),
    }


def format_seal_ratio_pct(seal_amt: float | None, turnover: float | None) -> str:
    ratio = seal_turnover_ratio(seal_amt, turnover)
    if ratio is None:
        return "-"
    return f"{100.0 * ratio:.1f}%"


def factors_to_display(factors: dict[str, bool]) -> dict[str, str]:
    """列表展示用：是/否。"""
    heavy = factors.get("heavy_seal")
    big = factors.get("big_order_seal")
    if heavy:
        seal_flag = "强封"
    elif big:
        seal_flag = "是"
    else:
        seal_flag = "否"
    return {
        "大单封板": seal_flag,
        "弱转强": "是" if factors.get("weak_to_strong") else "否",
    }


def predict_prob(
    boards: int,
    model: CalibratedModel,
    factors: dict[str, bool] | None = None,
) -> float | None:
    p = model.board_rates.get(boards)
    if p is None:
        p = fallback_board_rate(boards, model.board_rates)
    if p is None:
        p = model.overall_ge2_rate
    if p is None:
        return None
    if factors and model.factor_multipliers:
        mult = 1.0
        for name, active in factors.items():
            if active and name in model.factor_multipliers:
                mult *= model.factor_multipliers[name]
        p = min(max(p * mult, 0.02), 0.92)
    return p


def lianban_rows_from_raw(raw: pd.DataFrame, min_boards: int) -> pd.DataFrame:
    c_code = pick_col(raw, "代码", "股票代码")
    c_name = pick_col(raw, "名称", "股票名称")
    c_lb = pick_col(raw, "连板数", "连续涨停天数", "涨停统计")
    if not c_code or not c_lb:
        return pd.DataFrame()
    c_zb = pick_col(raw, "炸板次数")
    c_seal = pick_col(raw, "首次封板时间", "首次涨停时间")
    c_last = pick_col(raw, "最后封板时间")
    c_amt = pick_col(raw, "封板资金")
    c_turn = pick_col(raw, "成交额")
    rows = []
    for _, r in raw.iterrows():
        name = str(r[c_name]) if c_name else ""
        if is_st_or_delist(name):
            continue
        boards = pd.to_numeric(r[c_lb], errors="coerce")
        if pd.isna(boards) or int(boards) < min_boards:
            continue
        boards = int(boards)
        rows.append(
            {
                "code": str(r[c_code]).zfill(6),
                "name": name,
                "boards": boards,
                "_zhaban": pd.to_numeric(r[c_zb], errors="coerce") if c_zb else None,
                "_seal_minutes": parse_seal_minutes(r[c_seal]) if c_seal else None,
                "_last_seal_minutes": parse_seal_minutes(r[c_last]) if c_last else None,
                "_seal_amount": float(r[c_amt]) if c_amt and pd.notna(r[c_amt]) else None,
                "_turnover": float(r[c_turn]) if c_turn and pd.notna(r[c_turn]) else None,
            }
        )
    return pd.DataFrame(rows)


def actual_promoted(code: str, boards: int, df_next: pd.DataFrame) -> bool:
    m = df_next.set_index("code")["boards"]
    if code not in m.index:
        return False
    return int(m.loc[code]) == boards + 1


def learn_factor_multipliers(
    records: list[dict], base_probs: list[float], min_samples: int = 8
) -> dict[str, float]:
    if not records:
        return {}
    base_rate = sum(r["actual"] for r in records) / len(records) or 0.01
    multipliers: dict[str, float] = {}
    for fname in FACTOR_KEYS:
        sub = [r for r in records if r["factors"].get(fname)]
        if len(sub) < min_samples:
            continue
        rate = sum(r["actual"] for r in sub) / len(sub)
        raw_mult = rate / base_rate
        damped = 1.0 + 0.35 * (raw_mult - 1.0)
        multipliers[fname] = round(min(max(damped, 0.92), 1.08), 4)
    return multipliers


def sort_by_board_and_prob(df: pd.DataFrame) -> pd.DataFrame:
    """连板数从高到低；同一板数内按晋级概率从高到低。"""
    if df.empty or "连板数" not in df.columns:
        return df
    out = df.copy()
    out["_prob_sort"] = pd.to_numeric(out["晋级概率"], errors="coerce").fillna(-1.0)
    out = out.sort_values(
        by=["连板数", "_prob_sort"],
        ascending=[False, False],
        kind="mergesort",
    )
    return out.drop(columns=["_prob_sort"]).reset_index(drop=True)


def group_records_by_board(
    records: list[dict], prob_key: str = "晋级概率"
) -> dict[int, list[dict]]:
    """按连板数分组，组内按晋级概率降序。"""
    buckets: dict[int, list[dict]] = {}
    for r in records:
        n = int(r.get("连板数", 0))
        buckets.setdefault(n, []).append(r)
    for n in buckets:
        buckets[n].sort(
            key=lambda x: (
                float(x[prob_key]) if x.get(prob_key) is not None else -1.0
            ),
            reverse=True,
        )
    return dict(sorted(buckets.items(), key=lambda kv: kv[0], reverse=True))


def fit_calibration_grid(
    records: list[dict], board_rates: dict[int, float]
) -> dict[int, float]:
    """按档微调；样本充足时优先用实证率覆盖（避免 ratio 在短窗口上过拟合）。"""
    if not records:
        return board_rates
    adjusted = dict(board_rates)
    for n in sorted({r["boards"] for r in records}):
        sub = [r for r in records if r["boards"] == n]
        if len(sub) < 5:
            continue
        actual = sum(r["actual"] for r in sub) / len(sub)
        if len(sub) >= 10:
            adjusted[n] = min(max(actual, 0.05), 0.90)
            continue
        pred = sum(r.get("pred_base", r.get("pred", 0)) for r in sub) / len(sub)
        if pred <= 0:
            continue
        ratio = min(max(actual / pred, 0.90), 1.10)
        if n in adjusted:
            adjusted[n] = min(max(adjusted[n] * ratio, 0.05), 0.90)
    return adjusted


def fit_board_rates_from_stock_records(
    records: list[dict],
    min_boards: int,
    alpha: float,
    shrink: float,
) -> tuple[dict[int, float], float]:
    """由逐股 T→T+1 记录直接估计分层率（推荐用于短窗口校准）。"""
    if not records:
        return {}, 0.25
    p_all = sum(r["actual"] for r in records) / len(records)
    agg: dict[int, dict] = defaultdict(lambda: {"p": 0, "t": 0})
    for r in records:
        n = int(r.get("boards", 0))
        if n < min_boards:
            continue
        agg[n]["p"] += int(r.get("actual", 0))
        agg[n]["t"] += 1
    rates: dict[int, float] = {}
    for n, v in sorted(agg.items()):
        t, p = v["t"], v["p"]
        if t <= 0:
            continue
        if t >= 12:
            rates[n] = (p + 0.5) / (t + 1.0)
        else:
            rates[n] = smooth_rate(p, t, p_all, alpha, shrink)
    return rates, p_all
