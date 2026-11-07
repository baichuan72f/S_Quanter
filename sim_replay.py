# -*- coding: utf-8 -*-
r"""本地模拟盘历史回放驱动（SmallQuant 独立引擎版）。

以固定时间步长（默认 60 秒 = 1 分钟）逐日推进历史交易日，对现有策略生成
沙盒副本（strategy_id 加 sim_ 前缀）并跑单日报价。沙盒账户写进同一份
state/paper.db，因此 dashboard.py 无需改动即可看到回放结果，且不会覆盖
原有策略的状态。

本文件由 sdk 版的 sim_replay.py 移植而来，但**不依赖 bigquant**：跑批走
paper.runner.PaperRunner（paper 包），`--verify` 用独立引擎 smalltrader 的
一次性整窗回测对拍（口径与 tests/test_daily_vs_backtest.py 一致）。

用法：
  python sim_replay.py                              # 默认从 2026-01-05 开始回放
  python sim_replay.py --reset --yes                # 先清空之前的沙盒记录
  python sim_replay.py --reset-only --yes           # 仅清空沙盒，不进入回放
  python sim_replay.py --interval 0 --max-days 5    # 快速验证前 5 天
  python sim_replay.py --verify                     # 跑完与一次性回测逐日对拍净值
  python sim_replay.py --stop-file state/sim_replay.stop
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# pythonw.exe 下 sys.stdout/stderr 为 None。必须在 import 项目模块之前先补空流，
# 否则底层 print(...) 会因 None.write 直接崩溃（且无任何报错可查）。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_ROOT / 'engine')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402
import queries as queries_mod  # noqa: E402
import smalltrader as bt  # noqa: E402  （独立引擎，非 platform bigtrader）
from paper.checks import DataCheckError  # noqa: E402
from paper.local_data import LocalParquetDataSource  # noqa: E402
from paper.runner import PaperRunner, RunStopped, StrategySpec  # noqa: E402
from paper.store import StateStore  # noqa: E402

ROOT = Path(__file__).resolve().parent
DEFAULT_START = "2026-01-05"
DEFAULT_PREFIX = "sim_"
DEFAULT_INTERVAL = 60
DEFAULT_STOP_FILE = str(ROOT / "state" / "sim_replay.stop")
DEFAULT_PID_FILE = str(ROOT / "state" / "sim_replay.pid")
DEFAULT_LOG_FILE = str(ROOT / "state" / "sim_replay.log")

log = logging.getLogger("sim_replay")


# ── 命令行 ──────────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="本地模拟盘历史回放（每 1 分钟一天）")
    ap.add_argument("--db", default=str(ROOT / "state" / "paper.db"),
                    help="状态库路径（默认 state/paper.db）")
    ap.add_argument("--notifications", default=str(ROOT / "notifications"),
                    help="通知目录根路径")
    ap.add_argument("--start-date", default=DEFAULT_START,
                    help="回放起始日（默认 2026-01-05）")
    ap.add_argument("--end-date", default="",
                    help="回放结束日（默认到本地最新行情日）")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help="每两个交易日之间的真实等待秒数（默认 60，设 0 则不停顿连续跑）")
    ap.add_argument("--max-days", type=int, default=0,
                    help="最多跑多少个交易日（0=无限制，用于快速测试）")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX,
                    help="沙盒 strategy_id 前缀（默认 sim_）")
    ap.add_argument("--strategies", nargs="+", default=None,
                    help="显式指定策略文件路径；默认用状态库里 status≠stopped 的策略")
    ap.add_argument("--reset", action="store_true",
                    help="清空所有符合前缀的沙盒账户后重新开始")
    ap.add_argument("--reset-only", action="store_true",
                    help="仅清空符合前缀的沙盒账户，不进入回放")
    ap.add_argument("--yes", action="store_true",
                    help="reset / reset-only 时无需二次确认")
    ap.add_argument("--stop-file", default=DEFAULT_STOP_FILE,
                    help="停止信号文件；存在时当前交易日结束后退出")
    ap.add_argument("--pid-file", default=DEFAULT_PID_FILE,
                    help="PID 文件路径")
    ap.add_argument("--log-file", default=DEFAULT_LOG_FILE,
                    help="日志文件路径")
    ap.add_argument("--verify", action="store_true",
                    help="回放结束后，与该策略的一次性整窗回测逐日对拍净值并报告差异")
    return ap.parse_args()


# ── 日志 ─────────────────────────────────────────────────────────────────────
def _setup_logging(log_file: str) -> None:
    """配置日志。pythonw.exe 下 sys.stdout/stderr 为 None，需先补一个空流，
    否则 logging.StreamHandler / print 会因 None.write 直接崩溃。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    for h in handlers:
        h.setFormatter(fmt)


# ── 交易日历 ─────────────────────────────────────────────────────────────────
def _latest_bar_date(con) -> str:
    """取本地行情最新日（股票/基金取最大者，避免市场差异）。"""
    dates = []
    for table in ("cn_stock_real_bar1d", "cn_fund_real_bar1d"):
        try:
            rows = con.execute(f"SELECT MAX(date) AS d FROM {table}").fetchall()
            if rows and rows[0][0] is not None:
                dates.append(str(rows[0][0])[:10])
        except Exception:
            pass
    return max(dates) if dates else ""


def _calendar_days(con, start: str, end: str) -> List[str]:
    rows = con.execute(
        "SELECT date FROM trading_days WHERE market_code='CN' "
        "AND date BETWEEN DATE(?) AND DATE(?) ORDER BY date",
        [start, end],
    ).fetchall()
    return [str(r[0])[:10] for r in rows]


def _snap_to_next_trading_day(con, day: str) -> str:
    rows = con.execute(
        "SELECT MIN(date) FROM trading_days WHERE market_code='CN' AND date>=DATE(?)",
        [day],
    ).fetchall()
    return str(rows[0][0])[:10] if rows and rows[0][0] else day


# ── 绩效日期（沙盒已跑日） ───────────────────────────────────────────────────
def _done_dates(store: StateStore, strategy_id: str) -> List[str]:
    """策略已跑（有绩效行）的交易日，来自状态库 account_performances。"""
    return [str(r['trading_day'])[:10]
            for r in store.read_performances(strategy_id)]


def _perf_curve(store: StateStore, strategy_id: str) -> Dict[str, float]:
    """策略已跑绩效 → {trading_day: portfolio_value}（升序）。"""
    return {str(r['trading_day'])[:10]: float(r['portfolio_value'])
            for r in store.read_performances(strategy_id)}


# ── 策略发现 ─────────────────────────────────────────────────────────────────
def _load_source_strategies(args: argparse.Namespace,
                            store: StateStore) -> List[StrategySpec]:
    """返回要回放的策略 spec 列表（未加前缀的原始 spec）。"""
    paths: List[str] = []
    if args.strategies:
        paths = list(args.strategies)
    else:
        for row in store.list_strategies():
            sid = row.get("strategy_id") or ""
            st = row.get("status") or ""
            # 不要把已经生成的沙盒账户再当成源策略
            if sid.startswith(args.prefix):
                continue
            # 成功跑过的策略状态为 'ok'，也纳入回放；只有 'stopped' 显式排除
            if st == "stopped":
                continue
            src = (row.get("config") or {}).get("source") or ""
            if src and Path(src).is_file():
                paths.append(src)
            else:
                log.warning("[%s] 源文件不存在或已移动: %s", sid, src)
        if not paths:
            log.error("状态库中没有可回放的策略（需源文件存在且未停止）")
            sys.exit(2)

    specs = []
    for p in paths:
        try:
            spec = StrategySpec.load(p)
        except Exception as e:
            log.error("加载策略失败 %s: %s", p, e)
            continue
        specs.append(spec)
        log.info("发现策略: %s (%s)  source=%s", spec.strategy_id,
                 spec.strategy_name, p)
    if not specs:
        log.error("没有成功加载任何策略")
        sys.exit(2)
    return specs


def _make_sim_spec(spec: StrategySpec, prefix: str) -> StrategySpec:
    """复制原始 spec 并生成沙盒 ID / 名称（复用同一份 module/meta）。"""
    meta = dict(spec.meta)
    orig_id = spec.strategy_id
    meta["strategy_id"] = f"{prefix}{orig_id}"
    meta["strategy_name"] = f"{spec.strategy_name} (sim)"
    return StrategySpec(spec.module, spec.source, meta)


# ── 状态清理 ─────────────────────────────────────────────────────────────────
def _reset_sim_accounts(store: StateStore, prefix: str,
                        notifications_root: Path) -> None:
    removed = 0
    for row in store.list_strategies():
        sid = row.get("strategy_id") or ""
        if sid.startswith(prefix):
            store.reset_strategy(sid)
            ndir = notifications_root / sid
            if ndir.exists():
                shutil.rmtree(ndir, ignore_errors=True)
            log.info("已清空沙盒账户: %s", sid)
            removed += 1
    if removed == 0:
        log.info("没有符合前缀 %s 的沙盒账户需要清空", prefix)


# ── 与一次性回测核对（独立引擎） ─────────────────────────────────────────────
def _verify_against_backtest(store, sim_spec, src_spec, threshold=1e-4):
    """把沙盒逐日增量结果与「同一策略同一区间」的一次性整窗回测逐日对拍净值。

    与 sdk 版的差异：一次性回测用**本项目的独立引擎** smalltrader（本地
    LocalParquetDataSource 取数，逐字镜像 tests/test_daily_vs_backtest.py），
    而不是平台 bigtrader —— 与本项目「不依赖 bigquant」的约束一致。

    一致 → 打通过摘要并返回 True；有差异 → 打印逐日差异明细并返回 False。
    """
    sim_sid = sim_spec.strategy_id
    src = src_spec.strategy if hasattr(src_spec, "strategy") else src_spec.meta
    name = src.get("strategy_name")
    if not name:
        name = getattr(src_spec, 'strategy_name', src_spec.strategy_id)

    daily = _perf_curve(store, sim_sid)
    if not daily:
        log.warning("[verify] [%s] 沙盒账户没有绩效数据，无法核对（可先跑一次回放）",
                    sim_sid)
        return True

    inc = {d: v for d, v in sorted(daily.items())}
    sdates = sorted(inc)
    start, end = sdates[0], sdates[-1]
    warmup = int(getattr(src_spec, 'warmup_days', None) or 120)

    try:
        ds = LocalParquetDataSource()
        universe = list(src_spec.instruments)
        qs = (pd.Timestamp(start) - pd.Timedelta(days=warmup)).strftime('%Y-%m-%d')
        data = ds.get_bar_data(universe, qs, end)
        idx = ds.get_index_bar_data(src_spec.benchmark, qs, end)
        data[src_spec.benchmark] = idx
        ev = ds.get_dividend_events(universe, qs, end)
        perf = bt.run(
            instruments=universe, start_date=start, end_date=end,
            data=dict(data), capital_base=float(src_spec.capital_base),
            initialize=src_spec.initialize,
            before_trading_start=src_spec.before_trading_start,
            handle_data=src_spec.handle_data,
            after_trading=src_spec.after_trading,
            benchmark=src_spec.benchmark,
            order_price_field_buy=src_spec.order_price_field_buy,
            order_price_field_sell=src_spec.order_price_field_sell,
            dividend_events=ev if len(ev) else None,
            render='none',
        )
        rpd = perf.read_raw_perf()
    except Exception as e:  # noqa: BLE001
        log.error("[verify] [%s] 一次性回测运行失败，跳过核对: %s", sim_sid, e)
        return False

    back = {}
    for dt, v in rpd['portfolio_value'].items():
        key = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)[:10]
        back[key] = float(v)

    diffs, worst, compared = [], 0.0, 0
    for d in sorted(set(inc) & set(back)):
        diff = abs(inc[d] - back[d])
        rel = diff / max(abs(back[d]), 1.0)
        worst = max(worst, rel)
        compared += 1
        if rel >= threshold:
            diffs.append((d, inc[d], back[d], diff, rel))
    missing = sorted(set(inc) - set(back))

    ctx = f"[verify] [{sim_sid}] {name}  {start} ~ {end}"
    if diffs:
        log.error("%s ❌ 检出 %d 天净值差异（阈值 %.0e）:", ctx, len(diffs), threshold)
        for d, iv, bv, diff, rel in diffs[:20]:
            log.error("     %s  增量=%.4f  回测=%.4f  diff=%.4f  rel=%.2e",
                      d, iv, bv, diff, rel)
        if len(diffs) > 20:
            log.error("     … 另有 %d 天差异", len(diffs) - 20)
        if missing:
            log.warning("     ⚠ 另有 %d 天增量有而回测未覆盖: %s",
                        len(missing), missing[:10])
        return False

    log.info("%s ✅ 净值逐日一致（%d 天, 最大相对偏差 %.2e）", ctx, compared, worst)
    if missing:
        log.warning("[verify] [%s] ⚠ 有 %d 天回测未覆盖（不影响一致判定）。",
                    sim_sid, len(missing))
    return True


def _run_verification(store, raw_specs, sim_specs):
    """对每个源策略，把沙盒增量结果与一次性回测对拍，汇总报告。"""
    if not raw_specs:
        return
    log.info("════ 开始与一次性回测核对（逐日净值对拍，独立引擎）════")
    all_ok = True
    n_ok = 0
    for sim_spec, src_spec in zip(sim_specs, raw_specs):
        try:
            ok = _verify_against_backtest(store, sim_spec, src_spec)
        except Exception as e:  # noqa: BLE001
            log.error("[verify] [%s] 核对异常: %s", sim_spec.strategy_id, e)
            ok = False
        all_ok = all_ok and ok
        if ok:
            n_ok += 1
    log.info("════ 核对%s：%d/%d 个策略净值一致 ════",
             "通过 ✅" if all_ok else "存在差异 ❌", n_ok, len(raw_specs))


# ── 回放主循环 ───────────────────────────────────────────────────────────────
def main() -> int:
    args = _parse_args()
    _setup_logging(args.log_file)

    # PID
    Path(args.pid_file).parent.mkdir(parents=True, exist_ok=True)
    Path(args.pid_file).write_text(str(os.getpid()), encoding="utf-8")

    # 停止信号文件：启动时如果存在，先删除，否则一启动就退出
    stop_path = Path(args.stop_file)
    if stop_path.exists():
        stop_path.unlink()
        log.info("已清除上次的停止信号文件: %s", args.stop_file)

    store = StateStore(args.db)
    con = queries_mod.connect()

    # 清理旧沙盒
    if args.reset or args.reset_only:
        if not args.yes:
            log.error("--reset / --reset-only 需要配合 --yes 确认")
            return 2
        _reset_sim_accounts(store, args.prefix, Path(args.notifications))
        if args.reset_only:
            log.info("仅清空模式，已清空沙盒账户，不进入回放")
            return 0

    # 发现策略并生成沙盒 spec
    raw_specs = _load_source_strategies(args, store)
    sim_specs: List[StrategySpec] = [_make_sim_spec(s, args.prefix)
                                     for s in raw_specs]

    # 交易日序列
    end_date = args.end_date or _latest_bar_date(con)
    if not end_date:
        log.error("无法确定最新行情日，请检查本地 parquet 数据")
        return 1

    start_day = _snap_to_next_trading_day(con, args.start_date)
    all_days = _calendar_days(con, start_day, end_date)
    if not all_days:
        log.error("%s ~ %s 之间没有可回放交易日", start_day, end_date)
        return 1

    # 跳过已经跑过的沙盒交易日（支持断点续跑）
    done_by_sid: Dict[str, set] = {
        s.strategy_id: set(_done_dates(store, s.strategy_id)) for s in sim_specs
    }
    pending = [d for d in all_days
               if any(d not in done_by_sid[s.strategy_id] for s in sim_specs)]
    if args.max_days:
        pending = pending[: args.max_days]

    if not pending:
        log.info("没有待推进的沙盒交易日（均已存在），需重跑请加 --reset --yes%s",
                 "；下面直接核对已有进度" if args.verify else "")
    else:
        log.info("开始回放: %s ~ %s, 共 %d 个交易日, %d 个策略, 间隔 %d 秒",
                 pending[0], pending[-1], len(pending), len(sim_specs),
                 args.interval)
    days = pending

    # 中断信号
    stop_requested = False

    def _request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True
        log.info("收到中断信号，当前交易日跑完后退出")

    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)

    should_stop = lambda: stop_requested or stop_path.exists()  # noqa: E731

    # 每个沙盒策略一个 runner（避免每天重建）
    runners: Dict[str, PaperRunner] = {
        spec.strategy_id: PaperRunner(store, out_dir=args.notifications,
                                      should_stop=should_stop)
        for spec in sim_specs
    }

    try:
        total_days = len(days)
        for idx, day in enumerate(days, 1):
            if should_stop():
                log.info("收到停止信号，退出回放")
                break

            log.info("━━━━  [%d/%d] 交易日 %s  ━━━━", idx, total_days, day)
            for spec in sim_specs:
                if should_stop():
                    break
                # 已经跑过的策略-日可以跳过（每天的首次检查）
                if day in done_by_sid[spec.strategy_id]:
                    log.info("[%s] %s 已存在，跳过", spec.strategy_id, day)
                    continue

                runner = runners[spec.strategy_id]
                try:
                    sd = runner.run_one_day(spec, day)
                    log.info(
                        "[%s] %s OK  pv=%.2f today=%.2f%% planned=%d"
                        + (" 清单=%s" if sd.get("order_list_path") else ""),
                        spec.strategy_id, day,
                        sd.get("portfolio_value", 0),
                        sd.get("today_return", 0) * 100,
                        sd.get("planned_orders", 0),
                        sd.get("order_list_path") or "",
                    )
                except RunStopped:
                    log.info("[%s] %s 被停止", spec.strategy_id, day)
                    return 0
                except DataCheckError as e:
                    log.warning("[%s] %s 被数据校验拦截: %s",
                                spec.strategy_id, day, e)
                except Exception as e:  # noqa: BLE001
                    log.exception("[%s] %s 运行异常: %s",
                                  spec.strategy_id, day, e)

            # 更新已跑集合（后续断点续跑用）
            for spec in sim_specs:
                done_by_sid[spec.strategy_id].add(day)

            # 节奏控制（把下一日也打出来，方便看推进进度）
            if idx < total_days and args.interval > 0:
                next_day = days[idx]
                log.info("等待 %d 秒进入下一交易日 %s …", args.interval, next_day)
                # 分段 sleep，便于快速响应停止信号
                slept = 0
                while slept < args.interval and not should_stop():
                    step = min(1, args.interval - slept)
                    time.sleep(step)
                    slept += step
    except KeyboardInterrupt:
        log.info("用户中断，当前日已完成后退出")
    finally:
        log.info("回放结束")
        if stop_path.exists():
            try:
                stop_path.unlink()
            except Exception:
                pass
        try:
            Path(args.pid_file).unlink(missing_ok=True)
        except Exception:
            pass

    # 回放（或断点续跑）结束后，按需与该策略一次性回测逐日对拍，报告差异。
    # 注意：必须在 store.close() 之前做 —— close() 会把连接置 None，之后
    # 任何 state store 读取都会抛 'NoneType' object has no attribute 'execute'。
    if args.verify:
        _run_verification(store, raw_specs, sim_specs)
    try:
        store.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())