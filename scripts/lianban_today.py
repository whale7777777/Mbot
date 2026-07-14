# -*- coding: utf-8 -*-
"""
当日涨停池连板股分析（东方财富 / akshare），输出 Markdown + JSON。

- 样本：当日涨停池中连板数 >= --min-boards（默认 2，即不含首板）。
- 晋级概率：近若干相邻交易日对上，同连板高度 n 的「T 日 n 连板 → T+1 仍为 n+1」
  的实证晋级率（与 lianban_jinji_weekly.py 口径一致）；个股取与其当日连板数相同的历史分层率。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

try:
    import akshare as ak
except ImportError:
    print("请先安装: pip install akshare pandas", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lianban_jinji_weekly import (
    _fmt_rate,
    fetch_zt_pool,
    is_st_or_delist,
    pick_col,
    promotion_for_pair,
)
from lianban_lib import (
    CalibratedModel,
    aggregate_rates_from_pairs,
    build_board_rates,
    group_records_by_board,
    predict_prob,
    factors_to_display,
    format_seal_ratio_pct,
    row_factors,
    sort_by_board_and_prob,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lianban_data import load_lianban_config
from lianban_paths import (
    CALIB_JSON,
    daily_json,
    daily_md,
    ensure_daily_dir,
)
from lianban_similar import attach_similar_fields, build_similar_section
from lianban_zt_reason import fetch_zt_reason_map

OUT_DIR = ensure_daily_dir()


def resolve_trade_date(prefer: str | None, max_scan: int) -> str | None:
    """返回用于分析的交易日 YYYYMMDD（优先 prefer，否则从今日起向前找有数据的最近一日）。"""
    if prefer:
        df = fetch_zt_pool(prefer)
        if df is not None and not df.empty:
            return prefer
        return None
    end = datetime.now().date()
    for i in range(0, max_scan + 1):
        d = end - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        df = fetch_zt_pool(ds)
        if df is not None and not df.empty:
            return ds
    return None


def iter_trading_days_before(anchor: str, count: int, max_scan: int) -> list[str]:
    """anchor 及之前的有数据交易日，新到旧，最多 count 个。"""
    anchor_dt = datetime.strptime(anchor, "%Y%m%d").date()
    found: list[str] = []
    for i in range(0, max_scan + 1):
        d = anchor_dt - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        if ds in found:
            continue
        df = fetch_zt_pool(ds)
        if df is not None and not df.empty:
            found.append(ds)
        if len(found) >= count:
            break
    found.sort(reverse=True)
    return found


def load_calibrated_model() -> CalibratedModel | None:
    if not CALIB_JSON.is_file():
        return None
    try:
        data = json.loads(CALIB_JSON.read_text(encoding="utf-8"))
        return CalibratedModel.from_dict(data)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def aggregate_rates_by_board(
    days_new_first: list[str], pairs_limit: int, min_boards: int
) -> tuple[dict[int, dict], list[dict], CalibratedModel | None]:
    """跨多对相邻日汇总；若存在滚动回测校准文件则优先使用。"""
    cached = load_calibrated_model()
    pair_meta: list[dict] = []
    pair_stats: list[dict] = []

    for i in range(len(days_new_first) - 1):
        if len(pair_meta) >= pairs_limit:
            break
        t, t1 = days_new_first[i + 1], days_new_first[i]
        from lianban_jinji_weekly import fetch_zt_pool, normalize_frame

        raw_t, raw_t1 = fetch_zt_pool(t), fetch_zt_pool(t1)
        if raw_t is None or raw_t1 is None:
            continue
        df_t, df_t1 = normalize_frame(raw_t), normalize_frame(raw_t1)
        if df_t is None or df_t1 is None:
            continue
        stats = promotion_for_pair(df_t, df_t1)
        pair_meta.append({"T": t, "T1": t1, "stats": stats})
        pair_stats.append(stats)

    rates_raw, overall = aggregate_rates_from_pairs(pair_stats, min_boards)
    rates_display: dict[int, dict] = {}
    for n, row in rates_raw.items():
        rates_display[n] = row

    if cached and cached.board_rates:
        for n, p in cached.board_rates.items():
            rates_display[n] = {
                "n": n,
                "total": rates_raw.get(n, {}).get("total", 0),
                "promoted": rates_raw.get(n, {}).get("promoted", 0),
                "rate": p,
                "calibrated": True,
            }
        return rates_display, pair_meta, cached

    prior = overall.get("rate") or 0.25
    smoothed = build_board_rates(rates_raw, min_boards, prior, alpha=1.0, shrink=0.15)
    for n, p in smoothed.items():
        rates_display[n] = {
            **rates_raw.get(n, {"n": n, "total": 0, "promoted": 0}),
            "rate": p,
        }
    live = CalibratedModel(
        board_rates=smoothed,
        overall_ge2_rate=prior,
        lookback_pairs=pairs_limit,
        min_boards=min_boards,
    )
    return rates_display, pair_meta, live


def build_today_table(
    raw: pd.DataFrame,
    min_boards: int,
    rates_by_n: dict[int, dict],
    model: CalibratedModel | None,
    reason_map: dict[str, dict[str, str]] | None = None,
) -> pd.DataFrame:
    c_code = pick_col(raw, "代码", "股票代码")
    c_name = pick_col(raw, "名称", "股票名称")
    c_lb = pick_col(raw, "连板数", "连续涨停天数", "涨停统计")
    if not c_code or not c_lb:
        raise ValueError(f"涨停池列缺失，当前列：{list(raw.columns)}")

    col_map = {
        "代码": c_code,
        "名称": c_name,
        "连板数": c_lb,
        "涨跌幅": pick_col(raw, "涨跌幅"),
        "最新价": pick_col(raw, "最新价", "现价"),
        "换手率": pick_col(raw, "换手率"),
        "封板资金": pick_col(raw, "封板资金"),
        "成交额": pick_col(raw, "成交额"),
        "首次封板时间": pick_col(raw, "首次封板时间", "首次涨停时间"),
        "炸板次数": pick_col(raw, "炸板次数"),
        "所属行业": pick_col(raw, "所属行业", "行业"),
    }

    rows = []
    for _, r in raw.iterrows():
        name = str(r[c_name]) if c_name else ""
        if is_st_or_delist(name):
            continue
        boards = pd.to_numeric(r[c_lb], errors="coerce")
        if pd.isna(boards):
            continue
        boards = int(boards)
        if boards < min_boards:
            continue

        from lianban_lib import parse_seal_minutes

        c_seal = pick_col(raw, "首次封板时间", "首次涨停时间")
        c_last = pick_col(raw, "最后封板时间")
        c_amt = pick_col(raw, "封板资金")
        c_turn = pick_col(raw, "成交额")
        c_zb = pick_col(raw, "炸板次数")

        factors_row = {
            "code": str(r[c_code]).zfill(6),
            "boards": boards,
            "_zhaban": pd.to_numeric(r[c_zb], errors="coerce") if c_zb else None,
            "_seal_minutes": parse_seal_minutes(r[c_seal]) if c_seal else None,
            "_last_seal_minutes": parse_seal_minutes(r[c_last]) if c_last else None,
            "_seal_amount": float(r[c_amt]) if c_amt and pd.notna(r[c_amt]) else None,
            "_turnover": float(r[c_turn]) if c_turn and pd.notna(r[c_turn]) else None,
        }
        factors = row_factors(pd.Series(factors_row))
        disp = factors_to_display(factors)
        seal_amt = factors_row["_seal_amount"]
        turnover = factors_row["_turnover"]

        rate_info = rates_by_n.get(boards)
        if model:
            prob = predict_prob(boards, model, factors)
            note = "滚动回测校准"
            if model.factor_multipliers and any(factors.values()):
                note += "+因子乘子"
        else:
            prob = rate_info["rate"] if rate_info else None
            if prob is None and rates_by_n:
                prob = _fallback_rate(boards, rates_by_n, min_boards)
            note = _prob_note(boards, rate_info, prob)

        code = str(r[c_code]).zfill(6)
        zt_reason = "-"
        if reason_map:
            zt_reason = (reason_map.get(code) or {}).get("涨停原因") or "-"

        item = {
            "代码": code,
            "名称": name,
            "涨停原因": zt_reason,
            "连板数": boards,
            "晋级概率": prob,
            "晋级概率说明": note,
            "封单占比": format_seal_ratio_pct(seal_amt, turnover),
            "大单封板": disp["大单封板"],
            "弱转强": disp["弱转强"],
        }
        for label, col in col_map.items():
            if label in item or not col:
                continue
            val = r[col]
            if label == "涨跌幅" and pd.notna(val):
                item[label] = f"{float(val):.2f}%"
            elif label in ("换手率",) and pd.notna(val):
                item[label] = f"{float(val):.2f}%"
            elif label == "封板资金" and pd.notna(val):
                item[label] = _fmt_amount(float(val))
            else:
                item[label] = val if pd.notna(val) else ""
        rows.append(item)

    df = pd.DataFrame(rows)
    return sort_by_board_and_prob(df)


def _parse_amount_for_sort(val) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass
    mult = 1.0
    if s.endswith("亿"):
        mult = 1e8
        s = s[:-1]
    elif s.endswith("万"):
        mult = 1e4
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def _fallback_rate(
    boards: int, rates_by_n: dict[int, dict], min_boards: int
) -> float | None:
    known = [n for n in rates_by_n if rates_by_n[n]["rate"] is not None]
    if not known:
        return None
    # 取不高于当前高度的最近一档
    candidates = [n for n in known if n <= boards]
    if candidates:
        return rates_by_n[max(candidates)]["rate"]
    # 高于历史最高档：用最高档
    return rates_by_n[max(known)]["rate"]


def _prob_note(
    boards: int, rate_info: dict | None, prob: float | None
) -> str:
    if rate_info and rate_info.get("total", 0) > 0:
        return f"近段实证：{boards}连板→{boards + 1}（样本{rate_info['total']}）"
    if prob is not None:
        return f"近段实证：分层样本不足，参考相邻档位"
    return "样本不足"


def _fmt_amount(x: float) -> str:
    if abs(x) >= 1e8:
        return f"{x / 1e8:.2f}亿"
    if abs(x) >= 1e4:
        return f"{x / 1e4:.2f}万"
    return f"{x:.0f}"


def _fmt_prob(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{100.0 * x:.1f}%"


def build_markdown(
    trade_date: str,
    table: pd.DataFrame,
    rates_by_n: dict[int, dict],
    pairs_used: list[dict],
    min_boards: int,
    hist_pairs: int,
    stock_records: list[dict] | None = None,
) -> str:
    records = stock_records
    if records is None and not table.empty:
        records = attach_similar_fields(_stocks_json_records(table))
    ann_by_code: dict[str, str] = {}
    if records:
        ann_by_code = {
            str(r.get("代码", "")).zfill(6): r.get("类似标的") or "-"
            for r in records
        }

    display_cols = [
        "代码",
        "名称",
        "涨停原因",
        "类似标的",
        "连板数",
        "涨跌幅",
        "最新价",
        "换手率",
        "封板资金",
        "封单占比",
        "首次封板时间",
        "炸板次数",
        "大单封板",
        "弱转强",
        "所属行业",
        "晋级概率",
    ]
    lines = [
        f"# 今日连板股票分析（{trade_date}）",
        "",
        f"- 生成时间（本地）：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 数据源：见 `docs/03-智能策略/连板数据/lianban_config.json`（TuShare 优先，可回退东方财富）",
        f"- 个股范围：连板数 ≥ {min_boards}（已剔除 ST / *ST / 退市相关名称）",
        "- **晋级概率**：优先读取 `连板数据/连板预测校准.json`（由 `lianban_backtest_30d.py` 滚动验证生成）；",
        "  含拉普拉斯平滑、向整体率收缩、封板质量因子乘子及分层微调。",
        f"  若无校准文件，则用近 {hist_pairs} 对相邻日分层实证率。",
        "",
        "## 历史分层晋级率（用于填充个股「晋级概率」）",
        "",
        "| T日连板数 n | 累计样本 | 晋级到 n+1 | 晋级概率 |",
        "|-------------|----------|------------|----------|",
    ]
    for n in sorted(rates_by_n):
        row = rates_by_n[n]
        if row["total"] == 0:
            continue
        lines.append(
            f"| {n} | {row['total']} | {row['promoted']} | {_fmt_rate(row['rate'])} |"
        )

    if pairs_used:
        lines.extend(["", "## 近段相邻日核对（摘要）", ""])
        lines.append("| T 日 | T+1 日 | 连板(≥2)晋级率 |")
        lines.append("|------|--------|----------------|")
        for p in reversed(pairs_used):
            ex = p["stats"]["overall_excluding_first"]
            lines.append(
                f"| {p['T']} | {p['T1']} | {_fmt_rate(ex['rate'])} "
                f"({ex['promoted']}/{ex['denom']}) |"
            )

    if records:
        lines.extend(build_similar_section(records))

    lines.extend(
        [
            "## 按连板数分类",
            "",
            "各档位内个股按 **晋级概率从高到低** 排序。",
            "",
        ]
    )
    if table.empty:
        lines.append("_当日无符合筛选条件的连板股。_")
    else:
        table = sort_by_board_and_prob(table)
        hdr = []
        for c in display_cols:
            if c == "连板数":
                continue
            if c == "类似标的":
                if ann_by_code:
                    hdr.append(c)
            elif c in table.columns:
                hdr.append(c)
        for boards in sorted(table["连板数"].unique(), reverse=True):
            sub = table[table["连板数"] == boards].copy()
            sub = sort_by_board_and_prob(sub)
            tier = rates_by_n.get(int(boards), {})
            tier_rate = _fmt_rate(tier.get("rate")) if tier else "-"
            lines.append(f"### {boards} 连板（{len(sub)} 只，档内历史晋级率 {tier_rate}）")
            lines.append("")
            lines.append("| " + " | ".join(hdr) + " |")
            lines.append("| " + " | ".join(["---"] * len(hdr)) + " |")
            for _, row in sub.iterrows():
                cells = []
                code = str(row.get("代码", "")).zfill(6)
                for c in hdr:
                    if c == "晋级概率":
                        cells.append(_fmt_prob(row.get("晋级概率")))
                    elif c == "类似标的":
                        val = ann_by_code.get(code, row.get("类似标的", "-"))
                        cells.append(str(val).replace("|", "\\|") if val else "-")
                    else:
                        val = str(row.get(c, "")).replace("|", "\\|")
                        if c == "涨停原因":
                            val = val.replace("\n", " ")
                        cells.append(val)
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")

    lines.extend(
        [
            "",
            "## 使用说明",
            "",
            "1. 重新生成：`python scripts/lianban.py`",
            "2. 指定交易日：`python scripts/lianban.py today --date 20260519`",
            "3. 全量：`python scripts/lianban.py all`",
            "4. 含首板：`python scripts/lianban.py today --min-boards 1`",
            "",
        ]
    )
    return "\n".join(lines)


def _stocks_json_records(table: pd.DataFrame) -> list[dict]:
    if table.empty:
        return []
    t = sort_by_board_and_prob(table)
    return (
        t.assign(
            晋级概率_pct=lambda d: d["晋级概率"].map(
                lambda x: round(100 * x, 2) if x is not None else None
            )
        )
        .drop(columns=["晋级概率说明"], errors="ignore")
        .to_dict(orient="records")
    )


def _stocks_json_by_board(table: pd.DataFrame) -> dict[str, list[dict]]:
    if table.empty:
        return {}
    raw: list[dict] = []
    for _, row in sort_by_board_and_prob(table).iterrows():
        rec = row.drop(labels=["晋级概率说明"], errors="ignore").to_dict()
        prob = rec.get("晋级概率")
        rec["晋级概率_pct"] = round(100 * float(prob), 2) if prob is not None else None
        raw.append(rec)
    grouped = group_records_by_board(raw)
    return {str(n): items for n, items in grouped.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description="当日连板股分析并生成文档")
    ap.add_argument("--date", help="交易日 YYYYMMDD，默认自动取最近有数据一日")
    ap.add_argument("--min-boards", type=int, default=2, help="最低连板数（默认 2）")
    ap.add_argument(
        "--hist-pairs",
        type=int,
        default=20,
        help="用于估计晋级概率的相邻交易日对数（默认 20）",
    )
    ap.add_argument(
        "--max-scan",
        type=int,
        default=60,
        help="向前扫描自然日上限（找历史交易日）",
    )
    args = ap.parse_args()

    trade_date = resolve_trade_date(args.date, max_scan=args.max_scan)
    if not trade_date:
        print("未找到有效涨停池数据，请检查 --date 或网络。", file=sys.stderr)
        sys.exit(2)

    need_days = args.hist_pairs + 1
    days = iter_trading_days_before(trade_date, need_days, args.max_scan)
    if len(days) < 2:
        print("历史交易日不足，无法估计晋级概率。", file=sys.stderr)
        sys.exit(3)

    rates_by_n, pairs_used, model = aggregate_rates_by_board(
        days, args.hist_pairs, args.min_boards
    )

    raw = fetch_zt_pool(trade_date)
    if raw is None or raw.empty:
        print(f"当日涨停池为空：{trade_date}", file=sys.stderr)
        sys.exit(4)

    cfg = load_lianban_config()
    reason_map = fetch_zt_reason_map(trade_date, cfg)
    table = build_today_table(
        raw, args.min_boards, rates_by_n, model, reason_map=reason_map
    )

    ensure_daily_dir()
    out_md = daily_md(trade_date)
    out_json = daily_json(trade_date)

    def _json_safe(obj):
        if isinstance(obj, dict):
            return {str(k): _json_safe(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_json_safe(x) for x in obj]
        if hasattr(obj, "item"):
            return obj.item()
        return obj

    records = attach_similar_fields(_stocks_json_records(table))
    payload = {
        "trade_date": trade_date,
        "min_boards": args.min_boards,
        "hist_pairs": args.hist_pairs,
        "calibration": _json_safe(model.to_dict() if model else None),
        "rates_by_board": _json_safe(rates_by_n),
        "stocks": records,
        "by_boards": _stocks_json_by_board(
            pd.DataFrame(records) if records else table
        ),
    }

    md_text = build_markdown(
        trade_date,
        table,
        rates_by_n,
        pairs_used,
        args.min_boards,
        args.hist_pairs,
        stock_records=records,
    )
    json_text = json.dumps(_json_safe(payload), ensure_ascii=False, indent=2)

    out_md.write_text(md_text, encoding="utf-8")
    out_json.write_text(json_text, encoding="utf-8")

    print(f"交易日: {trade_date}，连板股 {len(table)} 只")
    print(f"已写入: {out_md}")
    print(f"已写入: {out_json}")


if __name__ == "__main__":
    main()
