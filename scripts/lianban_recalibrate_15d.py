# -*- coding: utf-8 -*-
"""
用「每日/」已落盘数据 + 涨停池核对 T→T+1 实际晋级，重估晋级概率并写回校准文件。

输出：
- docs/03-智能策略/连板数据/晋级概率校准分析_15d.md
- docs/03-智能策略/连板数据/连板预测校准.json（更新）
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lianban_jinji_weekly import fetch_zt_pool, normalize_frame
from lianban_lib import (
    FACTOR_KEYS,
    FACTOR_LABELS,
    CalibratedModel,
    actual_promoted,
    lianban_rows_from_raw,
    predict_prob,
    row_factors,
    smooth_rate,
)
from lianban_paths import CALIB_JSON, DAILY_DIR, DOC_DIR, ensure_doc_dir

OUT_MD = DOC_DIR / "晋级概率校准分析_15d.md"


def list_daily_dates() -> list[str]:
    dates = []
    for p in DAILY_DIR.glob("*.json"):
        if p.stem.isdigit() and len(p.stem) == 8:
            dates.append(p.stem)
    return sorted(dates)


def load_day_json(date: str) -> dict | None:
    p = DAILY_DIR / f"{date}.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def stock_pred(row: dict) -> float | None:
    if row.get("晋级概率") is not None:
        return float(row["晋级概率"])
    pct = row.get("晋级概率_pct")
    if pct is not None:
        return float(pct) / 100.0
    return None


def build_verification_records(days: list[str], min_boards: int = 2) -> list[dict]:
    records: list[dict] = []
    pools: dict[str, pd.DataFrame] = {}
    raw_pools: dict[str, pd.DataFrame] = {}

    for ds in days:
        raw = fetch_zt_pool(ds)
        if raw is None:
            continue
        raw_pools[ds] = raw
        norm = normalize_frame(raw)
        if norm is not None:
            pools[ds] = norm

    old_model = None
    if CALIB_JSON.is_file():
        old_model = CalibratedModel.from_dict(
            json.loads(CALIB_JSON.read_text(encoding="utf-8"))
        )

    for i in range(len(days) - 1):
        t, t1 = days[i], days[i + 1]
        if t not in pools or t1 not in pools or t not in raw_pools:
            continue
        lb = lianban_rows_from_raw(raw_pools[t], min_boards)
        if lb.empty:
            continue
        df_next = pools[t1]

        for _, row in lb.iterrows():
            factors = row_factors(row)
            pred = None
            if old_model:
                pred = predict_prob(int(row["boards"]), old_model, factors)
            act = actual_promoted(row["code"], int(row["boards"]), df_next)
            records.append(
                {
                    "T": t,
                    "T1": t1,
                    "code": row["code"],
                    "name": row["name"],
                    "boards": int(row["boards"]),
                    "pred": pred,
                    "actual": int(act),
                    "factors": factors,
                }
            )
    return records


def _mean_pred(records: list[dict], key: str) -> float:
    xs = [r[key] for r in records if r.get(key) is not None]
    return 100 * sum(xs) / len(xs) if xs else 0.0


def brier(records: list[dict], key: str = "pred") -> float:
    xs = [r for r in records if r.get(key) is not None]
    if not xs:
        return 0.0
    return sum((r[key] - r["actual"]) ** 2 for r in xs) / len(xs)


def fit_board_rates_empirical(
    records: list[dict],
    min_boards: int,
    alpha: float,
    shrink: float,
) -> tuple[dict[int, float], float]:
    """用 15 日逐股样本估计各档晋级率（非长历史分层表）。"""
    overall_act = sum(r["actual"] for r in records)
    overall_n = len(records)
    p_all = overall_act / overall_n if overall_n else 0.25

    agg: dict[int, dict] = defaultdict(lambda: {"p": 0, "t": 0})
    for r in records:
        n = r["boards"]
        if n < min_boards:
            continue
        agg[n]["p"] += r["actual"]
        agg[n]["t"] += 1

    rates: dict[int, float] = {}
    for n in sorted(agg):
        t, p = agg[n]["t"], agg[n]["p"]
        if t > 0:
            rates[n] = smooth_rate(p, t, p_all, alpha, shrink)
    return rates, p_all


def fit_factor_multipliers_empirical(
    records: list[dict],
    min_samples: int = 6,
) -> dict[str, float]:
    if not records:
        return {}
    base = sum(r["actual"] for r in records) / len(records)
    if base <= 0:
        base = 0.01
    mult: dict[str, float] = {}
    for fname in FACTOR_KEYS:
        sub = [r for r in records if r["factors"].get(fname)]
        if len(sub) < min_samples:
            continue
        rate = sum(r["actual"] for r in sub) / len(sub)
        raw_mult = rate / base
        damped = 1.0 + 0.35 * (raw_mult - 1.0)
        mult[fname] = round(min(max(damped, 0.92), 1.08), 4)
    return mult


def apply_model(records: list[dict], model: CalibratedModel) -> list[dict]:
    from lianban_lib import predict_prob

    out = []
    for r in records:
        p = predict_prob(r["boards"], model, r["factors"])
        out.append({**r, "pred_new": p if p is not None else r["pred"]})
    return out


def build_report(
    days: list[str],
    records: list[dict],
    old_model: CalibratedModel | None,
    new_model: CalibratedModel,
    records_new: list[dict],
) -> str:
    lines = [
        "# 晋级概率校准分析（近 15 交易日实证）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 交易日：{days[0]} ~ {days[-1]}（{len(days)} 日）",
        f"- 可验证相邻日对：{len(days) - 1} 对",
        f"- 逐股样本（T 日连板≥2，有预测值）：**{len(records)}** 只",
        "",
        "## 1. 实际晋级率（15 日逐股汇总）",
        "",
        f"- 实证晋级率：{100 * sum(r['actual'] for r in records) / len(records):.1f}%",
        f"- 预测均值（旧）：{_mean_pred(records, 'pred'):.1f}%",
        f"- 预测均值（新）：{100 * sum(r['pred_new'] for r in records_new) / len(records_new):.1f}%",
        "",
        "### 按连板高度：预测 vs 实际",
        "",
        "| 连板数 | 样本 | 实际晋级率 | 旧预测均值 | 新预测均值 | 偏差(新-实际) |",
        "|--------|------|------------|------------|------------|---------------|",
    ]

    for n in sorted({r["boards"] for r in records}):
        sub = [r for r in records if r["boards"] == n]
        sub_new = [r for r in records_new if r["boards"] == n]
        t = len(sub)
        act = sum(r["actual"] for r in sub) / t if t else 0
        pred_old = _mean_pred(sub, "pred") / 100.0
        pred_new = _mean_pred(sub_new, "pred_new") / 100.0
        lines.append(
            f"| {n} | {t} | {100*act:.1f}% | {100*pred_old:.1f}% | {100*pred_new:.1f}% | "
            f"{100*(pred_new-act):+.1f}pp |"
        )

    b_old = brier(records, "pred")
    b_new = brier(records_new, "pred_new")
    lines.extend(
        [
            "",
            "## 2. 拟合优度",
            "",
            f"| 指标 | 旧模型 | 新模型（15日实证重估） |",
            f"|------|--------|------------------------|",
            f"| Brier ↓ | {b_old:.4f} | **{b_new:.4f}** |",
            "",
            "## 3. 新分层晋级概率（已写入校准文件）",
            "",
            "| 连板数 | 校准概率 |",
            "|--------|----------|",
        ]
    )
    for n in sorted(new_model.board_rates):
        lines.append(f"| {n} | {100*new_model.board_rates[n]:.1f}% |")

    lines.extend(["", "### 因子乘子（相对基准）", "", "| 因子 | 乘子 |", "|------|------|"])
    for k, v in new_model.factor_multipliers.items():
        lines.append(f"| {FACTOR_LABELS.get(k, k)} | {v:.3f} |")
    if not new_model.factor_multipliers:
        lines.append("| - | 样本不足未调整 |")

    lines.extend(
        [
            "",
            "## 4. 修正说明",
            "",
            "1. **分层率**：改为用本窗口内「T 日 n 连板 → T+1 是否 n+1」的**逐股样本**估计，",
            "   不再主要依赖更长历史的分层表 + 少量验证日 ratio 微调。",
            "2. **平滑**：仍用拉普拉斯 α=1、向整体率收缩 15%，避免极高板样本过少。",
            "3. **因子**：含大单封板、弱转强等；子样本≥6 时估计乘子，35% 阻尼后限制在 [0.92, 1.08]。",
            "4. **个股概率** = clip(分层率 × 因子乘积, 2%, 92%)。",
            "",
            "### 因子口径",
            "",
            "| 因子 | 判定 |",
            "|------|------|",
            "| 大单封板 | 封板资金/成交额 ≥12% 且 <20% |",
            "| 强封单 | 封板资金/成交额 ≥20% |",
            "| 弱转强 | 炸板次数≥1，或最后封板较首次封板晚≥5分钟 |",
            "",
            "重新生成每日文档：`python scripts/lianban.py batch --days 15`",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    ensure_doc_dir()
    days = list_daily_dates()
    if len(days) < 2:
        print("每日 JSON 不足", file=sys.stderr)
        sys.exit(1)

    print(f"核对 {len(days)} 个交易日，{len(days)-1} 对相邻日…")
    min_boards_pre = 2
    if CALIB_JSON.is_file():
        min_boards_pre = CalibratedModel.from_dict(
            json.loads(CALIB_JSON.read_text(encoding="utf-8"))
        ).min_boards
    records = build_verification_records(days, min_boards=min_boards_pre)
    if not records:
        print("无验证记录", file=sys.stderr)
        sys.exit(2)

    old_model = None
    if CALIB_JSON.is_file():
        old_model = CalibratedModel.from_dict(
            json.loads(CALIB_JSON.read_text(encoding="utf-8"))
        )

    cfg_alpha = old_model.laplace_alpha if old_model else 1.0
    cfg_shrink = old_model.shrink_to_overall if old_model else 0.15
    min_boards = old_model.min_boards if old_model else 2

    board_rates, p_all = fit_board_rates_empirical(
        records, min_boards, cfg_alpha, cfg_shrink
    )
    factor_mult = fit_factor_multipliers_empirical(records, min_samples=6)

    new_model = CalibratedModel(
        lookback_pairs=len(days) - 1,
        min_boards=min_boards,
        laplace_alpha=cfg_alpha,
        shrink_to_overall=cfg_shrink,
        board_rates=board_rates,
        overall_ge2_rate=p_all,
        factor_multipliers=factor_mult,
        backtest_summary={
            "source": "15d_empirical_recalibration",
            "trade_days": len(days),
            "stock_samples": len(records),
            "actual_promotion_rate": sum(r["actual"] for r in records) / len(records),
            "brier_before": brier(records, "pred"),
            "brier_after": None,
            "pair_days": days,
        },
    )

    records_new = apply_model(records, new_model)
    new_model.backtest_summary["brier_after"] = brier(records_new, "pred_new")

    CALIB_JSON.write_text(
        json.dumps(new_model.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    OUT_MD.write_text(
        build_report(days, records, old_model, new_model, records_new),
        encoding="utf-8",
    )

    print(f"样本: {len(records)} 只")
    print(f"Brier 旧→新: {new_model.backtest_summary['brier_before']:.4f} → {new_model.backtest_summary['brier_after']:.4f}")
    print(f"已写入: {CALIB_JSON}")
    print(f"已写入: {OUT_MD}")


if __name__ == "__main__":
    main()
