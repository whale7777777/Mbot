# -*- coding: utf-8 -*-
"""
用「每日/」全部已落盘数据核对 T→T+1 实际晋级，滚动重估晋级概率并写回校准文件。

输出：
- docs/03-智能策略/连板数据/晋级概率校准分析_15d.md
- docs/03-智能策略/连板数据/连板预测校准.json（更新）
- docs/03-智能策略/连板数据/连板滚动验证_records.json
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lianban_lib import (
    FACTOR_KEYS,
    FACTOR_LABELS,
    CalibratedModel,
    parse_seal_minutes,
    predict_prob,
    smooth_rate,
)
from lianban_paths import (
    BACKTEST_DETAIL_MD,
    BACKTEST_MD,
    CALIB_JSON,
    DAILY_DIR,
    DOC_DIR,
    ensure_doc_dir,
)

OUT_MD = DOC_DIR / "晋级概率校准分析_15d.md"
VALIDATION_JSON = DOC_DIR / "连板滚动验证_records.json"


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


def _pct_value(value) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).strip().rstrip("%")) / 100.0
    except ValueError:
        return None


def local_row_factors(row: dict) -> dict[str, bool]:
    """从每日 JSON 的稳定展示字段还原因子，避免校准时在线重拉行情。"""
    first_seal = parse_seal_minutes(row.get("首次封板时间"))
    seal_ratio = _pct_value(row.get("封单占比"))
    zhaban = row.get("炸板次数")
    try:
        zero_zhaban = int(zhaban) == 0
    except (TypeError, ValueError):
        zero_zhaban = False
    return {
        "early_seal": first_seal is not None and first_seal <= 60,
        "zero_zhaban": zero_zhaban,
        "heavy_seal": seal_ratio is not None and seal_ratio >= 0.20,
        "big_order_seal": (
            seal_ratio is not None and 0.12 <= seal_ratio < 0.20
        ),
        "weak_to_strong": str(row.get("弱转强", "")).strip() == "是",
    }


def build_verification_records(days: list[str], min_boards: int = 2) -> list[dict]:
    """仅使用相邻每日 JSON 构造逐股验证记录。"""
    records: list[dict] = []
    for i in range(len(days) - 1):
        t, t1 = days[i], days[i + 1]
        day_t = load_day_json(t)
        day_t1 = load_day_json(t1)
        if not day_t or not day_t1:
            continue
        next_boards = {
            str(row.get("代码", "")).zfill(6): int(row.get("连板数", 0))
            for row in day_t1.get("stocks", [])
        }
        for row in day_t.get("stocks", []):
            boards = int(row.get("连板数", 0))
            if boards < min_boards:
                continue
            code = str(row.get("代码", "")).zfill(6)
            factors = local_row_factors(row)
            records.append(
                {
                    "T": t,
                    "T1": t1,
                    "code": code,
                    "name": str(row.get("名称", "")),
                    "boards": boards,
                    "pred": stock_pred(row),
                    "actual": int(next_boards.get(code) == boards + 1),
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


def fit_model(
    records: list[dict],
    min_boards: int,
    alpha: float,
    shrink: float,
    lookback_pairs: int,
) -> CalibratedModel:
    board_rates, p_all = fit_board_rates_empirical(
        records, min_boards, alpha, shrink
    )
    return CalibratedModel(
        lookback_pairs=lookback_pairs,
        min_boards=min_boards,
        laplace_alpha=alpha,
        shrink_to_overall=shrink,
        board_rates=board_rates,
        overall_ge2_rate=p_all,
        factor_multipliers=fit_factor_multipliers_empirical(
            records, min_samples=6
        ),
    )


def walk_forward_predictions(
    records: list[dict],
    min_boards: int,
    alpha: float,
    shrink: float,
    min_history_pairs: int = 5,
) -> list[dict]:
    """每个 T 日只用更早日期拟合，返回无未来数据泄漏的样本外预测。"""
    pair_keys = list(dict.fromkeys((r["T"], r["T1"]) for r in records))
    out: list[dict] = []
    for idx, (t, t1) in enumerate(pair_keys):
        if idx < min_history_pairs:
            continue
        train = [r for r in records if r["T1"] <= t]
        current = [r for r in records if r["T"] == t and r["T1"] == t1]
        if not train or not current:
            continue
        model = fit_model(
            train,
            min_boards,
            alpha,
            shrink,
            lookback_pairs=idx,
        )
        for row in current:
            pred = predict_prob(row["boards"], model, row["factors"])
            if pred is not None:
                out.append(
                    {
                        **row,
                        "pred_rolling": pred,
                        "pred_base": row.get("pred"),
                        "pred_final": pred,
                    }
                )
    return out


def build_report(
    days: list[str],
    records: list[dict],
    new_model: CalibratedModel,
    rolling_records: list[dict],
) -> str:
    evaluated_pairs = list(
        dict.fromkeys((r["T"], r["T1"]) for r in rolling_records)
    )
    lines = [
        "# 晋级概率校准分析（全量每日文档滚动实证）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 交易日：{days[0]} ~ {days[-1]}（{len(days)} 日）",
        f"- 可验证相邻日对：{len(days) - 1} 对",
        f"- 全量逐股样本（T 日连板≥{new_model.min_boards}）：**{len(records)}** 只",
        f"- 滚动样本外验证：{len(evaluated_pairs)} 对、**{len(rolling_records)}** 只",
        "- 数据口径：仅使用 `每日/*.json`；每个验证日只用更早日期拟合，不在线重拉历史行情。",
        "",
        "## 1. 滚动样本外表现",
        "",
        f"- 实证晋级率：{100 * sum(r['actual'] for r in rolling_records) / len(rolling_records):.1f}%",
        f"- 历史文档预测均值：{_mean_pred(rolling_records, 'pred'):.1f}%",
        f"- 新滚动预测均值：{_mean_pred(rolling_records, 'pred_rolling'):.1f}%",
        "",
        "### 按连板高度：预测 vs 实际",
        "",
        "| 连板数 | 样本 | 实际晋级率 | 历史预测均值 | 滚动预测均值 | 偏差(滚动-实际) |",
        "|--------|------|------------|------------|------------|---------------|",
    ]

    for n in sorted({r["boards"] for r in rolling_records}):
        sub = [r for r in rolling_records if r["boards"] == n]
        t = len(sub)
        act = sum(r["actual"] for r in sub) / t if t else 0
        pred_old = _mean_pred(sub, "pred") / 100.0
        pred_new = _mean_pred(sub, "pred_rolling") / 100.0
        lines.append(
            f"| {n} | {t} | {100*act:.1f}% | {100*pred_old:.1f}% | {100*pred_new:.1f}% | "
            f"{100*(pred_new-act):+.1f}pp |"
        )

    b_old = brier(rolling_records, "pred")
    b_new = brier(rolling_records, "pred_rolling")
    lines.extend(
        [
            "",
            "## 2. 样本外拟合优度",
            "",
            "| 指标 | 历史文档模型 | 新滚动模型 |",
            f"|------|--------|------------------------|",
            f"| Brier ↓ | {b_old:.4f} | **{b_new:.4f}** |",
            "",
            "## 3. 全量样本生产参数（已写入校准文件）",
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
            "1. **本地全量**：直接读取全部每日 JSON 的逐股记录，以相邻文档判断是否晋级。",
            "2. **平滑**：仍用拉普拉斯 α=1、向整体率收缩 15%，避免极高板样本过少。",
            "3. **因子**：含大单封板、弱转强等；子样本≥6 时估计乘子，35% 阻尼后限制在 [0.92, 1.08]。",
            "4. **个股概率** = clip(分层率 × 因子乘积, 2%, 92%)。",
            "5. **滚动验证**：前 5 对作为热身期，此后每一对仅使用此前已揭晓结果拟合。",
            "",
            "### 因子口径",
            "",
            "| 因子 | 判定 |",
            "|------|------|",
            "| 大单封板 | 封板资金/成交额 ≥12% 且 <20% |",
            "| 强封单 | 封板资金/成交额 ≥20% |",
            "| 弱转强 | 炸板次数≥1，或最后封板较首次封板晚≥5分钟 |",
            "",
            "重新校准：`python scripts/lianban.py recalibrate`",
            "",
        ]
    )
    return "\n".join(lines)


def build_detail_report(records: list[dict]) -> str:
    lines = [
        "# 连板滚动验证明细",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 共 {len(records)} 条本地文档滚动样本外记录",
        "",
        "| T 日 | T+1 | 代码 | 名称 | 连板 | 历史预测 | 滚动预测 | 实际 |",
        "|------|-----|------|------|------|----------|----------|------|",
    ]
    for row in records:
        old = row.get("pred")
        old_text = f"{100 * old:.1f}%" if old is not None else "-"
        lines.append(
            f"| {row['T']} | {row['T1']} | {row['code']} | {row['name']} | "
            f"{row['boards']} | {old_text} | {100 * row['pred_rolling']:.1f}% | "
            f"{'是' if row['actual'] else '否'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ensure_doc_dir()
    days = list_daily_dates()
    if len(days) < 2:
        print("每日 JSON 不足", file=sys.stderr)
        sys.exit(1)

    print(f"读取 {len(days)} 个本地交易日，{len(days)-1} 对相邻日…")
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

    rolling_records = walk_forward_predictions(
        records,
        min_boards,
        cfg_alpha,
        cfg_shrink,
    )
    if not rolling_records:
        print("滚动验证记录不足", file=sys.stderr)
        sys.exit(3)

    new_model = fit_model(
        records,
        min_boards,
        cfg_alpha,
        cfg_shrink,
        lookback_pairs=len(days) - 1,
    )
    new_model.backtest_summary = {
        "source": "local_documents_walk_forward_recalibration",
        "trade_days": len(days),
        "pair_count": len(days) - 1,
        "stock_samples": len(records),
        "evaluation_stock_samples": len(rolling_records),
        "evaluation_pairs": len(
            {(r["T"], r["T1"]) for r in rolling_records}
        ),
        "actual_promotion_rate": (
            sum(r["actual"] for r in records) / len(records)
        ),
        "evaluation_promotion_rate": (
            sum(r["actual"] for r in rolling_records) / len(rolling_records)
        ),
        "brier_before": brier(rolling_records, "pred"),
        "brier_after": brier(rolling_records, "pred_rolling"),
        "pair_days": days,
        "evaluation_pair_days": list(
            dict.fromkeys(r["T"] for r in rolling_records)
        ),
    }

    CALIB_JSON.write_text(
        json.dumps(new_model.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report = build_report(days, records, new_model, rolling_records)
    OUT_MD.write_text(report, encoding="utf-8")
    BACKTEST_MD.write_text(report, encoding="utf-8")
    BACKTEST_DETAIL_MD.write_text(
        build_detail_report(rolling_records), encoding="utf-8"
    )
    VALIDATION_JSON.write_text(
        json.dumps(rolling_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"样本: {len(records)} 只")
    print(f"滚动样本外验证: {len(rolling_records)} 只")
    print(f"Brier 旧→新: {new_model.backtest_summary['brier_before']:.4f} → {new_model.backtest_summary['brier_after']:.4f}")
    print(f"已写入: {CALIB_JSON}")
    print(f"已写入: {OUT_MD}")
    print(f"已写入: {BACKTEST_MD}")
    print(f"已写入: {BACKTEST_DETAIL_MD}")
    print(f"已写入: {VALIDATION_JSON}")


if __name__ == "__main__":
    main()
