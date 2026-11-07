"""
T+1 委托清单输出 —— 模拟盘的唯一"对外接口"
============================================

平台模拟盘只记账、不发真实委托（收盘后 send_wechat_message 推送信号）。
本地的等价物：每日跑完后把「下一交易日的计划订单」输出成 markdown/csv 清单，
供次日手动跟单；不做任何自动报单（将来要接券商时再留 save_state_external_match
那条外部撮合路径，见方案文档待确认事项 2）。
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .constants import DIRECTION_BUY, PLANNED_STATUS_TEXT
from .store import StateStore

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_order_list(store: StateStore, strategy_id: str,
                     trading_day: str) -> List[dict]:
    """取 trading_day（次日）待执行的计划订单，按先卖后买排序
    （先卖腾现金，与实盘下单习惯一致）。"""
    rows = store.read_planned_orders(strategy_id, trading_day,
                                     only_pending=True)
    sells = [r for r in rows if int(r['direction']) != DIRECTION_BUY]
    buys = [r for r in rows if int(r['direction']) == DIRECTION_BUY]
    return sells + buys


def write_order_list(store: StateStore, strategy_id: str, trading_day: str,
                     next_trading_day: str,
                     out_dir: str = None) -> Optional[str]:
    """输出 T+1 委托清单（markdown + csv），返回 markdown 文件路径。

    无委托时也写一份"无委托"清单 —— 空清单是明确的信号（今天不动），
    缺文件则是"没跑"，两者不能混淆。
    """
    rows = build_order_list(store, strategy_id, next_trading_day)
    out = Path(out_dir) if out_dir else _PROJECT_ROOT / 'notifications' / strategy_id
    out.mkdir(parents=True, exist_ok=True)

    md_lines = [
        f'# T+1 委托清单 · {strategy_id}',
        '',
        f'- 信号日：{trading_day}',
        f'- 执行日：{next_trading_day}（开盘价撮合，platform 口径）',
        f'- 生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'- 委托笔数：{len(rows)}',
        '',
    ]
    if rows:
        md_lines += [
            '| # | 代码 | 名称 | 方向 | 数量 | 参考价 | 状态 |',
            '|---|------|------|------|------|--------|------|',
        ]
        for i, r in enumerate(rows, 1):
            md_lines.append(
                f"| {i} | {r['instrument']} | {r.get('name') or '-'} "
                f"| {'买入' if int(r['direction']) == DIRECTION_BUY else '卖出'} "
                f"| {int(r['order_qty'])} | {float(r['order_price']):.3f} "
                f"| {PLANNED_STATUS_TEXT.get(int(r['order_status']), '')} |")
    else:
        md_lines.append('（下一交易日无委托）')

    md_path = out / f'{next_trading_day}.md'
    md_path.write_text('\n'.join(md_lines) + '\n', encoding='utf-8')

    csv_path = out / f'{next_trading_day}.csv'
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['执行日', '代码', '名称', '方向', '数量', '参考价',
                    '计划单号', '信号日'])
        for r in rows:
            w.writerow([next_trading_day, r['instrument'], r.get('name', ''),
                        '买入' if int(r['direction']) == DIRECTION_BUY else '卖出',
                        int(r['order_qty']), float(r['order_price']),
                        r['planned_order_id'], r.get('signal_day', '')])
    return str(md_path)
