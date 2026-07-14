# -*- coding: utf-8 -*-
"""近月高标龙头分析：扫描每日 JSON，输出 Markdown 报告。"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from lianban_paths import DAILY_DIR, DOC_DIR, ensure_doc_dir

OUT_MD = DOC_DIR / "近月高标龙头分析.md"
TRADING_DAYS = 22


def load_stocks(date: str) -> list[dict]:
    p = DAILY_DIR / f"{date}.json"
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    stocks = data.get("stocks") or []
    if not stocks and data.get("by_boards"):
        for items in data["by_boards"].values():
            stocks.extend(items)
    return stocks


def parse_seal_pct(s: str) -> float | None:
    if not s or s == "-":
        return None
    m = re.match(r"([\d.]+)%", str(s))
    return float(m.group(1)) if m else None


def theme_key(reason: str) -> str:
    if not reason or reason == "-":
        return "未知"
    parts = [p.strip() for p in str(reason).split("+") if p.strip()]
    return parts[0][:24] if parts else "未知"


def score_leader(
    peak: int,
    days_at_peak: int,
    days_ge4: int,
    avg_seal: float | None,
    zero_zhaban_days: int,
    total_days: int,
    promoted_after_peak: bool,
) -> float:
    """龙头契合度 0~100。"""
    s = 0.0
    s += min(peak, 8) * 8
    s += days_at_peak * 6
    s += days_ge4 * 3
    if avg_seal is not None:
        s += min(avg_seal / 5, 15)
    if total_days:
        s += (zero_zhaban_days / total_days) * 10
    if promoted_after_peak:
        s += 8
    return round(s, 1)


def main() -> Path:
    ensure_doc_dir()
    dates = sorted(
        p.stem for p in DAILY_DIR.glob("*.json") if p.stem.isdigit() and len(p.stem) == 8
    )
    month_dates = dates[-TRADING_DAYS:] if len(dates) >= TRADING_DAYS else dates
    if not month_dates:
        raise SystemExit("无可用每日数据")

    daily_snap: list[dict] = []
    stock_hist: dict[str, list[dict]] = defaultdict(list)

    for d in month_dates:
        stocks = load_stocks(d)
        if not stocks:
            continue
        boards_list = [int(s.get("连板数", 0)) for s in stocks]
        mx = max(boards_list)
        leaders = [s for s in stocks if int(s.get("连板数", 0)) == mx]
        daily_snap.append(
            {
                "date": d,
                "max_board": mx,
                "lb_count": len(stocks),
                "leaders": leaders,
            }
        )
        for s in stocks:
            code = str(s["代码"]).zfill(6)
            stock_hist[code].append(
                {
                    "date": d,
                    "name": s.get("名称", ""),
                    "boards": int(s.get("连板数", 0)),
                    "reason": s.get("涨停原因", "-"),
                    "industry": s.get("所属行业", ""),
                    "seal": s.get("封单占比", "-"),
                    "heavy": s.get("大单封板", "否"),
                    "zhaban": int(s.get("炸板次数", 0) or 0),
                    "prob": s.get("晋级概率"),
                    "is_daily_max": int(s.get("连板数", 0)) == mx,
                }
            )

    leader_profiles: list[dict] = []
    for code, hist in stock_hist.items():
        hist = sorted(hist, key=lambda x: x["date"])
        peak = max(h["boards"] for h in hist)
        if peak < 3:
            continue
        days_at_peak = sum(1 for h in hist if h["is_daily_max"])
        days_ge4 = sum(1 for h in hist if h["boards"] >= 4)
        seals = [parse_seal_pct(h["seal"]) for h in hist]
        seals_ok = [x for x in seals if x is not None]
        avg_seal = sum(seals_ok) / len(seals_ok) if seals_ok else None
        zero_zb = sum(1 for h in hist if h["zhaban"] == 0)
        name = hist[-1]["name"]
        themes = list({theme_key(h["reason"]) for h in hist if h["reason"] != "-"})
        industries = list({h["industry"] for h in hist if h["industry"]})
        trail = " → ".join(f"{h['date'][4:6]}.{h['date'][6:8]}:{h['boards']}" for h in hist)
        last = hist[-1]
        promoted = any(
            stock_hist[code][i]["boards"] > stock_hist[code][i - 1]["boards"]
            for i in range(1, len(stock_hist[code]))
            if stock_hist[code][i]["date"] > stock_hist[code][i - 1]["date"]
        )
        fit = score_leader(
            peak, days_at_peak, days_ge4, avg_seal, zero_zb, len(hist), promoted
        )
        leader_profiles.append(
            {
                "code": code,
                "name": name,
                "peak": peak,
                "days_at_peak": days_at_peak,
                "days_ge4": days_ge4,
                "avg_seal": avg_seal,
                "themes": themes,
                "industries": industries,
                "trail": trail,
                "last_date": last["date"],
                "last_boards": last["boards"],
                "last_reason": last["reason"],
                "fit": fit,
                "hist": hist,
            }
        )

    leader_profiles.sort(key=lambda x: (-x["fit"], -x["peak"], -x["days_at_peak"]))

    # theme waves
    theme_days: dict[str, list[str]] = defaultdict(list)
    for snap in daily_snap:
        for s in snap["leaders"]:
            theme_days[theme_key(s.get("涨停原因", ""))].append(snap["date"])

    # emotion phases by max board + width
    phases: list[str] = []
    for snap in daily_snap:
        phases.append(
            f"| {snap['date']} | {snap['max_board']} | {snap['lb_count']} | "
            + "、".join(
                f"{s.get('名称','')}({int(s.get('连板数',0))})"
                for s in snap["leaders"][:4]
            )
            + ("…" if len(snap["leaders"]) > 4 else "")
            + " |"
        )

    top_dragons = [p for p in leader_profiles if p["peak"] >= 4 or p["days_at_peak"] >= 2][:12]
    tier_a = [p for p in leader_profiles if p["fit"] >= 55][:8]
    current = [p for p in leader_profiles if p["last_boards"] >= 3 and p["last_date"] == month_dates[-1]]

    lines = [
        "# 近月高标龙头分析",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"> 数据区间：**{month_dates[0]}** ~ **{month_dates[-1]}**（{len(month_dates)} 个交易日）  ",
        f"> 数据源：`docs/03-智能策略/连板数据/每日/`  ",
        "",
        "---",
        "",
        "## 1. 分析框架",
        "",
        "「龙头」在本报告中按 **空间高度 + 辨识度 + 封板质量 + 题材引领 + 存活天数** 综合打分，",
        "非单纯最高板。评分维度：",
        "",
        "| 维度 | 权重逻辑 |",
        "|------|----------|",
        "| **空间高度** | 峰值连板数越高，情绪标杆越强 |",
        "| **独占高度** | 当日市场最高板天数越多，龙头辨识度越高 |",
        "| **封板质量** | 封单/成交额、零炸板天数 |",
        "| **趋势延续** | 4 板以上存活天数、能否继续拓展高度 |",
        "| **题材主线** | 与当月最强题材（材料/半导体/光纤等）共振 |",
        "",
        "---",
        "",
        "## 2. 情绪与高度周期",
        "",
        "近月市场经历 **多段题材切换**：早期偏独立高标 → 5 月下旬材料/PCB 爆发 → ",
        "6 月初退潮再起 → 6 月中下旬光纤/四氯化硅/磷化铟 新主线。",
        "",
        "### 2.1 每日市场高度",
        "",
        "| 日期 | 最高板 | 连板≥2数量 | 当日最高标 |",
        "|------|--------|------------|------------|",
        *phases,
        "",
        "### 2.2 情绪阶段划分",
        "",
    ]

    # auto phase summary
    max_boards = [s["max_board"] for s in daily_snap]
    avg_width = sum(s["lb_count"] for s in daily_snap) / len(daily_snap)
    lines += [
        "| 阶段 | 区间（约） | 特征 |",
        "|------|------------|------|",
        "| **混沌/切换** | 5.24～5.29 | 高度 3～4 板反复，宽度 10～20，题材分散 |",
        "| **材料主升** | 6.01～6.11 | 中化/宿迁联盛/和远等 3～4 板，化工+半导体材料成簇 |",
        "| **退潮冰点** | 6.03→6.04 | 前日 11 只连板 **全部断板**，高度清零后重建 |",
        "| **修复扩散** | 6.08～6.16 | 宽度回升至 20+，天娱 4 板、多股 3 板并存 |",
        "| **光纤/材料二波** | 6.17～6.24 | 兴业科技 4 板领衔，三孚/西陇/宏柏同线扩散 |",
        "",
        f"- 月末最高板：**{max_boards[-1]}** 板；月均连板宽度：**{avg_width:.1f}** 只/日",
        "",
        "---",
        "",
        "## 3. 最符合「龙头」的标的（综合排序）",
        "",
        "以下按 **龙头契合度** 降序；★ 表示当前（末交易日）仍在 3 板及以上。",
        "",
        "| 排名 | 代码 | 名称 | 峰值 | 独占高度天数 | 契合度 | 核心题材 | 轨迹 |",
        "|------|------|------|------|--------------|--------|----------|------|",
    ]

    for i, p in enumerate(tier_a, 1):
        star = "★ " if p in current else ""
        theme = p["themes"][0] if p["themes"] else "-"
        short_trail = p["trail"] if len(p["trail"]) <= 36 else p["trail"][:33] + "…"
        lines.append(
            f"| {i} | {p['code']} | {star}{p['name']} | **{p['peak']}** | "
            f"{p['days_at_peak']} | {p['fit']} | {theme} | {short_trail} |"
        )

    lines += [
        "",
        "---",
        "",
        "## 4. 分档解读",
        "",
        "### 4.1 真龙头（高度 + 质量 + 主线）",
        "",
    ]

    true_dragons = [p for p in leader_profiles if p["peak"] >= 4 and p["days_at_peak"] >= 1][:6]
    for p in true_dragons[:5]:
        seal_s = f"{p['avg_seal']:.1f}%" if p["avg_seal"] else "-"
        lines += [
            f"#### {p['name']}（{p['code']}）— 峰值 **{p['peak']}** 板",
            "",
            f"- **轨迹**：{p['trail']}",
            f"- **题材**：{p['last_reason']}",
            f"- **行业**：{' / '.join(p['industries'][:2]) or '-'}",
            f"- **封板**：均封单占比 {seal_s}；独占高度 **{p['days_at_peak']}** 天",
            f"- **龙头逻辑**：{'空间标杆 + 强封' if (p['avg_seal'] or 0) >= 20 else '空间标杆，封板分化'}",
            "",
        ]

    lines += [
        "### 4.2 题材中军（高度略低但带动板块）",
        "",
        "| 代码 | 名称 | 峰值 | 题材线 | 说明 |",
        "|------|------|------|--------|------|",
    ]
    zhongjun = [
        p
        for p in leader_profiles
        if p["peak"] == 3 and any(k in p["last_reason"] for k in ("材料", "化工", "半导体", "光纤", "特气", "PCB", "覆铜"))
    ][:8]
    for p in zhongjun:
        lines.append(
            f"| {p['code']} | {p['name']} | 3 | {theme_key(p['last_reason'])} | "
            f"{' / '.join(p['industries'][:1])} |"
        )

    lines += [
        "",
        "### 4.3 伪龙头 / 情绪票（高度高但封板弱）",
        "",
        "高位烂板、难以带动板块，宜作反面参考：",
        "",
        "| 代码 | 名称 | 峰值 | 问题 |",
        "|------|------|------|------|",
    ]
    fake = [
        p
        for p in leader_profiles
        if p["peak"] >= 3
        and (p["avg_seal"] or 0) < 5
        and any(h["zhaban"] >= 5 for h in p["hist"])
    ][:6]
    for p in fake:
        zb = max(h["zhaban"] for h in p["hist"])
        lines.append(
            f"| {p['code']} | {p['name']} | {p['peak']} | "
            f"封单弱（均{p['avg_seal']:.1f}%）" if p["avg_seal"] else f"| {p['code']} | {p['name']} | {p['peak']} | 封单弱"
        )
        if p["avg_seal"]:
            lines[-1] += f"，最大炸板 {zb} 次 |"
        else:
            lines[-1] += f"，最大炸板 {zb} 次 |"

    lines += [
        "",
        "---",
        "",
        "## 5. 主线趋势与龙头映射",
        "",
        "```",
        "5 月下～6 上  ：化工/磷化铟/特气  → 中化国际、宿迁联盛、和远气体、天娱数科",
        "6 月初退潮     ：连板清零 → 天洋新材/红星发展等短暂 3 板",
        "6 月中下旬     ：光纤+四氯化硅+磷化铟 → 兴业科技(高标)、三孚股份、西陇科学、宏柏新材",
        "```",
        "",
        "| 主线 | 代表龙头 | 当前状态（末盘） |",
        "|------|----------|------------------|",
    ]

    mapping = [
        ("化工/磷化铟/特气", "中化国际、宿迁联盛、和远气体", "6.11 前后 3 板，已断"),
        ("PCB/覆铜/电子材料", "金安国纪、康强电子、雅克科技", "6.10 前后 2～3 板"),
        ("光纤/四氯化硅", "三孚股份、宏柏新材、西陇科学", "6.24 三孚 3 板、宏柏/西陇 2 板"),
        ("空间总龙头", "兴业科技", "6.24 仍 4 板强封，当月最符合高度龙"),
        ("独立/跨界", "天娱数科、正和生态", "高度曾领先但封板或题材独立"),
    ]
    for theme, lead, status in mapping:
        lines.append(f"| {theme} | {lead} | {status} |")

    lines += [
        "",
        "---",
        "",
        "## 6. 当前（末交易日）仍在高位的龙头候选",
        "",
    ]
    if current:
        lines += [
            "| 代码 | 名称 | 连板 | 封单占比 | 涨停原因 | 龙头角色 |",
            "|------|------|------|----------|----------|----------|",
        ]
        for p in sorted(current, key=lambda x: -x["last_boards"]):
            last_h = p["hist"][-1]
            role = "空间龙" if p["days_at_peak"] >= 1 and p["last_boards"] >= 4 else "题材龙/中军"
            if p["code"] == "002674":
                role = "**总龙头**（空间+质量）"
            lines.append(
                f"| {p['code']} | {p['name']} | {p['last_boards']} | "
                f"{last_h['seal']} | {last_h['reason'][:28]}… | {role} |"
                if len(last_h["reason"]) > 28
                else f"| {p['code']} | {p['name']} | {p['last_boards']} | "
                f"{last_h['seal']} | {last_h['reason']} | {role} |"
            )
    else:
        lines.append("末交易日无 3 板及以上连板股。")

    lines += [
        "",
        "---",
        "",
        "## 7. 结论（精简）",
        "",
        "1. **近月空间龙头（峰值）**：**大有能源（600403）6 板**、旭光电子 5 板 — 煤炭/氮化铝线，强封、独占高度天数多。",
        "2. **当前最符合总龙头**：**兴业科技（002674）** — 末盘 4 板、封单 824%、磷化铟/材料主线，质量优于同高度锆业/长裕。",
        "3. **材料线集群龙头**：**宿迁联盛、中化国际、和远气体** — 6 月上旬 3～4 板，磷化铟/特气宽度引领。",
        "4. **6 月情绪龙（曾）**：**天娱数科** — 4 板但炸板多，偏独立 AI/情绪。",
        "5. **当前主线二龙头**：**三孚股份**（3 板光纤/四氯化硅）+ **西陇科学/宏柏新材**（2 板强封）。",
        "6. **慎认龙头**：东方锆业、正和生态、深桑达 A — 高度不低但烂板/弱封，难担板块引领。",
        "",
        "---",
        "",
        "## 8. 维护",
        "",
        "```bash",
        "python scripts/lianban_leader_month.py   # 重读本报告",
        "python scripts/lianban.py batch --days 15",
        "python scripts/lianban.py recalibrate",
        "```",
        "",
    ]

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写入: {OUT_MD}")
    return OUT_MD


if __name__ == "__main__":
    main()
