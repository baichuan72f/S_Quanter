#!/usr/bin/env python3
"""
run_daily.py —— 本地模拟盘唯一入口（cron / 任务计划 / 手工均可调用）

用法
----
  # 跑最新可得交易日（数据到位后日常跑批）
  python3 run_daily.py --strategy strategies/dual_ma_demo.py

  # 指定日期跑单日 / 补跑（该日已跑过会幂等覆盖）
  python3 run_daily.py --strategy strategies/dual_ma_demo.py --date 2026-08-07

  # 区间断点续跑（已跑日自动跳过）
  python3 run_daily.py --strategy strategies/dual_ma_demo.py \
      --from 2026-08-03 --to 2026-08-07

  # 补跑（任务计划用）：从上次已跑日跑到数据最新交易日
  python3 run_daily.py --strategy strategies/dual_ma_demo.py --catchup

  # 查看运行状态 / 净值序列
  python3 run_daily.py --strategy strategies/dual_ma_demo.py --status

  # 回滚某日（含）之后的状态后重跑
  python3 run_daily.py --strategy strategies/dual_ma_demo.py \
      --rollback-from 2026-08-05 --yes

  # 首次运行前：用一次性回测对拍状态装卸正确性（最硬自检，见 tests/）
  python3 tests/test_daily_vs_backtest.py

调度：Linux crontab 示例（15:30 数据到位后）
  30 15 * * 1-5 cd /path/to/模拟盘工具 && python3 run_daily.py \
      --strategy strategies/dual_ma_demo.py >> logs/cron.log 2>&1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from paper.checks import DataCheckError  # noqa: E402
from paper.local_data import LocalParquetDataSource  # noqa: E402
from paper.runner import PaperRunner, StrategySpec  # noqa: E402
from paper.store import StateStore  # noqa: E402

DEFAULT_DB = str(_ROOT / 'state' / 'paper.db')


def _fmt(v) -> str:
    if isinstance(v, float):
        return f'{v:,.4f}'
    return str(v)


def show_status(runner: PaperRunner, spec: StrategySpec) -> None:
    store = runner.store
    cfg = store.get_strategy(spec.strategy_id)
    if cfg is None:
        print(f'策略 {spec.strategy_id} 尚未注册（从未跑过）')
        return
    ran = store.ran_trading_days(spec.strategy_id)
    print(f'策略      : {cfg["strategy_name"]} ({spec.strategy_id})')
    print(f'初始资金  : {float(cfg["capital_base"]):,.2f}')
    print(f'交易日区间: {ran[0] if ran else "-"} ~ {ran[-1] if ran else "-"}'
          f'（{len(ran)} 天）')
    perf = store.last_performance(spec.strategy_id)
    if perf:
        print(f'最新净值  : {perf["trading_day"]}  '
              f'总资产 {float(perf["portfolio_value"]):,.2f}')
        print(f'累计收益  : {float(perf["cumulative_return"]):.2%}   '
              f'最大回撤 {float(perf["max_drawdown"]):.2%}   '
              f'年化 {float(perf["annualized_return"]):.2%}')
    pending = store.read_planned_orders(spec.strategy_id, only_pending=True)
    print(f'待执行计划单: {len(pending)} 笔（下一交易日撮合）')
    print('\n最近 10 日绩效:')
    print(f'{"交易日":<12}{"总资产":>14}{"日收益":>10}{"累计收益":>10}'
          f'{"回撤":>8}')
    for p in store.read_performances(spec.strategy_id)[-10:]:
        print(f'{p["trading_day"]:<12}'
              f'{float(p["portfolio_value"]):>14,.2f}'
              f'{float(p["today_return"]):>10.2%}'
              f'{float(p["cumulative_return"]):>10.2%}'
              f'{float(p["max_drawdown"]):>8.2%}')


def main() -> int:
    ap = argparse.ArgumentParser(description='本地模拟盘单日跑批入口')
    ap.add_argument('--strategy', required=True, help='策略文件路径（STRATEGY 契约）')
    ap.add_argument('--db', default=DEFAULT_DB, help=f'SQLite 状态库（默认 {DEFAULT_DB}）')
    ap.add_argument('--date', help='跑指定交易日 YYYY-MM-DD（缺省=数据最新交易日）')
    ap.add_argument('--from', dest='date_from', help='区间补跑起始日')
    ap.add_argument('--to', dest='date_to', help='区间补跑结束日')
    ap.add_argument('--catchup', action='store_true',
                    help='补跑：从上次已跑日跑到数据最新交易日（任务计划用）')
    ap.add_argument('--status', action='store_true', help='查看运行状态/净值序列')
    ap.add_argument('--rollback-from', help='回滚该日（含）之后全部状态')
    ap.add_argument('--reset', action='store_true', help='清空该策略全部状态（慎用）')
    ap.add_argument('--yes', action='store_true', help='对回滚/重置操作免确认')
    ap.add_argument('--skip-checks', action='store_true', help='跳过数据完整性校验（不建议）')
    args = ap.parse_args()

    spec = StrategySpec.load(args.strategy)
    store = StateStore(args.db)
    runner = PaperRunner(store)

    # ── 破坏性操作：显式确认 ───────────────────────────────────
    if args.rollback_from or args.reset:
        if not args.yes:
            print('回滚/重置会删除已跑状态（净值曲线随之丢失），'
                  '确认请加 --yes')
            return 2
        if args.reset:
            store.reset_strategy(spec.strategy_id)
            print(f'已清空策略 {spec.strategy_id} 全部状态')
        else:
            counts = store.rollback_from(spec.strategy_id, args.rollback_from)
            print(f'已回滚 {args.rollback_from}（含）之后的状态: '
                  + ', '.join(f'{k}={v}' for k, v in counts.items() if v))
        if not (args.date or args.date_from):
            return 0

    # ── 状态查看 ───────────────────────────────────────────────
    if args.status:
        show_status(runner, spec)
        return 0

    # ── 跑批 ───────────────────────────────────────────────────
    try:
        if args.catchup:
            ds = LocalParquetDataSource()
            latest = ds.latest_bar_date(spec.instruments, spec.benchmark)
            if latest is None:
                print('本地数据为空：请先运行 localbq 数据日更脚本')
                return 1
            ran = store.ran_trading_days(spec.strategy_id)
            start = ran[-1] if ran else latest
            if latest <= start:
                print(f'\n无新增交易日（已跑到 {start}，数据最新 {latest}）')
                return 0
            results = runner.run_range(spec, start, latest)
            if not results:
                print(f'\n无新增交易日（已跑到 {start}，数据最新 {latest}）')
            else:
                last = results[-1]
                print(f'\n补跑完成：{len(results)} 个交易日 '
                      f'（{results[0]["trading_day"]} ~ {last["trading_day"]}），'
                      f'总资产 {last["portfolio_value"]:,.2f}，'
                      f'T+1 计划单 {last["planned_orders"]} 笔')
        elif args.date_from:
            to = args.date_to or args.date_from
            results = runner.run_range(spec, args.date_from, to)
            print(f'\n区间补跑完成：{len(results)} 个交易日')
        else:
            date = args.date
            if date is None:
                ds = LocalParquetDataSource()
                date = ds.latest_bar_date(spec.instruments, spec.benchmark)
                if date is None:
                    print('本地数据为空：请先运行 localbq 数据日更脚本')
                    return 1
                print(f'未指定 --date，使用数据最新交易日: {date}')
            r = runner.run_one_day(spec, date, skip_checks=args.skip_checks)
            print(f'\n{date} 跑批完成：总资产 {r["portfolio_value"]:,.2f}，'
                  f'日收益 {r["today_return"]:.2%}，'
                  f'T+1 计划单 {r["planned_orders"]} 笔')
            if r['order_list_path']:
                print(f'T+1 委托清单: {r["order_list_path"]}')
        return 0
    except DataCheckError as e:
        print(f'\n[数据校验未通过，当日中止] {e}\n'
              f'（数据到位后重跑即可；状态库未写入任何当日快照）', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
