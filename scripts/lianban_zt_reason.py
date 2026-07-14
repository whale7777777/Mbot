# -*- coding: utf-8 -*-
"""涨停原因：同花顺涨停池（默认）或 TuShare limit_list_ths。"""

from __future__ import annotations

import sys
from typing import Any

import requests

from lianban_data import load_lianban_config, resolve_tushare_token, ts_code_to_a_share


def _norm_date(date_yyyymmdd: str) -> str:
    s = str(date_yyyymmdd).strip().replace("-", "")
    if len(s) != 8:
        raise ValueError(f"日期格式应为 YYYYMMDD: {date_yyyymmdd}")
    return s


def fetch_zt_reason_map_ths(date_yyyymmdd: str) -> dict[str, dict[str, str]]:
    """
    同花顺 dataapi limit_up_pool，字段 reason_type 为涨停原因。
    返回 code(6位) -> {涨停原因, 涨停类型, 连板标签}
    """
    date = _norm_date(date_yyyymmdd)
    url = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://data.10jqka.com.cn/",
    }
    page = 1
    limit = 200
    out: dict[str, dict[str, str]] = {}

    while True:
        params = {
            "page": str(page),
            "limit": str(limit),
            "field": "code,name,reason_type,high_days,limit_up_type,order_amount,turnover",
            "filter": "HS,GEM2STAR",
            "order_field": "code",
            "order_type": "0",
            "date": date,
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            print(f"[ths] {date} 涨停原因拉取失败: {exc}", file=sys.stderr)
            break

        if payload.get("status_code") != 0:
            print(f"[ths] {date} 接口异常: {payload}", file=sys.stderr)
            break

        data = payload.get("data") or {}
        info = data.get("info") or []
        if not info:
            break

        for row in info:
            code = str(row.get("code", "")).zfill(6)
            if not code or code == "000000":
                continue
            reason = str(row.get("reason_type") or "").strip()
            order_amt = row.get("order_amount")
            turnover = row.get("turnover")
            out[code] = {
                "涨停原因": reason or "-",
                "涨停类型": str(row.get("limit_up_type") or "").strip(),
                "连板标签": str(row.get("high_days") or "").strip(),
                "封板资金": float(order_amt) if order_amt not in (None, "") else None,
                "成交额": float(turnover) if turnover not in (None, "") else None,
            }

        total = int((data.get("page") or {}).get("total") or 0)
        if page * limit >= total or len(info) < limit:
            break
        page += 1

    return out


def fetch_zt_reason_map_tushare(
    date_yyyymmdd: str, token: str
) -> dict[str, dict[str, str]]:
    date = _norm_date(date_yyyymmdd)
    try:
        import tushare as ts

        pro = ts.pro_api(token)
        df = pro.limit_list_ths(
            trade_date=date,
            limit_type="涨停池",
            fields="ts_code,name,lu_desc,tag,status,limit_type",
        )
    except Exception as exc:
        print(f"[tushare] {date} 涨停原因拉取失败: {exc}", file=sys.stderr)
        return {}

    if df is None or df.empty:
        return {}

    out: dict[str, dict[str, str]] = {}
    for _, row in df.iterrows():
        code = ts_code_to_a_share(row.get("ts_code", ""))
        reason = str(row.get("lu_desc") or "").strip()
        out[code] = {
            "涨停原因": reason or "-",
            "涨停类型": str(row.get("limit_type") or "").strip(),
            "连板标签": str(row.get("status") or row.get("tag") or "").strip(),
        }
    return out


def fetch_zt_reason_map(
    date_yyyymmdd: str, cfg: dict[str, Any] | None = None
) -> dict[str, dict[str, str]]:
    cfg = cfg or load_lianban_config()
    source = str(cfg.get("zt_reason_source", "ths")).lower()
    fallback = bool(cfg.get("zt_reason_fallback", True))

    if source in ("ths", "10jqka", "tonghuashun"):
        m = fetch_zt_reason_map_ths(date_yyyymmdd)
        if m or not fallback:
            return m

    if source == "tushare":
        token = resolve_tushare_token(cfg)
        if token:
            m = fetch_zt_reason_map_tushare(date_yyyymmdd, token)
            if m or not fallback:
                return m

    if source not in ("none", "off", "false", "0") and fallback:
        return fetch_zt_reason_map_ths(date_yyyymmdd)

    return {}


def attach_zt_reasons(
    table: pd.DataFrame, reason_map: dict[str, dict[str, str]]
) -> pd.DataFrame:
    if table.empty or not reason_map:
        if table.empty:
            return table
        out = table.copy()
        if "涨停原因" not in out.columns:
            out["涨停原因"] = "-"
        return out

    reasons = []
    for _, row in table.iterrows():
        code = str(row.get("代码", "")).zfill(6)
        info = reason_map.get(code, {})
        reasons.append(info.get("涨停原因") or "-")
    out = table.copy()
    out["涨停原因"] = reasons
    return out
