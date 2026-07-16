# -*- coding: utf-8 -*-
"""连板脚本统一输出路径（文档目录）。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRATEGY_DIR = ROOT / "docs" / "03-智能策略"
STRATEGY_MD = STRATEGY_DIR / "连板策略.md"
DOC_DIR = STRATEGY_DIR / "连板数据"
# 按交易日落盘：每日一个 md + json，文件名为 YYYYMMDD
DAILY_DIR = DOC_DIR / "每日"

CONFIG_PATH = DOC_DIR / "lianban_config.json"
CONFIG_EXAMPLE = DOC_DIR / "lianban_config.example.json"

CALIB_JSON = DOC_DIR / "连板预测校准.json"
BACKTEST_MD = DOC_DIR / "连板滚动回测.md"
BACKTEST_DETAIL_MD = DOC_DIR / "连板滚动验证明细.md"
WEEKLY_MD = DOC_DIR / "连板晋级率周跟踪.md"
LEADER_MD = DOC_DIR / "近月高标龙头分析.md"
STABILIZE_MD = DOC_DIR / "连板回踩企稳扫描.md"
OVERVIEW_MD = DOC_DIR / "数据总览.md"

# 模拟盘
PAPER_DIR = DOC_DIR / "模拟盘"
PAPER_CONFIG = PAPER_DIR / "paper_config.json"
PAPER_STATE_JSON = PAPER_DIR / "持仓.json"
PAPER_TRADES_JSON = PAPER_DIR / "成交记录.json"
PAPER_LOG_MD = PAPER_DIR / "操作记录.md"
PAPER_DAILY_DIR = PAPER_DIR / "每日"


def daily_md(date: str) -> Path:
    return DAILY_DIR / f"{date}.md"


def daily_json(date: str) -> Path:
    return DAILY_DIR / f"{date}.json"


def list_daily_dates() -> list[str]:
    """已落盘交易日 YYYYMMDD，新到旧。"""
    if not DAILY_DIR.is_dir():
        return []
    dates = []
    for p in DAILY_DIR.glob("*.json"):
        stem = p.stem
        if len(stem) == 8 and stem.isdigit():
            dates.append(stem)
    return sorted(set(dates), reverse=True)


def latest_daily_json() -> Path | None:
    dates = list_daily_dates()
    if not dates:
        return None
    p = daily_json(dates[0])
    return p if p.is_file() else None


def latest_daily_md() -> Path | None:
    dates = list_daily_dates()
    if not dates:
        return None
    p = daily_md(dates[0])
    return p if p.is_file() else None


def ensure_doc_dir() -> Path:
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    return DOC_DIR


def ensure_daily_dir() -> Path:
    ensure_doc_dir()
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    return DAILY_DIR


def ensure_paper_dir() -> Path:
    ensure_doc_dir()
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    return PAPER_DIR


def paper_daily_md(date: str) -> Path:
    return PAPER_DAILY_DIR / f"{date}.md"


# 兼容旧引用
def today_md(date: str) -> Path:
    return daily_md(date)


def today_json(date: str) -> Path:
    return daily_json(date)
