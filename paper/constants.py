"""
平台字段码常量 —— 对齐 BigQuant 模拟交易数据字段
=================================================

来源（唯一依据，不做语义推测）：
  docs/BigQuant_SDK_使用文档/BigQuant_SDK_API_手册/Papertrading_模拟交易_API.md
    · 2.4.2 获取交易订单     → direction / offset_flag / order_status / order_type
    · 2.4.3 获取计划订单     → PlannedOrderStatus
    · 2.4.4 获取持仓信息     → posi_direction / hedge_flag

本模块只放「码值」与「码值 ↔ 引擎枚举」的映射，不含业务逻辑。
状态库表字段命名一律沿用上述文档，使本地库与平台 SDK 返回结构可直接互认。
"""

from __future__ import annotations


# ── 买卖方向 direction（订单表） ────────────────────────────────
DIRECTION_BUY = 1
DIRECTION_SELL = 2

# ── 持仓方向 posi_direction（持仓表） ───────────────────────────
POSI_DIRECTION_LONG = 1
POSI_DIRECTION_SHORT = 2

# ── 开平标志 offset_flag ────────────────────────────────────────
OFFSET_OPEN = 0
OFFSET_CLOSE = 1
OFFSET_CLOSETODAY = 2

# ── 委托状态 order_status（订单表） ─────────────────────────────
ORDER_STATUS_NOTTRADED = 0        # 未成交
ORDER_STATUS_PARTTRADED = 1       # 部分成交
ORDER_STATUS_ALLTRADED = 2        # 全部成交
ORDER_STATUS_PARTCANCELLED = 3    # 部分撤单
ORDER_STATUS_ALLCANCELLED = 4     # 全部撤单
ORDER_STATUS_REJECTED = 5         # 已拒绝

ORDER_STATUS_TEXT = {
    ORDER_STATUS_NOTTRADED: '未成交',
    ORDER_STATUS_PARTTRADED: '部分成交',
    ORDER_STATUS_ALLTRADED: '全部成交',
    ORDER_STATUS_PARTCANCELLED: '部分撤单',
    ORDER_STATUS_ALLCANCELLED: '全部撤单',
    ORDER_STATUS_REJECTED: '已拒绝',
}

# ── 委托类型 order_type ─────────────────────────────────────────
ORDER_TYPE_LIMIT = '0'            # 限价单
ORDER_TYPE_MARKET = 'U'           # 市价单

# ── 计划订单状态 planned_order.order_status ─────────────────────
PLANNED_NOTPLACE = 0              # 待执行
PLANNED_PLACING = 1               # 执行中
PLANNED_PLACED = 2                # 已执行
PLANNED_CANCELLED = 3             # 已取消

PLANNED_STATUS_TEXT = {
    PLANNED_NOTPLACE: '待执行',
    PLANNED_PLACING: '执行中',
    PLANNED_PLACED: '已执行',
    PLANNED_CANCELLED: '已取消',
}

# 只有「待执行」的计划单会被下一交易日撮合（对齐平台 modelconverter
# PlannedOrder.to_db_n 仅导出未下单状态的语义）
PLANNED_PENDING_STATUSES = (PLANNED_NOTPLACE, PLANNED_PLACING)

# ── 投保标记 hedge_flag ─────────────────────────────────────────
HEDGE_SPECULATION = 1             # 投机

# ── 绩效计算常量（平台环境变量默认值） ──────────────────────────
# 平台以环境变量 YEAR_TRADING_DAYS / RISK_FREE_DAILY 注入，默认值如下。
YEAR_TRADING_DAYS = 252
RISK_FREE_DAILY = 0.0001173       # 日无风险利率（≈年化 2.956%）
RISK_FREE_ANNUAL = RISK_FREE_DAILY * YEAR_TRADING_DAYS

# ── 运行模式 ────────────────────────────────────────────────────
RUN_MODE_BACKTEST = 'backtest'
RUN_MODE_PAPERTRADING = 'papertrading'
