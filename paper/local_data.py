"""
本地数据源 —— 模拟盘的行情/事件/日历取数层
==========================================

为什么不用 dai
--------------
模拟盘要在本地（无 bigquant 账号、无 AIStudio 环境、Windows 也能跑）可跑、可分享。
数据链路：DAI 日更 → localbq 导出脚本拉到本地 parquet → `queries.py`（DuckDB 视图）
→ `engine.data_source.LocalDuckDataSource` → 本模块（适配层）。

本模块是**薄适配层**：数据访问全部交给从自写回测工具 pro 移植过来的本地 DuckDB
数据源（`engine/data_source.py`，视图层 `queries.py`），这里只补三件回测侧不需要的
东西：
  1. 代码风格映射：策略用平台风格（`000001.SZA`/`600000.SHA`），本地表用
     `.SZ`/`.SH`；出参键名还原成调用方传入的风格（见 local_code/platform_style）；
  2. 交易日历：`trading_days` 表（market_code='CN'）→ get_trading_days /
     next_trading_day；
  3. 跑批辅助：latest_bar_date（数据到位性）、_read_parts（checks.py 的日粒度计数）。

数据位置
--------
parquet 快照默认放在**项目上一级**的 `data/`（= `D:\\Data\\bq\\data`，与自写回测
工具 pro 共用同一份快照），可用环境变量 `LOCALBQ_DATA_DIR` 或构造参数 `data_dir`
覆盖。表布局见 `queries.TABLES`。

数据分区（相对数据根目录）
  cn_stock_real_bar1d/YYYYMM.parquet  股票日线（真实价），月分区
  cn_fund_real_bar1d/YYYY.parquet     场内基金日线（真实价），年分区
  cn_stock_dividend/YYYY.parquet      股票除权除息事件，年分区
  cn_stock_index_bar1d/YYYY.parquet   指数日线（基准），年分区
  trading_days/YYYY.parquet           交易日历（market_code='CN'），年分区

相对旧版 parquet 直读实现的差异
  - 基金分红/份额折算：旧版未导出 `cn_fund_dividend` 只能 WARN；现由基金行情的
    `adjust_factor` 跳变反推（`LocalDuckDataSource.get_fund_dividend_events`），
    与回测引擎同一口径，持仓基金遇分红日不再有系统性偏差。
  - 基金 day_trading（T+0）标志本地仍无 → 跨境/商品 ETF 按 T+1 处理。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

import queries
from engine.data_source import LocalDuckDataSource, split_fund_stock

# 引擎行情契约列（与 engine.data_source._BAR_COLUMNS 对齐）
_BAR_COLUMNS = ['open', 'high', 'low', 'close', 'volume',
                'pre_close', 'upper_limit', 'lower_limit', 'amount',
                'adjust_factor', 'name']

# 除权除息事件出参列（引擎按 ex_date 分组）
_DIVIDEND_COLUMNS = ['ex_date', 'instrument', 'cash_before_tax',
                     'bonus_rate', 'conversed_rate', 'register_date']


def _to_ts(d) -> pd.Timestamp:
    return pd.Timestamp(d).normalize()


# 平台风格后缀(.SZA/.SHA) ↔ 本地风格(.SZ/.SH)
_SUFFIX_TO_LOCAL = {'SZA': 'SZ', 'SHE': 'SZ', 'SHA': 'SH', 'SSE': 'SH'}

# 「全市场」预设 → 本地行情表（模拟盘按此池取数与校验）
POOL_PRESETS = {
    '全市场股票': 'cn_stock_real_bar1d',
    '全市场基金': 'cn_fund_real_bar1d',
    'all_stock': 'cn_stock_real_bar1d',
    'all_fund': 'cn_fund_real_bar1d',
}


def pool_preset(value) -> Optional[str]:
    """值是「全市场」预设时返回对应行情表名，否则 None。"""
    return POOL_PRESETS.get(str(value or '').strip())


def local_code(instrument: str) -> str:
    """'000001.SZA' → '000001.SZ'（本地 parquet 表的代码风格）。"""
    code, _, ex = str(instrument).partition('.')
    return f'{code}.{_SUFFIX_TO_LOCAL.get(ex, ex)}'


def platform_style(instrument: str, original: str) -> str:
    """把本地代码还原回调用方传入的原始风格（键名以调用方为准）。"""
    code, _, ex = str(instrument).partition('.')
    _, _, ex0 = str(original).partition('.')
    if ex0 in _SUFFIX_TO_LOCAL and ex0 != ex:
        return f'{code}.{ex0}'
    return str(instrument)


class LocalParquetDataSource:
    """模拟盘数据源：本地 DuckDB（parquet 快照）之上的适配层。

    数据访问委托给 `engine.data_source.LocalDuckDataSource`（股票/基金各一实例，
    行情表按代码前缀分流），本类负责代码风格映射与交易日历/跑批辅助。
    只依赖 pandas + duckdb，无 bigquant 依赖。
    """

    def __init__(self, data_dir: str = None):
        self.data_dir = Path(data_dir) if data_dir else queries.DATA_ROOT
        self._stock = LocalDuckDataSource(data_root=self.data_dir)
        self._fund = LocalDuckDataSource(price_table='cn_fund_real_bar1d',
                                         data_root=self.data_dir)
        self._con = queries.connect(self.data_dir)
        self._views: Dict[str, bool] = {}
        self._pool_cache: Dict[tuple, List[str]] = {}

    # ── 标的池 ─────────────────────────────────────────────────
    def expand_pool(self, instruments, lookback_days: int = 30) -> List[str]:
        """展开「全市场股票 / 全市场基金」预设；普通代码原样返回。

        预设取最近 `lookback_days` 内有行情的标的（避免把退市代码拉进候选池），
        结果按 (预设, 天数) 缓存 —— 同一次跑批内多次取数不重复查库。
        """
        out: List[str] = []
        for s in instruments or []:
            table = pool_preset(s)
            if not table:
                out.append(str(s))
                continue
            key = (table, int(lookback_days))
            if key not in self._pool_cache:
                if not self._has_table(table):
                    raise ValueError(f'预设「{s}」对应的行情表 {table} 本地不存在')
                df = self._con.execute(
                    f'SELECT instrument FROM {table} WHERE date >= '
                    f'(SELECT max(date) FROM {table}) - INTERVAL {int(lookback_days)} DAY '
                    f'GROUP BY instrument ORDER BY instrument').df()
                self._pool_cache[key] = [str(x) for x in df['instrument'].tolist()]
            out.extend(self._pool_cache[key])
        # 去重保序（预设与显式代码可能重叠）
        return list(dict.fromkeys(out))

    # ── 代码风格工具 ───────────────────────────────────────────
    @classmethod
    def _split(cls, instruments: List[str]):
        """(local2orig, funds, stocks)：本地代码 → 调用方原始代码；按市场分流。"""
        local2orig: Dict[str, str] = {}
        for s in instruments or []:
            local2orig[local_code(s)] = str(s)
        funds, stocks = split_fund_stock(list(local2orig))
        return local2orig, funds, stocks

    def _has_table(self, table: str) -> bool:
        """该表在本地数据中是否存在（视图由 queries.connect 按文件存在性建）。"""
        if table not in self._views:
            df = self._con.execute(
                'SELECT 1 FROM duckdb_views() WHERE view_name = ?', [table]).df()
            self._views[table] = not df.empty
        return self._views[table]

    # ── 行情 ───────────────────────────────────────────────────
    def get_bar_data(self, instruments: List[str],
                     start_date: str, end_date: str) -> Dict[str, pd.DataFrame]:
        """引擎行情契约：dict[symbol, DataFrame]，DatetimeIndex + _BAR_COLUMNS。

        标的代码接受平台风格（.SZA/.SHA）或本地风格（.SZ/.SH），返回 dict 的
        键与调用方传入的风格保持一致。
        """
        if not instruments:
            return {}
        local2orig, funds, stocks = self._split(self.expand_pool(instruments))
        out: Dict[str, pd.DataFrame] = {}
        if stocks:
            out.update(self._stock.get_bar_data(stocks, start_date, end_date))
        if funds:
            out.update(self._fund.get_bar_data(funds, start_date, end_date))
        return {local2orig.get(k, k): v for k, v in out.items()}

    def get_index_bar_data(self, index: str = '000300.SH',
                           start_date: str = '', end_date: str = '') -> pd.DataFrame:
        """指数日线（基准用），输出契约同 get_bar_data 单标的；无数据返回空表。"""
        try:
            return self._stock.get_index_bar_data(
                local_code(index), start_date, end_date)
        except ValueError:
            # 数据未到位（指数行情尚未更新）→ 返回空表，由调用方判定中止
            return pd.DataFrame(columns=['close'])

    # ── 除权除息事件 ───────────────────────────────────────────
    def get_dividend_events(self, instruments: List[str],
                            start_date: str, end_date: str) -> pd.DataFrame:
        """除权除息事件（ex_date/instrument/...，instrument 还原为调用方风格）。

        股票：本地 cn_stock_dividend（现金红利 + 送/转股）；
        基金：由基金行情 adjust_factor 跳变反推的份额折算事件（is_fund_split=1），
              引擎按除息日参考价折算份额（与回测同一口径）。
        """
        if not instruments:
            return pd.DataFrame(columns=_DIVIDEND_COLUMNS)
        local2orig, funds, stocks = self._split(self.expand_pool(instruments))
        frames = []
        if stocks:
            ev = self._stock.get_dividend_events(stocks, start_date, end_date)
            if not ev.empty:
                frames.append(ev)
        if funds:
            ev = self._fund.get_fund_dividend_events(funds, start_date, end_date)
            if not ev.empty:
                frames.append(ev)
        if not frames:
            return pd.DataFrame(columns=_DIVIDEND_COLUMNS)
        df = pd.concat(frames, ignore_index=True, sort=False)
        # 引擎契约：除权处理日列名 ex_date（数据源口径列名为 date）
        if 'date' in df.columns:
            df = df.rename(columns={'date': 'ex_date'})
        df['ex_date'] = pd.to_datetime(df['ex_date'])
        df['instrument'] = df['instrument'].map(
            lambda x: local2orig.get(str(x), str(x)))
        keep = [c for c in _DIVIDEND_COLUMNS + ['is_fund_split'] if c in df.columns]
        return df[keep].sort_values('ex_date').reset_index(drop=True)

    # ── 名称 / T+0 ─────────────────────────────────────────────
    def get_instrument_names(self, instruments: List[str]) -> Dict[str, str]:
        """证券简称：本地名称表（cn_stock_instruments / cn_fund_instruments）。

        名称缺失不阻塞（引擎允许空名，报告退化为显示代码）。"""
        if not instruments:
            return {}
        local2orig, funds, stocks = self._split(self.expand_pool(instruments))
        out: Dict[str, str] = {}
        if stocks:
            out.update(self._stock.get_instrument_names(stocks))
        if funds:
            out.update(self._fund.get_instrument_names(funds))
        return {local2orig.get(k, k): v for k, v in out.items()}

    # ── 交易日历 ───────────────────────────────────────────────
    def get_trading_days(self, start_date: str, end_date: str,
                         market_code: str = 'CN') -> List[str]:
        """A 股交易日列表（'YYYY-MM-DD' 升序），来自本地 trading_days 表。"""
        df = self._con.execute(
            "SELECT date FROM trading_days WHERE market_code = ? "
            "AND date >= CAST(? AS DATE) AND date <= CAST(? AS DATE) ORDER BY date",
            [market_code, str(start_date), str(end_date)]).df()
        if df.empty:
            return []
        return [d.strftime('%Y-%m-%d') for d in pd.to_datetime(df['date'])]

    def next_trading_day(self, after: str, market_code: str = 'CN') -> Optional[str]:
        """after（不含）之后第一个交易日；日历未覆盖时返回 None。

        ⚠ 严格大于 after：get_trading_days 是闭区间含端点，若直接取 [after, ∞)
        的第一条会返回 after 自身 —— 曾导致计划单被写到信号日当天名下、
        次日撮合回写 0 行命中而静默失败（pending 只增不减、每天重复撮合）。
        """
        df = self._con.execute(
            "SELECT date FROM trading_days WHERE market_code = ? "
            "AND date > CAST(? AS DATE) ORDER BY date LIMIT 1",
            [market_code, str(after)]).df()
        if df.empty:
            return None
        return pd.to_datetime(df['date'].iloc[0]).strftime('%Y-%m-%d')

    # ── 跑批辅助 ───────────────────────────────────────────────
    def latest_bar_date(self, instruments: List[str],
                        benchmark: Optional[str] = None) -> Optional[str]:
        """池内标的最新的行情日期（数据到位性检查用）。

        benchmark 给定时取「池最新日」与「基准最新日」的较小值 —— 单日跑批
        同时需要行情与基准，基准滞后时按基准对齐（否则跑批会因基准无行情中止）。
        """
        _, funds, stocks = self._split(self.expand_pool(instruments))
        latest: Optional[pd.Timestamp] = None
        for table, insts in (('cn_stock_real_bar1d', stocks),
                             ('cn_fund_real_bar1d', funds)):
            if not insts or not self._has_table(table):
                continue
            df = self._con.execute(
                f"SELECT max(date) AS d FROM {table} "
                f"WHERE instrument IN ({', '.join('?' * len(insts))})",
                [str(i) for i in insts]).df()
            if not df.empty and pd.notna(df['d'].iloc[0]):
                d = pd.Timestamp(df['d'].iloc[0])
                latest = d if latest is None or d > latest else latest
        if latest is None:
            return None
        if benchmark and self._has_table('cn_stock_index_bar1d'):
            df = self._con.execute(
                "SELECT max(date) AS d FROM cn_stock_index_bar1d WHERE instrument = ?",
                [local_code(benchmark)]).df()
            if not df.empty and pd.notna(df['d'].iloc[0]):
                bm = pd.Timestamp(df['d'].iloc[0])
                latest = min(latest, bm)
        return latest.strftime('%Y-%m-%d')

    def _read_parts(self, table: str, start, end,
                    instrument_col: str = 'instrument',
                    instruments: Optional[List[str]] = None) -> pd.DataFrame:
        """读 [start, end] 闭区间的行（checks.py 的日粒度计数/缺失检查用）。

        注意：`instruments` 按**本地代码风格**过滤（调用方用 local_code() 转换），
        返回的 instrument 列也是本地风格 —— 与 checks.py 的比较口径一致。
        """
        if not self._has_table(table):
            return pd.DataFrame()
        sql = (f"SELECT * FROM {table} WHERE date >= CAST(? AS DATE) "
               f"AND date <= CAST(? AS DATE)")
        params: List[object] = [str(start), str(end)]
        if instruments:
            sql += f" AND {instrument_col} IN ({', '.join('?' * len(instruments))})"
            params += [str(i) for i in instruments]
        df = self._con.execute(sql, params).df()
        if not df.empty and 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
        return df
