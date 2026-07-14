# -*- coding: utf-8 -*-
"""汇总「数据总览」并更新策略文档索引。"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lianban_paths import (
    BACKTEST_DETAIL_MD,
    BACKTEST_MD,
    CALIB_JSON,
    DAILY_DIR,
    LEADER_MD,
    STABILIZE_MD,
    OVERVIEW_MD,
    STRATEGY_MD,
    WEEKLY_MD,
    ensure_doc_dir,
    latest_daily_json,
    list_daily_dates,
)


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _fmt_pct(x) -> str:
    if x is None:
        return "-"
    try:
        return f"{100 * float(x):.1f}%"
    except (TypeError, ValueError):
        return str(x)


def build_overview() -> str:
    ensure_doc_dir()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    calib = _read_json(CALIB_JSON)
    latest_path = latest_daily_json()
    today = _read_json(latest_path) if latest_path else None
    daily_dates = list_daily_dates()

    lines = [
        "# 连板数据总览",
        "",
        f"> 自动生成于 {now}，请运行 `python scripts/lianban.py publish`",
        "",
        "## 文档索引",
        "",
        "| 文档 | 说明 |",
        "|------|------|",
        f"| [连板策略.md](../连板策略.md) | 策略说明 |",
        f"| [每日/](./每日/) | **按日期落盘的当日连板**（`YYYYMMDD.md` / `.json`） |",
    ]
    for name, path, desc in (
        ("连板滚动回测.md", BACKTEST_MD, "滚动回测摘要"),
        ("连板滚动验证明细.md", BACKTEST_DETAIL_MD, "逐股验证"),
        ("连板晋级率周跟踪.md", WEEKLY_MD, "周报"),
        ("近月高标龙头分析.md", LEADER_MD, "近月高标与龙头筛选"),
        ("连板回踩企稳扫描.md", STABILIZE_MD, "连板回踩绿K企稳"),
        ("连板预测校准.json", CALIB_JSON, "校准参数"),
    ):
        flag = "✓" if path.is_file() else "—"
        lines.append(f"| [{name}](./{path.name}) {flag} | {desc} |")

    if daily_dates:
        lines.extend(
            [
                "",
                "## 已落盘交易日（每日文件夹）",
                "",
                "| 日期 | Markdown | JSON |",
                "|------|----------|------|",
            ]
        )
        for d in daily_dates[:15]:
            md_p = DAILY_DIR / f"{d}.md"
            js_p = DAILY_DIR / f"{d}.json"
            lines.append(
                f"| {d} | "
                f"{'[查看](./每日/' + d + '.md)' if md_p.is_file() else '—'} | "
                f"{'有' if js_p.is_file() else '—'} |"
            )
        if len(daily_dates) > 15:
            lines.append(f"| … | 共 {len(daily_dates)} 个交易日 | |")

    if calib:
        s = calib.get("backtest_summary", {})
        lines.extend(
            [
                "",
                "## 校准与回测摘要",
                "",
                f"- 连板整体先验（≥2）：{_fmt_pct(calib.get('overall_ge2_rate'))}",
                f"- 验证样本：{s.get('stock_samples', '-')} 只",
                f"- Brier（旧→新）：{s.get('brier_baseline_board_only', '-')} → {s.get('brier_calibrated', '-')}",
                f"- 阈值0.5命中率（校准后）：{_fmt_pct(s.get('acc_at_50_calibrated'))}",
                "",
                "### 分层晋级概率",
                "",
                "| 连板数 | 校准概率 |",
                "|--------|----------|",
            ]
        )
        for n in sorted(calib.get("board_rates", {}), key=lambda x: int(x)):
            lines.append(f"| {n} | {_fmt_pct(calib['board_rates'][n])} |")

    if today:
        trade_date = today.get("trade_date", "")
        by_boards = today.get("by_boards") or {}
        stocks = today.get("stocks", [])
        lines.extend(
            [
                "",
                f"## 最新一日连板池（{trade_date}）",
                "",
                f"明细文件：[每日/{trade_date}.md](./每日/{trade_date}.md)",
                "",
                f"共 **{len(stocks)}** 只（连板≥{today.get('min_boards', 2)}），**按板数分组、组内按晋级概率降序**。",
                "",
            ]
        )
        if by_boards:
            for boards in sorted(by_boards.keys(), key=int, reverse=True):
                items = by_boards[boards]
                lines.append(f"### {boards} 连板（{len(items)} 只）")
                lines.append("")
                lines.append(
                    "| 代码 | 名称 | 涨停原因 | 晋级概率 | 大单封板 | 弱转强 | 行业 | 封板资金 | 炸板 |"
                )
                lines.append(
                    "|------|------|----------|----------|----------|--------|------|----------|------|"
                )
                for r in items:
                    prob = r.get("晋级概率_pct")
                    if prob is None and r.get("晋级概率") is not None:
                        prob = round(100 * float(r["晋级概率"]), 1)
                    reason = str(r.get("涨停原因", "-")).replace("|", "\\|").replace(
                        "\n", " "
                    )
                    lines.append(
                        f"| {r.get('代码', '')} | {r.get('名称', '')} | {reason} | "
                        f"{prob if prob is not None else '-'}% | "
                        f"{r.get('大单封板', '-')} | {r.get('弱转强', '-')} | "
                        f"{r.get('所属行业', '')} | "
                        f"{r.get('封板资金', '')} | {r.get('炸板次数', '')} |"
                    )
                lines.append("")
        else:
            lines.append("| 代码 | 名称 | 连板 | 晋级概率 | 行业 |")
            lines.append("|------|------|------|----------|------|")
            for r in stocks:
                prob = r.get("晋级概率_pct", "-")
                lines.append(
                    f"| {r.get('代码')} | {r.get('名称')} | {r.get('连板数')} | {prob}% | {r.get('所属行业', '')} |"
                )

    lines.append("")
    return "\n".join(lines)


def patch_strategy_doc_index() -> bool:
    if not STRATEGY_MD.is_file():
        return False
    text = STRATEGY_MD.read_text(encoding="utf-8")
    block = (
        "\n---\n\n"
        "## 9. 数据文档（自动生成）\n\n"
        "运行 `python scripts/lianban.py` 后：\n\n"
        "| 位置 | 说明 |\n"
        "|------|------|\n"
        "| [连板数据/每日/](./连板数据/每日/) | 按日：`YYYYMMDD.md`、`YYYYMMDD.json` |\n"
        "| [连板数据/数据总览.md](./连板数据/数据总览.md) | 索引与最新摘要 |\n"
        "| [连板数据/连板滚动回测.md](./连板数据/连板滚动回测.md) | 回测 |\n"
        "| [连板数据/连板预测校准.json](./连板数据/连板预测校准.json) | 校准参数 |\n"
    )
    marker = "## 9. 数据文档（自动生成）"
    if marker in text:
        text = re.sub(r"\n---\n\n## 9\. 数据文档（自动生成）[\s\S]*$", "", text)
    STRATEGY_MD.write_text(text.rstrip() + block, encoding="utf-8")
    return True


def main() -> None:
    ensure_doc_dir()
    OVERVIEW_MD.write_text(build_overview(), encoding="utf-8")
    patch_strategy_doc_index()
    print(f"已写入: {OVERVIEW_MD}")


if __name__ == "__main__":
    main()
