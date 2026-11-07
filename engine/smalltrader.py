"""
SmallQuant — 全功能量化策略回测引擎
API 完全对齐 BigTrader，现有策略零修改可迁移

支持：
  - A 股日线 / 分钟线回测
  - 期货多空双向（buy_open / sell_open / buy_close / sell_close）
  - 信号驱动（HandleDataLib.handle_data_signal_based）
  - 权重驱动（HandleDataLib.handle_data_weight_based）
  - 调仓周期（TradingDaysRebalance / MonthlyRebalance / WeeklyRebalance / NaturalDaysRebalance）
  - 佣金模型（PerOrder / PerContract / 自定义 ISlippageModel）
  - 完整 context / portfolio / position / order / trade 数据结构
  - handle_trade / handle_order / after_trading 回调
  - 绩效报告（Performance.render / get_stats）

用法（与 bigtrader 完全一致）：
    import smalltrader as bigtrader

    def initialize(context):
        context.stock = '000001.SZA'

    def handle_data(context, data):
        bigtrader.order_target_percent(context, context.stock, 0.5)

    result = bigtrader.run(
        initialize=initialize,
        handle_data=handle_data,
        start_date='2020-01-01',
        end_date='2023-12-31',
        instruments=['000001.SZA'],
        capital_base=1_000_000,
    )
    result.render()
"""

from __future__ import annotations

import math
import logging
import sys
import warnings
import datetime
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Callable, Dict, List, Literal, Optional, Union

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
log = logging.getLogger('smalltrader')


def _sibling(name):
    """以与自身一致的布局解析同级模块（engine/smalltrader_report 或扁平
    smalltrader_report），兼容包式（import engine.smalltrader）与扁平两种布局。"""
    import importlib
    modname = (__package__ + '.' + name) if __package__ else name
    return importlib.import_module(modname)


class _SdkStyleFormatter(logging.Formatter):
    """平台 bigtrader 控制台日志样式（structlog ConsoleRenderer 风格）：
    `2026-11-05 14:43:47 [info     ] 消息` —— level 名小写、左对齐补到 9 列。"""
    def format(self, record):
        record.levelname = record.levelname.lower()
        return super().format(record)


def _ensure_logger():
    """保证 smalltrader logger 至少有一个 INFO 级 handler，让 run() 的
    `smalltrader init / run start / prepare data / run ..` 等进度提示
    能真正打印到控制台（对齐 SDK 回测引擎）。只接管本 logger、关闭传播，
    避免与调用方日志配置冲突或经 root/lastResort 双写。"""
    if not log.handlers:
        _sh = logging.StreamHandler(sys.stdout)
        _sh.setLevel(logging.INFO)
        _sh.setFormatter(_SdkStyleFormatter(
            '%(asctime)s [%(levelname)-9s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'))
        log.addHandler(_sh)
    log.setLevel(logging.INFO)
    log.propagate = False


_ensure_logger()

try:
    import matplotlib
    matplotlib.rcParams['axes.unicode_minus'] = False
    # 自动选择本机已安装的中文字体（不同系统字体名不同：Windows=SimHei/微软雅黑，
    # Linux/macOS=Noto Sans CJK SC/苹方等），找不到则保持默认并给出提示
    from matplotlib import font_manager
    _cjk_font = None
    for _name in ('SimHei', 'Microsoft YaHei', 'Noto Sans CJK SC',
                  'Noto Sans CJK JP', 'Source Han Sans SC', 'WenQuanYi Zen Hei',
                  'PingFang SC', 'AR PL UMing CN'):
        if _name in {f.name for f in font_manager.fontManager.ttflist}:
            _cjk_font = _name
            break
    if _cjk_font:
        matplotlib.rcParams['font.sans-serif'] = [_cjk_font, 'DejaVu Sans']
    else:
        log.warning('未找到中文字体（SimHei/Noto Sans CJK SC 等），'
                    '图表中文可能显示为方块；可安装 fonts-noto-cjk 后重启内核')
    import matplotlib.pyplot as plt
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

__version__ = '2.0.0'
RELEASE_VERSION = __version__
RELEASE_DATE = '2026-05-25'


# ══════════════════════════════════════════════════════════════════
# 枚举 / 常量  (与 bigtrader 同名同值)
# ══════════════════════════════════════════════════════════════════

class Market(str, Enum):
    CN_STOCK        = 'cn_stock'
    CN_FUTURE       = 'cn_future'
    CN_OPTION       = 'cn_option'
    CN_STOCK_OPTION = 'cn_stock_option'
    CN_FUTURE_OPTION= 'cn_future_option'
    CN_FUND         = 'cn_fund'
    CN_CBOND        = 'cn_cbond'
    CN_BOND         = 'cn_bond'
    CN_STOCK_INDEX  = 'cn_stock_index'
    CN_FUTURE_INDEX = 'cn_future_index'
    HK_STOCK        = 'hk_stock'
    US_STOCK        = 'us_stock'
    DEFAULT         = 'cn_stock'


class Frequency(str, Enum):
    TICK    = 'tick'
    MINUTE  = '1m'
    MINUTE5 = '5m'
    MINUTE15= '15m'
    MINUTE30= '30m'
    HOUR    = '1h'
    DAILY   = '1d'
    WEEK    = '1w'
    NONE    = ''


class Direction(str, Enum):
    LONG  = 'long'
    SHORT = 'short'
    NONE  = ''
    FinancingBuy    = 'financing_buy'
    LoadSell        = 'load_sell'
    SellRepay       = 'sell_repay'
    BuyRedeliver    = 'buy_redeliver'
    CashDirectRefund= 'cash_direct_refund'
    SecDirectRefund = 'sec_direct_refund'


class OffsetFlag(str, Enum):
    OPEN          = 'open'
    CLOSE         = 'close'
    CLOSETODAY    = 'closetoday'
    CLOSEYESTERDAY= 'closeyesterday'
    EXERCISE      = 'exercise'
    NONE          = ''


class OrderType(str, Enum):
    LIMIT        = 'limit'
    MARKET       = 'market'
    MARKET_FAK   = 'market_fak'
    MARKET_FOK   = 'market_fok'
    BEST_PRICE   = 'best_price'
    MKT2LMT      = 'mkt2lmt'
    LAST_PRICE   = 'last_price'
    FIVELEVEL_PRICE = 'fivelevel_price'
    LIMIT_L      = 'limit_l'
    LIMIT_P      = 'limit_p'


class OrderStatus(IntEnum):
    PENDINGPLACE    = 0
    PLACING         = 1
    ACCEPTED        = 2
    NOTTRADED       = 3
    PARTTRADED      = 4
    ALLTRADED       = 5
    CANCELLED       = 6
    PARTCANCELLED   = 7
    REJECTED        = 8
    PENDINGCANCEL   = 9
    PARTPENDINGCANCEL = 10
    EXPIRED         = 11
    NOTPLACE        = 12
    TRIGGERED       = 13
    NOTTRIGGER      = 14
    WAITCONFIRM     = 15
    INTERREMOVED    = 16
    UNKNOWN         = 99


class OrderProperty(str, Enum):
    NONE = ''
    FAK  = 'fak'
    FOK  = 'fok'


class BSFlag(IntEnum):
    NONE   = 0
    BUY    = 1
    SELL   = -1
    CANCEL = 2


class FilledType(IntEnum):
    NONE = 0
    F    = 1   # fully filled
    C    = 2   # cancelled


class HedgeFlag(str, Enum):
    Speculation = 'speculation'
    Hedge       = 'hedge'
    Arbitrage   = 'arbitrage'
    MarketMaker = 'market_maker'
    HedgeSpec   = 'hedge_spec'
    SpecHedge   = 'spec_hedge'
    NONE        = ''


class OptionCP(str, Enum):
    CALL = 'call'
    PUT  = 'put'
    NONE = ''


class StrikeType(str, Enum):
    Europe   = 'europe'
    American = 'american'
    Bermuda  = 'bermuda'


class AccountType(str, Enum):
    STOCK  = 'stock'
    FUTURE = 'future'
    OPTION = 'option'
    CREDIT = 'credit'
    NONE   = ''


class BacktestType(str, Enum):
    DEFAULT       = 'default'
    STOCK         = 'stock'
    FUTURE        = 'future'
    STOCK_OPTION  = 'stock_option'
    FUTURE_OPTION = 'future_option'
    STOCK_MARGIN  = 'stock_margin'


class RunMode(str, Enum):
    BACKTEST      = 'backtest'
    PAPER_TRADING = 'paper_trading'
    LIVE_TRADING  = 'live_trading'


class VMatchAt(IntEnum):
    CURRENT = 0
    NEXT    = 1


class SubscribeFlag(IntEnum):
    DEFAULT       = 0
    BarData       = 1
    L1Snapshot    = 2
    L2Snapshot    = 4
    L2Order       = 8
    L2OrderQueue  = 16
    L2Trade       = 32


# ══════════════════════════════════════════════════════════════════
# 数据模型  (对齐 bigtrader I* 接口)
# ══════════════════════════════════════════════════════════════════

@dataclass
class IContractData:
    instrument: str = ''
    trading_code: str = ''
    exchangeid: str = ''
    name: str = ''
    trading_day: str = ''
    product_code: str = ''
    product_type: str = 'stock'
    multiplier: float = 1.0
    price_tick: float = 0.01
    list_date: str = ''
    delist_date: str = ''
    security_status: int = 0
    sub_stk_type: str = ''
    long_margin_ratio: float = 1.0
    short_margin_ratio: float = 1.0
    day_trading: int = 0
    buy_unit: int = 100
    sell_unit: int = 100
    min_volume: int = 100
    strike_price: float = 0.0
    strike_type: StrikeType = StrikeType.Europe
    option_type: OptionCP = OptionCP.NONE
    underlying: str = ''
    optcontract_id: str = ''
    deliver_date: str = ''
    strike_date: str = ''
    margin_param1: float = 0.0
    margin_param2: float = 0.0
    underlying_pre_close: float = 0.0
    underlying_pre_settle: float = 0.0
    pre_close: float = 0.0
    pre_settle: float = 0.0
    upper_limit: float = 0.0
    lower_limit: float = 0.0


@dataclass
class IPositionData:
    instrument: str = ''
    trading_code: str = ''
    exchangeid: str = ''
    account_id: str = ''
    acct_type: str = ''
    broker_id: str = ''
    name: str = ''
    direction: Direction = Direction.LONG
    current_qty: int = 0
    frozen_qty: int = 0
    today_frozen_qty: int = 0
    avail_qty: int = 0
    cost_price: float = 0.0
    today_qty: int = 0
    yd_qty: int = 0
    position_pnl: float = 0.0
    commission: float = 0.0
    hedge_flag: str = ''
    trading_day: str = ''
    open_date: str = ''
    open_price: float = 0.0
    last_price: float = 0.0
    margin: float = 0.0
    market_value: float = 0.0          # 分位取整后的展示值（SDK position.market_value 口径）
    market_value_exact: float = 0.0    # 未取整 qty×price×multiplier（组合总值求和用）
    profit_ratio: float = 0.0
    dividend_qty: int = 0
    dividend_cash: float = 0.0
    frozen_margin: float = 0.0
    frozen_commission: float = 0.0
    sum_buy_value: float = 0.0
    sum_sell_value: float = 0.0
    hold_days: int = 0
    settle_price: float = 0.0
    compact_interest: float = 0.0
    liability: float = 0.0
    financing_qty: int = 0
    financing_avail_qty: int = 0
    contract: Any = None
    position_key: str = ''
    realized_pnl: float = 0.0
    update_time: str = ''
    user_id: str = ''
    is_last: bool = True
    gateway_name: str = 'smalltrader'

    def update_market(self, price: float, multiplier: float = 1.0):
        self.last_price = price
        # 仓位「展示市值」按分半进位取整（SDK position.market_value 口径：2021-01-18
        # 510300 69,809×5.513=384,857.017 → 384,857.02）；组合总值不用该取整值，
        # 而是用 market_value_exact 求和后整体取整（见 IPortfolio._update）。
        _mv = self.current_qty * price * multiplier
        self.market_value_exact = _mv
        self.market_value = PerOrder._half_away_cents(_mv) / 100.0 if _mv >= 0 \
            else -PerOrder._half_away_cents(-_mv) / 100.0
        self.position_pnl = (price - self.cost_price) * self.current_qty * multiplier
        if self.cost_price > 0:
            self.profit_ratio = price / self.cost_price - 1.0


@dataclass
class IOrderData:
    instrument: str = ''
    trading_code: str = ''
    exchangeid: str = ''
    account_id: str = ''
    acct_type: str = ''
    broker_id: str = ''
    trading_day: str = ''
    order_id: str = ''
    order_sysid: str = ''
    bt_order_sysid: str = ''
    direction: Direction = Direction.LONG
    offset_flag: OffsetFlag = OffsetFlag.OPEN
    order_price: float = 0.0
    order_qty: int = 0
    filled_qty: int = 0
    avg_price: float = 0.0
    filled_money: float = 0.0
    order_status: OrderStatus = OrderStatus.NOTTRADED
    order_type: OrderType = OrderType.LIMIT
    order_property: OrderProperty = OrderProperty.NONE
    covered_flag: int = 0
    status_msg: str = ''
    user_id: str = ''
    order_key: str = ''
    insert_date: str = ''
    order_time: str = ''
    cancel_time: str = ''
    order_datetime: str = ''
    name: str = ''
    entrust_no: str = ''
    algo_order_id: int = 0
    contract: Any = None
    is_last: bool = True
    front_id: int = 0
    session_id: int = 0
    gateway_name: str = 'smalltrader'
    sim_que_volume: int = 0
    sim_last_volume: int = 0


@dataclass
class ITradeData:
    instrument: str = ''
    trading_code: str = ''
    exchangeid: str = ''
    account_id: str = ''
    acct_type: str = ''
    broker_id: str = ''
    order_id: str = ''
    order_sysid: str = ''
    bt_order_sysid: str = ''
    trade_id: str = ''
    bt_trade_id: str = ''
    user_id: str = ''
    order_key: str = ''
    direction: Direction = Direction.LONG
    offset_flag: OffsetFlag = OffsetFlag.OPEN
    filled_qty: int = 0
    filled_price: float = 0.0
    filled_money: float = 0.0
    trading_day: str = ''
    hedge_flag: str = ''
    trade_type: str = ''
    trade_date: str = ''
    trade_time: str = ''
    trade_datetime: Any = None
    name: str = ''
    margin: float = 0.0
    commission: float = 0.0
    realized_pnl: float = 0.0
    covered_flag: int = 0
    is_last: bool = True
    contract: Any = None
    order_qty: int = 0
    algo_order_id: int = 0
    entrust_no: str = ''
    creator: str = ''
    gateway_name: str = 'smalltrader'


@dataclass
class IBar:
    instrument: str = ''
    datetime: Any = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    amount: float = 0.0
    open_interest: int = 0
    pre_close: float = 0.0
    upper_limit: float = 0.0
    lower_limit: float = 0.0
    adjust_factor: float = 1.0
    trading_day: str = ''
    period: str = '1d'


@dataclass
class ITickData:
    instrument: str = ''
    trading_code: str = ''
    exchangeid: str = ''
    time: int = 0
    datetime: Any = None
    trading_day: str = ''
    action_day: str = ''
    volume: int = 0
    amount: float = 0.0
    last_price: float = 0.0
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    pre_close: float = 0.0
    pre_settlement: float = 0.0
    pre_open_interest: int = 0
    open_interest: int = 0
    close_price: float = 0.0
    settlement: float = 0.0
    upper_limit: float = 0.0
    lower_limit: float = 0.0
    deal_number: int = 0
    iopv: float = 0.0
    bid_price1: float = 0.0
    bid_price2: float = 0.0
    bid_price3: float = 0.0
    bid_price4: float = 0.0
    bid_price5: float = 0.0
    ask_price1: float = 0.0
    ask_price2: float = 0.0
    ask_price3: float = 0.0
    ask_price4: float = 0.0
    ask_price5: float = 0.0
    bid_volume1: int = 0
    bid_volume2: int = 0
    bid_volume3: int = 0
    bid_volume4: int = 0
    bid_volume5: int = 0
    ask_volume1: int = 0
    ask_volume2: int = 0
    ask_volume3: int = 0
    ask_volume4: int = 0
    ask_volume5: int = 0
    pre_delta: float = 0.0
    curr_delta: float = 0.0
    gateway_name: str = 'smalltrader'


@dataclass
class ICommissionRateData:
    instrument: str = ''
    trading_code: str = ''
    exchangeid: str = ''
    trading_day: str = ''
    account_id: str = ''
    broker_id: str = ''
    open_ratio_by_money: float = 0.0003
    open_ratio_by_volume: float = 0.0
    close_ratio_by_money: float = 0.0013
    close_ratio_by_volume: float = 0.0
    close_today_ratio_by_money: float = 0.0
    close_today_ratio_by_volume: float = 0.0
    gateway_name: str = 'smalltrader'


@dataclass
class IMarginRateData:
    instrument: str = ''
    trading_code: str = ''
    exchangeid: str = ''
    trading_day: str = ''
    account_id: str = ''
    broker_id: str = ''
    hedge_flag: str = ''
    long_margin_ratio_by_money: float = 1.0
    long_margin_ratio_by_volume: float = 0.0
    short_margin_ratio_by_money: float = 1.0
    short_margin_ratio_by_volume: float = 0.0
    is_relative: int = 0
    gateway_name: str = 'smalltrader'


@dataclass
class IOrderReq:
    instrument: str = ''
    trading_code: str = ''
    exchangeid: str = ''
    account_id: str = ''
    acct_type: str = ''
    broker_id: str = ''
    order_price: float = 0.0
    order_qty: int = 0
    order_type: OrderType = OrderType.LIMIT
    order_property: OrderProperty = OrderProperty.NONE
    direction: Direction = Direction.LONG
    offset_flag: OffsetFlag = OffsetFlag.OPEN
    covered_flag: int = 0
    hedge_flag: str = ''
    order_key: str = ''
    user_id: str = ''
    order_id: str = ''
    front_id: int = 0
    session_id: int = 0
    datetime: Any = None
    dt: Any = None
    name: str = ''
    trading_day: str = ''
    order_time: str = ''
    order_status: OrderStatus = OrderStatus.PENDINGPLACE
    status_msg: str = ''
    contract: Any = None
    op_station: str = ''
    order_cmd: str = ''
    order_cmd_value: Any = None
    order_params: Any = None
    sim_que_volume: int = 0
    sim_last_volume: int = 0


class IPortfolio:
    """投资组合（对齐 bigtrader IPortfolio）"""
    def __init__(self, starting_cash: float = 1_000_000.0):
        self.starting_cash: float = starting_cash
        self.cash: float = starting_cash
        self.positions: Dict[str, IPositionData] = {}
        self.portfolio_value: float = starting_cash

    @property
    def positions_value(self) -> float:
        # 仅统计仍有持仓的市值（已清仓的 market_value 已清零，双保险排除陈旧值）
        return sum(p.market_value for p in self.positions.values()
                   if p.current_qty > 0)

    def _update(self):
        # 真实引擎的 portfolio_value = 现金(保留亚分精度) + Σ(qty×last_price 未取整)，
        # **合计后**再按分半进位（SDK raw_perf 实证：2022-11-17 cash=62.004、
        # 未取整市值和 1,876,677.514 → PV 1,876,739.52；若先对每个仓位取整再求和得 .51）。
        _exact = sum(p.market_value_exact for p in self.positions.values()
                     if p.current_qty > 0)
        _pv = self.cash + _exact
        self.portfolio_value = _round_cash_cents(_pv)

    def __repr__(self):
        return (f"Portfolio(cash={self.cash:,.2f} "
                f"positions_value={self.positions_value:,.2f} "
                f"total={self.portfolio_value:,.2f})")


# ══════════════════════════════════════════════════════════════════
# 佣金 / 滑点模型
# ══════════════════════════════════════════════════════════════════

class ISlippageModel(ABC):
    @abstractmethod
    def get_raw_price_field(self, is_buy: bool) -> str: ...
    @abstractmethod
    def process_order(self, data: Any, order: IOrderData):
        """返回 (exec_price, exec_qty) 或 [(exec_price, exec_qty), ...]"""
        ...


class _FixedSlippage(ISlippageModel):
    """固定比例滑点（内部默认）"""
    def __init__(self, ratio: float = 0.001):
        self.ratio = ratio

    def get_raw_price_field(self, is_buy: bool) -> str:
        return 'open'

    def process_order(self, data: Any, order: IOrderData):
        raw = order.order_price
        if raw <= 0:
            return (raw, order.order_qty)
        slip = self.ratio if order.direction == Direction.LONG else -self.ratio
        return (raw * (1 + slip), order.order_qty)


class PerOrder:
    """
    A 股股票佣金模型（对齐 bigtrader.PerOrder 平台撮合口径）

    buy_cost  : 买入佣金率（默认万3）
    sell_cost : 卖出综合费用率（默认万3佣金 + 千1印花税 = 0.0013）
    min_cost  : 最低佣金（默认5元）
    tax_ratio : 卖出额外费用率（sell_cost 之外；通常 None）

    平台费用按「分」整数计算，分位**四舍五入(半进位，C++ round)**：
        buy : fee = max(round(金额×buy_cost×100), round(min_cost×100)) 分
        sell: fee = max(round(金额×sell_cost×100), round(min_cost×100)) 分
    实证（2026-09-07，logs/cash_probe_platform = 平台引擎实跑 trade.commission
    37/37；logs/26a8bfc3 素材展示费用 269/269 + 289/289 买入）：平台**不把佣金与
    印花税拆分后各自取整**，卖出按 sell_cost 一体费率一次 round。此前误用
    "佣金 ceil + 印花 round 相加"（来自平台旧版引擎行为/2f9c8e09 9-05 素材），
    会在 47% 单上多收 1 分 → 26a8bfc3 全窗累计 2.65 元净值差。
    """
    def __init__(self, buy_cost: float = 0.0003, sell_cost: float = 0.0013,
                 min_cost: float = 5.0, tax_ratio: float = None):
        self.buy_cost  = buy_cost
        self.sell_cost = sell_cost
        self.min_cost  = min_cost
        self.tax_ratio = tax_ratio  # 额外费用率（已含在 sell_cost 中时设 None）

    @staticmethod
    def _half_away_cents(x: float) -> int:
        """分位四舍五入(半进位,平台 C++ round 语义)。

        容差用 +1e-6 而非 1e-9：组合总值量级 ~1e8 分时 float64 的 ulp≈1.5e-8，
        1e-9 不足以把 0.4999999… 拉回 0.5（实证 2021-01-22 PV=1,205,206.595 分位，
        本地曾少进 1 分）。佣金量级 ~1e4 分，1e-6 远小于 0.5，不会误进位。
        """
        return int(np.floor(x * 100 + 0.5 + 1e-6))

    def calc(self, direction: Direction, value: float) -> float:
        # 佣金 = max(金额×buy_cost, min_cost)（买卖同佣金率）；卖出印花税单列
        # = 金额×(sell_cost − buy_cost)（sell_cost 已含印花；tax_ratio 为额外税）。
        # 「佣金+印花税」合计后在分位一次半进位（非各自取整再相加）。
        # 真实引擎实测：卖 9240 → max(2.772,5)+9.24=14.24；3000→5+3=8.00；
        #   10000→5+10=15.00；大额 81774→max(24.53,5)+81.774=106.3062→106.31。
        # 此前用 max(金额×sell_cost, min) 一体费率，仅在 0.0003×金额<5≤0.0013×金额
        # 的小额卖出上偏低（min 误作用于印花税部分）。
        comm = max(value * self.buy_cost, self.min_cost)
        if direction == Direction.LONG:
            fee = comm
        else:
            fee = comm + value * (self.sell_cost - self.buy_cost) \
                + value * (self.tax_ratio or 0.0)
        return self._half_away_cents(fee) / 100.0


def _round_cash_cents(x: float) -> float:
    """按「分」半进位取整（组合总值 / 仓位展示市值口径；现金本身不逐笔取整）。

    实证 2022-11-17：cash=62.004（保留亚分）、Σ 未取整市值=1,876,677.514
    → portfolio_value 取整为 1,876,739.52。
    """
    return (PerOrder._half_away_cents(x) if x >= 0
            else -PerOrder._half_away_cents(-x)) / 100.0


class PerContract:
    """
    期货按手数佣金模型（对齐 bigtrader.PerContract）
    cost: dict[product_code, (open_per_lot, close_per_lot, closetoday_per_lot)]
    """
    def __init__(self, cost: Dict[str, tuple]):
        self.cost = cost
        self.cost_per_contract = cost

    def calc(self, instrument: str, offset: OffsetFlag, qty: int) -> float:
        product = instrument.rstrip('0123456789')
        rates = self.cost.get(product, self.cost.get('default', (3.0, 3.0, 3.0)))
        if offset == OffsetFlag.OPEN:
            return rates[0] * qty
        elif offset == OffsetFlag.CLOSETODAY:
            return rates[2] * qty if len(rates) > 2 else rates[1] * qty
        else:
            return rates[1] * qty


# ══════════════════════════════════════════════════════════════════
# 调仓周期
# ══════════════════════════════════════════════════════════════════

class _BaseRebalance:
    def __init__(self, days=1, *, roll_forward: bool = True,
                 market_code: str = 'CN', context=None):
        self.days = days
        self.roll_forward = roll_forward
        self.market_code = market_code
        self._context = context
        self._dates: pd.DatetimeIndex = None

    def build(self, start_date, end_date=None, extra_days: int = 500,
              _trading_dates=None):
        raise NotImplementedError

    def is_signal_date(self, date=None) -> bool:
        if self._dates is None:
            return True
        ts = pd.Timestamp(date) if date is not None else None
        if ts is None:
            return True
        return ts in self._dates

    def is_trival(self) -> bool:
        return self._dates is None

    def select_rebalance_data(self, data: pd.DataFrame,
                              date_col: str = 'date') -> pd.DataFrame:
        if self._dates is None:
            return data
        return data[data[date_col].isin(self._dates)]

    def _get_trading_dates(self, start, end, extra_days, _trading_dates):
        if _trading_dates is not None:
            return pd.DatetimeIndex(_trading_dates)
        s = pd.Timestamp(start) - pd.Timedelta(days=extra_days)
        e = pd.Timestamp(end) if end else pd.Timestamp.now()
        return pd.bdate_range(s, e)


class TradingDaysRebalance(_BaseRebalance):
    """每 N 个交易日调仓"""
    def build(self, start_date, end_date=None, extra_days=500, _trading_dates=None):
        td = self._get_trading_dates(start_date, end_date, extra_days, _trading_dates)
        n = int(self.days) if isinstance(self.days, (int, float)) else 1
        self._dates = td[::n]
        return self

    def is_signal_date(self, date=None) -> bool:
        if self._dates is None:
            return True
        ts = pd.Timestamp(date) if date is not None else None
        if ts is None:
            return True
        # 找最近的交易日
        idx = self._dates.searchsorted(ts)
        if idx < len(self._dates) and self._dates[idx] == ts:
            return True
        return False


class MonthlyRebalance(_BaseRebalance):
    """每月第 N 个交易日调仓"""
    def build(self, start_date, end_date=None, extra_days=500, _trading_dates=None):
        td = self._get_trading_dates(start_date, end_date, extra_days, _trading_dates)
        n = int(self.days) if isinstance(self.days, (int, float)) else 1
        df = pd.DataFrame({'date': td})
        df['ym'] = df['date'].dt.to_period('M')
        df['rank'] = df.groupby('ym').cumcount() + 1
        self._dates = pd.DatetimeIndex(df[df['rank'] == n]['date'])
        return self


class WeeklyRebalance(_BaseRebalance):
    """每周第 N 个交易日调仓（days=1 表示周一）"""
    def build(self, start_date, end_date=None, extra_days=500, _trading_dates=None):
        td = self._get_trading_dates(start_date, end_date, extra_days, _trading_dates)
        n = int(self.days) if isinstance(self.days, (int, float)) else 1
        df = pd.DataFrame({'date': td})
        df['yw'] = df['date'].dt.to_period('W')
        df['rank'] = df.groupby('yw').cumcount() + 1
        self._dates = pd.DatetimeIndex(df[df['rank'] == n]['date'])
        return self


class NaturalDaysRebalance(_BaseRebalance):
    """每 N 个自然日调仓"""
    def build(self, start_date, end_date=None, extra_days=500, _trading_dates=None):
        td = self._get_trading_dates(start_date, end_date, extra_days, _trading_dates)
        n = int(self.days) if isinstance(self.days, (int, float)) else 1
        s = td[0]
        dates = []
        cur = s
        while cur <= td[-1]:
            # 找最近的交易日
            idx = td.searchsorted(cur)
            if idx < len(td):
                dates.append(td[idx])
            cur += pd.Timedelta(days=n)
        self._dates = pd.DatetimeIndex(dates)
        return self


class QuarterlyRebalance(_BaseRebalance):
    """每季度第 N 个交易日调仓"""
    def build(self, start_date, end_date=None, extra_days=500, _trading_dates=None):
        td = self._get_trading_dates(start_date, end_date, extra_days, _trading_dates)
        n = int(self.days) if isinstance(self.days, (int, float)) else 1
        df = pd.DataFrame({'date': td})
        df['yq'] = df['date'].dt.to_period('Q')
        df['rank'] = df.groupby('yq').cumcount() + 1
        self._dates = pd.DatetimeIndex(df[df['rank'] == n]['date'])
        return self


class YearlyRebalance(_BaseRebalance):
    """每年第 N 个交易日调仓"""
    def build(self, start_date, end_date=None, extra_days=500, _trading_dates=None):
        td = self._get_trading_dates(start_date, end_date, extra_days, _trading_dates)
        n = int(self.days) if isinstance(self.days, (int, float)) else 1
        df = pd.DataFrame({'date': td})
        df['yr'] = df['date'].dt.year
        df['rank'] = df.groupby('yr').cumcount() + 1
        self._dates = pd.DatetimeIndex(df[df['rank'] == n]['date'])
        return self


# Aliases
MonthlyTradingDaysRebalance   = MonthlyRebalance
WeeklyTradingDaysRebalance    = WeeklyRebalance
QuarterlyTradingDaysRebalance = QuarterlyRebalance
YearlyTradingDaysRebalance    = YearlyRebalance


# ══════════════════════════════════════════════════════════════════
# IBarData — 行情数据访问接口
# ══════════════════════════════════════════════════════════════════

class IBarData:
    """
    对齐 bigtrader IBarData 接口

    data.current(instrument, fields)
    data.history(instrument, fields, bar_count, frequency)
    data.get_daily_value(instrument, field, dt)
    data.current_dt
    """
    def __init__(self, all_data: dict, current_dt: pd.Timestamp):
        self._data = all_data
        self.current_dt = current_dt

    # ── current ──────────────────────────────────────────────────
    def current(self, instrument, fields):
        if isinstance(instrument, (list, tuple)):
            if isinstance(fields, (list, tuple)):
                return pd.DataFrame(
                    {s: self._get_row(s, fields) for s in instrument}
                ).T
            return pd.Series({s: self._get_scalar(s, fields) for s in instrument})
        if isinstance(fields, (list, tuple)):
            return self._get_row(instrument, fields)
        return self._get_scalar(instrument, fields)

    def _get_scalar(self, symbol: str, field: str):
        df = self._data.get(symbol)
        if df is None or self.current_dt not in df.index:
            return np.nan
        return df.at[self.current_dt, field] if field in df.columns else np.nan

    def _get_row(self, symbol: str, fields: list) -> pd.Series:
        df = self._data.get(symbol)
        if df is None or self.current_dt not in df.index:
            return pd.Series({f: np.nan for f in fields})
        row = df.loc[self.current_dt]
        return row[[f for f in fields if f in row.index]]

    # ── history ──────────────────────────────────────────────────
    def history(self, instrument, fields, bar_count: int,
                frequency: str = '1d', expect_ndarray: bool = False):
        if isinstance(instrument, (list, tuple)):
            if isinstance(fields, (list, tuple)):
                # returns dict of DataFrames
                return {s: self._hist_df(s, fields, bar_count) for s in instrument}
            return pd.DataFrame(
                {s: self._hist_series(s, fields, bar_count) for s in instrument}
            )
        if isinstance(fields, (list, tuple)):
            result = self._hist_df(instrument, fields, bar_count)
        else:
            result = self._hist_series(instrument, fields, bar_count)
        if expect_ndarray:
            return result.values
        return result

    def _hist_series(self, symbol: str, field: str, n: int) -> pd.Series:
        df = self._data.get(symbol)
        if df is None or self.current_dt not in df.index:
            return pd.Series(dtype=float)
        loc = df.index.get_loc(self.current_dt)
        return df.iloc[max(0, loc - n + 1): loc + 1][field]

    def _hist_df(self, symbol: str, fields: list, n: int) -> pd.DataFrame:
        df = self._data.get(symbol)
        if df is None or self.current_dt not in df.index:
            return pd.DataFrame(columns=fields)
        loc = df.index.get_loc(self.current_dt)
        return df.iloc[max(0, loc - n + 1): loc + 1][[f for f in fields if f in df.columns]]

    # ── get_daily_value ──────────────────────────────────────────
    def get_daily_value(self, instrument: str, field, dt=None):
        target_dt = pd.Timestamp(dt) if dt else self.current_dt
        df = self._data.get(instrument)
        if df is None:
            return np.nan
        if target_dt not in df.index:
            # 找最近的前一个交易日
            idx = df.index.searchsorted(target_dt)
            if idx == 0:
                return np.nan
            target_dt = df.index[idx - 1]
        if isinstance(field, (list, tuple)):
            return df.loc[target_dt, [f for f in field if f in df.columns]]
        return df.at[target_dt, field] if field in df.columns else np.nan

    def can_trade(self, instrument: str) -> bool:
        p = self._get_scalar(instrument, 'close')
        return not (isinstance(p, float) and math.isnan(p)) and p > 0


# ══════════════════════════════════════════════════════════════════
# IContext — 策略上下文（完整对齐 bigtrader IContext）
# ══════════════════════════════════════════════════════════════════

class IContext:
    """
    策略上下文，贯穿整个回测生命周期。
    完整对齐 bigtrader IContext 接口。
    """
    def __init__(self, starting_cash: float = 1_000_000.0,
                 account_type: AccountType = AccountType.STOCK):
        self.portfolio = IPortfolio(starting_cash)
        self.current_dt: pd.Timestamp = None
        self.trading_day_index: int = 0
        self.start_date: str = ''
        self.end_date: str = ''
        self.first_trading_date: str = ''
        self.data: Any = None          # 用于信号/权重驱动策略
        self.options: Dict[str, Any] = {}
        self.user_store: Dict[str, Any] = {}
        self.logger = log
        self._account_type = account_type
        self._engine: '_Engine' = None  # 回调引擎（内部）

    # ── 交易日工具 ────────────────────────────────────────────────
    def get_trading_day(self) -> str:
        # 对齐平台 IContext:返回 'YYYY-mm-dd' 格式的当前交易日
        return self.current_dt.strftime('%Y-%m-%d') if self.current_dt else ''

    def get_next_trading_day(self, cur_trading_day: str = '') -> str:
        eng = self._engine
        if eng is None:
            return ''
        td = eng._trading_dates
        dt = pd.Timestamp(cur_trading_day) if cur_trading_day else self.current_dt
        idx = td.searchsorted(dt)
        if idx + 1 < len(td):
            return td[idx + 1].strftime('%Y%m%d')
        return ''

    def get_prev_trading_day(self, cur_trading_day: str = '') -> str:
        eng = self._engine
        if eng is None:
            return ''
        td = eng._trading_dates
        dt = pd.Timestamp(cur_trading_day) if cur_trading_day else self.current_dt
        idx = td.searchsorted(dt)
        if idx > 0:
            return td[idx - 1].strftime('%Y%m%d')
        return ''

    def add_trading_days(self, date: str, days: int) -> pd.Timestamp:
        eng = self._engine
        if eng is None:
            return pd.Timestamp(date) + pd.Timedelta(days=days)
        td = eng._trading_dates
        dt = pd.Timestamp(date)
        idx = td.searchsorted(dt)
        new_idx = max(0, min(len(td) - 1, idx + days))
        return td[new_idx]

    @property
    def trading_calendar(self):
        return self._engine._trading_dates if self._engine else None

    # ── 持仓查询 ──────────────────────────────────────────────────
    def get_position(self, instrument: str,
                     direction: Direction = Direction.LONG,
                     create_if_none: bool = True,
                     account_id: str = '') -> Optional[IPositionData]:
        key = f'{instrument}_{direction.value}' if direction != Direction.NONE else instrument
        pos = self.portfolio.positions.get(key)
        if pos is None and create_if_none:
            pos = IPositionData(instrument=instrument, direction=direction,
                                position_key=key)
            self.portfolio.positions[key] = pos
        return pos

    def get_positions(self, account_id: str = '') -> Dict[str, IPositionData]:
        return {k: v for k, v in self.portfolio.positions.items()
                if v.current_qty > 0}

    def get_account_positions(self, account_id: str = '') -> Dict[str, IPositionData]:
        """获取全部有持仓的股票（以 instrument 为 key），对齐平台 bigtrader API"""
        return {v.instrument: v for k, v in self.portfolio.positions.items()
                if v.current_qty > 0}

    def get_available_cash(self, account_id: str = '') -> float:
        return self.portfolio.cash

    def get_balance(self, account_id: str = '') -> float:
        return self.portfolio.portfolio_value

    # ── 订单查询 ──────────────────────────────────────────────────
    def get_orders(self, instrument: str = '',
                   only_from_this: bool = False) -> List[IOrderData]:
        if self._engine is None:
            return []
        orders = self._engine._filled_orders
        if instrument:
            orders = [o for o in orders if o.instrument == instrument]
        return orders

    def get_open_orders(self, instrument: str = '',
                        only_from_this: bool = False) -> List[IOrderData]:
        if self._engine is None:
            return []
        pending = self._engine._pending_orders
        if instrument:
            pending = [o for o in pending if o.instrument == instrument]
        return pending

    def get_order(self, order_param: str, account_id: str = '') -> Optional[IOrderData]:
        if self._engine is None:
            return None
        for o in self._engine._filled_orders:
            if o.order_id == order_param or o.order_key == order_param:
                return o
        return None

    def get_pending_order_reqs(self, account_id: str = '') -> List[IOrderReq]:
        return []

    def get_trades(self, instrument: str = '',
                   only_from_this: bool = False) -> List[ITradeData]:
        if self._engine is None:
            return []
        trades = self._engine._trades
        if instrument:
            trades = [t for t in trades if t.instrument == instrument]
        return trades

    def get_last_order_key(self) -> str:
        if self._engine and self._engine._filled_orders:
            return self._engine._filled_orders[-1].order_key
        return ''

    def get_error_msg(self, error_id: int) -> str:
        return ''

    # ── 合约信息 ──────────────────────────────────────────────────
    def get_contract(self, instrument: str) -> IContractData:
        if self._engine:
            return self._engine._contracts.get(instrument, IContractData(instrument=instrument))
        return IContractData(instrument=instrument)

    def get_all_contracts(self, product_code: str = '', **kwargs) -> Dict[str, IContractData]:
        if self._engine is None:
            return {}
        contracts = self._engine._contracts
        if product_code:
            return {k: v for k, v in contracts.items()
                    if v.product_code == product_code}
        return contracts

    def get_dominant(self, product_code):
        return ''

    # ── 佣金 / 保证金 ─────────────────────────────────────────────
    def get_commission(self, instrument: str, account_id: str = '') -> ICommissionRateData:
        return ICommissionRateData(instrument=instrument)

    def get_margin_rate(self, instrument: str, account_id: str = '') -> IMarginRateData:
        return IMarginRateData(instrument=instrument)

    def set_commission(self, equities_commission=None, futures_commission=None,
                       options_commission=None, account_id: str = ''):
        if self._engine and equities_commission is not None:
            if isinstance(equities_commission, PerOrder):
                self._engine._commission_model = equities_commission
            elif isinstance(equities_commission, (int, float)):
                self._engine._commission_model = PerOrder(
                    buy_cost=equities_commission, sell_cost=equities_commission + 0.001)

    def set_slippage_value(self, slippage_type: str = None,
                           slippage_value: float = 0,
                           volume_limit: int = None, account_id: str = ''):
        if self._engine:
            self._engine._slippage_ratio = slippage_value

    def set_slippage(self, tick_slippage=None, bar_slippage=None,
                     us_equities=None, account_id: str = ''):
        pass  # 可扩展

    def set_slippage_name(self, slippage_name: str, account_id: str = ''):
        pass

    def set_margin_rate(self, instrument: str, margin_ratio: float, account_id: str = ''):
        if self._engine:
            c = self._engine._contracts.get(instrument)
            if c:
                c.long_margin_ratio = margin_ratio

    def set_stock_t1(self, on: int):
        if self._engine:
            self._engine._t1 = bool(on)

    def set_dividend_reinvestment(self, on: int):
        pass

    def set_vmatch_at(self, val):
        if self._engine:
            self._engine._vmatch_at = VMatchAt(val)

    def set_initial_positions(self, initial_positions: list, account_id: str = ''):
        """设置初始持仓，格式: [{'instrument': ..., 'qty': ..., 'cost': ...}]"""
        for item in initial_positions:
            inst = item.get('instrument', '')
            qty  = int(item.get('qty', item.get('amount', 0)))
            cost = float(item.get('cost', item.get('cost_price', 0.0)))
            if inst and qty > 0:
                pos = self.get_position(inst, Direction.LONG)
                pos.current_qty = qty
                pos.avail_qty   = qty
                pos.cost_price  = cost
                pos.market_value = qty * cost

    def add_account(self, acct_type: AccountType, account_id: str = '',
                    capital_base: float = 1_000_000.0, **kwargs):
        pass  # 多账户扩展预留

    def schedule(self, func, *, days=None, weekly=None, monthly=None,
                 quarterly=None, yearly=None, minutes=None, time=None,
                 market_open=None, market_close=None,
                 calendar='trading', roll_forward=True,
                 trigger_on_start=True) -> None:
        """注册日频调度回调（对齐平台 IContext.schedule 接口，须在 initialize 中调用）。

        本地引擎为 DAILY 日频回测：回调 func(context, data) 在当日 handle_data
        之后触发，下单与 handle_data 内一致、下一交易日开盘价撮合。
        规则（trading 日历，与平台同语义）：
          - days=N         每 N 个回测交易日（trading_day_index % N == 0）
          - weekly=±w      自然周内第 w 个 / 倒数第 w 个交易日（1=周一）
          - monthly=±m     月内第 m 个 / 倒数第 m 个交易日（1=月初第 1 交易日）
          - quarterly=±q   季内第 q 个 / 倒数第 q 个交易日
          - yearly=±y      年内第 y 个 / 倒数第 y 个交易日
          - trigger_on_start=True（平台默认）首回测交易日即使不匹配也触发一次
        分钟级参数 minutes/time/market_open/market_close 供平台分钟回测使用，
        日频引擎无法模拟：传入时忽略并记一次 warning（不影响运行）。
        """
        if self._engine is None:
            raise RuntimeError('schedule 只能在 initialize(context) 中调用（context 未绑定引擎）')
        if calendar != 'trading':
            log.warning('schedule(calendar=%s) 本地引擎仅支持 trading 日历，按交易日历处理', calendar)
        for name in ('minutes', 'time', 'market_open', 'market_close'):
            if locals().get(name) not in (None, [], ''):
                log.warning('schedule(%s=…) 为分钟级参数，本地日频引擎忽略', name)
        self._engine._scheduled.append(dict(
            func=func, days=days, weekly=weekly, monthly=monthly,
            quarterly=quarterly, yearly=yearly, minutes=minutes, time=time,
            market_open=market_open, market_close=market_close,
            calendar=calendar, roll_forward=roll_forward,
            trigger_on_start=trigger_on_start))

    def login_account(self, account_setting: dict):
        pass

    # ── 订阅 ──────────────────────────────────────────────────────
    def subscribe(self, instruments, subscribe_flags=SubscribeFlag.DEFAULT):
        pass

    def unsubscribe(self, instruments, subscribe_flags=SubscribeFlag.DEFAULT):
        pass

    def subscribe_bar(self, instruments, period='1m', handle_bar=None,
                      history_window=None):
        pass

    def current_tick(self, instrument: str) -> ITickData:
        return ITickData(instrument=instrument)

    # ── 日志 ──────────────────────────────────────────────────────
    def record_log(self, level: str, content: str, account_id: str = ''):
        getattr(log, level.lower(), log.info)(content)

    # ── 期权相关（存根，可扩展）──────────────────────────────────
    def get_option_contracts(self, underlying: str) -> list:
        return []

    def get_option_strike_prices(self, underlying: str, strike_year_month: int) -> list:
        return []

    def get_atm_option_contract(self, underlying, strike_year_month, underlying_price,
                                option_type, adjust_flag='A'):
        return None

    def exercise(self, instrument: str, volume: int, **kwargs):
        pass

    def have_night_trading(self, trading_day: str) -> bool:
        return False

    def get_marginasset_data(self, account_id: str = '') -> dict:
        return {}

    # ── 融资融券（存根）──────────────────────────────────────────
    def margin_trade(self, instrument, volume, limit_price=None, order_type=None, **kwargs):
        return self.order(instrument, volume, limit_price, order_type, **kwargs)

    def margincash_open(self, instrument, volume, limit_price=None, order_type=None, **kwargs):
        return self.order(instrument, volume, limit_price, order_type, **kwargs)

    def margincash_close(self, instrument, volume, limit_price=None, order_type=None, **kwargs):
        return self.order(instrument, -volume, limit_price, order_type, **kwargs)

    def margincash_direct_refund(self, value: float) -> int:
        return 0

    def marginsec_open(self, instrument, volume, limit_price, **kwargs):
        return self.sell_open(instrument, volume, limit_price, **kwargs)

    def marginsec_close(self, instrument, volume, limit_price=None, order_type=None, **kwargs):
        return self.buy_close(instrument, volume, limit_price, order_type, **kwargs)

    def marginsec_direct_refund(self, instrument: str, volume: int) -> int:
        return 0

    def order_reverse_repo(self, value_or_percent, n=1, rate=None, **kwargs):
        pass

    def place_order(self, req: IOrderReq, **kwargs) -> int:
        return self._engine._place_order_req(req) if self._engine else -1

    # ── 核心下单接口 ──────────────────────────────────────────────
    def order(self, instrument: str, volume: int,
              limit_price: float = None,
              order_type: OrderType = None,
              offset_flag: OffsetFlag = None, **kwargs) -> int:
        if self._engine is None:
            return -1
        direction = Direction.LONG if volume > 0 else Direction.SHORT
        offset = offset_flag or (OffsetFlag.OPEN if volume > 0 else OffsetFlag.CLOSE)
        return self._engine.submit_order(
            instrument, abs(volume), direction, offset, limit_price, order_type)

    def order_value(self, instrument: str, value: float,
                    limit_price: float = None,
                    order_type: OrderType = None, **kwargs) -> int:
        if self._engine is None:
            return -1
        price = self._engine._get_ref_price(instrument, self.current_dt)
        if not price or math.isnan(price):
            return -1
        contract = self._engine._contracts.get(instrument, IContractData())
        unit = contract.buy_unit if value > 0 else contract.sell_unit
        # 买入预扣固定成本缓冲（真实引擎 calc_open_volume 反编译口径）：
        #   fee = max(value × 0.0003, 5.0)
        #   qty = floor((value − fee) / price / unit) × unit
        # 与 set_commission 无关（0.0003/5.0 是引擎常量 0x1800b3c60/0x1800b3d18）。
        _v = abs(value)
        if value > 0:
            _v = _v - max(_v * 0.0003, 5.0)
        qty = int(_v / price / unit) * unit
        if qty == 0:
            return -1
        return self.order(instrument, qty if value > 0 else -qty, limit_price, order_type)

    def order_target(self, instrument: str, target: float,
                     limit_price: float = None,
                     order_type: OrderType = None, **kwargs) -> int:
        pos = self.get_position(instrument, Direction.LONG, create_if_none=False)
        current = pos.current_qty if pos else 0
        delta = int(target) - current
        if delta == 0:
            return -1
        return self.order(instrument, delta, limit_price, order_type)

    def order_target_value(self, instrument: str, target: float,
                           limit_price: float = None,
                           order_type: OrderType = None, **kwargs) -> int:
        """调仓到目标市值（平台口径，与平台逐笔对拍 9775c13f/2f9c8e09）。

        平台按「目标市值 − 当前市值」差额换算下单数量并向下取整到整手：
        - 减仓(keep 调权)：qty = floor((当前市值 − 目标市值)/价格/整手)×整手，
          与「先 floor 目标份额再减当前」在边界可能差 1 手（平台对拍修正点）；
        - 清仓(target<=0)：一次性卖出全部持仓（含折算产生的零股，如 10259 份）；
        - 买入：从差额中预扣估算佣金再 floor（平台同款，避免次日撮合再缩一手）。
        """
        if self._engine is None:
            return -1
        price = self._engine._get_ref_price(instrument, self.current_dt)
        if not price or math.isnan(price) or price <= 0:
            return -1
        contract = self._engine._contracts.get(instrument, IContractData())
        unit = contract.buy_unit
        pos = self.get_position(instrument, Direction.LONG, create_if_none=False)
        cur_qty = pos.current_qty if pos else 0
        diff = target - cur_qty * price
        if abs(diff) < 1e-9:
            return -1
        if diff > 0:
            # 买入/加仓：预扣固定成本缓冲（真实引擎 calc_open_volume 反编译口径）：
            #   fee = max(diff × 0.0003, 5.0)，qty = floor((diff − fee)/price/unit)×unit。
            # 不是 set_commission 的费率（buy_cost=0/0.001/0.01 时下单量不变）；
            # 缓冲只作用于增量 diff，不是整个目标市值（探针 target 0.3333 → 32600）。
            fee = max(diff * 0.0003, 5.0)
            qty = int((diff - fee) / price / unit) * unit
            if qty <= 0:
                return -1
            return self.order(instrument, qty, limit_price, order_type)
        if target <= 0:
            # 清仓：全部卖出（不限整手 —— 平台份额折算可产生 10259 等零股）
            if cur_qty <= 0:
                return -1
            return self.order(instrument, -cur_qty, limit_price, order_type)
        # 减仓(keep 调权)：按差额市值换算整手
        qty = int(-diff / price / unit) * unit
        if qty <= 0:
            return -1
        return self.order(instrument, -qty, limit_price, order_type)

    def order_target_percent(self, instrument: str, target: float,
                             limit_price: float = None,
                             order_type: OrderType = None, **kwargs) -> int:
        pv = self.portfolio.portfolio_value
        return self.order_target_value(instrument, pv * target, limit_price, order_type)

    def order_percent(self, instrument: str, percent: float,
                      limit_price: float = None,
                      order_type: OrderType = None, **kwargs) -> int:
        pv = self.portfolio.portfolio_value
        return self.order_value(instrument, pv * percent, limit_price, order_type)

    # ── 期货专用下单 ──────────────────────────────────────────────
    def buy_open(self, instrument, volume, limit_price=None, order_type=None, **kwargs):
        if self._engine is None:
            return -1
        return self._engine.submit_order(
            instrument, volume, Direction.LONG, OffsetFlag.OPEN, limit_price, order_type)

    def sell_open(self, instrument, volume, limit_price=None, order_type=None, **kwargs):
        if self._engine is None:
            return -1
        return self._engine.submit_order(
            instrument, volume, Direction.SHORT, OffsetFlag.OPEN, limit_price, order_type)

    def buy_close(self, instrument, volume, limit_price=None, order_type=None, **kwargs):
        if self._engine is None:
            return -1
        return self._engine.submit_order(
            instrument, volume, Direction.LONG, OffsetFlag.CLOSE, limit_price, order_type)

    def sell_close(self, instrument, volume, limit_price=None, order_type=None, **kwargs):
        if self._engine is None:
            return -1
        return self._engine.submit_order(
            instrument, volume, Direction.SHORT, OffsetFlag.CLOSE, limit_price, order_type)

    def cancel_order(self, order_param) -> int:
        if self._engine is None:
            return -1
        return self._engine.cancel_order(order_param)

    def cancel_all(self, only_from_this: bool = False, account_id: str = '') -> None:
        if self._engine:
            self._engine._pending_orders.clear()


# ══════════════════════════════════════════════════════════════════
# HandleDataLib — 信号/权重驱动辅助
# ══════════════════════════════════════════════════════════════════

class HandleDataLib:
    """
    对齐 bigtrader.HandleDataLib

    context.data 格式（信号驱动）：
        DataFrame with columns: date, instrument, signal(1/0/-1), weight(可选)

    context.data 格式（权重驱动）：
        DataFrame with columns: date, instrument, weight
    """

    @staticmethod
    def handle_data_signal_based(
        context: IContext, data: IBarData,
        max_hold_days: int = None,
        take_profit: float = None,
        stop_loss: float = None,
        max_open_weights_per_day: float = None,
        show_progress: str = None,
    ) -> None:
        """
        基于信号驱动的自动调仓。
        context.data 需含 date / instrument / signal / weight 列。
        signal: 1=买入, 0=持有, -1=卖出
        """
        if context.data is None or len(context.data) == 0:
            return
        dt = context.current_dt
        today_str = dt.strftime('%Y-%m-%d')
        df = context.data
        # 支持 date 列为字符串或 Timestamp
        if 'date' in df.columns:
            today_data = df[df['date'].astype(str).str[:10] == today_str]
        else:
            today_data = df[df.index == dt]

        if len(today_data) == 0:
            # 检查止盈止损
            if take_profit or stop_loss:
                HandleDataLib._check_tp_sl(context, data, take_profit, stop_loss)
            return

        # 检查是否是调仓日（如果 context.data 是 Rebalance 对象）
        rebalance = getattr(context, '_rebalance', None)
        if rebalance and not rebalance.is_signal_date(dt):
            return

        # 卖出信号
        sell_rows = today_data[today_data['signal'] == -1]
        for _, row in sell_rows.iterrows():
            inst = row['instrument']
            pos = context.get_position(inst, Direction.LONG, create_if_none=False)
            if pos and pos.current_qty > 0:
                context.order_target_percent(inst, 0.0)

        # 买入信号
        buy_rows = today_data[today_data['signal'] == 1]
        if len(buy_rows) == 0:
            return
        total_weight = buy_rows['weight'].sum() if 'weight' in buy_rows.columns else len(buy_rows)
        for _, row in buy_rows.iterrows():
            inst = row['instrument']
            w = row.get('weight', 1.0 / len(buy_rows)) if 'weight' in row.index else 1.0 / len(buy_rows)
            if total_weight > 0:
                w = w / total_weight
            if max_open_weights_per_day:
                w = min(w, max_open_weights_per_day)
            context.order_target_percent(inst, w)

        # 止盈止损
        if take_profit or stop_loss:
            HandleDataLib._check_tp_sl(context, data, take_profit, stop_loss)

    @staticmethod
    def _check_tp_sl(context: IContext, data: IBarData,
                     take_profit: float, stop_loss: float):
        for key, pos in list(context.get_positions().items()):
            if pos.current_qty <= 0 or pos.cost_price <= 0:
                continue
            cur_price = data.current(pos.instrument, 'close')
            if math.isnan(cur_price):
                continue
            ret = cur_price / pos.cost_price - 1.0
            if take_profit and ret >= take_profit:
                context.order_target_percent(pos.instrument, 0.0)
            elif stop_loss and ret <= -abs(stop_loss):
                context.order_target_percent(pos.instrument, 0.0)

    @staticmethod
    def handle_data_weight_based(
        context: IContext, data: IBarData,
        show_progress: str = None,
    ) -> None:
        """
        基于权重驱动的自动调仓。
        context.data 需含 date / instrument / weight 列。
        """
        if context.data is None or len(context.data) == 0:
            return
        dt = context.current_dt
        today_str = dt.strftime('%Y-%m-%d')
        df = context.data

        # 检查调仓周期
        rebalance = getattr(context, '_rebalance', None)
        if rebalance and not rebalance.is_signal_date(dt):
            return

        if 'date' in df.columns:
            today_data = df[df['date'].astype(str).str[:10] == today_str]
        else:
            today_data = df[df.index == dt]

        if len(today_data) == 0:
            return

        # 目标权重
        target: Dict[str, float] = {}
        for _, row in today_data.iterrows():
            inst = row['instrument']
            w = float(row.get('weight', 0.0))
            target[inst] = w

        # 先卖出不在目标中或权重降低的
        for key, pos in list(context.get_positions().items()):
            inst = pos.instrument
            new_w = target.get(inst, 0.0)
            if new_w == 0.0:
                context.order_target_percent(inst, 0.0)

        # 再买入/调整
        for inst, w in target.items():
            if w > 0:
                context.order_target_percent(inst, w)


# ══════════════════════════════════════════════════════════════════
# Performance — 绩效报告
# ══════════════════════════════════════════════════════════════════

class Performance:
    """
    对齐 bigtrader.Performance
    result.render() / result.get_stats() / result.read_raw_perf()

    render() 输出与平台 BigTrader(高性能回测&交易) 相同的 HTML dashboard
    （由 smalltrader_report 模块渲染；含指标条/收益曲线/交易持仓日志表/
    区间收益图/收益报表）。引擎已自动采集渲染所需附属数据（每日持仓快照、
    订单日志、证券名称），无需用户额外操作。
    """
    def __init__(self, equity_curve: pd.Series, orders: List[IOrderData],
                 trades: List[ITradeData], starting_cash: float,
                 benchmark_curve: pd.Series = None,
                 benchmark_symbol: str = '000300.SH',
                 daily_positions: List[dict] = None,
                 order_logs: List[dict] = None,
                 instrument_names: Dict[str, str] = None,
                 daily_stats: List[dict] = None,
                 dividend_log: List[dict] = None):
        self.equity_curve    = equity_curve
        self.orders          = orders
        self.trades          = trades
        self.starting_cash   = starting_cash
        self.benchmark_curve = benchmark_curve
        self.benchmark_symbol = benchmark_symbol
        # 报告采集数据（引擎在 run 主循环中自动记录）
        self._daily_positions = daily_positions or []
        self._order_logs     = order_logs or []
        self._instrument_names = instrument_names or {}
        self._daily_stats    = daily_stats or []
        self._dividend_log   = dividend_log or []
        self._stats: dict = None

    # ── 统计指标 ──────────────────────────────────────────────────
    def get_stats(self) -> dict:
        if self._stats is not None:
            return self._stats
        ec = self.equity_curve
        if len(ec) < 2:
            self._stats = {}
            return self._stats
        dr = ec.pct_change().dropna()
        n = len(dr)
        years = n / 252
        total_ret  = ec.iloc[-1] / ec.iloc[0] - 1
        ann_ret    = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0
        ann_vol    = dr.std() * np.sqrt(252)
        sharpe     = (ann_ret - 0.025) / ann_vol if ann_vol > 0 else 0
        roll_max   = ec.cummax()
        dd         = (ec - roll_max) / roll_max
        max_dd     = dd.min()
        calmar     = ann_ret / abs(max_dd) if max_dd != 0 else 0
        win_rate   = (dr > 0).sum() / n
        sortino_dr = dr[dr < 0]
        sortino_vol = sortino_dr.std() * np.sqrt(252) if len(sortino_dr) > 0 else 1e-9
        sortino    = (ann_ret - 0.025) / sortino_vol if sortino_vol > 0 else 0
        # 最大回撤持续天数
        dd_dur = 0
        cur_dur = 0
        for v in dd.values:
            if v < 0:
                cur_dur += 1
                dd_dur = max(dd_dur, cur_dur)
            else:
                cur_dur = 0
        # 基准对比
        alpha = beta = info_ratio = 0.0
        if self.benchmark_curve is not None and len(self.benchmark_curve) > 1:
            bm_dr = self.benchmark_curve.pct_change().dropna()
            common = dr.index.intersection(bm_dr.index)
            if len(common) > 10:
                s_dr = dr.loc[common]
                b_dr = bm_dr.loc[common]
                cov = np.cov(s_dr, b_dr)
                beta = cov[0, 1] / cov[1, 1] if cov[1, 1] != 0 else 0
                bm_ann = (1 + b_dr.mean()) ** 252 - 1
                alpha = ann_ret - (0.025 + beta * (bm_ann - 0.025))
                excess = s_dr - b_dr
                info_ratio = excess.mean() / excess.std() * np.sqrt(252) if excess.std() > 0 else 0

        self._stats = dict(
            总收益率=total_ret, 年化收益率=ann_ret, 年化波动率=ann_vol,
            Sharpe比率=sharpe, Sortino比率=sortino,
            最大回撤=max_dd, 最大回撤持续天数=dd_dur,
            Calmar比率=calmar, Alpha=alpha, Beta=beta,
            信息比率=info_ratio, 日胜率=win_rate,
            交易次数=len(self.orders), 成交次数=len(self.trades),
            回测天数=n, 回测年数=round(years, 2),
            期初资金=self.starting_cash, 期末净值=ec.iloc[-1],
        )
        return self._stats

    def read_raw_perf(self) -> pd.DataFrame:
        """返回逐日净值 DataFrame"""
        nav = self.equity_curve / self.starting_cash
        df = pd.DataFrame({'portfolio_value': self.equity_curve, 'nav': nav})
        df['daily_return'] = df['nav'].pct_change()
        df['drawdown'] = (df['nav'] - df['nav'].cummax()) / df['nav'].cummax()
        return df

    # ── 平台对齐报告数据 ────────────────────────────────────────────
    def _trade_display_rows(self) -> List[dict]:
        """成交记录 -> 平台"交易详情"展示行（普通成交 + 除权除息行）"""
        rows: List[dict] = []
        for t in self.trades:
            d = str(t.trade_date)
            date = f'{d[:4]}-{d[4:6]}-{d[6:]}' if len(d) == 8 else str(d)
            time_ = f'{date} 09:30:00.000'
            is_buy = t.direction == Direction.LONG
            money = round(float(t.filled_money), 2)
            # 平台"平仓盈亏"口径：价差毛利（含买佣成本、不含卖费）保留 2 位
            cost_px = getattr(t, 'cost_price', None)
            if is_buy:
                pnl = None
                pnl_info = '--'
            else:
                if cost_px:
                    pnl = round((float(t.filled_price) - float(cost_px))
                                * int(t.filled_qty), 2)
                else:
                    pnl = round(float(t.realized_pnl), 2)
                ratio = round(pnl / (money - pnl) * 100, 2) if money != pnl else 0.0
                pnl_info = f'{pnl} / {ratio}%'
            rows.append(dict(
                日期=date, 时间=time_,
                证券代码=str(t.instrument),
                证券名称=str(t.name) or self._instrument_names.get(str(t.instrument), ''),
                **{'买/卖': '买入' if is_buy else '卖出'},
                数量=int(t.filled_qty),
                成交价=round(float(t.filled_price) + 1e-6, 3),
                成交金额=money,
                平仓盈亏=pnl_info,
                交易费用=round(float(t.commission), 2),
                成交类型='普通成交',
                _realized_pnl=pnl,
            ))
        # 除权除息行（近似平台 DR 交易行：现金红利=卖出/0股/净额+税；
        # 送转股=买入/股数/负红利净额 + 税）
        dlog = getattr(self, '_dividend_log', None)
        for ev in (dlog or []):
            date_raw = str(ev.get('date', ''))
            date = f'{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}' if len(date_raw) == 8 else date_raw
            inst = str(ev.get('instrument', ''))
            add_qty = int(ev.get('bonus_qty', 0) or 0) + int(ev.get('conversed_qty', 0) or 0)
            net = round(float(ev.get('net_cash', 0) or 0), 2)
            tax = round(float(ev.get('tax', 0) or 0), 2)
            if add_qty > 0:
                bs, qty, money = '买入', add_qty, -net
            else:
                bs, qty, money = '卖出', 0, net
            rows.append(dict(
                日期=date, 时间=f'{date} 00:00:00.000',
                证券代码=inst,
                证券名称=self._instrument_names.get(inst, ''),
                **{'买/卖': bs}, 数量=qty, 成交价=0.0, 成交金额=money,
                平仓盈亏='--', 交易费用=tax, 成交类型='除权除息'))
        # 平台展示顺序：日期倒序（最新在前）
        rows.sort(key=lambda x: (x['日期'], x['时间']), reverse=True)
        return rows

    def _build_report_data(self):
        """构造 smalltrader_report.ReportData（平台渲染层数据载体）"""
        _rp = _sibling('smalltrader_report')

        ec = self.equity_curve
        dates = [d.strftime('%Y-%m-%d') for d in pd.to_datetime(ec.index)]
        bm_values = None
        if self.benchmark_curve is not None and len(self.benchmark_curve) > 1:
            b = self.benchmark_curve.dropna()
            if len(b) > 1 and b.iloc[0] > 0:
                bm_cum = (b / b.iloc[0] - 1).reindex(ec.index).ffill().fillna(0.0)
                bm_values = [float(x) for x in bm_cum.tolist()]
        stats_list = self._daily_stats
        dates_set = {d: i for i, d in enumerate(dates)}
        n_pts = len(dates)
        leverage = [None] * n_pts
        hold_count = [0] * n_pts
        for s in stats_list:
            i = dates_set.get(str(s.get('date', '')))
            if i is not None:
                leverage[i] = float(s.get('leverage', 0.0))
                hold_count[i] = int(s.get('hold_count', 0))
        # 无持仓日仓位归 0，仅供副区绘图
        lev_series = [0.0 if v is None else v for v in leverage]
        return _rp.ReportData(
            dates=dates,
            portfolio_values=[float(x) for x in ec.tolist()],
            benchmark_values=bm_values,
            capital_base=float(self.starting_cash),
            transactions=self._trade_display_rows(),
            positions=self._daily_positions[::-1],   # 平台展示：日期倒序（最新在前）
            logs=self._order_logs[::-1],
            leverage=lev_series if any(v > 0 for v in lev_series) else None,
            hold_count=hold_count if any(v > 0 for v in hold_count) else None,
        )

    def render(self, render_type: Literal['auto', 'notebook', 'report'] = 'auto',
               round_num: int = 3, table_max_rows: int = 3000,
               sub_title: str = None, report_output_path=True) -> None:
        """渲染绩效报告（对齐平台 bigtrader.Performance.render）

        - render_type='notebook'/'auto'：展示与平台相同的 HTML dashboard
          （BigTrader(高性能回测&交易)：收益概况/交易详情/每日持仓和收益/日志/
          区间收益/收益报表），notebook 环境直接内联显示；
        - render_type='report'：stdout 打印与平台一致的 markdown 报告。
        """
        _rp = _sibling('smalltrader_report')

        rd = self._build_report_data()
        if render_type == 'report' or render_type == 'text':
            print(_rp.report_text_markdown(rd))
            return
        html = _rp.render_dashboard(rd, display=True)
        if html is not None:
            # 非 notebook 环境：打印文本报告
            print(_rp.report_text_markdown(rd))

    def _plot(self, figsize=(14, 9)):
        """matplotlib 简版绩效图（保留备用；默认 render 使用平台 HTML 展示）"""
        if not HAS_MPL:
            return
        fig, axes = plt.subplots(3, 1, figsize=figsize,
                                  gridspec_kw={'height_ratios': [3, 1, 1]})
        fig.suptitle('SmallQuant 回测结果', fontsize=13, fontweight='bold')
        nav = self.equity_curve / self.starting_cash
        # 净值
        ax = axes[0]
        ax.plot(nav.index, nav.values, label='策略净值', color='#2196F3', lw=1.5)
        if self.benchmark_curve is not None:
            bm = self.benchmark_curve / self.benchmark_curve.iloc[0]
            ax.plot(bm.index, bm.values, label=f'基准({self.benchmark_symbol})',
                    color='#FF9800', lw=1, ls='--', alpha=0.8)
        ax.axhline(1.0, color='gray', ls=':', lw=0.8)
        ax.set_ylabel('净值'); ax.legend(loc='upper left'); ax.grid(alpha=0.3)
        # 回撤
        ax = axes[1]
        dd = (nav - nav.cummax()) / nav.cummax() * 100
        ax.fill_between(dd.index, dd.values, 0, color='#F44336', alpha=0.4)
        ax.set_ylabel('回撤(%)'); ax.grid(alpha=0.3)
        # 日收益
        ax = axes[2]
        dr = nav.pct_change().dropna() * 100
        colors = ['#4CAF50' if r >= 0 else '#F44336' for r in dr]
        ax.bar(dr.index, dr.values, color=colors, alpha=0.7, width=1)
        ax.axhline(0, color='gray', lw=0.8)
        ax.set_ylabel('日收益(%)'); ax.grid(alpha=0.3)
        plt.tight_layout(); plt.show()

    def get_orders_df(self) -> pd.DataFrame:
        if not self.orders:
            return pd.DataFrame()
        return pd.DataFrame([{
            'date': o.trading_day, 'instrument': o.instrument,
            'direction': o.direction.value, 'offset': o.offset_flag.value,
            'order_qty': o.order_qty, 'filled_qty': o.filled_qty,
            'avg_price': round(o.avg_price, 4),
            'filled_money': round(o.filled_money, 2),
            'status': o.order_status.name,
        } for o in self.orders])

    def get_trades_df(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([{
            'date': t.trade_date, 'instrument': t.instrument,
            'direction': t.direction.value, 'offset': t.offset_flag.value,
            'filled_qty': t.filled_qty, 'filled_price': round(t.filled_price, 4),
            'filled_money': round(t.filled_money, 2),
            'commission': round(t.commission, 4),
            'realized_pnl': round(t.realized_pnl, 4),
        } for t in self.trades])

    def __repr__(self):
        s = self.get_stats()
        return (f"Performance(总收益={s.get('总收益率',0):.2%} "
                f"Sharpe={s.get('Sharpe比率',0):.3f} "
                f"最大回撤={s.get('最大回撤',0):.2%})")


# ══════════════════════════════════════════════════════════════════
# 回测引擎核心
# ══════════════════════════════════════════════════════════════════

class _Engine:
    """内部回测引擎，不对外暴露"""

    _order_counter = 0
    _trade_counter = 0

    def __init__(self,
                 commission_model=None,
                 slippage_ratio: float = 0.0,
                 price_field_buy: str = 'open',
                 price_field_sell: str = 'open',
                 volume_limit: float = 1.0,
                 t1: bool = True,
                 vmatch_at: VMatchAt = VMatchAt.NEXT,
                 dividend_events: pd.DataFrame = None,
                 dividend_tax_rate: float = 0.2):
        self._commission_model = commission_model or PerOrder()
        self._slippage_ratio   = slippage_ratio
        self._price_field_buy  = price_field_buy
        self._price_field_sell = price_field_sell
        self._volume_limit     = volume_limit
        self._t1               = t1
        self._vmatch_at        = vmatch_at
        # 除权除息事件表（由外部数据源提供，引擎不关心数据来源）
        self._dividend_events = dividend_events
        self._dividend_tax_rate = dividend_tax_rate  # 现金红利税率（平台口径：除息日按 20% 扣税）

        self.context: IContext = None
        self._all_data: dict   = {}
        self._contracts: Dict[str, IContractData] = {}
        self._trading_dates: pd.DatetimeIndex = None

        self._pending_orders: List[IOrderData] = []
        self._filled_orders:  List[IOrderData] = []
        self._trades:         List[ITradeData]  = []
        self._equity:         List[tuple]       = []
        self._dividend_log:   List[dict]       = []   # 除权除息调整记录
        self._scheduled: List[dict]            = []   # context.schedule() 注册的日频回调

        # ── 绩效报告采集（供 Performance.render 平台对齐展示） ──────
        self._name_map: Dict[str, str] = {}          # instrument -> 证券名称
        self._order_logs: List[dict] = []            # 平台日志表行（每订单一条）
        self._order_log_map: Dict[str, dict] = {}    # order_key -> 日志 dict
        self._position_snapshots: List[dict] = []    # 每日收盘持仓快照（平台持仓表行）
        self._daily_stats: List[dict] = []           # 每日 {date, leverage, hold_count}

        # T+1 冻结队列: {symbol: qty_to_unfreeze_next_day}
        self._t1_freeze: Dict[str, int] = {}

        # ── 单日(paper trading)模式 ────────────────────────────────
        # 注入的昨日计划订单 [(planned_order_id, IOrderData)]，run 结束后据此
        # 汇报每笔计划单的撮合结果（对齐平台 planned_order 状态回写语义）。
        self._injected_planned: List[tuple] = []
        # paper_mode 下 run 结束的当日导出（持仓/现金/订单/成交/未成交新单/
        # user_store），由 paper.runner 落库。回测模式恒为 None。
        self._paper_export: Optional[dict] = None

    # ── 价格获取 ──────────────────────────────────────────────────
    def _get_price(self, instrument: str, dt: pd.Timestamp,
                   field: str = None) -> float:
        if field is None:
            field = 'close'
        df = self._all_data.get(instrument)
        if df is None or dt not in df.index:
            return np.nan
        return float(df.at[dt, field]) if field in df.columns else np.nan

    def _prev_adjust_factor(self, instrument: str, dt: pd.Timestamp) -> float:
        """除息日前交易日的 adjust_factor（基金红利再投资反推每股分红用）。

        平台算法 cash_ps = pre_close × (af_ex/af_prev − 1) 需除息日与其前交易日
        的 adjust_factor 比值；这里取 dt 之前最近一个交易日（行情索引 < dt）的
        adjust_factor。行情缺 adjust_factor 列或 dt 前无数据时返回 None。
        索引已按日期升序 → 用 searchsorted 二分取前一个位置（旧实现 df.index<dt
        全表布尔切片，股票回测每个持仓每天调用一次，实测 ~4.6s/run）。"""
        df = self._all_data.get(instrument)
        if df is None or 'adjust_factor' not in df.columns:
            return None
        pos = df.index.searchsorted(dt, side='left')   # 首个 >= dt 的位置
        if pos == 0:
            return None                                # dt 前无交易日
        v = df.iat[pos - 1, df.columns.get_loc('adjust_factor')]
        return float(v) if not pd.isna(v) else None

    def _prev_day_close(self, instrument: str, dt: pd.Timestamp) -> Optional[float]:
        """除权日前一交易日的收盘价（参考价法除权的分子基准 prev_close）。

        平台口径：除权参考价用除权前一交易日收盘，现金红利按税前冲减分子。"""
        df = self._all_data.get(instrument)
        if df is None or 'close' not in df.columns:
            return None
        pos = df.index.searchsorted(dt, side='left')   # 二分：dt 前最近交易日位置
        if pos == 0:
            return None
        v = df.iat[pos - 1, df.columns.get_loc('close')]
        return float(v) if not pd.isna(v) else None

    def _adjust_factor_ratio(self, instrument: str, dt: pd.Timestamp) -> float:
        """当日/前交易日 adjust_factor 之比(±都返回)，供送转/逆送转份额调整。

        平台(SDK)对股票/基金都按复权因子日间跳变做份额调整，**升则补送转股、
        降则逆送转减股**：new_qty = int(qty × af_ex/af_prev)，总成本不变、零头现金
        找补(920765.BJ 2021-12-03 送转 1.242→1.667、12-06 反转 1.667→1.242 实证)。
        本地原仅基金走 adjust_factor、股票只认事件表，故这里对事件表缺行的股票补记。
        返回 factor=af_ex/af_prev(c 恒正)；无 adjust 列/前值无效时返回 0.0。"""
        af_ex = self._get_price(instrument, dt, 'adjust_factor')
        if af_ex is None or math.isnan(af_ex) or af_ex <= 0:
            return 0.0
        af_prev = self._prev_adjust_factor(instrument, dt)
        if af_prev is None or math.isnan(af_prev) or af_prev <= 0:
            return 0.0
        return af_ex / af_prev

    def _get_ref_price(self, instrument: str, dt: pd.Timestamp) -> float:
        """下单参考价：当日收盘；停牌(close 缺失/NaN)时回退 pre_close。

        平台口径（logs/26a8bfc3 实测：600647.SH 2022-08-01 停牌日提交
        SELLCLOSE 4200@21.18 = pre_close，8-02 复牌按开盘 23.30 成交；
        正常票 8-01 日志 px=当日 close）。此前 close=NaN 直接放弃提交，
        会错过复牌日的涨停开盘卖出（600647 案例差 ~9700 元/0.57pp）。
        注意：pre_close 回退只用于卖单/减仓调权/委托价日志；**买单**建单前需
        下单日有真实行情（_has_live_bar 把关，停牌日不建买单 —— 603828 案例），
        因此买单实际不会走到 pre_close 回退。"""
        p = self._get_price(instrument, dt, 'close')
        if p is None or math.isnan(p) or p <= 0:
            p = self._get_price(instrument, dt, 'pre_close')
        return p

    def _has_live_bar(self, instrument: str, dt: pd.Timestamp) -> bool:
        """下单日该标的是否有真实行情（有量柱，volume>0）。

        平台口径实证（603828.SH 2024-04-30，本地小市值 vs SDK cad569d1 对拍）：
        停牌日 real bar 行仍存在，但 open/high/low/close=NaN、volume=0；平台**不会
        为该股创建买单** —— 全 run 882 笔 BUYOPEN 的下单日 volume 全部 >0，而该股
        当日在 target 里却被跳建（订单/日志零条）。本地此前靠 _get_ref_price 的
        pre_close 回退在停牌日也能换算买单，多建 603828 47700@2.28(04-30 提交)、
        05-06 复牌开盘 2.17 成交，成为与 SDK 全段净值的唯一单向分歧（最终差
        +79,118.64）。此方法用于买侧建单把关：无量柱 → 不建单。
        卖单不受限（600647.SH 停牌日 SELLCLOSE 照常提交、复牌成交，见
        _get_ref_price 600647 案例），故只在 submit_order 的 LONG+OPEN 买单处调用。"""
        df = self._all_data.get(instrument)
        if df is None or dt is None or dt not in df.index:
            return False
        if 'volume' in df.columns:
            v = df.at[dt, 'volume']
            v = float(v) if not pd.isna(v) else 0.0
            return v > 0
        # 无 volume 列（罕见契约类数据）退而求其次：以当日 close 是否有效为准
        c = df.at[dt, 'close'] if 'close' in df.columns else np.nan
        return not math.isnan(float(c)) and float(c) > 0

    def _get_exec_price(self, instrument: str, dt: pd.Timestamp,
                        is_buy: bool) -> float:
        field = self._price_field_buy if is_buy else self._price_field_sell
        raw = self._get_price(instrument, dt, field)
        if math.isnan(raw) or raw <= 0:
            return np.nan
        slip = self._slippage_ratio if is_buy else -self._slippage_ratio
        return raw * (1 + slip)

    # ── 合约信息初始化 ────────────────────────────────────────────
    def _init_contracts(self, instruments: list, name_map: Dict[str, str] = None,
                        t0_set: set = None):
        """初始化合约。t0_set: 允许当日回转(T+0)的标的集合（如跨境/商品/债券 ETF）。
        day_trading=1 的合约在 t1=True 时不受 T+1 限制（平台按合约 day_trading 标志处理）。"""
        name_map = name_map or {}
        t0_set = t0_set or set()
        self._name_map = name_map
        for inst in instruments:
            if inst not in self._contracts:
                _code = str(inst)
                # 板块整手粒度：科创板(688/689.SH)最小 200 股、其后 1 股递增，
                # 下单数量不按 100 整手（实证 688021.SH 2024-11-08 平台买 24521、
                # 688701.SH 44109，而本地按 100 整手得 24500/44100）。其余板块 100。
                _kcb = (_code.startswith(('688', '689')) and _code.endswith('.SH'))
                _bu = 1 if _kcb else 100
                self._contracts[inst] = IContractData(
                    instrument=inst, trading_code=inst,
                    buy_unit=_bu, sell_unit=100, min_volume=(200 if _kcb else 100),
                    name=name_map.get(str(inst), ''),
                    day_trading=1 if str(inst) in t0_set else 0,
                )

    # ── 刷新持仓市值 ──────────────────────────────────────────────
    def _refresh_portfolio(self, dt: pd.Timestamp):
        for key, pos in self.context.portfolio.positions.items():
            if pos.current_qty <= 0:
                continue
            p = self._get_price(pos.instrument, dt, 'close')
            if not math.isnan(p) and p > 0:
                contract = self._contracts.get(pos.instrument, IContractData())
                pos.update_market(p, contract.multiplier)
        self.context.portfolio._update()

    # ── T+1 解冻 ──────────────────────────────────────────────────
    def _unfreeze_t1(self):
        for inst, qty in list(self._t1_freeze.items()):
            pos = self.context.portfolio.positions.get(f'{inst}_{Direction.LONG.value}')
            if pos is None:
                pos = self.context.portfolio.positions.get(inst)
            if pos:
                pos.avail_qty = min(pos.current_qty, pos.avail_qty + qty)
        self._t1_freeze.clear()

    # ── 提交订单 ──────────────────────────────────────────────────
    def submit_order(self, instrument: str, volume: int,
                     direction: Direction, offset: OffsetFlag,
                     limit_price: float = None,
                     order_type: OrderType = None) -> int:
        # 买单建单把关：下单日该标的无真实行情（停牌 volume=0/无柱）→ 不创建买单，
        # 直接 return -1（不占订单号、不进 pending、无订单日志）。平台口径实证：
        # 603828.SH 2024-04-30 停牌日被选中但平台订单数 0、全 run 882 笔 BUYOPEN
        # 下单日 volume 全部 >0（详见 _has_live_bar）。卖单不受限（600647 语义）。
        _dt = self.context.current_dt
        if (_dt is not None and direction == Direction.LONG
                and offset == OffsetFlag.OPEN
                and not self._has_live_bar(instrument, _dt)):
            return -1
        _Engine._order_counter += 1
        oid = str(_Engine._order_counter)
        dt_str = self.context.current_dt.strftime('%Y%m%d') if self.context.current_dt else ''
        price = limit_price or self._get_ref_price(instrument, self.context.current_dt)
        order = IOrderData(
            instrument=instrument,
            direction=direction,
            offset_flag=offset,
            order_qty=volume,
            order_price=price if not math.isnan(price) else 0.0,
            order_type=order_type or OrderType.LIMIT,
            order_status=OrderStatus.NOTTRADED,
            order_id=oid,
            order_key=oid,
            trading_day=dt_str,
            insert_date=dt_str,
            order_time=dt_str,
        )
        self._pending_orders.append(order)
        self._log_order_submit(order)
        return int(oid)

    def _log_order_submit(self, order: IOrderData):
        """订单提交日志（平台口径）：**提交即记一条**，date=提交日、qty=委托量
        （未经撮合日缩量/失败）、px=委托价（提交日收盘价，非撮合价）。

        平台对"后来失败或缩量"的单**同样保留**这条提交日志 —— 实证
        2021-05-07 513500.SH `BUYOPEN 200@2.5790` 后于 05-10
        `failed=cash 40.66 not available` + `adjust 200->0`；缩量单同理保留
        委托量（2025-07-29 513130.SH `BUYOPEN 120700` + `adjust 120700->118800`）。
        失败/缩量另记 WARN failed / INFO adjust（date=撮合日）。

        本地曾改到撮合后再记、且用缩量后的 qty：导致 121 笔 adjust 单的提交量
        与平台不符、34 笔失败单整条缺失（订单日志 590 vs 平台 622）。已改回提交时记录。
        """
        td = order.trading_day
        lg_date = f'{td[:4]}-{td[4:6]}-{td[6:]}' if len(td) == 8 else str(td)
        word = ('BUYOPEN' if (order.direction == Direction.LONG
                              and order.offset_flag == OffsetFlag.OPEN) else 'SELLCLOSE')
        px = order.order_price if (order.order_price or 0.0) > 0 else 0.0
        lg = {'date': lg_date, 'level': 'INFO',
              'msg': (f'order[93000000][id:{order.order_id},{order.instrument} '
                      f'{word} {abs(int(order.order_qty))}@{px:.4f},Market]')}
        self._order_logs.append(lg)
        self._order_log_map[order.order_key] = lg

    def _place_order_req(self, req: IOrderReq) -> int:
        return self.submit_order(
            req.instrument, req.order_qty, req.direction,
            req.offset_flag, req.order_price, req.order_type)

    def cancel_order(self, order_param) -> int:
        for i, o in enumerate(self._pending_orders):
            if o.order_id == str(order_param) or o.order_key == str(order_param):
                o.order_status = OrderStatus.CANCELLED
                self._pending_orders.pop(i)
                return 0
        return -1

    # ── 佣金计算 ──────────────────────────────────────────────────
    def _calc_commission(self, direction: Direction, offset_flag: OffsetFlag,
                         instrument: str, qty: int, gross: float) -> float:
        m = self._commission_model
        if isinstance(m, PerOrder):
            return m.calc(direction, gross)
        if isinstance(m, PerContract):
            return m.calc(instrument, offset_flag, qty)
        # 兜底：按平台默认万3/卖出加千1印花
        tax = 0.001 if direction != Direction.LONG else 0.0
        return round(max(gross * 0.0003, 5.0) + gross * tax, 2)

    # ── 除权除息（分红/送转股/基金份额折算） ─────────────────────
    def _process_dividends(self, dt: pd.Timestamp):
        """除息日开盘前处理除权除息。事件表列契约区分两种口径：
        - 股票事件（列含 bonus_rate/conversed_rate）：现金红利按 dividend_tax_rate
          扣税后入账，送/转股按比例增加持股（转增不征税，送红股按面值 1 元计红利税）；
          成本按参考价法：送转摊薄 + 现金红利冲减成本。
        - 基金事件（data_source 加 is_fund_split 列）：平台口径 —— 现金分红按
          adjust_factor 折算为份额（红利再投资）：除息日 bar 的 pre_close 即除权
          参考价，折算份额 = 红利金额/参考价取整，零头现金入账，总成本不变
          （与平台逐笔对拍 logs/9775c13f：买入 259@4.736, 金额 −3.38）。
          每股分红 cash_ps 平台由行情 adjust_factor 反推：cash_ps = pre_close ×
          (af_ex / af_prev − 1)，af_prev=除息日前交易日、af_ex=除息日 adjust_factor
          （实证 510300.SH 2021-01-18: af 1.1314378→1.1463728, pre=5.454 →
          0.0719928，恰为平台值；cn_fund_dividend.cash_dividend 四舍五入 0.072
          会差 0.02 元零头）。adjust_factor 缺失时回退 cash_before_tax。

        平台口径：按除息日开盘前持仓计算（当日买入不参与）。"""
        portfolio = self.context.portfolio
        dt_str = dt.strftime('%Y%m%d')
        group = self._dividend_by_date.get(dt.normalize())
        # 汇总除权行：事件表行 + bar adjust_factor 跳变反推的送转/逆送转(对齐 SDK)。
        # 事件表已覆盖的当日标的不再重复(handled 去重)。
        rows = []
        handled = set()
        if group is not None and not group.empty:
            for _, r in group.iterrows():
                r = dict(r)
                rows.append(r)
                handled.add(str(r['instrument']))
        # ── 事件表缺行时的送转/逆送转回退（bar adjust_factor 日间跳变）──
        # 真实引擎 Position::handle_split 的"因子回退"分支（Ghidra 反编译确认）：
        #   ratio_inv = af_prev / af_ex
        #   exact_new = qty / ratio_inv          # 送转>qty、逆送转<qty
        #   new_qty   = int(exact_new)           # 截断
        #   frac      = exact_new - new_qty
        #   cash      = round分(frac × ratio_inv × 昨收)
        # 注意零头按 **ratio_inv × 昨收**（因子隐含的除权价）计价，不是除权日 pre_close。
        # 实证 920765.BJ：2021-12-03 送转 frac=0.383185 × (1.241890/1.667271) × 9.90
        # = 2.826 → 入账 2.83；12-06 逆送转 frac=0.710817 × (1.667271/1.241889) × 9.69
        # = 9.248 → 入账 9.25。事件表有行的标的不走此分支（handled 去重）。
        for pos_key, pos in list(portfolio.positions.items()):
            inst = pos.instrument
            if pos.current_qty <= 0 or inst in handled:
                continue
            af_ex = self._get_price(inst, dt, 'adjust_factor')
            af_prev = self._prev_adjust_factor(inst, dt)
            if (af_ex is None or math.isnan(af_ex) or af_ex <= 0 or
                    af_prev is None or math.isnan(af_prev) or af_prev <= 0):
                continue
            ratio_inv = af_prev / af_ex
            if abs(ratio_inv - 1.0) < 1e-9:
                continue
            qty_cur = int(pos.current_qty)
            exact_new = qty_cur / ratio_inv
            new_qty = int(exact_new)
            added = new_qty - qty_cur
            frac = exact_new - new_qty
            prev_close = self._prev_day_close(inst, dt)
            resid = 0.0
            if frac > 0 and prev_close and prev_close > 0:
                resid = _round_cash_cents(frac * ratio_inv * prev_close)
            if added == 0 and resid == 0:
                handled.add(inst)
                continue
            old_cost = pos.cost_price
            pos.current_qty = new_qty
            if added > 0:
                pos.avail_qty += added
            else:
                pos.avail_qty = int(min(pos.avail_qty, new_qty))
            if new_qty > 0:
                # 总成本不变 → 每股成本按股数反比调整（与 SDK positions 逐值一致）
                pos.cost_price = old_cost * qty_cur / new_qty
            portfolio.cash += resid
            self._dividend_log.append(dict(
                date=dt_str, instrument=inst,
                event_type=('送转(因子回退)' if added >= 0 else '逆送转(因子回退)'),
                cash_dividend=resid, tax=0.0, net_cash=resid,
                bonus_qty=max(added, 0), conversed_qty=0,
            ))
            handled.add(inst)
        if not rows:
            return
        # 口径判定在事件行级（支持股票+基金混合池；基金事件 is_fund_split=1，
        # 股票事件无该列或为 0 → 走现金红利/送转路径）
        for row in rows:
            inst = str(row['instrument'])
            pos = portfolio.positions.get(f'{inst}_{Direction.LONG.value}')
            if pos is None or pos.current_qty <= 0:
                continue
            qty = pos.current_qty
            cash_ps = float(row.get('cash_before_tax') or 0.0) \
                if pd.notna(row.get('cash_before_tax')) else 0.0
            fund_split = bool(row.get('is_fund_split')
                              and float(row.get('is_fund_split')) == 1)
            if fund_split:
                # ── 基金：红利按除权参考价折算份额，零头现金找补 ──
                # 平台口径：每股分红由行情 adjust_factor 反推 cash_ps = pre_close
                # × (af_ex/af_prev − 1)，避免 cn_fund_dividend.cash_dividend 的
                # 四舍五入误差（510300.SH 2021-01-18: 0.072→0.0719928，零头
                # 1.33→1.31）。adjust_factor 不可用（行情缺列）时回退 cash_before_tax。
                af_ex = self._get_price(inst, dt, 'adjust_factor')
                if af_ex is not None and not math.isnan(af_ex) and af_ex > 0:
                    af_prev = self._prev_adjust_factor(inst, dt)
                    if af_prev is not None and af_prev > 0 and af_ex > af_prev:
                        pre0 = self._get_price(inst, dt, 'pre_close')
                        if pre0 is not None and not math.isnan(pre0) and pre0 > 0:
                            cash_adj = pre0 * (af_ex / af_prev - 1)
                            if cash_adj > 0:
                                cash_ps = cash_adj
                if cash_ps <= 0:
                    continue
                pre = self._get_price(inst, dt, 'pre_close')
                if not (pre > 0):
                    log.warning(f'份额折算缺除息日参考价 @{dt_str} {inst}, 跳过')
                    continue
                total_cash = qty * cash_ps
                add_qty = int(total_cash / pre)      # 整份折算（截断，平台同款）
                # 真实引擎不找补零头现金（SDK 探针 probe_div 实测：177000→179336，
                # 份额+2336，dividend_cash=0、ending_cash 全程不变）。旧本地把
                # 零头折算成现金入账（2021-01-18 多记 3.93 元），与 SDK 不符。
                residual = 0.0
                if add_qty:
                    pos.current_qty += add_qty
                    pos.avail_qty   += add_qty
                # 总成本不变（份额摊薄体现在均价），与平台 positions 逐值一致
                if add_qty:
                    pos.cost_price = pos.cost_price * qty / (qty + add_qty)
                self._dividend_log.append(dict(
                    date=dt_str, instrument=inst, event_type='份额折算(分红再投)',
                    cash_dividend=round(cash_ps * qty, 2), tax=0.0,
                    net_cash=residual, bonus_qty=add_qty, conversed_qty=0,
                ))
                continue
            # ── 股票：现金红利（扣税入账）+ 送转股 ──
            bonus = float(row.get('bonus_rate') or 0.0) \
                if pd.notna(row.get('bonus_rate')) else 0.0
            conv = float(row.get('conversed_rate') or 0.0) \
                if pd.notna(row.get('conversed_rate')) else 0.0
            # 增股数量：向下取整（平台口径，实证 bonus 0.3333×45400=15131.82→15131）
            bonus_qty = int(qty * bonus)
            conv_qty = int(qty * conv)
            # 现金红利扣税；送红股按面值1元计红利税
            tax = (cash_ps * qty + bonus_qty * 1.0) * self._dividend_tax_rate
            cash_in = round(cash_ps * qty - tax, 2)
            add_qty = bonus_qty + conv_qty
            if add_qty:
                pos.current_qty += add_qty
                pos.avail_qty   += add_qty
            portfolio.cash += cash_in
            # 除权日持仓成本调整 —— 参考价法（对齐真实 bigtrader，逆向自
            # _btre/probe4；此前"减式 cost-=cash_ps"与真实引擎不符）：
            #     ex_ref = round(分, 半进位)( (prev_close − 每股税前红利) / (1+bonus+conv) )
            #     cost'  = cost × ex_ref / prev_close
            # 送/转股不单独摊薄也无现金减项——统一用除权参考价比缩放每股成本，
            # 实证：现金1.0 → 10.5/11.5、送0.2 → round2(9.5833)=9.58 /11.5。
            prev_close = self._prev_day_close(inst, dt)
            if prev_close and prev_close > 0:
                denom = 1.0 + bonus + conv
                ex_ref = PerOrder._half_away_cents((prev_close - cash_ps) / denom) / 100.0 \
                    if denom else (prev_close - cash_ps)
                pos.cost_price = pos.cost_price * ex_ref / prev_close
            self._dividend_log.append(dict(
                date=dt_str, instrument=inst, event_type='除权除息',
                cash_dividend=round(cash_ps * qty, 2), tax=round(tax, 2),
                net_cash=cash_in, bonus_qty=bonus_qty, conversed_qty=conv_qty,
            ))
        portfolio._update()

    # ── 撮合成交 ──────────────────────────────────────────────────
    def _log_order_fail(self, dt: pd.Timestamp, order, word: str, reason: str):
        """平台口径订单失败日志（WARN，date=撮合日）：
        order[93000000][id:N,CODE BUYOPEN qty@px,Market] failed=<reason>"""
        px = order.order_price if order.order_price is not None else 0.0
        self._order_logs.append(dict(
            date=dt.strftime('%Y-%m-%d'), level='WARN',
            msg=(f'order[93000000][id:{order.order_id},{order.instrument} {word} '
                 f'{order.order_qty}@{px:.4f},Market] failed={reason}')))

    def _log_order_adjust(self, dt: pd.Timestamp, order, buy_word: str,
                          orig_qty: int, final_qty: int):
        """平台口径缩量/拒单日志（INFO，date=撮合日）：
        order[93000000] [id:N,CODE BUY adjust 2900->100]"""
        self._order_logs.append(dict(
            date=dt.strftime('%Y-%m-%d'), level='INFO',
            msg=(f'order[93000000] [id:{order.order_id},{order.instrument} '
                 f'{buy_word} adjust {orig_qty}->{final_qty}]')))

    def _fill_orders(self, dt: pd.Timestamp,
                     handle_trade_cb=None, handle_order_cb=None):
        portfolio = self.context.portfolio
        dt_str = dt.strftime('%Y%m%d')
        filled = []
        carried = []   # 框架保留：现无顺延路径（停牌/无效行情当日 Cancelled，对齐平台）

        for order in list(self._pending_orders):
            inst = order.instrument
            is_buy = order.direction == Direction.LONG
            word = ('BUYOPEN' if (is_buy and order.offset_flag == OffsetFlag.OPEN)
                    else 'SELLCLOSE')

            # 平台实测语义（logs/15644cd0 订单日志逆向，2025-04-29 八笔失败样本）：
            #   停牌/无行情(volume=0 / open 无效) → 当日失败 Cancelled，不保留不顺延；
            #   开盘=跌停 → 卖出单当日失败；现金不足 → 当笔缩量(adjust 口径)。
            bar_df = self._all_data.get(inst)
            has_bar = bar_df is not None and dt in bar_df.index
            exec_price = self._get_exec_price(inst, dt, is_buy)
            volume_v = 0.0
            lower_v = 0.0
            upper_v = 0.0
            high_v = 0.0
            low_v = 0.0
            if has_bar:
                if 'volume' in bar_df.columns:
                    v = bar_df.at[dt, 'volume']
                    volume_v = float(v) if not pd.isna(v) else 0.0
                if 'lower_limit' in bar_df.columns:
                    lv = bar_df.at[dt, 'lower_limit']
                    lower_v = float(lv) if not pd.isna(lv) else 0.0
                if 'upper_limit' in bar_df.columns:
                    uv = bar_df.at[dt, 'upper_limit']
                    upper_v = float(uv) if not pd.isna(uv) else 0.0
                if 'high' in bar_df.columns:
                    hv = bar_df.at[dt, 'high']
                    high_v = float(hv) if not pd.isna(hv) else 0.0
                if 'low' in bar_df.columns:
                    lv = bar_df.at[dt, 'low']
                    low_v = float(lv) if not pd.isna(lv) else 0.0

            fail_reason = ''
            if not has_bar or math.isnan(exec_price) or exec_price <= 0:
                fail_reason = ("'open' price is null" if has_bar
                               else 'Cancelled: No bar volume')
            elif volume_v == 0:
                fail_reason = 'Cancelled: No bar volume'
            elif is_buy and upper_v > 0 and low_v >= upper_v - 1e-9:
                # 全天一字涨停（最低价=涨停价，从未开板）→ 买单无法成交。
                # 平台判据 = 全天是否封死，而非开盘价：盘中开板（low<涨停价）
                # 的买单以开盘价正常成交（logs/26a8bfc3 实测: 603168.SH
                # 2021-04-28 开8.62=涨停但 low 8.50 → 平台成交 5700@8.62）；
                # 一字封死则 Cancelled（002188.SZ 2022-06-24 一字 6.18 →
                # failed='open' price is upper limit，efc25e64/26a8bfc3 双证）。
                fail_reason = "'open' price is upper limit"
            elif not is_buy and lower_v > 0 and high_v <= lower_v + 1e-9:
                # 全天一字跌停（最高价=跌停价，从未开板）→ 卖单无法成交。
                # 盘中开板（high>跌停价）的卖单以开盘价正常成交（600051.SH
                # 2022-04-26 开7.50=跌停但 high 7.55 → 平台卖出 8100@7.50）。
                fail_reason = "'open' price is lower limit"
            # 注：此前版本按「开盘价=涨/跌停」拦截属近似，会在盘中开板日误拦
            # （600051/603168 案例）；改为全天 high/low 判据后与平台案例全对。
            if fail_reason:
                order.order_status = OrderStatus.CANCELLED
                order.status_msg = fail_reason
                filled.append(order)
                self._log_order_fail(dt, order, word, fail_reason)
                continue

            contract = self._contracts.get(inst, IContractData())
            qty = order.order_qty
            orig_qty = qty

            # 成交量限制（平台 vmatch 口径：**始终**按 volume_limit×当日成交量 封顶，
            # 默认 volume_limit=1.0 即封顶到当日全额成交量；封顶后**不按整手取整**——
            # BSE 允许零股，实证 920396.BJ 2022-07-18：委托 8400、当日成交量 8328 →
            # 平台实际只成交 8328（order_status=PartTraded）。此前仅 volume_limit<1.0
            # 才封顶且 floor 到整手，默认 1.0 时完全不限→多成交 72 股，造成后续分歧。）
            if self._volume_limit > 0:
                df = self._all_data.get(inst)
                if df is not None and dt in df.index and 'volume' in df.columns:
                    vol = df.at[dt, 'volume']
                    if not pd.isna(vol):
                        max_vol = int(float(vol) * self._volume_limit)
                        qty = min(qty, max_vol)

            if qty <= 0:
                order.order_status = OrderStatus.REJECTED
                order.status_msg = '成交量为0'
                filled.append(order)
                continue

            # 持仓 key
            pos_key = f'{inst}_{order.direction.value}'
            pos = portfolio.positions.get(pos_key)
            if pos is None:
                pos = IPositionData(instrument=inst, direction=order.direction,
                                    position_key=pos_key)
                portfolio.positions[pos_key] = pos

            # ── 买入 / 开多 ──────────────────────────────────────
            if order.direction == Direction.LONG and order.offset_flag == OffsetFlag.OPEN:
                unit = contract.buy_unit
                mult = contract.multiplier

                # ── 资金不足缩量（真实引擎 StockAccount::check_order_cash_or_adjust
                # 反编译口径）：按**原始委托量 Q** 在成交价上计提佣金，若现金不足
                # 全额成交，则
                #   qty = floor((cash − round(max(Q×成交价×buy_cost, min_cost)))
                #               / 成交价 / 整手) × 整手
                # 注意佣金按 Q 计提（不是按缩量后的 qty，也不是"余量冻结"）——
                # 2020-01-17 513500 Q=155,000、成交价 2.143、佣金 66.43、现金 118,767.16
                # → 55,300；2022-05-23 513130 同式得 989,500（仅剩 4.60 元也成交）。
                # 缩量用撮合日成交价（不是下单日参考价）；旧本地用参考价曾使 2020-01-03
                # 现金为负（−1,956.96 vs SDK +200.88）。
                Q = int(orig_qty)
                full_gross = exec_price * Q * mult
                full_comm = self._calc_commission(Direction.LONG, OffsetFlag.OPEN,
                                                  inst, Q, full_gross)
                if portfolio.cash < full_gross + full_comm:
                    shortage = full_gross + full_comm - portfolio.cash
                    adj_qty = int((portfolio.cash - full_comm) / exec_price / mult)
                    if adj_qty > 0:
                        adj_qty -= adj_qty % unit
                    if adj_qty < int(contract.min_volume):
                        adj_qty = 0
                    if adj_qty <= 0:
                        order.order_status = OrderStatus.REJECTED
                        order.status_msg = '资金不足'
                        filled.append(order)
                        self._log_order_fail(
                            dt, order, word, f'cash {shortage:.2f} not available')
                        self._log_order_adjust(dt, order, 'BUY', orig_qty, 0)
                        continue
                    self._log_order_adjust(dt, order, 'BUY', orig_qty, adj_qty)
                    qty = min(qty, adj_qty)

                # 成交金额/佣金（按实际成交 qty 计收；缩量只影响数量）
                gross = exec_price * qty * mult
                comm = self._calc_commission(Direction.LONG, OffsetFlag.OPEN,
                                             inst, qty, gross)
                total_cost = gross + comm

                # 更新持仓（加权平均成本；平台口径：买入佣金摊入持仓成本）
                old_val = pos.current_qty * pos.cost_price * contract.multiplier
                pos.current_qty += qty
                pos.today_qty   += qty
                pos.cost_price   = (old_val + gross + comm) / (pos.current_qty * contract.multiplier) if pos.current_qty > 0 else 0
                pos.sum_buy_value += gross
                # T+1: 今日买入不可卖（合约 day_trading=1（如跨境/商品 ETF）不受限）
                if self._t1 and not contract.day_trading:
                    self._t1_freeze[inst] = self._t1_freeze.get(inst, 0) + qty
                else:
                    pos.avail_qty += qty
                portfolio.cash -= total_cost
                realized_pnl = 0.0

            # ── 卖出 / 平多 ──────────────────────────────────────
            elif order.direction == Direction.SHORT or order.offset_flag in (
                    OffsetFlag.CLOSE, OffsetFlag.CLOSETODAY, OffsetFlag.CLOSEYESTERDAY):
                # 找对应多头持仓
                long_key = f'{inst}_{Direction.LONG.value}'
                long_pos = portfolio.positions.get(long_key, pos)
                sell_qty = min(qty, long_pos.avail_qty)
                if sell_qty <= 0:
                    order.order_status = OrderStatus.REJECTED
                    order.status_msg = '可用持仓不足'
                    filled.append(order)
                    continue
                qty = sell_qty
                gross = exec_price * qty * contract.multiplier
                comm = self._calc_commission(order.direction, order.offset_flag,
                                             inst, qty, gross)
                sell_cost_price = float(long_pos.cost_price)   # 成交时成本（渲染平仓盈亏用）
                realized_pnl = (exec_price - long_pos.cost_price) * qty * contract.multiplier - comm
                long_pos.current_qty -= qty
                long_pos.avail_qty   -= qty
                long_pos.sum_sell_value += gross
                long_pos.realized_pnl   += realized_pnl
                if long_pos.current_qty == 0:
                    long_pos.cost_price  = 0.0
                    long_pos.market_value = 0.0   # 清仓后市值清零，避免计入组合总值
                portfolio.cash += gross - comm

            # ── 开空（期货）──────────────────────────────────────
            elif order.direction == Direction.SHORT and order.offset_flag == OffsetFlag.OPEN:
                gross = exec_price * qty * contract.multiplier
                margin = gross * contract.short_margin_ratio
                comm = self._calc_commission(order.direction, order.offset_flag,
                                             inst, qty, gross)
                if margin + comm > portfolio.cash:
                    order.order_status = OrderStatus.REJECTED
                    order.status_msg = '保证金不足'
                    filled.append(order)
                    continue
                pos.current_qty += qty
                pos.avail_qty   += qty
                pos.cost_price   = exec_price
                pos.margin      += margin
                portfolio.cash  -= comm
                realized_pnl = 0.0
            else:
                order.order_status = OrderStatus.REJECTED
                filled.append(order)
                continue

            # ── 生成成交记录 ──────────────────────────────────────
            _Engine._trade_counter += 1
            trade = ITradeData(
                instrument=inst,
                name=contract.name,
                direction=order.direction,
                offset_flag=order.offset_flag,
                filled_qty=qty,
                filled_price=exec_price,
                filled_money=exec_price * qty * contract.multiplier,
                commission=comm,
                realized_pnl=realized_pnl,
                trading_day=dt_str,
                trade_date=dt_str,
                trade_time=dt_str,
                trade_datetime=dt,
                order_id=order.order_id,
                order_key=order.order_key,
                trade_id=str(_Engine._trade_counter),
                bt_trade_id=str(_Engine._trade_counter),
            )
            if not is_buy:
                trade.cost_price = sell_cost_price   # 成交时持仓成本（绩效渲染毛利盈亏用）
            self._trades.append(trade)

            # 更新订单状态
            order.filled_qty   = qty
            order.avg_price    = exec_price
            order.filled_money = trade.filled_money
            order.order_status = OrderStatus.ALLTRADED
            filled.append(order)

            if handle_trade_cb:
                try:
                    handle_trade_cb(self.context, trade)
                except Exception as e:
                    log.warning(f'handle_trade error: {e}')
            if handle_order_cb:
                try:
                    handle_order_cb(self.context, order)
                except Exception as e:
                    log.warning(f'handle_order error: {e}')

        # 已成交/被拒订单移入已成交列表；无行情顺延的订单留待次日继续撮合
        self._pending_orders = carried
        self._filled_orders.extend(filled)

    # ── 每日持仓快照（平台"每日持仓和收益"表 + 主图仓位/数量副区） ──
    def _record_daily_snapshot(self, dt: pd.Timestamp):
        portfolio = self.context.portfolio
        pv = portfolio.portfolio_value
        n_hold = 0
        pos_value = 0.0
        date_str = dt.strftime('%Y-%m-%d')
        for key, pos in portfolio.positions.items():
            if pos.current_qty <= 0:
                continue
            n_hold += 1
            cost = pos.cost_price
            last = pos.last_price
            qty = pos.current_qty
            mv = pos.market_value
            pos_value += mv
            pnl = (last - cost) * qty
            ratio = round(pnl / (qty * cost) * 100, 2) if qty > 0 and cost else 0.0
            self._position_snapshots.append(dict(
                日期=date_str,
                证券代码=pos.instrument,
                证券名称=pos.name or self._name_map.get(pos.instrument, ''),
                数量=qty,
                持仓均价=round(cost, 3),
                收盘价=f'{last:.3f}',
                持仓市值=round(mv, 2),
                持仓占比=f'{round(mv / pv * 100, 2)}%' if pv else '0.00%',
                收益=f'{round(pnl, 2)} / {ratio}%',
            ))
        self._daily_stats.append(dict(
            date=date_str,
            leverage=pos_value / pv if pv else 0.0,
            hold_count=n_hold,
        ))

    # ── schedule 日频调度（对齐平台 bigtrader.schedule 日频子集）──────
    def _run_scheduled(self, dt: pd.Timestamp, context: 'IContext', bar) -> None:
        """主循环 handle_data 后触发 context.schedule() 注册的回调。

        预热期不触发；规则按回测段交易日（start_date 起）计算，语义：
        days=N → trading_day_index%N==0；weekly/monthly/quarterly/yearly 的正值
        = 组内第 k 个交易日、负值 = 组内倒数第 k 个交易日；首回测日按
        trigger_on_start（平台默认 True）触发一次。
        """
        if not self._scheduled:
            return
        idx = context.trading_day_index
        start = pd.Timestamp(context.start_date)
        bt = self._trading_dates[self._trading_dates >= start]
        d = dt.normalize()

        # 当日所在组区间（ISO 周 / 自然月 / 自然季 / 自然年）
        mon_lo = pd.Timestamp(d.year, d.month, 1)
        q_lo = pd.Timestamp(d.year, (d.month - 1) // 3 * 3 + 1, 1)
        ranges = {
            'weekly':    (d - pd.Timedelta(days=d.weekday()),
                          d + pd.Timedelta(days=6 - d.weekday())),
            'monthly':   (mon_lo, mon_lo + pd.DateOffset(months=1) - pd.Timedelta(days=1)),
            'quarterly': (q_lo, q_lo + pd.DateOffset(months=3) - pd.Timedelta(days=1)),
            'yearly':    (pd.Timestamp(d.year, 1, 1),
                          pd.Timestamp(d.year + 1, 1, 1) - pd.Timedelta(days=1)),
        }

        def due(sch: dict) -> bool:
            if idx == 0 and sch.get('trigger_on_start', True):
                return True
            days = sch.get('days')
            if days and idx % int(days) == 0:
                return True
            for kind in ('weekly', 'monthly', 'quarterly', 'yearly'):
                v = sch.get(kind)
                if v is None:
                    continue
                lo, hi = ranges[kind]
                in_grp = (bt >= lo) & (bt <= hi)
                n = int(in_grp.sum())
                if n == 0:
                    continue
                p = int(((bt >= lo) & (bt <= d)).sum())   # 组内正序（1 起）
                for w in (v if isinstance(v, list) else [v]):
                    if not isinstance(w, int) or w == 0:
                        continue
                    if (w > 0 and p == w) or (w < 0 and n - p + 1 == -w):
                        return True
            return False

        for sch in self._scheduled:
            try:
                if due(sch):
                    sch['func'](context, bar)
            except Exception as e:
                log.warning(f'schedule func error @ {dt}: {e}')

    # ── 交易日历清洗 ──────────────────────────────────────────────
    @staticmethod
    def _prune_dirty_dates(data: dict, all_dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
        """剔除「无有效行情」的占位日，返回清洗后的交易日历。

        问题：引擎交易日历 = 各标的行情日期的并集。个别标的会在非交易日残留一行
        全 NaN / volume=0 的占位数据（实证 `160526.SZ` 2022-06-03 端午节，全市场
        1136 只基金仅它一只有行且全空），并集日历于是凭空多出一个"交易日"：
        handle_data 多跑一次 → 调仓计数器相位漂移 → 全窗口调仓日与平台错位
        （轮动宝v3 实测 249 vs 269 个下单日、终值 -8827.52 元 = -8.83pp）。

        判据：某日全市场无任何标的的有效行情（volume>0；无 volume 列时退化为
        close 非 NaN）→ 判为占位日剔除。**仅裁剪日历，不动行情数据** —— 停牌标的
        当日仍保留其行（volume=0/close=NaN），平台"停牌不成交"语义不受影响。
        """
        # 有效行情判据与旧版逐字一致：volume>0（NaN 视 0，即排除）；无 volume 列时
        # close 非 NaN；均无则全有效。向量化：不再对每标的整列 pd.to_numeric +
        # 逐 Timestamp 建 Python set（实测占 run() ~5.8s），而是只在列非数值时
        # 才 to_numeric，并把有效行对应的 datetime64 底值拼接后 np.unique。
        valid_parts = []
        for df in data.values():
            if df is None or len(df) == 0:
                continue
            if 'volume' in df.columns:
                col = df['volume']
                if col.dtype.kind == 'O':
                    col = pd.to_numeric(col, errors='coerce')
                m = col.fillna(0) > 0
            elif 'close' in df.columns:
                col = df['close']
                if col.dtype.kind == 'O':
                    col = pd.to_numeric(col, errors='coerce')
                m = col.notna()
            else:
                m = pd.Series(True, index=df.index)
            arr = np.asarray(m, dtype=bool)
            valid_parts.append(df.index.values[arr])
        if not valid_parts:       # 数据整体无有效行（纯合成/异常）→ 退回原并集
            return all_dates
        valid_ns = np.unique(np.concatenate(valid_parts))
        all_ns = all_dates.asi8
        kept_mask = np.isin(all_ns, valid_ns.astype('int64'))
        if not kept_mask.any():
            return all_dates
        kept = all_dates[kept_mask]
        if kept_mask.sum() < kept_mask.shape[0]:
            dropped = all_dates[~kept_mask]
            head = ', '.join(str(d.date()) for d in dropped[:10])
            log.warning(f'交易日历剔除无有效行情的占位日 {dropped.shape[0]} 个'
                        f'（疑似脏行/非交易日）: {head}'
                        f'{" ..." if dropped.shape[0] > 10 else ""}')
        return kept

    # ── 单日(paper trading)模式：状态注入 / 当日导出 ─────────────
    def _apply_initial_state(self, state: dict):
        """把 T-1 外置状态装回引擎（对齐平台 PaperTradingRunner._pre_run
        → load_state 的语义）。必须在主循环开始前调用：除权除息与计划单
        撮合都发生在 T 日开盘前，依赖已恢复的持仓与现金。

        state 结构（由 paper.state.load_state 组装）：
            cash           昨日收盘现金
            positions      昨日收盘持仓行列表（含 cost_price/sum_buy_value 等）
            user_store     策略自定义跨日变量（平台每日 initialize 重跑，
                           故必须在 initialize 之前装回）
            planned_orders 昨日未成交计划订单行列表（今日开盘撮合）
            trading_day_index  本日应使用的交易日序号（paper 跨日携带；无则保持 0）
        """
        if state.get('trading_day_index') is not None:
            self.context.trading_day_index = int(state['trading_day_index'])
        pf = self.context.portfolio
        if state.get('cash') is not None:
            pf.cash = float(state['cash'])
            pf.starting_cash = pf.cash
            pf.portfolio_value = pf.cash
        for row in state.get('positions') or []:
            inst = str(row['instrument'])
            qty = int(row['current_qty'])
            if qty <= 0:
                continue
            key = f'{inst}_{Direction.LONG.value}'
            # T-1 收盘时当日买入尚冻结（T+1），T 日盘前全部解冻 → avail=qty
            pos = IPositionData(
                instrument=inst, name=str(row.get('name', '')),
                direction=Direction.LONG, position_key=key,
                current_qty=qty, avail_qty=qty,
                cost_price=float(row.get('cost_price', 0.0) or 0.0),
                last_price=float(row.get('last_price', 0.0) or 0.0),
                market_value=float(row.get('market_value', 0.0) or 0.0),
                sum_buy_value=float(row.get('sum_buy_value', 0.0) or 0.0),
                sum_sell_value=float(row.get('sum_sell_value', 0.0) or 0.0),
                commission=float(row.get('commission', 0.0) or 0.0),
                realized_pnl=float(row.get('realized_pnl', 0.0) or 0.0),
                dividend_qty=int(row.get('dividend_qty', 0) or 0),
                dividend_cash=float(row.get('dividend_cash', 0.0) or 0.0),
                open_date=str(row.get('open_date', '') or ''),
            )
            pf.positions[key] = pos
        pf._update()
        us = state.get('user_store')
        if us:
            self.context.user_store = dict(us)
        # 昨日计划订单 → 待撮合队列。注意：不重记提交日志（提交日志在
        # 信号日已记录；平台口径撮合日只记 failed/adjust）。order_key 用
        # planned_order_id，撮合结果可回溯到状态库计划单行。
        for prow in state.get('planned_orders') or []:
            inst = str(prow['instrument'])
            direction = (Direction.LONG if int(prow.get('direction', 1)) == 1
                         else Direction.SHORT)
            offset = (OffsetFlag.OPEN if int(prow.get('offset_flag', 0)) == 0
                      else OffsetFlag.CLOSE)
            otype = (OrderType.MARKET if str(prow.get('order_type', '0')) == 'U'
                     else OrderType.LIMIT)
            _Engine._order_counter += 1
            order = IOrderData(
                instrument=inst, name=str(prow.get('name', '')),
                direction=direction, offset_flag=offset,
                order_qty=int(prow.get('order_qty', 0) or 0),   # 原始数量，撮合日再按现金/风控缩量
                order_price=float(prow.get('order_price', 0.0) or 0.0),
                order_type=otype,
                order_status=OrderStatus.NOTTRADED,
                order_id=str(_Engine._order_counter),
                order_key=str(prow.get('planned_order_id', '')),
                trading_day=self.context.current_dt.strftime('%Y%m%d') if self.context.current_dt else '',
                insert_date=str(prow.get('signal_day', '')),
            )
            order.planned_order_id = str(prow.get('planned_order_id', ''))
            # 计划单在状态库的归属交易日（写入时的生效日）；盘后导出 planned_
            # results 时带回，供 save_state 精确回写该行。
            order.planned_trading_day = str(prow.get('trading_day', ''))
            self._pending_orders.append(order)
            self._injected_planned.append((order.planned_order_id, order))

    def _build_paper_export(self) -> dict:
        """单日模式盘后导出（对齐平台 PaperTradingRunner._post_run
        → save_state 的语义）：当日持仓/现金快照、当日订单与成交、
        今日 handle_data 产生但未成交的新单（→ T+1 计划订单）、
        注入计划单的撮合结果、user_store。"""
        pf = self.context.portfolio
        positions = []
        for pos in pf.positions.values():
            if pos.current_qty <= 0:
                continue
            positions.append(dict(
                instrument=pos.instrument,
                name=pos.name or self._name_map.get(str(pos.instrument), ''),
                current_qty=int(pos.current_qty),
                available_qty=int(pos.avail_qty),
                today_qty=int(pos.today_qty),
                cost_price=float(pos.cost_price),
                last_price=float(pos.last_price),
                market_value=float(pos.market_value),
                sum_buy_value=float(pos.sum_buy_value),
                sum_sell_value=float(pos.sum_sell_value),
                commission=float(pos.commission),
                realized_pnl=float(pos.realized_pnl),
                dividend_qty=int(pos.dividend_qty),
                dividend_cash=float(pos.dividend_cash),
                open_date=str(pos.open_date),
            ))

        def _order_row(o: IOrderData) -> dict:
            # order() 建单时不带 name（见 IOrderData 构造处），这里补 _name_map；
            # 否则落库的委托/计划单名称恒为空，看板与通知只能显示代码。
            return dict(
                instrument=o.instrument,
                name=o.name or self._name_map.get(str(o.instrument), ''),
                direction=int(1 if o.direction == Direction.LONG else 2),
                offset_flag=int(0 if o.offset_flag == OffsetFlag.OPEN else 1),
                order_qty=int(o.order_qty), order_price=float(o.order_price),
                filled_qty=int(o.filled_qty), average_price=float(o.avg_price),
                order_status=int(o.order_status), status_msg=str(o.status_msg),
                order_type=('U' if o.order_type == OrderType.MARKET else '0'),
                order_key=str(o.order_key),
                trading_day=str(o.trading_day), insert_date=str(o.insert_date),
            )

        orders = [_order_row(o) for o in self._filled_orders]
        trades = [dict(
            instrument=t.instrument, name=t.name,
            trade_id=str(t.trade_id), order_key=str(t.order_key),
            direction=int(1 if t.direction == Direction.LONG else 2),
            offset_flag=int(0 if t.offset_flag == OffsetFlag.OPEN else 1),
            filled_qty=int(t.filled_qty), filled_price=float(t.filled_price),
            filled_money=float(t.filled_money),
            commission=float(t.commission), realized_pnl=float(t.realized_pnl),
        ) for t in self._trades]

        # 计划单撮合结果（含成交与当日失败撤单两种去向）
        planned_results = []
        planned_carry = []
        for pid, o in self._injected_planned:
            if o in self._filled_orders:
                planned_results.append(dict(
                    planned_order_id=pid, order_status=int(o.order_status),
                    filled_qty=int(o.filled_qty),
                    average_price=float(o.avg_price),
                    status_msg=str(o.status_msg), order_key=str(o.order_key),
                    trading_day=getattr(o, 'planned_trading_day', '')))
            else:
                planned_carry.append(dict(
                    planned_order_id=pid, instrument=o.instrument,
                    name=o.name or self._name_map.get(str(o.instrument), ''),
                    direction=int(1 if o.direction == Direction.LONG else 2),
                    offset_flag=int(0 if o.offset_flag == OffsetFlag.OPEN else 1),
                    order_qty=int(o.order_qty), order_price=float(o.order_price),
                    order_type=('U' if o.order_type == OrderType.MARKET else '0'),
                    signal_day=str(o.insert_date)))
        # 今日新提交且未成交的订单 → T+1 计划订单（对齐平台 PlannedOrder
        # 只导出未发出订单的口径；提交日志已在本日 run_logs 中）
        new_pending = [_order_row(o) for o in self._pending_orders
                       if not getattr(o, 'planned_order_id', None)]
        buy_trades = [t for t in self._trades if t.direction == Direction.LONG]
        sell_trades = [t for t in self._trades if t.direction != Direction.LONG]
        return dict(
            cash=float(pf.cash),
            portfolio_value=float(pf.portfolio_value),
            total_market_value=float(pf.positions_value),
            positions=positions,
            orders=orders,
            trades=trades,
            planned_results=planned_results,
            planned_carry=planned_carry,
            new_pending_orders=new_pending,
            user_store=dict(self.context.user_store or {}),
            today_sum_buy_value=float(sum(t.filled_money for t in buy_trades)),
            today_sum_sell_value=float(sum(t.filled_money for t in sell_trades)),
            commission=float(sum(t.commission for t in self._trades)),
            realized_pnl=float(sum(t.realized_pnl for t in sell_trades)),
            profit_count=sum(1 for t in sell_trades if t.realized_pnl > 0),
            loss_count=sum(1 for t in sell_trades if t.realized_pnl <= 0),
        )

    # ── 主循环 ────────────────────────────────────────────────────
    def run(self, initialize, handle_data, before_trading_start,
            after_trading, handle_trade, handle_order,
            data: dict, instruments: list,
            start_date: str, end_date: str,
            capital_base: float,
            before_start_days: int = 0,
            benchmark_symbol: str = '000300.SH',
            benchmark_data: pd.Series = None,
            dividend_events: pd.DataFrame = None,
            dividend_tax_rate: float = 0.2,
            instrument_names: Dict[str, str] = None,
            instrument_t0: set = None,
            initial_state: dict = None,
            paper_mode: bool = False) -> Performance:

        self._all_data = data
        if dividend_events is not None:
            self._dividend_events = dividend_events
        self._dividend_tax_rate = dividend_tax_rate
        _Engine._order_counter = 0
        _Engine._trade_counter = 0

        # 初始化合约
        self._init_contracts(instruments or list(data.keys()), instrument_names, instrument_t0)

        # 构建交易日序列。交易日历覆盖传入数据的全部日期（含 start_date 前的
        # 预热段，供策略在 initialize 中 add_trading_days(-N) 取因子预热窗口；
        # 预热段主循环只推进行情、不调用策略回调不撮合 —— 与平台一致）。
        # 交易日历 = 各标的行情日期的并集。向量化实现：把所有 DatetimeIndex 的
        # datetime64 底层值拼接后 np.unique（排序+去重）即得有序唯一并集 —— 与
        # 旧的 `sorted(set(for dt in df.index))` 结果逐元素一致，但避免遍历千万级
        # datetime 对象（实测轮动宝v3 这处占 run() ~4.6s → ~0）。
        _idx_parts = [df.index.values for df in data.values()
                      if df is not None and len(df)]
        if _idx_parts:
            all_dates = pd.DatetimeIndex(np.unique(np.concatenate(_idx_parts)))
        else:
            all_dates = pd.DatetimeIndex([])
        all_dates = self._prune_dirty_dates(data, all_dates)
        s = pd.Timestamp(start_date)
        e = pd.Timestamp(end_date)
        warmup_start = all_dates[0]   # 数据首日（原 before_start_days 截断会破坏负向 add_trading_days）
        mask = (all_dates >= warmup_start) & (all_dates <= e)
        self._trading_dates = all_dates[mask]
        trading_dates = all_dates[(all_dates >= s) & (all_dates <= e)]

        if len(trading_dates) == 0:
            raise ValueError(f'在 {start_date}~{end_date} 范围内没有找到交易日')
        # 回测段在交易日历(dates<=e)里的第几个位置起算 —— 供 trading_day_index 用
        # 循环下标直接换算，避免每天 O(n) 的 `(trading_dates < dt).sum()`（O(n²)）。
        _bt_start = int((self._trading_dates < s).sum())

        # 除权除息事件按除权日分组（平台/数据源口径：事件列名为 date=除权处理日）
        self._dividend_by_date: Dict[pd.Timestamp, pd.DataFrame] = {}
        if self._dividend_events is not None and len(self._dividend_events) > 0:
            ev = self._dividend_events.copy()
            if 'date' in ev.columns:
                ev['ex_date'] = pd.to_datetime(ev['date'])
            if 'ex_date' not in ev.columns:
                raise ValueError('dividend_events 需含 ex_date/date(除权日) 列')
            ev['ex_date'] = pd.to_datetime(ev['ex_date'])
            for d, g in ev.groupby(ev['ex_date'].dt.normalize()):
                self._dividend_by_date[d] = g

        # 初始化 context
        self.context = IContext(capital_base)
        self.context._engine = self
        self.context.start_date = start_date
        self.context.end_date   = end_date
        self.context.first_trading_date = trading_dates[0].strftime('%Y-%m-%d')  # 对齐平台 'YYYY-mm-dd'

        # 单日(paper trading)模式：先装回 T-1 外置状态（持仓/现金/user_store/
        # 计划订单），再跑 initialize —— 平台每日重跑 initialize，user_store
        # 必须在它之前恢复，策略的 init_once 守卫才能看到已有状态。
        if initial_state:
            self._apply_initial_state(initial_state)

        # 初始化调仓周期（如果 context.data 是 Rebalance 对象）
        if initialize:
            initialize(self.context)
            # 如果 context.data 是 Rebalance 对象，build 它
            if isinstance(getattr(self.context, 'data', None), _BaseRebalance):
                self.context._rebalance = self.context.data
                self.context._rebalance.build(start_date, end_date,
                                               _trading_dates=self._trading_dates)
                self.context.data = None

        # 主循环（含预热期）。单日时序对齐平台 bigtrader（A股日频）：
        #   开盘前: T+1解冻 → 除权除息 → 撮合昨日订单(今日开盘价)
        #   收盘后: 按收盘价重估 → before_trading_start → handle_data(产生次日订单)
        #         → after_trading → 记录净值
        # 预热期（start_date 之前的数据，供因子/指标预热）：只准备行情与上下文，
        # 不调用策略回调、不下单不撮合（对齐平台：预热期无成交、净值从首日 0 起）。
        for i, dt in enumerate(self._trading_dates):
            self.context.current_dt = dt
            in_backtest = dt >= s

            if in_backtest:
                # 单日(paper trading)模式：trading_day_index 由 paper 层跨日携带，
                # 在 _apply_initial_state 注入（否则单日窗口里恒为 0，会破坏
                # trading_day_index%N 调仓类策略的节拍，使其每天调仓）。回测才按
                # 窗口内序号重算。
                if not paper_mode:
                    self.context.trading_day_index = i - _bt_start
                self._unfreeze_t1()
                self._process_dividends(dt)
                self._fill_orders(dt, handle_trade, handle_order)

            if not in_backtest:
                # 预热期：仅推进行情（不产生订单），跳过策略回调
                continue

            self._refresh_portfolio(dt)
            bar = IBarData(self._all_data, dt)

            if before_trading_start:
                try:
                    before_trading_start(self.context, bar)
                except Exception as e:
                    log.warning(f'before_trading_start error @ {dt}: {e}')

            if handle_data:
                try:
                    handle_data(self.context, bar)
                except Exception as e:
                    log.warning(f'handle_data error @ {dt}: {e}')

            # schedule 注册的日频回调（无注册则零开销直接返回）
            self._run_scheduled(dt, self.context, bar)

            if after_trading:
                try:
                    after_trading(self.context, bar)
                except Exception as e:
                    log.warning(f'after_trading error @ {dt}: {e}')

            self._equity.append((dt, self.context.portfolio.portfolio_value))
            self._record_daily_snapshot(dt)

        # 回测结束仍未成交的订单置为已撤销（平台口径：也记一条订单日志）。
        # 单日(paper trading)模式例外：pending 单就是 T+1 计划订单，原样导出
        # 交由状态库落库、次日撮合，不得置 CANCELLED。
        if paper_mode:
            self._paper_export = self._build_paper_export()
        else:
            for o in self._pending_orders:
                o.order_status = OrderStatus.CANCELLED
                o.status_msg = '回测结束未成交'
                self._filled_orders.append(o)
                if o.order_key not in self._order_log_map:
                    td = o.trading_day
                    lg_date = f'{td[:4]}-{td[4:6]}-{td[6:]}' if len(td) == 8 else str(td)
                    word = 'BUYOPEN' if (o.direction == Direction.LONG
                                         and o.offset_flag == OffsetFlag.OPEN) else 'SELLCLOSE'
                    msg = (f'order[93000000][id:{o.order_id},{o.instrument} '
                           f'{word} {o.order_qty}@{o.order_price:.4f},Market]')
                    self._order_logs.append({'date': lg_date, 'level': 'INFO', 'msg': msg})
        self._pending_orders.clear()

        equity_curve = pd.Series(
            {dt: v for dt, v in self._equity}, name='portfolio_value'
        )
        return Performance(
            equity_curve, self._filled_orders, self._trades,
            capital_base, benchmark_data, benchmark_symbol,
            daily_positions=self._position_snapshots,
            order_logs=self._order_logs,
            instrument_names=self._name_map,
            daily_stats=self._daily_stats,
            dividend_log=self._dividend_log,
        )


# ══════════════════════════════════════════════════════════════════
# 模块级辅助函数（RunMode 查询）
# ══════════════════════════════════════════════════════════════════

_current_run_mode: RunMode = RunMode.BACKTEST


def get_run_mode() -> RunMode:
    return _current_run_mode


def in_backtest_mode(mode: RunMode = None) -> bool:
    m = mode or _current_run_mode
    return m == RunMode.BACKTEST


def in_paper_trading_mode(mode: RunMode = None) -> bool:
    m = mode or _current_run_mode
    return m == RunMode.PAPER_TRADING


def in_live_trading_mode(mode: RunMode = None) -> bool:
    m = mode or _current_run_mode
    return m == RunMode.LIVE_TRADING


def trading_date(backtest_date: str = None, sub_days: float = None) -> str:
    """返回当前回测日期字符串（YYYYMMDD）"""
    global _active_engine
    if _active_engine and _active_engine.context and _active_engine.context.current_dt:
        dt = _active_engine.context.current_dt
        if sub_days:
            td = _active_engine._trading_dates
            idx = td.searchsorted(dt)
            idx = max(0, idx - int(sub_days))
            dt = td[idx]
        return dt.strftime('%Y%m%d')
    return backtest_date or ''


# ══════════════════════════════════════════════════════════════════
# 主入口 run()  — 完整对齐 bigtrader.run()
# ══════════════════════════════════════════════════════════════════

_active_engine: _Engine = None

# 自动数据源（平台隐式取数的本地等价物）：由 data_source 模块 import 时注册。
# 签名: loader(market, instruments, start_date, end_date, benchmark, warmup_days)
#       -> dict(data/instruments/dividend_events/instrument_names/instrument_t0)
_AUTO_LOADER = None


def run(*,
        market: Market = Market.CN_STOCK,
        frequency: Frequency = Frequency.DAILY,
        instruments: List[str] = None,
        start_date: str = '',
        end_date: str = '',
        data: Any = None,
        capital_base: float = 1_000_000.0,
        initialize: Callable = None,
        before_trading_start: Callable = None,
        handle_data: Callable = None,
        handle_trade: Callable = None,
        handle_order: Callable = None,
        handle_tick: Callable = None,
        handle_l2order: Callable = None,
        handle_l2trade: Callable = None,
        after_trading: Callable = None,
        benchmark: str = '000300.SH',
        options_data: Any = None,
        before_start_days: int = 0,
        volume_limit: float = 1.0,
        order_price_field_buy: str = 'open',
        order_price_field_sell: str = 'open',
        user_data: Any = None,
        engine: str = None,
        logger=None,
        account_type: AccountType = AccountType.NONE,
        backtest_only: bool = False,
        render: str = 'auto',
        report_output_path=True,
        _runner=None,
        # 扩展参数（smalltrader 专有）
        commission_model=None,
        slippage: float = 0.0,   # 平台默认无滑点：成交价 = 行情价
        t1: bool = True,
        dividend_events: pd.DataFrame = None,
        dividend_tax_rate: float = 0.2,
        instrument_names: Dict[str, str] = None,
        instrument_t0: set = None,   # 当日回转(T+0)标的集合，如跨境/商品/债券 ETF
        warmup_days: Optional[int] = None,  # 自动取数：行情窗口前推自然日（None=按市场自适应）
        # ── 单日(paper trading)模式参数 ────────────────────────────
        run_mode: RunMode = RunMode.BACKTEST,
        initial_positions: List[dict] = None,   # T-1 收盘持仓行（paper.state.load_state 产出）
        initial_cash: float = None,             # T-1 收盘现金
        planned_orders: List[dict] = None,      # 昨日未成交计划订单行（今日开盘撮合）
        user_store: dict = None,                # 策略自定义跨日变量
        initial_trading_day_index: int = None,  # 昨日+1 的交易日序号（paper 跨日携带，
                                                # 使 trading_day_index%N 调度与回测一致）
        ) -> Performance:
    """
    启动回测，API 完全对齐 bigtrader.run()。

    Parameters
    ----------
    instruments  : 标的列表，如 ['000001.SZA', '600000.SHA']
    start_date   : 回测开始日期 'YYYY-MM-DD'
    end_date     : 回测结束日期 'YYYY-MM-DD'
    data         : dict[symbol, DataFrame]，DataFrame 需含 OHLCV 列，DatetimeIndex
                   或 pd.DataFrame（宽表，columns 为 symbol，index 为日期）
                   通常由 data_source.StockDataSource.get_bar_data() 生成。
                   不传时若已注册自动数据源（import data_source 即注册），
                   引擎按 market/instruments 自动拉取 —— 与平台隐式取数一致，
                   股票/基金/混合池自适应，写法与平台原版逐字相同。
    capital_base : 初始资金
    initialize   : 初始化函数 initialize(context)
    handle_data  : 每日调用 handle_data(context, data)
    before_trading_start : 盘前 before_trading_start(context, data)
    after_trading: 盘后 after_trading(context, data)
    handle_trade : 成交回调 handle_trade(context, trade)
    handle_order : 订单回调 handle_order(context, order)
    benchmark    : 基准代码（仅用于显示，需在 data 中提供对应数据）
    order_price_field_buy/sell : 成交价字段（'open'/'close'/'twap_*'/'vwap_*'）
    commission_model : PerOrder / PerContract 实例（smalltrader 扩展参数）
    slippage     : 滑点比例（smalltrader 扩展参数，默认 0 对齐平台）
    t1           : 是否启用 T+1 限制（smalltrader 扩展参数）
    dividend_events : 除权除息事件 DataFrame（由数据源提供：ex_date/instrument/
                     cash_before_tax/bonus_rate/conversed_rate 等列；
                     混合池中基金行带 is_fund_split=1 标记走份额折算）
    dividend_tax_rate : 现金红利税率（平台口径 0.2，除息日扣税）
    warmup_days  : 自动取数时行情窗口前推自然日（None=自适应：池含基金取 365
                   覆盖因子预热，纯股票 0；股票策略 initialize 有 add_trading_days
                   预热时显式传大值）

    Returns
    -------
    Performance
    """
    global _active_engine, _current_run_mode
    _current_run_mode = run_mode
    is_paper = run_mode != RunMode.BACKTEST
    # —— 进度日志（对齐 SDK 回测引擎提示，bigtrader→smalltrader）─────────
    _acct = (account_type if account_type is not None else AccountType.NONE)
    _acct = (_acct.value if isinstance(_acct, AccountType) else str(_acct)).upper() or 'STOCK'
    _mode = run_mode.value if isinstance(run_mode, RunMode) else str(run_mode)
    _market = market.value if isinstance(market, Market) else str(market)
    _freq = frequency.value if isinstance(frequency, Frequency) else str(frequency)
    log.info('smalltrader init ..')
    log.info('smalltrader.run start: market=%s, frequency=%s, mode=%s, account_type=%s, '
             'start_date=%s, end_date=%s',
             _market, _freq, _mode, _acct, start_date, end_date)
    log.info('smalltrader<%s> init ..', _mode)
    # 单日模式：外置状态装回引擎（持仓/现金/user_store/昨日计划订单）。
    # 撮合、除权除息等引擎语义与回测完全共用，差异只在状态来源与去向。
    initial_state = None
    if is_paper and (initial_positions or initial_cash is not None
                     or planned_orders or user_store
                     or initial_trading_day_index is not None):
        initial_state = dict(
            cash=initial_cash,
            positions=initial_positions or [],
            planned_orders=planned_orders or [],
            user_store=user_store or {},
            trading_day_index=initial_trading_day_index,
        )

    # ── 自动取数（平台隐式取数的本地等价物）────────────────────
    # data 未传且已注册自动数据源（data_source 模块 import 时注册）：
    # 按 market + instruments 拉取行情/事件/名称/基准，显式参数始终优先。
    log.info('prepare data ..')
    if data is None:
        if _AUTO_LOADER is None:
            raise ValueError(
                '未传 data 参数且未注册自动数据源：请 import data_source '
                '（自动按 market 取数）或显式传 data=dict[symbol, DataFrame]')
        auto = _AUTO_LOADER(
            market=market, instruments=instruments,
            start_date=start_date, end_date=end_date,
            benchmark=benchmark, warmup_days=warmup_days)
        data = auto['data']
        if instruments is None:
            instruments = auto.get('instruments')
        if dividend_events is None:
            dividend_events = auto.get('dividend_events')
        if instrument_names is None:
            instrument_names = auto.get('instrument_names')
        if instrument_t0 is None:
            instrument_t0 = auto.get('instrument_t0')

    # 数据格式标准化
    if data is None:
        raise ValueError('请提供 data 参数：dict[symbol, DataFrame]')

    if isinstance(data, pd.DataFrame):
        # 宽表转字典
        data_dict = {}
        for col in data.columns:
            data_dict[col] = pd.DataFrame({'close': data[col]})
    elif isinstance(data, dict):
        data_dict = data
    else:
        raise ValueError('data 必须是 dict[symbol, DataFrame] 或 pd.DataFrame')

    # 如果没有指定 instruments，从 data 中推断
    if instruments is None:
        instruments = list(data_dict.keys())

    # 对齐 SDK：打印合并行情表的行数×列数（行 = 各标的行情行数之和），
    # 以及除权除息事件表维度。
    try:
        _n_rows = sum(len(df) for df in data_dict.values()
                      if isinstance(df, pd.DataFrame) and df is not None)
        _n_cols = len(set().union(*[set(df.columns) for df in data_dict.values()
                                    if isinstance(df, pd.DataFrame) and df is not None]) or {0})
        log.info('bar1d_df: (%d, %d)', _n_rows, _n_cols)
    except Exception:  # noqa: BLE001 - 进度日志失败不阻塞回测
        pass
    if dividend_events is not None and hasattr(dividend_events, 'shape'):
        log.info('smalltrader use dividend data: (%d, %d)',
                 dividend_events.shape[0], dividend_events.shape[1])

    # 基准数据（裁剪到回测区间起点的行情；基准累计收益率从回测首日起算）
    benchmark_data = None
    if benchmark and benchmark in data_dict:
        bm_df = data_dict[benchmark]
        if 'close' in bm_df.columns:
            benchmark_data = bm_df['close']
            if start_date:
                benchmark_data = benchmark_data[benchmark_data.index >= pd.Timestamp(start_date)]

    # 证券名称映射（report 展示用）：优先显式传入；否则尝试从行情 DataFrame
    # 的 name 列（单值）推断
    if instrument_names is None:
        instrument_names = {}
        for sym, df in data_dict.items():
            if 'name' in df.columns:
                vals = df['name'].dropna().unique()
                if len(vals):
                    instrument_names[str(sym)] = str(vals[0])
    elif isinstance(instrument_names, pd.Series):
        instrument_names = {str(k): str(v) for k, v in instrument_names.items()}

    # 构建引擎
    _active_engine = _Engine(
        commission_model=commission_model,
        slippage_ratio=slippage,
        price_field_buy=order_price_field_buy,
        price_field_sell=order_price_field_sell,
        volume_limit=volume_limit,
        t1=t1,
        dividend_events=dividend_events,
        dividend_tax_rate=dividend_tax_rate,
    )

    paper_export = None
    log.info('smalltrader run ..')
    try:
        result = _active_engine.run(
            initialize=initialize,
            handle_data=handle_data,
            before_trading_start=before_trading_start,
            after_trading=after_trading,
            handle_trade=handle_trade,
            handle_order=handle_order,
            data=data_dict,
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
            capital_base=capital_base,
            before_start_days=before_start_days,
            benchmark_symbol=benchmark,
            benchmark_data=benchmark_data,
            instrument_names=instrument_names,
            instrument_t0=instrument_t0,
            initial_state=initial_state,
            paper_mode=is_paper,
        )
        paper_export = _active_engine._paper_export
    finally:
        _active_engine = None

    # 单日模式：把盘后导出（持仓/现金/订单/成交/计划单撮合结果/user_store）
    # 挂到 Performance 上，paper.runner 据此落库；回测模式恒为 None。
    result.paper_export = paper_export

    if render in ('auto', 'notebook', 'report'):
        result.render(render_type=render)
    log.info('smalltrader run done.')
    return result


# ══════════════════════════════════════════════════════════════════
# 模块级下单快捷函数（兼容旧版 smalltrader 写法）
# ══════════════════════════════════════════════════════════════════

def order(context: IContext, instrument: str, volume: int,
          limit_price: float = None, order_type: OrderType = None, **kwargs) -> int:
    return context.order(instrument, volume, limit_price, order_type, **kwargs)


def order_value(context: IContext, instrument: str, value: float,
                limit_price: float = None, order_type: OrderType = None, **kwargs) -> int:
    return context.order_value(instrument, value, limit_price, order_type, **kwargs)


def order_target(context: IContext, instrument: str, target: float,
                 limit_price: float = None, order_type: OrderType = None, **kwargs) -> int:
    return context.order_target(instrument, target, limit_price, order_type, **kwargs)


def order_target_value(context: IContext, instrument: str, target: float,
                       limit_price: float = None, order_type: OrderType = None, **kwargs) -> int:
    return context.order_target_value(instrument, target, limit_price, order_type, **kwargs)


def order_target_percent(context: IContext, instrument: str, target: float,
                         limit_price: float = None, order_type: OrderType = None, **kwargs) -> int:
    return context.order_target_percent(instrument, target, limit_price, order_type, **kwargs)


def order_percent(context: IContext, instrument: str, percent: float,
                  limit_price: float = None, order_type: OrderType = None, **kwargs) -> int:
    return context.order_percent(instrument, percent, limit_price, order_type, **kwargs)


def buy_open(context: IContext, instrument: str, volume: int,
             limit_price: float = None, order_type: OrderType = None, **kwargs) -> int:
    return context.buy_open(instrument, volume, limit_price, order_type, **kwargs)


def sell_open(context: IContext, instrument: str, volume: int,
              limit_price: float = None, order_type: OrderType = None, **kwargs) -> int:
    return context.sell_open(instrument, volume, limit_price, order_type, **kwargs)


def buy_close(context: IContext, instrument: str, volume: int,
              limit_price: float = None, order_type: OrderType = None, **kwargs) -> int:
    return context.buy_close(instrument, volume, limit_price, order_type, **kwargs)


def sell_close(context: IContext, instrument: str, volume: int,
               limit_price: float = None, order_type: OrderType = None, **kwargs) -> int:
    return context.sell_close(instrument, volume, limit_price, order_type, **kwargs)


def cancel_order(context: IContext, order_param) -> int:
    return context.cancel_order(order_param)


# ══════════════════════════════════════════════════════════════════
# __all__
# ══════════════════════════════════════════════════════════════════

__all__ = [
    # 枚举
    'Market', 'Frequency', 'Direction', 'OffsetFlag', 'OrderType', 'OrderStatus',
    'OrderProperty', 'BSFlag', 'FilledType', 'HedgeFlag', 'OptionCP', 'StrikeType',
    'AccountType', 'BacktestType', 'RunMode', 'VMatchAt', 'SubscribeFlag',
    # 数据模型
    'IContractData', 'IPositionData', 'IOrderData', 'ITradeData', 'IBar',
    'ITickData', 'ICommissionRateData', 'IMarginRateData', 'IOrderReq',
    'IPortfolio', 'IBarData', 'IContext', 'ISlippageModel',
    # 佣金/滑点
    'PerOrder', 'PerContract',
    # 调仓周期
    'TradingDaysRebalance', 'MonthlyRebalance', 'WeeklyRebalance',
    'NaturalDaysRebalance', 'QuarterlyRebalance', 'YearlyRebalance',
    'MonthlyTradingDaysRebalance', 'WeeklyTradingDaysRebalance',
    'QuarterlyTradingDaysRebalance', 'YearlyTradingDaysRebalance',
    # 辅助
    'HandleDataLib', 'Performance',
    # 主入口
    'run',
    # RunMode 查询
    'get_run_mode', 'in_backtest_mode', 'in_paper_trading_mode',
    'in_live_trading_mode', 'trading_date',
    # 模块级下单快捷函数
    'order', 'order_value', 'order_target', 'order_target_value',
    'order_target_percent', 'order_percent',
    'buy_open', 'sell_open', 'buy_close', 'sell_close', 'cancel_order',
    # 版本
    '__version__', 'RELEASE_VERSION', 'RELEASE_DATE',
]
