# -*- coding: utf-8 -*-
"""连板池同线/类似标的聚类与标注。"""

from __future__ import annotations

import re
from typing import Any

# 优先级靠前 = 题材更具体；同簇内互相标注为「类似标的」
THEME_GROUPS: list[tuple[str, list[str]]] = [
    ("磷化铟/半导体材料", ["磷化铟", "存储材料", "电子化学品", "光刻胶", "特气", "六氟", "电子特气", "芳纶", "树脂", "覆铜", "PCB", "半导体", "硅片", "封装", "陶瓷载板", "氮化铝"]),
    ("光纤/四氯化硅", ["光纤", "四氯化硅", "光缆", "光通信", "CPO", "光模块"]),
    ("锆/小金属", ["锆", "氧氯化锆", "钨", "钼", "小金属"]),
    ("化工/磷", ["磷化工", "磷化", "化学制品", "化学原料", "氟化工", "氢氟酸"]),
    ("煤炭/电力", ["煤炭", "焦煤", "火电", "热电", "电力", "煤电"]),
    ("地产/城开", ["地产", "房地产", "城开", "旧改"]),
    ("AI/算力/机器人", ["AI", "具身", "机器人", "算力", "液冷", "智谱", "物理AI"]),
    ("医药/创新药", ["创新药", "医药", "脑机", "制药"]),
    ("PCB/元件", ["PCB", "元件", "覆铜板", "电子布", "MLCC"]),
    ("消费/独立", ["小家电", "服装", "家居", "食品", "白酒"]),
]

# 近月龙头文档中的高标代码 → 题材（用于跨日「类似高标」提示）
KNOWN_DRAGONS: dict[str, str] = {
    "002674": "磷化铟/材料",
    "603065": "磷化铟/材料",
    "600500": "磷化铟/材料",
    "002971": "磷化铟/材料",
    "603938": "光纤/四氯化硅",
    "605366": "光纤/四氯化硅",
    "002584": "光纤/四氯化硅",
    "600403": "煤炭/电力",
    "600353": "磷化铟/半导体材料",
    "002354": "AI/算力/机器人",
}


def _split_reason(reason: str) -> list[str]:
    if not reason or reason == "-":
        return []
    parts = re.split(r"[+＋/,，、]", str(reason))
    return [p.strip() for p in parts if p.strip()]


def assign_theme(reason: str, industry: str = "") -> str:
    tags = _split_reason(reason)
    blob = " ".join(tags) + " " + str(industry or "")
    for theme, keys in THEME_GROUPS:
        for k in keys:
            if k in blob:
                return theme
    if industry:
        return f"行业:{industry[:8]}"
    if tags:
        return tags[0][:12]
    return "独立"


def cluster_stocks(stocks: list[dict[str, Any]]) -> tuple[list[dict], dict[str, str]]:
    """
    返回 (题材簇列表, 代码→标注文案)。
    标注示例：「同线: 三孚股份、宏柏新材 | 似龙头: 兴业科技」
    """
    if not stocks:
        return [], {}

    themed: dict[str, list[dict]] = {}
    code_theme: dict[str, str] = {}

    for s in stocks:
        code = str(s.get("代码", "")).zfill(6)
        theme = assign_theme(s.get("涨停原因", ""), s.get("所属行业", ""))
        code_theme[code] = theme
        themed.setdefault(theme, []).append(
            {
                "代码": code,
                "名称": s.get("名称", ""),
                "连板数": int(s.get("连板数", 0) or 0),
                "涨停原因": s.get("涨停原因", "-"),
            }
        )

    clusters: list[dict] = []
    annotations: dict[str, str] = {}

    for theme, members in sorted(themed.items(), key=lambda x: (-len(x[1]), x[0])):
        if len(members) < 2 and not theme.startswith("行业:"):
            continue
        members = sorted(members, key=lambda x: (-x["连板数"], x["名称"]))
        leader = members[0]
        peer_names = [m["名称"] for m in members[1:4]]
        clusters.append(
            {
                "题材簇": theme,
                "数量": len(members),
                "龙头参考": f"{leader['名称']}({leader['连板数']}板)",
                "成员": members,
            }
        )
        for m in members:
            code = m["代码"]
            peers = [x["名称"] for x in members if x["代码"] != code][:3]
            parts: list[str] = []
            if peers:
                parts.append("同线: " + "、".join(peers))
            dragon_hint = _dragon_hint(code, theme)
            if dragon_hint:
                parts.append(f"似近月龙头: {dragon_hint}")
            if parts:
                annotations[code] = " | ".join(parts)
            elif len(members) >= 2:
                annotations[code] = f"同线簇:{theme}"

    return clusters, annotations


def _dragon_hint(code: str, theme: str) -> str:
    return ""


def enrich_annotations(
    stocks: list[dict[str, Any]], annotations: dict[str, str]
) -> dict[str, str]:
    """补充「似近月龙头」名称。"""
    dragon_names = {
        "002674": "兴业科技",
        "603065": "宿迁联盛",
        "600500": "中化国际",
        "002971": "和远气体",
        "603938": "三孚股份",
        "605366": "宏柏新材",
        "002584": "西陇科学",
        "600403": "大有能源",
        "600353": "旭光电子",
        "002354": "天娱数科",
    }
    out = dict(annotations)
    for s in stocks:
        code = str(s.get("代码", "")).zfill(6)
        theme = assign_theme(s.get("涨停原因", ""), s.get("所属行业", ""))
        hints = [
            dragon_names.get(dc, dc)
            for dc, dt in KNOWN_DRAGONS.items()
            if dc != code and (dt == theme or theme in dt or dt in theme)
        ][:2]
        if not hints:
            continue
        hint_s = "、".join(hints)
        if code in out:
            if "似近月龙头" not in out[code]:
                out[code] += f" | 似近月龙头: {hint_s}"
        else:
            out[code] = f"似近月龙头: {hint_s}"
    return out


def attach_similar_fields(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为每只股写入 题材簇、类似标的 字段。"""
    clusters, raw_ann = cluster_stocks(stocks)
    ann = enrich_annotations(stocks, raw_ann)
    out = []
    for s in stocks:
        row = dict(s)
        code = str(row.get("代码", "")).zfill(6)
        row["题材簇"] = assign_theme(row.get("涨停原因", ""), row.get("所属行业", ""))
        row["类似标的"] = ann.get(code, "-")
        out.append(row)
    return out


def build_similar_section(stocks: list[dict[str, Any]]) -> list[str]:
    """Markdown 段落行。"""
    clusters, raw_ann = cluster_stocks(stocks)
    ann = enrich_annotations(stocks, raw_ann)
    if not clusters and not any(v for v in ann.values()):
        return []

    lines = [
        "",
        "## 同线 / 类似标的",
        "",
        "同题材簇内互相为 **类似标的**（可联动观察）；「似近月龙头」指与近月高标同主线。",
        "",
        "| 题材簇 | 数量 | 龙头参考 | 同线成员 |",
        "|--------|------|----------|----------|",
    ]
    for c in clusters:
        members = "、".join(
            f"{m['名称']}({m['连板数']})" for m in c["成员"][:6]
        )
        if len(c["成员"]) > 6:
            members += "…"
        lines.append(
            f"| {c['题材簇']} | {c['数量']} | {c['龙头参考']} | {members} |"
        )

    singled = [
        s
        for s in stocks
        if ann.get(str(s.get("代码", "")).zfill(6), "-") != "-"
        and assign_theme(s.get("涨停原因", ""), s.get("所属行业", ""))
        not in {c["题材簇"] for c in clusters if c["数量"] >= 2}
    ]
    if singled:
        lines.extend(["", "### 个股标注", ""])
        for s in sorted(singled, key=lambda x: -int(x.get("连板数", 0) or 0)):
            code = str(s.get("代码", "")).zfill(6)
            tag = ann.get(code, "-")
            if tag != "-":
                lines.append(f"- **{s.get('名称')}**（{code}）：{tag}")
    lines.append("")
    return lines
