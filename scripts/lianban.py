# -*- coding: utf-8 -*-
"""
连板策略 · 统一入口（akshare 东方财富涨停池）

默认执行（无子命令）：
  生成当日连板 Markdown 文档，并更新数据总览。

示例：
  python scripts/lianban.py
  python scripts/lianban.py today --date 20260519
  python scripts/lianban.py all
  python scripts/lianban.py backtest
  python scripts/lianban.py test
  python scripts/lianban.py batch --days 15   # 最近 N 个交易日逐日落盘
  python scripts/lianban.py backfill          # 补全涨停原因并重写 MD
  python scripts/lianban.py stabilize       # 连板回踩绿K企稳扫描
  python scripts/lianban.py paper           # 模拟盘运行（默认最近交易日）
  python scripts/lianban.py paper backfill --from 20260710 --to 20260716 --reset

产出目录：docs/03-智能策略/连板数据/每日/
  - YYYYMMDD.md / YYYYMMDD.json   每个交易日各一份
  - ../数据总览.md                 汇总索引

说明文档：docs/03-智能策略/连板策略.md
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
PY = sys.executable


def _run(script: str, *args: str) -> None:
    cmd = [PY, str(SCRIPTS / script), *args]
    print(">>", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def cmd_today(ns: argparse.Namespace) -> Path | None:
    argv = []
    if ns.date:
        argv += ["--date", ns.date]
    if ns.min_boards is not None:
        argv += ["--min-boards", str(ns.min_boards)]
    if ns.hist_pairs is not None:
        argv += ["--hist-pairs", str(ns.hist_pairs)]
    _run("lianban_today.py", *argv)
    from lianban_paths import daily_md, latest_daily_md
    from lianban_today import resolve_trade_date

    d = ns.date or resolve_trade_date(None, max_scan=60)
    out = daily_md(d) if d else latest_daily_md()
    return out if out and out.is_file() else latest_daily_md()


def cmd_publish(_: argparse.Namespace) -> None:
    _run("lianban_publish_docs.py")


def cmd_backtest(ns: argparse.Namespace) -> None:
    argv = []
    if ns.days:
        argv += ["--days", str(ns.days)]
    if ns.lookback:
        argv += ["--lookback", str(ns.lookback)]
    _run("lianban_backtest_30d.py", *argv)


def cmd_weekly(ns: argparse.Namespace) -> None:
    argv = []
    if ns.pairs:
        argv += ["--pairs", str(ns.pairs)]
    _run("lianban_jinji_weekly.py", *argv)


def cmd_all(ns: argparse.Namespace) -> None:
    cmd_backtest(ns)
    cmd_today(ns)
    cmd_publish(ns)


def cmd_test(_: argparse.Namespace) -> None:
    _run("lianban_data.py", "--test")


def cmd_init(_: argparse.Namespace) -> None:
    _run("lianban_data.py", "--init")


def cmd_recalibrate(_: argparse.Namespace) -> None:
    _run("lianban_recalibrate_15d.py")


def cmd_backfill(ns: argparse.Namespace) -> None:
    argv = []
    if ns.date:
        for d in ns.date:
            argv += ["--date", d]
    _run("lianban_backfill.py", *argv)
    cmd_publish(ns)


def cmd_stabilize(ns: argparse.Namespace) -> None:
    argv = []
    if ns.trade_days is not None:
        argv += ["--trade-days", str(ns.trade_days)]
    if ns.green_days is not None:
        argv += ["--green-days", str(ns.green_days)]
    if ns.min_boards is not None:
        argv += ["--min-boards", str(ns.min_boards)]
    if ns.min_signals is not None:
        argv += ["--min-signals", str(ns.min_signals)]
    if ns.min_price is not None:
        argv += ["--min-price", str(ns.min_price)]
    _run("lianban_stabilize_pullback.py", *argv)


def cmd_batch(ns: argparse.Namespace) -> None:
    """逐个交易日生成 每日/YYYYMMDD.md 与 .json。"""
    sys.path.insert(0, str(SCRIPTS))
    from lianban_data import get_open_trade_dates, load_lianban_config
    from lianban_paths import DAILY_DIR, ensure_daily_dir

    cfg = load_lianban_config()
    days = get_open_trade_dates(count=ns.days, cfg=cfg)
    if not days:
        print("未找到任何交易日数据。", file=sys.stderr)
        raise SystemExit(2)

    ensure_daily_dir()
    print(f"批量生成 {len(days)} 个交易日 → {DAILY_DIR}")
    print(f"区间: {days[0]} ~ {days[-1]}")

    ok, fail = 0, 0
    for d in days:
        sub = argparse.Namespace(
            date=d,
            min_boards=ns.min_boards,
            hist_pairs=ns.hist_pairs,
        )
        try:
            cmd_today(sub)
            ok += 1
        except subprocess.CalledProcessError:
            fail += 1
            print(f"[跳过] {d}", file=sys.stderr)

    cmd_publish(ns)
    print(f"\n完成: 成功 {ok} 日，失败 {fail} 日，文件目录: {DAILY_DIR}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="连板策略统一入口：默认输出当日连板文档",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command")

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--date", help="交易日 YYYYMMDD")
        sp.add_argument("--min-boards", type=int, default=2)
        sp.add_argument("--hist-pairs", type=int, default=None)

    sp_today = sub.add_parser("today", help="生成当日连板文档（默认行为）")
    add_common(sp_today)
    sp_today.set_defaults(func=cmd_today)

    sp_all = sub.add_parser("all", help="回测校准 + 当日文档 + 数据总览")
    sp_all.add_argument("--days", type=int, default=35)
    sp_all.add_argument("--lookback", type=int, default=20)
    add_common(sp_all)
    sp_all.set_defaults(func=cmd_all)

    sp_bt = sub.add_parser("backtest", help="滚动回测并写校准参数")
    sp_bt.add_argument("--days", type=int, default=35)
    sp_bt.add_argument("--lookback", type=int, default=20)
    sp_bt.set_defaults(func=cmd_backtest)

    sp_wk = sub.add_parser("weekly", help="连板晋级率周跟踪")
    sp_wk.add_argument("--pairs", type=int, default=5)
    sp_wk.set_defaults(func=cmd_weekly)

    sub.add_parser("publish", help="刷新数据总览.md").set_defaults(func=cmd_publish)
    sub.add_parser("test", help="检查 akshare 数据源").set_defaults(func=cmd_test)
    sub.add_parser("init", help="生成 lianban_config.json").set_defaults(func=cmd_init)

    sub.add_parser(
        "recalibrate", help="用每日文件夹数据核对 T+1 并重估校准"
    ).set_defaults(func=cmd_recalibrate)

    sp_batch = sub.add_parser("batch", help="批量生成最近 N 个交易日每日文档")
    sp_batch.add_argument("--days", type=int, default=15, help="交易日数量（默认 15）")
    add_common(sp_batch)
    sp_batch.set_defaults(func=cmd_batch)

    sp_bf = sub.add_parser("backfill", help="补全每日 JSON 涨停原因并重写 MD")
    sp_bf.add_argument("--date", action="append", help="指定 YYYYMMDD，可多次；默认全部")
    sp_bf.set_defaults(func=cmd_backfill)

    sp_st = sub.add_parser("stabilize", help="连板回踩绿K企稳扫描")
    sp_st.add_argument("--trade-days", type=int, default=22)
    sp_st.add_argument("--green-days", type=int, default=2, choices=[2, 3])
    sp_st.add_argument("--min-boards", type=int, default=3)
    sp_st.add_argument("--min-signals", type=int, default=1)
    sp_st.add_argument("--min-price", type=float, default=20.0, help="最新收盘价下限")
    sp_st.set_defaults(func=cmd_stabilize)

    sp_paper = sub.add_parser("paper", help="模拟盘：自动买卖并记录操作")
    sp_paper.add_argument(
        "paper_cmd",
        nargs="?",
        default="run",
        choices=["run", "backfill", "status", "reset", "expect", "notify"],
        help="子命令（默认 run）",
    )
    sp_paper.add_argument("--date", help="交易日 YYYYMMDD")
    sp_paper.add_argument("--prev-date", help="上一交易日")
    sp_paper.add_argument("--from", dest="date_from", help="回测起始日")
    sp_paper.add_argument("--to", dest="date_to", help="回测结束日")
    sp_paper.add_argument("--reset", action="store_true", help="回测前重置")
    sp_paper.add_argument("--print-only", action="store_true", help="notify：仅打印")
    sp_paper.add_argument("--dry-run", action="store_true", help="notify：不实际发送")
    sp_paper.add_argument("--skip-wait", action="store_true", help="notify/expect：不等待竞价结束")
    sp_paper.set_defaults(func=cmd_paper)

    return p


def cmd_paper(ns: argparse.Namespace) -> None:
    argv = [ns.paper_cmd]
    if ns.date:
        argv += ["--date", ns.date]
    if ns.prev_date:
        argv += ["--prev-date", ns.prev_date]
    if ns.date_from:
        argv += ["--from", ns.date_from]
    if ns.date_to:
        argv += ["--to", ns.date_to]
    if ns.reset:
        argv.append("--reset")
    if ns.print_only:
        argv.append("--print-only")
    if ns.dry_run:
        argv.append("--dry-run")
    if getattr(ns, "skip_wait", False):
        argv.append("--skip-wait")
    _run("lianban_paper.py", *argv)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)

    if ns.command is None:
        # 默认：当日文档 + 总览
        if not hasattr(ns, "date"):
            ns.date = None
            ns.min_boards = 2
            ns.hist_pairs = None
        sys.path.insert(0, str(SCRIPTS))
        out = cmd_today(ns)
        cmd_publish(ns)
        if out and out.is_file():
            print(f"\n当日连板文档: {out}")
        else:
            print("\n当日文档路径见上方输出。", file=sys.stderr)
        return 0

    ns.func(ns)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as e:
        raise SystemExit(e.returncode) from e
