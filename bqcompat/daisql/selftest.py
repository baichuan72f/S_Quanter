"""daiSQL 映射自检：把每个映射函数与 pandas / TA-Lib 基准逐项对数。

用法：
  .venv\\Scripts\\python.exe -m bqcompat.daisql.selftest          # 跑全部对拍
  .venv\\Scripts\\python.exe -m bqcompat.daisql.selftest --list   # 只列支持的函数

为什么要有这个：把"看起来对"换成"数值对得上"。每个用例都写明**参照口径**
（例如 c_pct_rank 用 SQL 的 percent_rank =（rank-1)/(n-1)，不是 pandas 的 rank/n），
免得基准本身就错。
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

try:
    import duckdb
except Exception:  # noqa: BLE001
    duckdb = None

try:
    import talib
except Exception:  # noqa: BLE001
    talib = None

from . import translate
from .runtime import ensure_functions

TOL = 1e-9
TOL_TA = 1e-6


def build_panel(n_days: int = 90, n_inst: int = 3, seed: int = 7) -> pd.DataFrame:
    """确定性的合成面板（够长，能算 MACD/KDJ）。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range('2026-01-01', periods=n_days)
    rows = []
    for k in range(n_inst):
        base = 10.0 + 3 * k
        drift = rng.normal(0, 0.004, n_days).cumsum()
        close = base * np.exp(drift) * (1 + 0.05 * np.sin(np.arange(n_days) / 7 + k))
        high = close * (1 + np.abs(rng.normal(0, 0.01, n_days)))
        low = close * (1 - np.abs(rng.normal(0, 0.01, n_days)))
        open_ = close * (1 + rng.normal(0, 0.005, n_days))
        vol = np.abs(rng.normal(1_000_000, 200_000, n_days))
        for i, d in enumerate(dates):
            rows.append(dict(date=d, instrument=f'CODE{k}.XX', open=float(open_[i]),
                             high=float(high[i]), low=float(low[i]),
                             close=float(close[i]), volume=float(vol[i])))
    return pd.DataFrame(rows).sort_values(['instrument', 'date']).reset_index(drop=True)


def _pct_rank_window(w: np.ndarray) -> float:
    cur = w[-1]
    return float((np.sum(w < cur) + 0.5 * np.sum(w == cur)) / len(w))


def reference(df: pd.DataFrame, name: str, args: str) -> pd.Series | None:
    """参照实现：返回与面板同序的期望值。"""
    g = df.groupby('instrument', sort=False)

    def grp(col):
        return g[col]

    table = {
        'm_lag(close,1)': lambda: grp('close').shift(1),
        'm_lead(close,1)': lambda: grp('close').shift(-1),
        'm_delta(close,3)': lambda: df['close'] - grp('close').shift(3),
        'm_returns(close,1)': lambda: df['close'] / grp('close').shift(1) - 1,
        'm_avg(close,5)': lambda: grp('close').transform(lambda s: s.rolling(5).mean()),
        'm_sum(close,5)': lambda: grp('close').transform(lambda s: s.rolling(5).sum()),
        'm_stddev(close,5)': lambda: grp('close').transform(lambda s: s.rolling(5).std(ddof=1)),
        'm_max(close,5)': lambda: grp('close').transform(lambda s: s.rolling(5).max()),
        'm_min(close,5)': lambda: grp('close').transform(lambda s: s.rolling(5).min()),
        'm_median(close,5)': lambda: grp('close').transform(lambda s: s.rolling(5).median()),
        'm_rolling_rank(close,5)': lambda: grp('close').transform(
            lambda s: s.rolling(5).apply(_pct_rank_window, raw=True)),
        'm_corr(close,volume,20)': lambda: pd.concat(
            [sub['close'].rolling(20).corr(sub['volume'])
             for _i, sub in df.groupby('instrument', sort=False)]).sort_index(),
        'm_decay_linear(close,5)': lambda: grp('close').transform(
            lambda s: s.rolling(5).apply(
                lambda w: float(np.dot(w, np.arange(1, len(w) + 1)) / np.arange(1, len(w) + 1).sum()),
                raw=True)),
        # 截面：口径见文件头说明
        'c_pct_rank(close)': lambda: df.groupby('date')['close'].transform(
            lambda s: (s.rank(method='min') - 1) / (len(s) - 1) if len(s) > 1 else np.nan),
        'c_rank(close)': lambda: df.groupby('date')['close'].transform(
            lambda s: s.rank(method='min')),
        'c_avg(close)': lambda: df.groupby('date')['close'].transform('mean'),
        'c_std(close)': lambda: df.groupby('date')['close'].transform(lambda s: s.std(ddof=1)),
        'c_zscore(close)': lambda: df.groupby('date')['close'].transform(
            lambda s: (s - s.mean()) / s.std(ddof=1)),
        'c_normalize(close)': lambda: df.groupby('date')['close'].transform(
            lambda s: (s - s.min()) / (s.max() - s.min())),
        'c_median(close)': lambda: df.groupby('date')['close'].transform('median'),
        'c_quantile_cont(close,0.25)': lambda: df.groupby('date')['close'].transform(
            lambda s: np.quantile(s, 0.25, method='linear')),
    }

    if name in table:
        return table[name]()

    if talib is None:
        return None

    def per_group(fn):
        out = []
        for _inst, sub in df.groupby('instrument', sort=False):
            out.append(pd.Series(fn(sub), index=sub.index))
        return pd.concat(out).sort_index()

    ta_table = {
        'm_ta_ema(close,10)': lambda s: talib.EMA(s['close'].to_numpy(), 10),
        'm_ta_rsi(close,14)': lambda s: talib.RSI(s['close'].to_numpy(), 14),
        'm_ta_macd_dif(close, fastperiod:=12, slowperiod:=26, signalperiod:=9)':
            lambda s: talib.MACD(s['close'].to_numpy(), 12, 26, 9)[0],
        'm_ta_macd_dea(close, fastperiod:=12, slowperiod:=26, signalperiod:=9)':
            lambda s: talib.MACD(s['close'].to_numpy(), 12, 26, 9)[1],
        'm_ta_kdj_k(high, low, close, fastk_period:=9, slowk_period:=3, slowd_period:=3)':
            lambda s: talib.STOCH(s['high'].to_numpy(), s['low'].to_numpy(),
                                  s['close'].to_numpy(), 9, 3, 0, 3, 0)[0],
        'm_ta_kdj_d(high, low, close, fastk_period:=9, slowk_period:=3, slowd_period:=3)':
            lambda s: talib.STOCH(s['high'].to_numpy(), s['low'].to_numpy(),
                                  s['close'].to_numpy(), 9, 3, 0, 3, 0)[1],
        'm_ta_kdj_j(high, low, close, fastk_period:=9, slowk_period:=3, slowd_period:=3)':
            lambda s: (lambda k, d: 3 * k - 2 * d)(
                *talib.STOCH(s['high'].to_numpy(), s['low'].to_numpy(),
                             s['close'].to_numpy(), 9, 3, 0, 3, 0)),
    }
    if name in ta_table:
        return per_group(ta_table[name])
    return None


CASES = [
    'm_lag(close,1)', 'm_lead(close,1)', 'm_delta(close,3)', 'm_returns(close,1)',
    'm_avg(close,5)', 'm_sum(close,5)', 'm_stddev(close,5)', 'm_max(close,5)',
    'm_min(close,5)', 'm_median(close,5)', 'm_rolling_rank(close,5)',
    'm_corr(close,volume,20)', 'm_decay_linear(close,5)',
    'c_pct_rank(close)', 'c_rank(close)', 'c_avg(close)', 'c_std(close)',
    'c_zscore(close)', 'c_normalize(close)', 'c_median(close)',
    'c_quantile_cont(close,0.25)',
    'm_ta_ema(close,10)', 'm_ta_rsi(close,14)',
    'm_ta_macd_dif(close, fastperiod:=12, slowperiod:=26, signalperiod:=9)',
    'm_ta_macd_dea(close, fastperiod:=12, slowperiod:=26, signalperiod:=9)',
    'm_ta_kdj_k(high, low, close, fastk_period:=9, slowk_period:=3, slowd_period:=3)',
    'm_ta_kdj_d(high, low, close, fastk_period:=9, slowk_period:=3, slowd_period:=3)',
    'm_ta_kdj_j(high, low, close, fastk_period:=9, slowk_period:=3, slowd_period:=3)',
]


def main() -> int:
    if '--list' in sys.argv:
        from .registry import describe
        print(describe())
        return 0
    if duckdb is None:
        print('缺少 duckdb，无法自检')
        return 2

    df = build_panel()
    con = duckdb.connect()
    con.register('panel', df)
    ensure_functions(con)

    ok = fail = skip = 0
    print(f'{"用例":58s} {"最大偏差":>12s}  结论')
    for expr in CASES:
        try:
            sql, _used = translate(
                f'select date, instrument, {expr} as v from panel'
                ' order by instrument, date')
            got = con.execute(sql).df()['v']
        except Exception as exc:  # noqa: BLE001
            print(f'{expr:58s} {"-":>12s}  ERROR {type(exc).__name__}: {exc}')
            fail += 1
            continue

        want = reference(df, expr, '')
        if want is None:
            print(f'{expr:58s} {"-":>12s}  SKIP（无参照实现）')
            skip += 1
            continue

        a = pd.Series(np.asarray(got, dtype='float64'))
        b = pd.Series(np.asarray(want, dtype='float64')).reset_index(drop=True)
        both = a.notna() & b.notna()
        if not both.any():
            same_null = (a.isna() == b.isna()).all()
            print(f'{expr:58s} {"-":>12s}  {"OK(全空且一致)" if same_null else "MISMATCH(空值不一致)"}')
            (ok := ok + 1) if same_null else (fail := fail + 1)
            continue
        diff = float((a[both] - b[both]).abs().max())
        tol = TOL_TA if expr.startswith('m_ta_') else TOL
        null_mismatch = int((a.isna() != b.isna()).sum())
        verdict = 'OK' if diff <= tol and null_mismatch == 0 else 'MISMATCH'
        extra = '' if null_mismatch == 0 else f'  空值不一致 {null_mismatch} 处'
        print(f'{expr:58s} {diff:12.3e}  {verdict}{extra}')
        if verdict == 'OK':
            ok += 1
        else:
            fail += 1

    print(f'\n结果：OK={ok} MISMATCH/ERROR={fail} SKIP={skip}'
          f'（talib={"有" if talib is not None else "无，走 numpy 兜底"}）')
    return 0 if fail == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
