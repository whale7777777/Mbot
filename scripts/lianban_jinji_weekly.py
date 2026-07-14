# -*- coding: utf-8 -*-
"""
近若干交易日涨停池连板晋级率统计（东方财富数据源，依赖 akshare）。

口径说明
--------
- T 日「n 连板」：当日收盘涨停股池中「连板数」字段为 n（首板 n=1）。
- 「晋级」：同一代码在 T+1 日仍出现在涨停池，且连板数为 n+1。
- 「不含 ST / 退市」：名称以 *ST、ST 开头，或名称含「退」的样本剔除。

媒体常用的「连板股晋级率」多指：仅统计 T 日连板数>=2 的样本在 T+1 多封一天的比例
（本脚本同时输出含首板、不含首板两套整体晋级率，以及按 n 分层晋级率）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

try:
    import akshare as ak
except ImportError:
    print("请先安装: pip install akshare pandas", file=sys.stderr)
    sys.exit(1)

from lianban_paths import DOC_DIR, WEEKLY_MD, ensure_doc_dir

OUT_DIR = DOC_DIR
OUT_MD = WEEKLY_MD
OUT_JSON = DOC_DIR / "连板晋级率_latest.json"


def is_st_or_delist(name: str) -> bool:
    n = str(name).strip()
    if not n:
        return False
    if n.startswith("*ST") or n.startswith("ST"):
        return True
    if "退" in n:
        return True
    return False


def pick_col(df: pd.DataFrame, *candidates: str) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def fetch_zt_pool(date_yyyymmdd: str) -> pd.DataFrame | None:
    """涨停池（优先 TuShare，见 docs/research/lianban_config.json）。"""
    try:
        from lianban_data import fetch_zt_pool as _fetch

        return _fetch(date_yyyymmdd)
    except ImportError:
        pass
    try:
        df = ak.stock_zt_pool_em(date=date_yyyymmdd)
        if df is None or df.empty:
            return None
        return df
    except Exception:
        return None


def normalize_frame(df: pd.DataFrame) -> pd.DataFrame | None:
    c_code = pick_col(df, "代码", "股票代码")
    c_name = pick_col(df, "名称", "股票名称")
    c_lb = pick_col(df, "连板数", "连续涨停天数", "涨停统计")
    if not c_code or not c_lb:
        return None
    out = pd.DataFrame(
        {
            "code": df[c_code].astype(str).str.zfill(6),
            "name": df[c_name].astype(str) if c_name else "",
            "boards": pd.to_numeric(df[c_lb], errors="coerce"),
        }
    )
    out = out.dropna(subset=["boards"])
    out["boards"] = out["boards"].astype(int)
    if c_name:
        out = out[~out["name"].map(is_st_or_delist)]
    out = out.drop_duplicates(subset=["code"], keep="last")
    return out


def iter_recent_trading_days_with_data(max_calendar_days: int = 20) -> list[str]:
    """从昨天起向前尝试，返回有涨停池数据的日期 YYYYMMDD（新到旧）。"""
    end = datetime.now().date()
    found: list[str] = []
    for i in range(1, max_calendar_days + 1):
        d = end - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        df = fetch_zt_pool(ds)
        if df is not None and not df.empty:
            found.append(ds)
    # 新到旧排序
    found.sort(reverse=True)
    return found


def promotion_for_pair(
    df_t: pd.DataFrame, df_t1: pd.DataFrame
) -> dict:
    """df_t 为较早一日，df_t1 为较晚一日（T 然后 T+1）。"""
    m1 = df_t1.set_index("code")["boards"]
    rows_by_n: dict[int, dict] = {}

    for n in sorted(df_t["boards"].unique()):
        sub = df_t[df_t["boards"] == n]
        total = len(sub)
        promoted = 0
        promoted_codes: list[str] = []
        for code in sub["code"]:
            if code not in m1.index:
                continue
            b1 = m1.loc[code]
            if hasattr(b1, "iloc"):
                b1 = int(b1.iloc[0])
            else:
                b1 = int(b1)
            if b1 == n + 1:
                promoted += 1
                promoted_codes.append(code)
        rows_by_n[n] = {
            "n": n,
            "count_T": total,
            "promoted": promoted,
            "rate": (promoted / total) if total else None,
            "codes_promoted": promoted_codes,
        }

    # 整体：含首板
    all_n = df_t["boards"] >= 1
    denom_all = int(all_n.sum())
    prom_all = sum(
        1
        for code, b in zip(df_t.loc[all_n, "code"], df_t.loc[all_n, "boards"])
        if code in m1.index and int(m1.loc[code]) == int(b) + 1
    )

    # 不含首板（连板数>=2）
    ge2 = df_t["boards"] >= 2
    denom_ge2 = int(ge2.sum())
    prom_ge2 = sum(
        1
        for code, b in zip(df_t.loc[ge2, "code"], df_t.loc[ge2, "boards"])
        if code in m1.index and int(m1.loc[code]) == int(b) + 1
    )

    return {
        "by_boards": rows_by_n,
        "overall_including_first": {
            "denom": denom_all,
            "promoted": prom_all,
            "rate": (prom_all / denom_all) if denom_all else None,
        },
        "overall_excluding_first": {
            "denom": denom_ge2,
            "promoted": prom_ge2,
            "rate": (prom_ge2 / denom_ge2) if denom_ge2 else None,
        },
    }


def build_report(
    trading_days_new_first: list[str], pair_results: list[dict]
) -> str:
    lines = [
        "# 连板晋级率跟踪（自动生成）",
        "",
        f"- 生成时间（本地）：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 数据源：`akshare.stock_zt_pool_em`（东方财富涨停池）",
        "- 晋级定义：T 日连板数为 n 的个股，T+1 日仍在涨停池且连板数为 n+1。",
        "- 已剔除名称以 ST、*ST 开头及名称含「退」的样本。",
        "",
        "## 近段交易日相邻日晋级率汇总",
        "",
        "| T 日 | T+1 日 | 连板样本数(>=2) | 晋级数 | 晋级率(>=2) | 全样本(含首板) | 晋级数 | 晋级率(含首板) |",
        "|------|--------|-----------------|--------|-------------|----------------|--------|----------------|",
    ]
    for pr in pair_results:
        t, t1 = pr["T"], pr["T1"]
        ex = pr["stats"]["overall_excluding_first"]
        inc = pr["stats"]["overall_including_first"]
        lines.append(
            f"| {t} | {t1} | {ex['denom']} | {ex['promoted']} | "
            f"{_fmt_rate(ex['rate'])} | {inc['denom']} | {inc['promoted']} | {_fmt_rate(inc['rate'])} |"
        )

    lines.extend(["", "## 按连板高度分层（最近一对交易日）", ""])
    if pair_results:
        last = pair_results[-1]
        lines.append(f"- 区间：{last['T']} → {last['T1']}")
        lines.append("")
        lines.append("| T日连板数n | T日家数 | 晋级到n+1 | 晋级率 |")
        lines.append("|------------|---------|-----------|--------|")
        for n in sorted(last["stats"]["by_boards"].keys()):
            row = last["stats"]["by_boards"][n]
            lines.append(
                f"| {n} | {row['count_T']} | {row['promoted']} | {_fmt_rate(row['rate'])} |"
            )

    lines.extend(
        [
            "",
            "## 校验说明（准确率）",
            "",
            "1. **实证晋级率**：上表由原始涨停池逐日核对得到，可作为基准。",
            "2. **前一日预判校准**：若你在 T 日收盘后写了「关注池/高度预判」，请在 T+1 收盘后在本节下方手写对照：",
            "   - 命中条件建议与上表一致：预测「能再多一封」则 T+1 连板数应等于 T 日连板数+1。",
            "3. 重新生成：在项目根目录执行 `python scripts/lianban_jinji_weekly.py`（可加 `--pairs 5` 控制相邻交易日对数）。",
            "",
            "## 附录：本次使用的交易日序列（新→旧）",
            "",
            "`" + ", ".join(trading_days_new_first) + "`",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt_rate(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{100.0 * x:.2f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--pairs",
        type=int,
        default=5,
        help="输出最近多少对相邻交易日（默认 5 对，约一周）",
    )
    ap.add_argument(
        "--max-scan",
        type=int,
        default=25,
        help="向前最多扫描多少个自然日找有数据的交易日",
    )
    args = ap.parse_args()

    days = iter_recent_trading_days_with_data(max_calendar_days=args.max_scan)
    if len(days) < 2:
        print("未能获取至少两个有数据的交易日，请检查网络或 akshare 接口。", file=sys.stderr)
        sys.exit(2)

    # days 新到旧，例如 [20260508, 20260507, ...]
    # 相邻对 (T, T+1) 时间顺序：T 早，T+1 晚 → 在列表中 T+1 索引小
    pair_results: list[dict] = []
    for i in range(len(days) - 1):
        t, t1 = days[i + 1], days[i]  # t 较早，t1 较晚（days 新→旧）
        raw_t = fetch_zt_pool(t)
        raw_t1 = fetch_zt_pool(t1)
        if raw_t is None or raw_t1 is None:
            continue
        df_t = normalize_frame(raw_t)
        df_t1 = normalize_frame(raw_t1)
        if df_t is None or df_t1 is None:
            print(f"列名不匹配，T={t} T1={t1}，列：{list(raw_t.columns)}", file=sys.stderr)
            sys.exit(3)
        stats = promotion_for_pair(df_t, df_t1)
        pair_results.append({"T": t, "T1": t1, "stats": stats})
        if len(pair_results) >= args.pairs:
            break

    if not pair_results:
        print("无有效相邻交易日对。", file=sys.stderr)
        sys.exit(4)

    ensure_doc_dir()
    payload = {
        "trading_days": days,
        "pairs": [
            {
                "T": p["T"],
                "T1": p["T1"],
                "overall_excluding_first": p["stats"]["overall_excluding_first"],
                "overall_including_first": p["stats"]["overall_including_first"],
                "by_boards": {
                    str(k): {kk: vv for kk, vv in v.items() if kk != "codes_promoted"}
                    for k, v in p["stats"]["by_boards"].items()
                },
            }
            for p in pair_results
        ],
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(
        build_report(days, list(reversed(pair_results))), encoding="utf-8"
    )
    # pair_results 当前是「从新到旧」收集的，build_report 要按时间正序展示则 reverse
    print(f"已写入: {OUT_MD}")
    print(f"已写入: {OUT_JSON}")


if __name__ == "__main__":
    main()
