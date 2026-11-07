"""
示例策略：双均线择时（股票池，回测/模拟盘同形）
================================================

策略逻辑：每只标的 5 日均线上穿 20 日均线时持有目标市值 40 万，否则清仓。
信号完全由 data.history 推出 —— 不依赖 trading_day_index / 调仓计数器，
因此回测与单日模拟盘语义天然一致（平台模拟盘每日 trading_day_index 从 0
计起，依赖序号的调度在单日模式下会每天触发，策略应避免使用）。

注意平台语义：模拟盘每日冷启动、initialize 每天重跑。跨日状态放
context.user_store（本地为普通 dict，每日由状态库装回）。
"""

import sys
from pathlib import Path

# 定位 engine/：本文件与 engine/ 同级（独立分发包），或在其上一级（stars/ 子目录布局）
_HERE = Path(__file__).resolve().parent
for _cand in (_HERE / 'engine', _HERE.parent / 'engine'):
    if _cand.is_dir():
        sys.path.insert(0, str(_cand))
        break

import smalltrader as bt  # noqa: E402

INSTRUMENTS = ['000001.SZA', '600000.SHA']
TARGET_VALUE = 400_000
FAST, SLOW = 5, 20


def initialize(context):
    # 平台口径费用：万3佣金（最低 5 元）+ 卖出千1印花（PerOrder 综合费率）
    context.set_commission(0.0003)


def handle_data(context, data):
    for inst in INSTRUMENTS:
        h = data.history(inst, 'close', SLOW, '1d')
        # 预热不足 / 当日停牌（close NaN）→ 不动（不报错也不下单）
        if len(h) < SLOW or h.isna().any():
            continue
        ma_fast = h.tail(FAST).mean()
        ma_slow = h.mean()
        target = TARGET_VALUE if ma_fast > ma_slow else 0
        bt.order_target_value(context, inst, target)


STRATEGY = dict(
    strategy_id='dual_ma_demo',
    strategy_name='双均线演示',
    capital_base=1_000_000.0,
    instruments=INSTRUMENTS,
    benchmark='000300.SH',
    warmup_days=120,          # 行情窗口前推 120 自然日，覆盖 MA20 预热
    initialize=initialize,
    handle_data=handle_data,
)
