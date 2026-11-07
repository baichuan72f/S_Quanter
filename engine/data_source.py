"""
数据源模块 —— 回测/模拟盘的行情/事件数据获取层（与引擎解耦）

来源：自写回测工具 pro（`D:\\Data\\bq\\SmallQuant\\data_source.py`）原样移植，
把云端 dai 数据源整体换成本地 DuckDB（`queries.connect()`，惰性读
`data/*.parquet`）。本项目因此**全离线**：不 import bigquant / dai / bigtradercpp。

设计目标
--------
smalltrader 引擎只认「dict[symbol, DataFrame]」形态的日线数据，不关心数据从哪来。
本模块把「数据引用 / 数据获取」单独收口到数据源类中：

    - LocalDuckDataSource : （唯一实现）从本地 DuckDB（queries.connect()，
                            视图/表名见 queries.TABLES）取数 —— 全离线可跑；
                            数据快照更新到 data/ 即自动生效。

新增数据源只需继承 StockDataSource 并实现同名方法即可替换。

数据源职责
----------
1. get_bar_data(instruments, start_date, end_date)
   -> dict[symbol, pd.DataFrame]  每只标的一个 DataFrame：
        index  : DatetimeIndex（交易日）
        columns: open/high/low/close/volume/pre_close/upper_limit/lower_limit/amount
   该字典可直接作为 smalltrader.run(data=...) 的输入；
   扩展字段（涨跌停价、adjust_factor 等）在 DataFrame 里附带，引擎按需读取，
   缺列不报错。

2. get_dividend_events(instruments, start_date, end_date)
   -> pd.DataFrame  回测区间内的除权除息事件（分红/送转股），供引擎在除息日做资金与持股调整：
        date            : 除息除权日（交易日）
        instrument      : 标的
        cash_before_tax : 每股税前现金红利（元）
        cash_after_tax  : 每股税后现金红利（元）
        bonus_rate      : 每股送红股数（红股按面值 1 元计红利税）
        conversed_rate  : 每股转增股数（转增不征税）
        register_date   : 股权登记日

3. get_fund_backtest_data / get_stock_backtest_data(...)
   -> dict  基金/股票回测一站式取数：行情(含基准) + 分红/除权事件 + 证券简称 + T+0
            标的；instruments 默认 None = 取 price_table 全市场。返回 dict 可直接
            ** 展开为 smalltrader.run() 的数据类关键字参数。

4. run_fund_backtest / run_stock_backtest(**kwargs)
   -> Performance  （便捷别名，语义与 bigtrader.run(market=...) 相同，可直接用 run）

自动取数（推荐写法，与 bigtrader.run 同形）：
    import smalltrader as bigtrader
    import data_source            # ← import 即注册：bigtrader.run(data=缺省) 自动取数
    perf = bigtrader.run(market=bigtrader.Market.CN_FUND, start_date=..., end_date=...,
                         initialize=initialize, handle_data=handle_data, ...)
    # market=CN_STOCK / CN_FUND / 混合池(传 instruments 混股票+基金) 均自适应：
    # 基金行/股票行按代码前缀自动分流到 cn_fund_real_bar1d / cn_stock_real_bar1d，
    # 分红事件按行区分份额折算(基金)与现金红利/送转(股票)，名称/T+0 分表合并。
    # warmup_days 扩展参数：行情窗口前推自然日（None=池含基金 365 / 纯股票 0）。

高级用法（行情显式传入，离线缓存回放等）：
    from data_source import LocalDuckDataSource
    ds = LocalDuckDataSource(price_table='cn_fund_real_bar1d')
    bt = ds.get_fund_backtest_data(start_date='2026-01-01', end_date='2026-09-03')
    perf = bigtrader.run(..., **bt)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import queries   # 本地 DuckDB：connect() 返回已建好视图的连接（惰性读 data/ 下 parquet）

# cn_stock_real_bar1d 全部行情字段（除 name 外按原样保留）
# adjust_factor 供引擎反推基金分红（见引擎 _process_dividends），本地表自带该列。
_BAR_COLUMNS = [
    'open', 'high', 'low', 'close', 'volume',
    'pre_close', 'upper_limit', 'lower_limit', 'amount', 'adjust_factor',
]

_DIVIDEND_COLUMNS = [
    'ex_date', 'instrument', 'cash_before_tax', 'bonus_rate',
    'conversed_rate', 'register_date', 'publish_date',
]


def _engine_module():
    """解析回测引擎模块 smalltrader，兼容两种布局：
    - 包式：engine/data_source.py ↔ engine/smalltrader.py（import engine.data_source）；
    - 扁平：data_source.py ↔ smalltrader.py（engine/ 目录直接进 sys.path）。
    除注册 _AUTO_LOADER 外，引擎内部须解析自身同级模块（见 data_source.py 各
    'import smalltrader' / smalltrader.py 的 'import smalltrader_report'），
    二者都需走本函数而非顶层 import，否则包式布局会因找不到顶层模块而静默失败。
    """
    import importlib
    import sys
    pkgname = getattr(sys.modules.get(__name__, None), '__package__', '') or ''
    modname = (pkgname + '.smalltrader') if pkgname else 'smalltrader'
    return importlib.import_module(modname)


class StockDataSource(ABC):
    """行情数据源抽象基类：新增数据源只需继承并实现本类方法。"""

    @abstractmethod
    def get_bar_data(self, instruments: List[str],
                     start_date: str, end_date: str) -> Dict[str, pd.DataFrame]:
        """获取 [start_date, end_date] 内标的的日线行情。

        Returns
        -------
        dict[symbol, DataFrame]
            每标的一个 DataFrame，index 为 DatetimeIndex（仅含该标的的交易日），
            列含 open/high/low/close/volume 等。
        """

    @abstractmethod
    def get_dividend_events(self, instruments: List[str],
                            start_date: str, end_date: str) -> pd.DataFrame:
        """获取回测区间内的除权除息事件（含 register_date/ex_date）。"""


class LocalDuckDataSource(StockDataSource):
    """从本地 DuckDB（queries.connect()，视图/表名见 queries.TABLES）读取行情/事件数据。

    data/ 下的 parquet 为本地行情/静态数据快照（股票 cn_stock_real_bar1d /
    cn_stock_dividend，基金 cn_fund_real_bar1d / cn_fund_bar1d，指数 cn_stock_index_bar1d，
    名称 cn_stock_instruments / cn_fund_instruments），全离线可跑、结果可复现。

    接口见 StockDataSource 基类：get_bar_data / get_dividend_events /
    get_fund_dividend_events / get_instrument_names / get_t0_instruments /
    get_index_bar_data / get_fund_backtest_data / get_stock_backtest_data。

    差异（已在方法内消化）：
      * 股票除权除息读本地 cn_stock_dividend（含 bonus_rate/conversed_rate，无配股表）；
      * 基金无独立分红表 —— 用 cn_fund_real_bar1d 的 adjust_factor 跳变定位除息日，
        现金红利 = 前一交易日收盘 − 除息日 pre_close（除权参考价），生成 is_fund_split=1
        的份额折算事件（份额折算主逻辑在引擎）；
      * T+0 当日回转无本地标志 → 空集（多日调仓策略不受影响）。
    """

    _con_cache: Dict[str, object] = {}

    def __init__(self, price_table: str = 'cn_stock_real_bar1d',
                 dividend_table: str = 'cn_stock_dividend',
                 data_root=None):
        self.price_table = price_table
        self.dividend_table = dividend_table
        self.data_root = data_root          # None = queries.DATA_ROOT
        # 名称表按代码类型分流：基金源码自身表含 date，但名称表独立
        self.name_table = 'cn_stock_instruments'

    # ── 查询工具 ──────────────────────────────────────────────
    @classmethod
    def _db(cls, data_root=None):
        """按数据根目录缓存连接（缺省用 queries.DATA_ROOT）。"""
        key = str(data_root) if data_root else '__default__'
        if key not in cls._con_cache:
            cls._con_cache[key] = queries.connect(data_root)
        return cls._con_cache[key]

    def _run(self, sql: str, params: Optional[list] = None) -> pd.DataFrame:
        return self._db(self.data_root).execute(sql, params or []).df()

    @staticmethod
    def _fmt_in(instruments: List[str]) -> str:
        return ', '.join(f"'{str(i)}'" for i in instruments)

    @staticmethod
    def _chunks(instruments: List[str], n: int = 600) -> List[List[str]]:
        return [instruments[k:k + n] for k in range(0, len(instruments), n)]

    @staticmethod
    def _norm_date(df: pd.DataFrame) -> pd.DataFrame:
        if not df.empty and 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date']).dt.normalize()
        return df

    # ── 日线行情 ──────────────────────────────────────────────
    def get_bar_data(self, instruments: Optional[List[str]] = None,
                     start_date: str = '', end_date: str = '') -> Dict[str, pd.DataFrame]:
        """[start_date, end_date] 内标的日线（本地价格表，含扩展列）。

        instruments=None → 整表（全市场）；None + 空日期 → 全区间。
        """
        cols = ', '.join(_BAR_COLUMNS)
        if instruments is not None and not instruments:
            return {}
        if instruments is not None:
            dfs = []
            for part in self._chunks([str(i) for i in instruments]):
                sql = (f"SELECT date, instrument, {cols} FROM {self.price_table} "
                       f"WHERE instrument IN ({self._fmt_in(part)})")
                if start_date or end_date:
                    cond = []
                    if start_date:
                        cond.append(f"date >= DATE '{start_date}'")
                    if end_date:
                        cond.append(f"date <= DATE '{end_date}'")
                    sql += ' AND ' + ' AND '.join(cond)
                dfs.append(self._run(sql + ' ORDER BY instrument, date'))
            raw = pd.concat(dfs, ignore_index=True) if len(dfs) > 1 else dfs[0]
        else:
            sql = f"SELECT date, instrument, {cols} FROM {self.price_table}"
            cond = []
            if start_date:
                cond.append(f"date >= DATE '{start_date}'")
            if end_date:
                cond.append(f"date <= DATE '{end_date}'")
            if cond:
                sql += ' WHERE ' + ' AND '.join(cond)
            raw = self._run(sql + ' ORDER BY instrument, date')
        raw = self._norm_date(raw)
        out: Dict[str, pd.DataFrame] = {}
        for inst, g in raw.groupby('instrument'):
            g = g.sort_values('date').set_index('date').drop(columns='instrument')
            out[str(inst)] = g.astype({c: np.float64 for c in g.select_dtypes(
                include=[np.number]).columns})
        return out

    # ── 股票除权除息事件 ──────────────────────────────────────
    def get_dividend_events(self, instruments: List[str],
                            start_date: str, end_date: str) -> pd.DataFrame:
        """本地 cn_stock_dividend 的除权除息事件（date=除息日，含送/转比例）。"""
        if not instruments:
            return pd.DataFrame(columns=_DIVIDEND_COLUMNS)
        dfs = []
        cols = ('date, instrument, cash_before_tax, cash_after_tax, bonus_rate, '
                'conversed_rate, register_date, publish_date')
        for part in self._chunks([str(i) for i in instruments]):
            sql = (f"SELECT {cols} FROM {self.dividend_table} "
                   f"WHERE instrument IN ({self._fmt_in(part)})")
            if start_date or end_date:
                cond = []
                if start_date:
                    cond.append(f"date >= DATE '{start_date}'")
                if end_date:
                    cond.append(f"date <= DATE '{end_date}'")
                sql += ' AND ' + ' AND '.join(cond)
            dfs.append(self._run(sql))
        df = pd.concat(dfs, ignore_index=True) if len(dfs) > 1 else dfs[0]
        df = self._norm_date(df)
        return df.sort_values(['date', 'instrument']).reset_index(drop=True)

    # ── 基金分红事件（本地无 cn_fund_dividend → 由 adjust_factor 反推） ──
    def get_fund_dividend_events(self, instruments: List[str],
                                 start_date: str = '', end_date: str = '') -> pd.DataFrame:
        """场内基金份额折算事件（adjust_factor 份额折算口径）。

        本地没有独立的基金分红表，用 cn_fund_real_bar1d 自带的 adjust_factor 还原：
        除息日 adjust_factor 相对上一交易日跳变 → 现金红利(每股) = 上一交易日收盘
        − 除息日 pre_close（除权参考价）。引擎按除息日 pre_close 折算整份份额、
        零头现金入账，总成本不变。

        Returns: date(除息日), instrument, cash_before_tax(每份派现, 元), is_fund_split=1
        """
        if not instruments:
            return pd.DataFrame()
        rows = []
        for part in self._chunks([str(i) for i in instruments]):
            cond = f"instrument IN ({self._fmt_in(part)})"
            if start_date or end_date:
                c2 = []
                if start_date:
                    c2.append(f"date >= DATE '{start_date}'")
                if end_date:
                    c2.append(f"date <= DATE '{end_date}'")
                cond += ' AND ' + ' AND '.join(c2)
            sql = f"""
                SELECT date, instrument, pre_close, close,
                       LAG(close) OVER (PARTITION BY instrument ORDER BY date)       AS prev_close,
                       LAG(adjust_factor) OVER (PARTITION BY instrument ORDER BY date) AS prev_af,
                       adjust_factor
                FROM {self.price_table}
                WHERE {cond} ORDER BY instrument, date
            """
            rows.append(self._run(sql))
        d = pd.concat(rows, ignore_index=True) if len(rows) > 1 else rows[0]
        d = self._norm_date(d)
        if d.empty:
            return pd.DataFrame()
        chg = d[d['adjust_factor'].notna() & d['prev_af'].notna() &
                (d['adjust_factor'] - d['prev_af']).abs() > 1e-9].copy()
        if chg.empty:
            return pd.DataFrame()
        chg['cash_before_tax'] = (chg['prev_close'] - chg['pre_close']).round(4)
        chg = chg[chg['cash_before_tax'] > 0]
        chg['is_fund_split'] = 1
        return chg[['date', 'instrument', 'cash_before_tax', 'is_fund_split']].sort_values('date')

    # ── 证券名称 / 指数基准（绩效报告展示用） ──────────────────
    def get_instrument_names(self, instruments: List[str],
                             basic_table: str = 'cn_stock_basic_info') -> Dict[str, str]:
        """instrument -> 简称。本地名称表按日期快照：取每个代码最新一条。

        basic_table 仅为兼容参数；本地自动按 cn_stock_instruments / cn_fund_instruments
        （由 price_table 前缀决定）读取。
        """
        if not instruments:
            return {}
        name_tbl = ('cn_fund_instruments' if self.price_table.startswith('cn_fund')
                    else 'cn_stock_instruments')
        out: Dict[str, str] = {}
        for part in self._chunks([str(i) for i in instruments]):
            sql = f"""
                SELECT instrument, name FROM (
                    SELECT instrument, name,
                           ROW_NUMBER() OVER (PARTITION BY instrument ORDER BY date DESC) AS rn
                    FROM {name_tbl} WHERE instrument IN ({self._fmt_in(part)})
                ) WHERE rn = 1
            """
            df = self._run(sql)
            if not df.empty:
                out.update({str(r['instrument']): str(r['name']) for _, r in df.iterrows()})
        return out

    # ── 当日回转(T+0) 标的（本地无 day_trading 标志 → 空集） ─────────
    def get_t0_instruments(self, instruments: List[str],
                           basic_table: str = 'cn_fund_basic_info') -> set:
        """本地静态表无 day_trading 字段：返回空集（回退全 T+1，不阻断回测）。

        多日调仓策略不日内回转，T+1 冻结与否不影响成交结果。
        """
        return set()

    def get_index_bar_data(self, index: str = '000300.SH',
                           start_date: str = '', end_date: str = '') -> pd.DataFrame:
        """指数日行情（cn_stock_index_bar1d）→ index=DatetimeIndex 的 DataFrame。"""
        cols = [c for c in _BAR_COLUMNS if c in
                ('open', 'high', 'low', 'close', 'volume', 'pre_close', 'amount')]
        sql = (f"SELECT date, {', '.join(cols)} FROM cn_stock_index_bar1d "
               f"WHERE instrument = '{index}'")
        cond = []
        if start_date:
            cond.append(f"date >= DATE '{start_date}'")
        if end_date:
            cond.append(f"date <= DATE '{end_date}'")
        if cond:
            sql += ' AND ' + ' AND '.join(cond)
        raw = self._run(sql + ' ORDER BY date')
        if raw.empty:
            raise ValueError(f'指数 {index} 在 {start_date}~{end_date} 无行情')
        raw = self._norm_date(raw)
        df = raw.set_index('date')
        return df.astype({c: np.float64 for c in df.select_dtypes(
            include=[np.number]).columns})

    # ── 基金/股票回测一站式取数（smalltrader.run 直接消费） ──────
    def get_fund_backtest_data(self, instruments: Optional[List[str]] = None,
                               start_date: str = '', end_date: str = '',
                               warmup_days: int = 365,
                               basic_table: str = 'cn_fund_basic_info',
                               benchmark: str = '000300.SH') -> Dict[str, object]:
        if not start_date or not end_date:
            raise ValueError('get_fund_backtest_data 需 start_date/end_date（与 run 同值）')
        qs = (pd.Timestamp(start_date) - pd.Timedelta(days=warmup_days)).strftime('%Y-%m-%d')
        bar_data = self.get_bar_data(instruments, qs, end_date)
        if benchmark:
            bar_data[benchmark] = self.get_index_bar_data(benchmark, qs, end_date)
        insts = sorted(str(k) for k in bar_data.keys() if str(k) != benchmark)
        return {
            'data': bar_data,
            'instruments': insts,
            'dividend_events': self.get_fund_dividend_events(insts, qs, end_date),
            'dividend_tax_rate': 0.0,   # 基金分红免税
            'instrument_names': self.get_instrument_names(insts, basic_table),
            'instrument_t0': self.get_t0_instruments(insts, basic_table),
        }

    def get_stock_backtest_data(self, instruments: Optional[List[str]] = None,
                                start_date: str = '', end_date: str = '',
                                warmup_days: int = 0,
                                basic_table: str = 'cn_stock_basic_info',
                                benchmark: str = '000300.SH') -> Dict[str, object]:
        if not start_date or not end_date:
            raise ValueError('get_stock_backtest_data 需 start_date/end_date（与 run 同值）')
        qs = start_date
        if warmup_days:
            qs = (pd.Timestamp(start_date) - pd.Timedelta(days=warmup_days)).strftime('%Y-%m-%d')
        bar_data = self.get_bar_data(instruments, qs, end_date)
        if benchmark:
            bar_data[benchmark] = self.get_index_bar_data(benchmark, qs, end_date)
        insts = sorted(str(k) for k in bar_data.keys() if str(k) != benchmark)
        return {
            'data': bar_data,
            'instruments': insts,
            'dividend_events': self.get_dividend_events(insts, qs, end_date),
            'instrument_names': self.get_instrument_names(insts, basic_table),
        }


def split_fund_stock(instruments: List[str]):
    """按代码前缀把标的分为场内基金 / 股票。

    Returns (funds, stocks)：funds=SH 50/51/52/56/58、SZ 15/16/18 开头；
    其余按股票处理。混合池（股票+ETF）自动分流取不同行情表。
    """
    funds, stocks = [], []
    for s in instruments:
        code, _, ex = str(s).partition('.')
        if (ex == 'SH' and code[:2] in ('50', '51', '52', '56', '58')) or \
           (ex == 'SZ' and code[:2] in ('15', '16', '18')):
            funds.append(str(s))
        else:
            stocks.append(str(s))
    return funds, stocks


def _auto_fetch_backtest_data(market, instruments, start_date, end_date,
                              benchmark, warmup_days):
    """bigtrader.run 的自动取数实现（本地取数）。

    引擎与数据源解耦、行情须显式传入；本函数被注册为 smalltrader._AUTO_LOADER
    （import data_source 即注册），当 run(data=...) 缺省时按以下规则取数并组装：

    - instruments 给出 → 前缀分流：基金走 cn_fund_real_bar1d（份额折算分红），
      股票走 cn_stock_real_bar1d（现金红利/送转）→ 支持股票+基金混合买卖池；
    - instruments 缺省 → 按 market 取对应市场全量（cn_fund → 基金市场全量；
      cn_stock → A 股全市场）；
    - warmup_days=None 自适应：池内含基金/基金市场 → 365 自然日（覆盖 initialize
      因子预热 add_trading_days(-200)），纯股票 → 0；显式传值则用之；
    - 事件表混合列对齐（is_fund_split=1 基金行走份额折算，引擎逐行判定）；
    - 返回 run 数据类 dict：data/instruments/dividend_events/instrument_names/
      instrument_t0。

    """
    _bx = _engine_module()
    market_v = getattr(market, 'value', None) or str(market)
    if not start_date or not end_date:
        raise ValueError('run 需 start_date/end_date（回测区间）')

    funds, stocks = split_fund_stock(instruments or [])
    has_fund = bool(funds) or (market_v == 'cn_fund' and not instruments)
    warmup = 365 if warmup_days is None and has_fund else (warmup_days or 0)
    qs = (pd.Timestamp(start_date) - pd.Timedelta(days=warmup)).strftime('%Y-%m-%d')

    ds_stock = LocalDuckDataSource()   # cn_stock_real_bar1d
    ds_fund = LocalDuckDataSource(price_table='cn_fund_real_bar1d')  # 基金（分红按份额折算）

    data: Dict[str, pd.DataFrame] = {}
    if stocks or (not instruments and market_v != 'cn_fund'):
        data.update(ds_stock.get_bar_data(stocks or None, qs, end_date))
    if funds or (not instruments and market_v == 'cn_fund'):
        data.update(ds_fund.get_bar_data(funds or None, qs, end_date))

    if benchmark:
        data[benchmark] = ds_stock.get_index_bar_data(benchmark, qs, end_date)
    # 附属数据以「实际拉到的行情键」为准再分流（全市场模式下 funds/stocks
    # 输入为空，但行情键已含全部标的）
    insts = [str(k) for k in data.keys() if str(k) != benchmark]
    funds_all, stocks_all = split_fund_stock(insts)
    if not instruments:
        instruments = sorted(insts)

    # 事件：股票现金/送转 + 基金份额折算（is_fund_split=1），列对齐后合并
    frames = []
    if stocks_all:
        ev = ds_stock.get_dividend_events(stocks_all, qs, end_date)
        if not ev.empty:
            ev = ev.rename(columns={'ex_date': 'date'})
            frames.append(ev)
    if funds_all:
        ev = ds_fund.get_fund_dividend_events(funds_all, qs, end_date)
        if not ev.empty:
            frames.append(ev)
    dividend_events = pd.concat(frames, ignore_index=True, sort=False) if frames \
        else pd.DataFrame()

    names: Dict[str, str] = {}
    if stocks_all:
        names.update(ds_stock.get_instrument_names(stocks_all, 'cn_stock_basic_info'))
    if funds_all:
        names.update(ds_fund.get_instrument_names(funds_all, 'cn_fund_basic_info'))
    t0 = set()
    if funds_all:
        t0 = ds_fund.get_t0_instruments(funds_all, 'cn_fund_basic_info')

    return {
        'data': data,
        'instruments': sorted(instruments),
        'dividend_events': dividend_events,
        'instrument_names': names,
        'instrument_t0': t0,
    }


# ── 注册为 bigtrader.run 的自动数据源 ──────────────────────────
# import data_source 即生效：之后 bigtrader.run(data=...) 缺省时按 market/
# instruments 自动取数（股票/基金/混合自适应，行情全部来自本地 DuckDB）。
try:
    _bx = _engine_module()
    _bx._AUTO_LOADER = _auto_fetch_backtest_data
except Exception:          # 引擎未安装等情形不阻断数据源独立使用
    pass


def run_fund_backtest(**kwargs):
    """（便捷别名）场内基金一站式回测：等价于 bigtrader.run(market=CN_FUND, ...)。

    自动取数由 data_source 注册到 bigtrader.run 后生效（见模块说明）；
    本函数仅为语义化别名，建议直接写 bigtrader.run。
    """
    _bx = _engine_module()
    kwargs.setdefault('market', _bx.Market.CN_FUND)
    return _bx.run(**kwargs)


def run_stock_backtest(**kwargs):
    """（便捷别名）A 股一站式回测：等价于 bigtrader.run(market=CN_STOCK, ...)。

    自动取数由 data_source 注册到 bigtrader.run 后生效（见模块说明）；
    本函数仅为语义化别名，建议直接写 bigtrader.run。
    """
    _bx = _engine_module()
    kwargs.setdefault('market', _bx.Market.CN_STOCK)
    return _bx.run(**kwargs)
