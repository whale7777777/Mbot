# -*- coding: utf-8 -*-
"""
近 N 个交易日连板晋级滚动回测：用 T 日之前历史估计晋级概率，在 T+1 验证。

输出：
- docs/03-智能策略/连板数据/连板预测校准.json
- docs/03-智能策略/连板数据/连板滚动回测.md
- docs/03-智能策略/连板数据/连板滚动验证明细.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

try:
    import akshare as ak  # noqa: F401
except ImportError:
    print("请先安装: pip install akshare pandas", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lianban_data import (
    data_source_label,
    ensure_config_from_example,
    get_open_trade_dates,
    load_lianban_config,
    resolve_tushare_token,
)
from lianban_jinji_weekly import _fmt_rate, fetch_zt_pool, normalize_frame, promotion_for_pair
from lianban_lib import (
    FACTOR_LABELS,
    CalibratedModel,
    actual_promoted,
    aggregate_rates_from_pairs,
    build_board_rates,
    fit_board_rates_from_stock_records,
    fit_calibration_grid,
    learn_factor_multipliers,
    lianban_rows_from_raw,
    predict_prob,
    row_factors,
    smooth_rate,
)

from lianban_paths import (
    BACKTEST_DETAIL_MD,
    BACKTEST_MD,
    CALIB_JSON,
    CONFIG_PATH,
    DOC_DIR,
    ensure_doc_dir,
)

OUT_DIR = DOC_DIR
OUT_MD = BACKTEST_MD
VALIDATION_JSON = DOC_DIR / "连板滚动验证_records.json"


def collect_trading_days(count: int, cfg: dict) -> list[str]:
    return get_open_trade_dates(count=count, cfg=cfg)


def load_pools(days_asc: list[str]) -> dict[str, pd.DataFrame]:
    pools: dict[str, pd.DataFrame] = {}
    for ds in days_asc:
        raw = fetch_zt_pool(ds)
        if raw is None:
            continue
        norm = normalize_frame(raw)
        if norm is not None:
            pools[ds] = norm
    return pools


def walk_forward_backtest(
    days_asc: list[str],
    pools: dict[str, pd.DataFrame],
    lookback_pairs: int,
    min_boards: int,
    validate_pairs: int,
    alpha: float,
    shrink: float,
) -> tuple[list[dict], list[dict], CalibratedModel]:
    """
    对最近 validate_pairs 个 (T,T+1) 做滚动验证。
    返回：逐股记录、逐日汇总、用于生产的校准模型（用全样本+验证结论）。
    """
    stock_records: list[dict] = []
    daily_summary: list[dict] = []

    n_days = len(days_asc)
    if n_days < lookback_pairs + validate_pairs + 1:
        raise ValueError("交易日数量不足")

    # 可验证的 T 日索引：需要之前 lookback_pairs 对 + 之后 T+1
    start_idx = lookback_pairs
    end_idx = n_days - 1
    validate_indices = list(range(end_idx - validate_pairs, end_idx))
    if validate_indices[0] < start_idx:
        validate_indices = list(range(start_idx, end_idx))

    for t_idx in validate_indices:
        t_day = days_asc[t_idx]
        t1_day = days_asc[t_idx + 1]
        if t_day not in pools or t1_day not in pools:
            continue

        hist_end = t_idx
        hist_start = max(0, hist_end - lookback_pairs)
        pair_stats = []
        for j in range(hist_start, hist_end):
            d0, d1 = days_asc[j], days_asc[j + 1]
            if d0 not in pools or d1 not in pools:
                continue
            pair_stats.append(promotion_for_pair(pools[d0], pools[d1]))

        rates_raw, overall = aggregate_rates_from_pairs(pair_stats, min_boards)
        prior = overall["rate"] if overall["rate"] is not None else 0.25
        board_rates = build_board_rates(
            rates_raw, min_boards, prior, alpha, shrink
        )
        model_tmp = CalibratedModel(
            lookback_pairs=lookback_pairs,
            min_boards=min_boards,
            laplace_alpha=alpha,
            shrink_to_overall=shrink,
            board_rates=board_rates,
            overall_ge2_rate=prior,
        )

        raw_t = fetch_zt_pool(t_day)
        if raw_t is None:
            continue
        lb = lianban_rows_from_raw(raw_t, min_boards)
        df_next = pools[t1_day]

        day_preds, day_actuals = [], []
        for _, row in lb.iterrows():
            factors = row_factors(row)
            p_base = predict_prob(int(row["boards"]), model_tmp, None) or prior
            p_old = p_base
            p_new = predict_prob(int(row["boards"]), model_tmp, factors) or p_base
            act = actual_promoted(row["code"], int(row["boards"]), df_next)
            stock_records.append(
                {
                    "T": t_day,
                    "T1": t1_day,
                    "code": row["code"],
                    "name": row["name"],
                    "boards": int(row["boards"]),
                    "pred_base": p_old,
                    "pred_v1": p_new,
                    "actual": int(act),
                    "factors": factors,
                }
            )
            day_preds.append(p_old)
            day_actuals.append(act)

        if day_preds:
            brier = sum((p - a) ** 2 for p, a in zip(day_preds, day_actuals)) / len(
                day_preds
            )
            hit50 = sum((p >= 0.5) == a for p, a in zip(day_preds, day_actuals)) / len(
                day_preds
            )
            daily_summary.append(
                {
                    "T": t_day,
                    "T1": t1_day,
                    "n": len(day_preds),
                    "promoted": sum(day_actuals),
                    "rate": sum(day_actuals) / len(day_actuals),
                    "brier_base": brier,
                    "acc_threshold_50": hit50,
                }
            )

    # 全历史（截至最新日）拟合生产模型 + 因子乘子
    all_pair_stats = []
    for j in range(len(days_asc) - 1):
        d0, d1 = days_asc[j], days_asc[j + 1]
        if d0 in pools and d1 in pools:
            all_pair_stats.append(promotion_for_pair(pools[d0], pools[d1]))

    rates_raw, overall = aggregate_rates_from_pairs(all_pair_stats, min_boards)
    prior = overall["rate"] if overall["rate"] is not None else 0.25
    board_rates = build_board_rates(rates_raw, min_boards, prior, alpha, shrink)

    # 优先用验证窗逐股实证分层率（短窗口更准确）
    if stock_records:
        board_rates, prior = fit_board_rates_from_stock_records(
            stock_records, min_boards, alpha, shrink
        )

    factor_mult = learn_factor_multipliers(
        stock_records, [r["pred_base"] for r in stock_records], min_samples=6
    )
    model_prod = CalibratedModel(
        lookback_pairs=lookback_pairs,
        min_boards=min_boards,
        laplace_alpha=alpha,
        shrink_to_overall=shrink,
        board_rates=board_rates,
        overall_ge2_rate=prior,
        factor_multipliers=factor_mult,
    )

    for r in stock_records:
        r["pred_calibrated"] = predict_prob(
            r["boards"], model_prod, r["factors"]
        )

    board_rates_adj = fit_calibration_grid(stock_records, model_prod.board_rates)
    model_prod.board_rates = board_rates_adj

    for r in stock_records:
        r["pred_final"] = predict_prob(r["boards"], model_prod, r["factors"])

    base_brier = _mean_brier([r["pred_base"] for r in stock_records], stock_records)
    final_brier = _mean_brier([r["pred_final"] for r in stock_records], stock_records)

    model_prod.backtest_summary = {
        "validate_pairs": len(daily_summary),
        "stock_samples": len(stock_records),
        "actual_promotion_rate": (
            sum(r["actual"] for r in stock_records) / len(stock_records)
            if stock_records
            else None
        ),
        "brier_baseline_board_only": base_brier,
        "brier_calibrated": final_brier,
        "acc_at_50_baseline": _acc_at_threshold(stock_records, "pred_base", 0.5),
        "acc_at_50_calibrated": _acc_at_threshold(stock_records, "pred_final", 0.5),
        "daily": daily_summary,
    }

    return stock_records, daily_summary, model_prod


def _mean_brier(preds: list[float], records: list[dict]) -> float | None:
    if not preds:
        return None
    return sum((p - r["actual"]) ** 2 for p, r in zip(preds, records)) / len(preds)


def _acc_at_threshold(records: list[dict], key: str, th: float) -> float | None:
    if not records:
        return None
    ok = sum((r[key] >= th) == r["actual"] for r in records if r.get(key) is not None)
    return ok / len(records)


def build_markdown(
    days_asc: list[str],
    records: list[dict],
    daily: list[dict],
    model: CalibratedModel,
    cfg: dict,
) -> str:
    s = model.backtest_summary
    lines = [
        "# 连板晋级预测 · 滚动回测",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 数据源：`{data_source_label(cfg)}`",
        f"- 交易日范围：{days_asc[0]} ~ {days_asc[-1]}（共 {len(days_asc)} 个交易日）",
        f"- 滚动验证：最近 {s.get('validate_pairs', '-')} 对相邻日（数据源可用范围内尽可能多），"
        f"预测仅用 T 日前 {model.lookback_pairs} 对历史",
        f"- 样本：连板数 ≥ {model.min_boards}，剔除 ST/*ST/退市",
        "",
        "## 回测指标",
        "",
        "| 指标 | 分层率（旧） | 校准后 |",
        "|------|--------------|--------|",
        f"| Brier 分数（越低越好） | {s.get('brier_baseline_board_only', 0):.4f} | {s.get('brier_calibrated', 0):.4f} |",
        f"| 阈值0.5命中率 | {_fmt_rate(s.get('acc_at_50_baseline'))} | {_fmt_rate(s.get('acc_at_50_calibrated'))} |",
        f"| 验证样本数 | {s.get('stock_samples', 0)} 只股票 | |",
        f"| 实证晋级率 | {_fmt_rate(s.get('actual_promotion_rate'))} | |",
        "",
        "## 校准后分层晋级概率（用于 `lianban_today.py`）",
        "",
        "| 连板数 n | 校准晋级概率 |",
        "|----------|--------------|",
    ]
    for n in sorted(model.board_rates):
        lines.append(f"| {n} | {_fmt_rate(model.board_rates[n])} |")

    lines.extend(
        [
            "",
            "## 因子乘子（相对基准）",
            "",
            "| 因子 | 含义 | 乘子 |",
            "|------|------|------|",
        ]
    )
    for k, v in model.factor_multipliers.items():
        lines.append(f"| {k} | {FACTOR_LABELS.get(k, k)} | {v:.3f} |")
    if not model.factor_multipliers:
        lines.append("| - | 样本不足未启用 | - |")

    lines.extend(
        [
            "",
            f"- 拉普拉斯 α={model.laplace_alpha}，向整体率收缩权重={model.shrink_to_overall}",
            f"- 整体连板(≥{model.min_boards})先验：{_fmt_rate(model.overall_ge2_rate)}",
            "",
            "## 逐日验证摘要",
            "",
            "| T 日 | T+1 | 样本数 | 实证晋级 | Brier(旧) | 0.5命中(旧) |",
            "|------|-----|--------|----------|-----------|-------------|",
        ]
    )
    for d in daily:
        lines.append(
            f"| {d['T']} | {d['T1']} | {d['n']} | {_fmt_rate(d['rate'])} "
            f"({d['promoted']}/{d['n']}) | {d['brier_base']:.4f} | {_fmt_rate(d['acc_threshold_50'])} |"
        )

    lines.extend(["", "## 最近验证日个股明细（预测 vs 实际）", ""])
    if records:
        last_t = records[-1]["T"]
        sub = [r for r in records if r["T"] == last_t]
        lines.append(f"### {last_t} → {sub[0]['T1'] if sub else ''}")
        lines.append("")
        lines.append("| 代码 | 名称 | 连板 | 预测(旧) | 预测(校准) | 实际晋级 |")
        lines.append("|------|------|------|----------|------------|----------|")
        for r in sorted(sub, key=lambda x: (-x["boards"], x["code"])):
            lines.append(
                f"| {r['code']} | {r['name']} | {r['boards']} | "
                f"{_fmt_rate(r['pred_base'])} | {_fmt_rate(r['pred_final'])} | "
                f"{'是' if r['actual'] else '否'} |"
            )

    lines.extend(
        [
            "",
            "## 使用",
            "",
            "1. 配置 TuShare：复制 `lianban_config.example.json` → `lianban_config.json`，填入 token",
            "2. 回测并更新校准：`python scripts/lianban_backtest_30d.py`",
            "3. 当日报告（自动读校准）：`python scripts/lianban_today.py`",
            "",
        ]
    )
    return "\n".join(lines)


def build_detail_markdown(records: list[dict]) -> str:
    lines = [
        "# 连板滚动验证明细",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 共 {len(records)} 条（T 日连板股 → T+1 是否晋级）",
        "",
        "| T 日 | T+1 | 代码 | 名称 | 连板 | 预测(旧) | 预测(校准) | 实际 |",
        "|------|-----|------|------|------|----------|------------|------|",
    ]
    for r in records:
        lines.append(
            f"| {r['T']} | {r['T1']} | {r['code']} | {r['name']} | {r['boards']} | "
            f"{_fmt_rate(r['pred_base'])} | {_fmt_rate(r['pred_final'])} | "
            f"{'是' if r['actual'] else '否'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40, help="目标交易日数量（受数据源可用天数限制）")
    ap.add_argument(
        "--validate-pairs",
        type=int,
        default=0,
        help="滚动验证对数，0=自动（全部可用验证日）",
    )
    ap.add_argument("--lookback", type=int, default=10, help="估计概率的历史对数")
    ap.add_argument("--min-boards", type=int, default=2)
    ap.add_argument("--alpha", type=float, default=1.0, help="拉普拉斯平滑强度")
    ap.add_argument("--shrink", type=float, default=0.15, help="向整体率收缩")
    args = ap.parse_args()

    cfg = load_lianban_config()
    if not CONFIG_PATH.is_file():
        try:
            p = ensure_config_from_example()
            print(f"已生成配置模板: {p}，请填入 tushare_token 后重跑。", file=sys.stderr)
        except FileNotFoundError:
            pass

    days = collect_trading_days(args.days, cfg)
    src = data_source_label(cfg)
    token_ok = bool(resolve_tushare_token(cfg)) if cfg.get("data_source") == "tushare" else False

    min_need = args.lookback + 2
    if len(days) < min_need:
        if len(days) < 5:
            hint = (
                "请配置 TuShare token（见 docs/03-智能策略/连板策略.md）。"
                if cfg.get("data_source") == "tushare" and not token_ok
                else "请检查网络或 data_source 配置。"
            )
            print(f"交易日不足：仅 {len(days)} 日。{hint}", file=sys.stderr)
            sys.exit(2)
        args.lookback = max(5, len(days) - 3)
        print(
            f"警告：仅 {len(days)} 个交易日，lookback 自动降为 {args.lookback}。"
            "配置 TuShare token 后可跑满 30 日。",
            file=sys.stderr,
        )

    validate_pairs = args.validate_pairs
    if validate_pairs <= 0:
        validate_pairs = max(1, len(days) - args.lookback - 1)
    validate_pairs = min(validate_pairs, max(1, len(days) - args.lookback - 1))

    print(f"数据源: {src}，加载 {len(days)} 个交易日，滚动验证 {validate_pairs} 对…")
    pools = load_pools(days)

    records, daily, model = walk_forward_backtest(
        days,
        pools,
        args.lookback,
        args.min_boards,
        validate_pairs,
        args.alpha,
        args.shrink,
    )

    ensure_doc_dir()
    CALIB_JSON.write_text(
        json.dumps(model.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    OUT_MD.write_text(build_markdown(days, records, daily, model, cfg), encoding="utf-8")
    BACKTEST_DETAIL_MD.write_text(build_detail_markdown(records), encoding="utf-8")
    VALIDATION_JSON.write_text(
        json.dumps(records, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    s = model.backtest_summary
    print(f"验证样本: {s.get('stock_samples')} 只")
    print(f"Brier 旧→新: {s.get('brier_baseline_board_only'):.4f} → {s.get('brier_calibrated'):.4f}")
    print(f"已写入: {CALIB_JSON}")
    print(f"已写入: {OUT_MD}")
    print(f"已写入: {BACKTEST_DETAIL_MD}")


if __name__ == "__main__":
    main()
