"""
状态装卸 load_state / save_state —— 模拟盘跨日缝合的核心
==========================================================

对应平台 tradingserver.stateloader.load_state / statesaver.save_state（Nuitka
编译，本地不可用，照其签名与语义自写，口径依据 docs/BigQuant模拟盘实现研究
与本地复刻方案.md 第 3 节）。

load_state  组装 T-1 外置状态：持仓/现金快照、未成交计划订单、user_store。
            取「最近一个有快照的交易日」而非「日历前一交易日」——漏跑一天也能
            续上；但缺口必须打 WARN（漏跑那天的信号已错过，净值有跳跃）。
save_state  当日跑完落库：先删后写（平台 remove-then-insert 同款幂等语义），
            未成交新单导出为 T+1 计划订单（对齐 PlannedOrder.to_db 只导出
            未发出订单的口径），并把当日计划单撮合结果回写状态库。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .constants import (ORDER_STATUS_ALLTRADED, PLANNED_CANCELLED,
                        PLANNED_PLACED)
from .store import StateStore

# 引擎订单状态 → 计划单状态（撮合结果回写口径）
_ORDER_STATUS_TO_PLANNED = {
    ORDER_STATUS_ALLTRADED: PLANNED_PLACED,
}
# 引擎非成交终态（CANCELLED=6 / REJECTED=8 等）一律视为已取消
_PLANNED_CANCELLED_STATUSES = {6, 8}


def load_state(store: StateStore, strategy_id: str, trading_day: str) -> Dict[str, Any]:
    """组装 trading_day 的初始状态（engine.run 的 initial_state 入参）。

    Returns
    -------
    dict:
        state            引擎注入用：cash/positions/planned_orders/user_store
        prev_day         最近一个已跑交易日（首日为 None）
        prev_pv          最近一天收盘总资产（首日 None，绩效 today_return 用）
        is_first_day     是否首日（无任何历史快照）
        pending_planned_ids  本次注入引擎的计划单 id 集合
    （交易日缺口告警由 runner 凭日历判断：prev_day 早于 T 的前一交易日即漏跑）
    """
    prev_cash = store.latest_cash_before(strategy_id, trading_day)
    state: Dict[str, Any] = {'cash': None, 'positions': [],
                             'planned_orders': [], 'user_store': {}}
    prev_day = None
    prev_pv = None
    if prev_cash is not None:
        prev_day = str(prev_cash['trading_day'])
        prev_pv = float(prev_cash['portfolio_value'])
        state['cash'] = float(prev_cash['balance'])
        state['positions'] = store.read_positions(strategy_id, prev_day)
        state['user_store'] = store.read_user_store(strategy_id)
        # 计划订单按「待撮合日」取：既取 trading_day 当日应撮合的，
        # 也兜底取漏跑期间遗留的 pending 单（平台口径：未终结的计划单持续有效）
        pending = store.read_planned_orders(strategy_id, only_pending=True)
        state['planned_orders'] = [r for r in pending
                                   if str(r['trading_day']) <= trading_day]
    pending_ids = {str(r.get('planned_order_id'))
                   for r in state['planned_orders']}
    return dict(state=state, prev_day=prev_day, prev_pv=prev_pv,
                is_first_day=prev_cash is None,
                pending_planned_ids=pending_ids)


def save_state(store: StateStore, *, strategy_id: str, trading_day: str,
               next_trading_day: Optional[str],
               export: Dict[str, Any], perf_row: Dict[str, Any],
               logs: List[Dict[str, str]] = None) -> Dict[str, Any]:
    """当日跑完的全部状态落库（先删后写，可重跑覆盖）。

    Parameters
    ----------
    export          engine.run(..., paper_export) 的导出 dict
    perf_row        paper.perf.evaluate 的绩效行
    next_trading_day  T+1 交易日（新计划单的生效日）；日历未知时传 None，
                      此时新计划单挂在 trading_day 本名下，下次跑批仍会
                      被读取（load_state 兜底取 pending）
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ── 1. 当日快照（account_cash / positions / orders / trades） ──
    store.write_cash(strategy_id, trading_day,
                     balance=export['cash'],
                     total_market_value=export['total_market_value'],
                     portfolio_value=export['portfolio_value'])
    store.write_positions(strategy_id, trading_day, export['positions'])
    store.write_orders(strategy_id, trading_day, export['orders'])
    store.write_trades(strategy_id, trading_day, export['trades'])

    # ── 2. 当日计划单撮合结果回写 ──────────────────────────────
    #    回写键 = (trading_day, planned_order_id)，若 0 行命中说明计划单
    #    写入的交易日与撮合回写错位（曾出过 next_trading_day 含当天的 bug，
    #    导致 pending 只增不减、每天重复撮合），必须报警而不是静默。
    for pr in export.get('planned_results', []):
        st = int(pr['order_status'])
        if st in _ORDER_STATUS_TO_PLANNED:
            planned_status, msg = _ORDER_STATUS_TO_PLANNED[st], ''
        elif st in _PLANNED_CANCELLED_STATUSES:
            planned_status, msg = PLANNED_CANCELLED, str(pr.get('status_msg', ''))
        else:
            planned_status, msg = PLANNED_PLACED, str(pr.get('status_msg', ''))
        n = store.update_planned_order_status(
            strategy_id, trading_day=str(pr.get('trading_day', trading_day)),
            planned_order_id=str(pr['planned_order_id']),
            order_status=planned_status, status_msg=msg,
            order_key=str(pr.get('order_key', '')),
            order_placed_dt=now)
        if n == 0:
            # 找不到行 = 写入日与回写日错位（曾出过 next_trading_day 含当天
            # 的 bug）。补一行终态防其被当作 pending 反复注入，宁可留痕不静默。
            print(f'[WARN] 计划单 {pr["planned_order_id"]} 撮合回写 0 行命中'
                  f'（trading_day={pr.get("trading_day", trading_day)}），'
                  f'可能状态错位，已补记终态', flush=True)
            store.mark_planned_order_terminal(
                strategy_id, trading_day=str(pr.get('trading_day', trading_day)),
                planned_order_id=str(pr['planned_order_id']),
                order_status=PLANNED_CANCELLED, status_msg=msg or '状态错位补记',
                order_key=str(pr.get('order_key', '')),
                order_placed_dt=now)

    # ── 3. 未成交新单 → T+1 计划订单（对齐平台 PlannedOrder.to_db） ──
    pending = export.get('new_pending_orders', [])
    rows = []
    for i, o in enumerate(pending):
        # order_dt 按 order_price_field 推算（平台口径：open→T+1 09:30，
        # 否则 15:00）；本地日频买卖都用 open，统一 09:30
        rows.append(dict(
            planned_order_id=datetime.now().strftime('%H%M%S') + f'{i:02d}',
            signal_day=trading_day,
            instrument=o['instrument'], name=o.get('name', ''),
            direction=int(o['direction']), offset_flag=int(o.get('offset_flag', 0)),
            original_order_qty=int(o['order_qty']),
            order_qty=int(o['order_qty']),
            order_price=float(o.get('order_price', 0.0) or 0.0),
            order_type=str(o.get('order_type', '0')),
            order_status=0, status_msg='',
            order_dt=f'{next_trading_day or trading_day} 09:30:00',
            order_placed_dt=now,
        ))
    target_day = next_trading_day or trading_day
    store.write_planned_orders(strategy_id, target_day, rows)

    # ── 4. 绩效 / user_store / 运行日志 ─────────────────────────
    store.write_performance(strategy_id, trading_day, perf_row)
    store.write_user_store(strategy_id, trading_day, export.get('user_store', {}))
    if logs:
        store.write_run_logs(strategy_id, trading_day, logs)
    store.set_last_trading_day(strategy_id, trading_day)
    store.set_first_trading_day_if_empty(strategy_id, trading_day)
    return dict(cash_row=export['portfolio_value'],
                new_planned_orders=rows)
