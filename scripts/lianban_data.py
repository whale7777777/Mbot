# -*- coding: utf-8 -*-
"""
连板分析统一数据源：默认东方财富 akshare；可选 TuShare limit_list_d（长历史）。

配置：`docs/03-智能策略/连板数据/lianban_config.json`
Token 优先级：环境变量 LIANBAN_TUSHARE_TOKEN / TUSHARE_TOKEN → 配置文件 → utils/configure/config.json
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from lianban_paths import CONFIG_EXAMPLE, CONFIG_PATH, ensure_doc_dir

PROJECT_CONFIG = ROOT / "utils" / "configure" / "config.json"

_EM_COLUMNS = [
    "代码",
    "名称",
    "连板数",
    "涨跌幅",
    "最新价",
    "换手率",
    "成交额",
    "封板资金",
    "首次封板时间",
    "最后封板时间",
    "炸板次数",
    "所属行业",
]


def load_lianban_config() -> dict[str, Any]:
    path = CONFIG_PATH if CONFIG_PATH.is_file() else CONFIG_EXAMPLE
    if not path.is_file():
        return {
            "data_source": "em",
            "fallback_to_em": True,
            "trade_days_target": 15,
            "lookback_pairs": 10,
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "data_source": "em",
            "fallback_to_em": True,
            "trade_days_target": 15,
            "lookback_pairs": 10,
        }


def resolve_tushare_token(cfg: dict[str, Any]) -> str:
    for env_key in (
        "LIANBAN_TUSHARE_TOKEN",
        cfg.get("tushare_token_env") or "TUSHARE_TOKEN",
    ):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val
    if cfg.get("tushare_token", "").strip():
        return str(cfg["tushare_token"]).strip()
    rel = cfg.get("read_token_from")
    if rel:
        p = (CONFIG_PATH.parent / rel).resolve()
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                field = cfg.get("read_token_field", "ts_token")
                tok = data.get(field, "")
                if str(tok).strip():
                    return str(tok).strip()
            except (json.JSONDecodeError, OSError):
                pass
    if PROJECT_CONFIG.is_file():
        try:
            data = json.loads(PROJECT_CONFIG.read_text(encoding="utf-8"))
            tok = data.get("ts_token", "")
            if str(tok).strip():
                return str(tok).strip()
        except (json.JSONDecodeError, OSError):
            pass
    return ""


@lru_cache(maxsize=1)
def get_tushare_pro(token: str):
    import tushare as ts

    if not token:
        raise ValueError("未配置 TuShare token")
    return ts.pro_api(token)


def ts_code_to_a_share(ts_code: str) -> str:
    return str(ts_code).split(".")[0].zfill(6)


def fetch_zt_pool_tushare(date_yyyymmdd: str, token: str) -> pd.DataFrame | None:
    try:
        pro = get_tushare_pro(token)
        df = pro.limit_list_d(trade_date=date_yyyymmdd, limit_type="U")
        if df is None or df.empty:
            return None
    except Exception as exc:
        print(f"[tushare] {date_yyyymmdd} 拉取失败: {exc}", file=sys.stderr)
        return None

    col_map = {
        "ts_code": "代码",
        "name": "名称",
        "limit_times": "连板数",
        "pct_chg": "涨跌幅",
        "close": "最新价",
        "fd_amount": "封板资金",
        "first_time": "首次封板时间",
        "last_time": "最后封板时间",
        "open_times": "炸板次数",
        "industry": "所属行业",
    }
    out = pd.DataFrame()
    for src, dst in col_map.items():
        if src in df.columns:
            out[dst] = df[src]
    if "代码" not in out.columns and "ts_code" in df.columns:
        out["代码"] = df["ts_code"].map(ts_code_to_a_share)
    else:
        out["代码"] = out["代码"].map(ts_code_to_a_share)
    if "连板数" in out.columns:
        out["连板数"] = pd.to_numeric(out["连板数"], errors="coerce")
    if "炸板次数" in out.columns:
        out["炸板次数"] = pd.to_numeric(out["炸板次数"], errors="coerce").fillna(0).astype(int)
    if "涨跌幅" in out.columns:
        out["涨跌幅"] = pd.to_numeric(out["涨跌幅"], errors="coerce")
    if "封板资金" in out.columns:
        out["封板资金"] = pd.to_numeric(out["封板资金"], errors="coerce")
    if "首次封板时间" in out.columns:
        out["首次封板时间"] = out["首次封板时间"].astype(str).str.replace(":", "", regex=False)
    out.attrs["source"] = "tushare"
    return out


def fetch_zt_pool_em(date_yyyymmdd: str) -> pd.DataFrame | None:
    try:
        import akshare as ak

        df = ak.stock_zt_pool_em(date=date_yyyymmdd)
        if df is None or df.empty:
            return None
        df = df.copy()
        df.attrs["source"] = "em"
        return df
    except Exception as exc:
        print(f"[em] {date_yyyymmdd} 拉取失败: {exc}", file=sys.stderr)
        return None


def fetch_zt_pool(date_yyyymmdd: str, cfg: dict[str, Any] | None = None) -> pd.DataFrame | None:
    """统一涨停池接口，列名与东方财富口径对齐。"""
    cfg = cfg or load_lianban_config()
    source = str(cfg.get("data_source", "em")).lower()
    fallback = bool(cfg.get("fallback_to_em", True))

    if source in ("em", "akshare", "eastmoney"):
        return fetch_zt_pool_em(date_yyyymmdd)

    if source == "tushare":
        token = resolve_tushare_token(cfg)
        if token:
            df = fetch_zt_pool_tushare(date_yyyymmdd, token)
            if df is not None and not df.empty:
                return df
            if not fallback:
                return None
        elif not fallback:
            print("[lianban] 未配置 tushare token 且 fallback_to_em=false", file=sys.stderr)
            return None

    return fetch_zt_pool_em(date_yyyymmdd)


def get_open_trade_dates(
    end_date: str | None = None,
    count: int = 35,
    cfg: dict[str, Any] | None = None,
) -> list[str]:
    """
    返回截至 end_date（含）的最近 count 个开市日，升序 YYYYMMDD。
    TuShare 可用时用 trade_cal；否则按自然日扫描涨停池（东方财富）。
    """
    cfg = cfg or load_lianban_config()
    source = str(cfg.get("data_source", "em")).lower()
    end = end_date or datetime.now().strftime("%Y%m%d")

    if source in ("em", "akshare", "eastmoney"):
        pass  # 下方按涨停池扫描交易日
    elif source == "tushare":
        token = resolve_tushare_token(cfg)
        if token:
            try:
                pro = get_tushare_pro(token)
                start = (
                    datetime.strptime(end, "%Y%m%d") - timedelta(days=count * 2 + 30)
                ).strftime("%Y%m%d")
                cal = pro.trade_cal(
                    exchange="SSE",
                    start_date=start,
                    end_date=end,
                    is_open="1",
                    fields="cal_date",
                )
                if cal is not None and not cal.empty:
                    days = sorted(cal["cal_date"].astype(str).tolist())
                    return days[-count:]
            except Exception as exc:
                print(f"[tushare] trade_cal 失败，回退扫描: {exc}", file=sys.stderr)

    end_dt = datetime.strptime(end, "%Y%m%d").date()
    found: list[str] = []
    max_scan = count * 3 + 30
    fetch_fn = fetch_zt_pool_em if source in ("em", "akshare", "eastmoney") else fetch_zt_pool
    for i in range(0, max_scan):
        d = end_dt - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        df = fetch_fn(ds) if fetch_fn is fetch_zt_pool_em else fetch_zt_pool(ds, cfg)
        if df is not None and not df.empty:
            found.append(ds)
        if len(found) >= count:
            break
    found.sort()
    return found[-count:] if len(found) > count else found


def data_source_label(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_lianban_config()
    src = str(cfg.get("data_source", "em")).lower()
    if src == "tushare" and resolve_tushare_token(cfg):
        return "tushare.limit_list_d"
    return "akshare.stock_zt_pool_em"


def ensure_config_from_example() -> Path:
    ensure_doc_dir()
    if CONFIG_PATH.is_file():
        return CONFIG_PATH
    # 兼容旧路径
    legacy = ROOT / "docs" / "research" / "lianban_config.example.json"
    src = CONFIG_EXAMPLE if CONFIG_EXAMPLE.is_file() else legacy
    if src.is_file():
        CONFIG_PATH.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return CONFIG_PATH
    raise FileNotFoundError(f"缺少配置模板: {CONFIG_EXAMPLE}")


def _cli_test() -> int:
    cfg = load_lianban_config()
    token = resolve_tushare_token(cfg)
    print(f"config: {CONFIG_PATH}")
    print(f"data_source: {cfg.get('data_source')}")
    print(f"token: {'已配置(' + str(len(token)) + '字符)' if token else '未配置'}")
    print(f"label: {data_source_label(cfg)}")
    days = get_open_trade_dates(count=35, cfg=cfg)
    print(f"trade_days: {len(days)}", end="")
    if days:
        print(f"  [{days[0]} .. {days[-1]}]")
    else:
        print()
    if days:
        sample = fetch_zt_pool(days[-1], cfg)
        print(f"latest pool rows: {len(sample) if sample is not None else 0}")
    cfg_src = str(cfg.get("data_source", "em")).lower()
    if cfg_src in ("em", "akshare", "eastmoney"):
        return 0 if len(days) >= 1 else 1
    return 0 if len(days) >= 30 and token else 1


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="检查配置与数据可用性")
    ap.add_argument("--init", action="store_true", help="从 example 生成 lianban_config.json")
    ns = ap.parse_args()
    if ns.init:
        p = ensure_config_from_example()
        print(f"已写入: {p}")
        sys.exit(0)
    if ns.test:
        sys.exit(_cli_test())
    ap.print_help()
