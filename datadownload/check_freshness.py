#!/usr/bin/env python3
"""数据新鲜度检测（只读）
========================

检查本地 parquet 快照各表的最新交易日是否落后于市场日历，供跑批前置检查、
cron 任务和 agent 调用。**只读**：不写 `data/`，不改导出脚本与查询层。

判定口径
--------
- 市场日历：由当前时间推算最近一个已收盘的交易日（周一~周五、收盘 15:30 后
  才算当日；落在已知交易日历覆盖范围内的节假日会回退到前一个交易日）。
  不读任何行情表，避免「行情表互相作证」。
- `trading_days`：零容忍，必须等于市场日历算出的最近交易日（它是回看窗口的
  输入，落后一天就会让策略静默少跑）。
- 行情 / 因子表：允许落后 ≤ 1 个交易日；`cn_stock_dividend` 与
  `*_instruments` 允许落后 ≤ 5 个交易日。
- 判定基准统一用市场日历算出的最近交易日；**不拿行情表互相作证**，也不让落后
  的 `trading_days` 反过来把行情表判成新鲜（日历表自身按零容忍单独判定）。
- 每个分片额外核对 distinct(date) 与应有交易日数，抓「月中截断」。
- `cn_stock_bar1d` 与 `cn_stock_real_bar1d` 的最大日期相差 > 1 个交易日时单独告警。

用法
----
    python datadownload/check_freshness.py                 # 每表一行中文 + 汇总
    python datadownload/check_freshness.py --json          # 机器可读（给脚本/agent）
    python datadownload/check_freshness.py --strict        # 任一表过期 → 退出码 1
    python datadownload/check_freshness.py --as-of 2026-11-09T16:00
    python datadownload/check_freshness.py --data-root /path/to/data
    python datadownload/check_freshness.py --env-check     # 打印沙箱边界事实

退出码：0 正常；`--strict` 下存在过期表或分片截断时为 1。
只依赖 duckdb + 标准库（不 import bigquant / pandas）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402

import queries  # noqa: E402

# 收盘（含数据落盘缓冲）之后才算当日交易日
MARKET_CLOSE = time(15, 30)
# 显式给 --as-of 日期（不带时间）时，按当日收盘后处理
DEFAULT_AS_OF_TIME = time(16, 0)

# 每张表的判定参数：tolerance=允许落后的交易日数；dense=是否应有连续的每日行
_SPECS: Dict[str, dict] = {
    'trading_days': dict(tolerance=0, dense=True, kind='calendar'),
    'cn_stock_bar1d': dict(tolerance=1, dense=True),
    'cn_stock_real_bar1d': dict(tolerance=1, dense=True),
    'cn_stock_prefactors': dict(tolerance=1, dense=True),
    'cn_fund_bar1d': dict(tolerance=1, dense=True),
    'cn_fund_real_bar1d': dict(tolerance=1, dense=True),
    'cn_stock_index_bar1d': dict(tolerance=1, dense=True),
    'cn_stock_instruments': dict(tolerance=5, dense=True),
    'cn_fund_instruments': dict(tolerance=5, dense=True),
    # 除权除息日稀疏（且可能含未来事件日），不做分片日期连续性校验
    'cn_stock_dividend': dict(tolerance=5, dense=False),
}
_DEFAULT_SPEC = dict(tolerance=5, dense=False)

_STATUS_CN = {
    'ok': '合格',
    'stale': '过期',
    'missing': '缺失',
    'error': '读取失败',
}


# ── 日历工具（纯标准库） ──────────────────────────────────────────────
def _prev_weekday(d: date) -> date:
    """d 及之前最近的一个工作日。"""
    while d.weekday() >= 5:  # 5=周六, 6=周日
        d -= timedelta(days=1)
    return d


def market_latest_trading_day(now: datetime, calendar_dates: Set[date],
                              calendar_max: Optional[date]) -> date:
    """按市场日历推算最近一个已收盘交易日。

    - 工作日且已过收盘时间 → 当天；
    - 其余情况（盘前、周末）→ 之前最近的工作日；
    - 回退结果若落在 `trading_days` 已覆盖范围内又不在日历里（法定节假日），
      继续往前回退。`trading_days` 覆盖不到的日期无法判断节假日，按工作日处理
      （宁可报过期，也不把过期数据当新鲜）。
    """
    today = now.date()
    if today.weekday() < 5 and now.time() >= MARKET_CLOSE:
        ref = today
    else:
        ref = _prev_weekday(today - timedelta(days=1))

    if calendar_max is not None:
        while ref <= calendar_max and ref not in calendar_dates:
            ref = _prev_weekday(ref - timedelta(days=1))
    return ref


def _weekday_gap(start: date, end: date) -> int:
    """(start, end] 内的工作日数（end > start）。"""
    n = 0
    d = start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def trading_day_gap(start: date, end: date, calendar_dates: Set[date],
                    calendar_max: Optional[date]) -> int:
    """(start, end] 内的交易日数。日历覆盖内用真实日历，覆盖外按工作日估算。"""
    if end <= start:
        return 0
    n = 0
    if calendar_max is not None:
        known_end = min(end, calendar_max)
        if known_end > start:
            n = sum(1 for d in calendar_dates if start < d <= known_end)
        if end > calendar_max:
            n += _weekday_gap(max(start, calendar_max), end)
        return n
    return _weekday_gap(start, end)


# ── 数据读取 ──────────────────────────────────────────────────────────
def _read_sql(files: List[Path]) -> str:
    lst = ", ".join(f"'{f.as_posix()}'" for f in files)
    return f"read_parquet([{lst}])"


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _query_dates(con, files: List[Path], *, start: Optional[date] = None,
                 end: Optional[date] = None,
                 cn_only: bool = False) -> List[date]:
    """读取若干 parquet 的 distinct(date)，可按闭区间过滤。"""
    where = ["date IS NOT NULL"]
    if cn_only:
        where.append("market_code = 'CN'")
    if start is not None:
        where.append(f"date >= DATE '{start.isoformat()}'")
    if end is not None:
        where.append(f"date <= DATE '{end.isoformat()}'")
    rows = con.execute(
        f"SELECT DISTINCT CAST(date AS DATE) FROM {_read_sql(files)} "
        f"WHERE {' AND '.join(where)} ORDER BY 1"
    ).fetchall()
    return [_as_date(r[0]) for r in rows]


def _table_stats(con, files: List[Path], *, cn_only: bool = False
                 ) -> tuple[Optional[date], int]:
    """整表 max(date) 与行数（行数用于展示数据量）。"""
    where = "WHERE market_code = 'CN'" if cn_only else ""
    row = con.execute(
        f"SELECT MAX(CAST(date AS DATE)), COUNT(*) FROM {_read_sql(files)} {where}"
    ).fetchone()
    return _as_date(row[0]), int(row[1] or 0)


# ── 主分析 ────────────────────────────────────────────────────────────
def analyze(data_root: Optional[Path] = None,
            now: Optional[datetime] = None) -> dict:
    """分析数据根目录下各表的新鲜度，返回结构化结果（不打印、不退出）。"""
    root = Path(data_root) if data_root else Path(queries.DATA_ROOT)
    now = now or datetime.now()
    con = duckdb.connect()
    try:
        files_map: Dict[str, List[Path]] = {}
        for t in queries.TABLES:
            files_map[t['name']] = queries.parquet_files(t, root)

        # ── 交易日历（市场日历的已知部分） ──
        cal_files = files_map.get('trading_days') or []
        calendar_dates: List[date] = []
        if cal_files:
            try:
                calendar_dates = _query_dates(con, cal_files, cn_only=True)
            except Exception:
                calendar_dates = []
        calendar_max = max(calendar_dates) if calendar_dates else None
        calendar_set = set(calendar_dates)

        market_latest = market_latest_trading_day(now, calendar_set, calendar_max)
        # 判定基准统一用市场日历；落后的 `trading_days` 不能把行情表掩盖成新鲜
        data_reference = market_latest

        tables: List[dict] = []
        stale_names: List[str] = []
        truncated_names: List[str] = []
        warnings: List[str] = []

        for t in queries.TABLES:
            name = t['name']
            spec = _SPECS.get(name, _DEFAULT_SPEC)
            files = files_map[name]
            is_calendar = spec.get('kind') == 'calendar'
            ref = data_reference
            item = {
                'table': name,
                'shard': None,
                'shard_min_date': None,
                'max_date': None,
                'rows': 0,
                'reference_date': ref.isoformat(),
                'tolerance': spec['tolerance'],
                'lag_days': None,
                'ahead_days': 0,
                'status': 'missing',
                'status_cn': _STATUS_CN['missing'],
                'truncated': False,
                'missing_days': 0,
                'note': '',
            }
            if not files:
                tables.append(item)
                continue

            last = files[-1]
            item['shard'] = last.name
            try:
                max_date, rows = _table_stats(con, files, cn_only=is_calendar)
                item['max_date'] = max_date.isoformat() if max_date else None
                item['rows'] = rows
            except Exception as exc:  # 分片损坏 / schema 不一致
                item.update(status='error', status_cn=_STATUS_CN['error'],
                            note=f'读取失败: {exc}')
                tables.append(item)
                continue

            if max_date is None:
                tables.append(item)
                continue

            lag = trading_day_gap(max_date, ref, calendar_set, calendar_max)
            item['lag_days'] = lag
            if max_date > ref:
                # 领先基准不是数据问题（通常是日历表自身落后），只展示不告警
                item['ahead_days'] = trading_day_gap(ref, max_date,
                                                     calendar_set, calendar_max)
                if not spec.get('dense', False):
                    item['note'] = '含未来事件日'
            if is_calendar and lag:
                item['note'] = '按市场日历零容忍判定'
            item['status'] = 'stale' if lag > spec['tolerance'] else 'ok'
            item['status_cn'] = _STATUS_CN[item['status']]
            if item['status'] == 'stale':
                stale_names.append(name)

            # ── 分片截断：distinct(date) 是否少于应有交易日数 ──
            if spec.get('dense') and calendar_dates:
                try:
                    shard_min = min(_query_dates(con, [last]))
                except Exception:
                    shard_min = None
                if shard_min is not None:
                    item['shard_min_date'] = shard_min.isoformat()
                    cap = (min(ref, calendar_max)
                           if calendar_max is not None else ref)
                    expected = {d for d in calendar_dates if shard_min <= d <= cap}
                    if expected:
                        actual = set(_query_dates(con, [last],
                                                  start=shard_min, end=cap))
                        missing = expected - actual
                        item['missing_days'] = len(missing)
                        if missing:
                            item['truncated'] = True
                            truncated_names.append(name)
            tables.append(item)

        # ── bar1d / real_bar1d 最大日期差 > 1 个交易日 ──
        by_name = {x['table']: x for x in tables}
        bar, real = by_name.get('cn_stock_bar1d'), by_name.get('cn_stock_real_bar1d')
        if bar and real and bar['max_date'] and real['max_date']:
            d_bar = date.fromisoformat(bar['max_date'])
            d_real = date.fromisoformat(real['max_date'])
            if d_bar > d_real:
                d_bar, d_real = d_real, d_bar
            gap = trading_day_gap(d_bar, d_real, calendar_set, calendar_max)
            if gap > 1:
                warnings.append(
                    f'cn_stock_bar1d 与 cn_stock_real_bar1d 最大日期相差 '
                    f'{gap} 个交易日（>1）：{d_bar} vs {d_real}')

        # ── 交易日历落后 / 超前市场日历 ──
        if calendar_max is not None and calendar_max > market_latest:
            warnings.append(
                f'trading_days 含晚于市场日历的日期 {calendar_max}'
                f'（市场最近交易日 {market_latest}），请检查日历数据。')

        counts = {k: 0 for k in _STATUS_CN}
        for x in tables:
            counts[x['status']] += 1
        stale = bool(stale_names)
        result = {
            'as_of': now.isoformat(timespec='minutes'),
            'data_root': str(root),
            'market_latest_trading_day': market_latest.isoformat(),
            'calendar_max_date': (calendar_max.isoformat()
                                  if calendar_max else None),
            'data_reference_date': data_reference.isoformat(),
            'strict_failed': bool(stale or truncated_names),
            'summary': {
                'tables': len(tables),
                'ok': counts['ok'],
                'stale': counts['stale'],
                'missing': counts['missing'],
                'error': counts['error'],
                'truncated': len(truncated_names),
                'stale_tables': stale_names,
                'truncated_tables': truncated_names,
            },
            'warnings': warnings,
            'tables': tables,
        }
        return result
    finally:
        con.close()


# ── 输出 ──────────────────────────────────────────────────────────────
def _format_line(item: dict) -> str:
    name = item['table']
    if item['status'] == 'missing':
        return f'{name} - 无可用分片 [{item["status_cn"]}]'
    if item['status'] == 'error':
        return f'{name} {item["shard"]} 读取失败 [{item["status_cn"]}]'
    if item['ahead_days']:
        line = (f'{name} {item["shard"]} 到 {item["max_date"]} '
                f'领先 {item["ahead_days"]} 个交易日 [{item["status_cn"]}]')
    else:
        line = (f'{name} {item["shard"]} 到 {item["max_date"]} '
                f'落后 {item["lag_days"]} 个交易日 [{item["status_cn"]}]')
    if item['truncated']:
        line += f'；分片缺 {item["missing_days"]} 个交易日'
    if item['note']:
        line += f'（{item["note"]}）'
    return line


def print_text(result: dict) -> None:
    print(f'数据根目录: {result["data_root"]}')
    print(f'市场最近交易日: {result["market_latest_trading_day"]}'
          f'    交易日历最大日期: {result["calendar_max_date"] or "-"}'
          f'    判定基准: {result["data_reference_date"]}')
    print('-' * 72)
    for item in result['tables']:
        print(_format_line(item))
    print('-' * 72)
    s = result['summary']
    print(f'汇总: 共 {s["tables"]} 张表，过期 {s["stale"]}、合格 {s["ok"]}、'
          f'缺失 {s["missing"]}、读取失败 {s["error"]}，'
          f'分片截断 {s["truncated"]} 张')
    if (result['calendar_max_date']
            and result['calendar_max_date'] < result['market_latest_trading_day']):
        print('说明: 判定基准为市场日历最近交易日；交易日历自身按零容忍判定，'
              '它落后时不会把其它表掩盖成新鲜。')
    for w in result['warnings']:
        print(f'[告警] {w}')


# ── 沙箱边界自检（--env-check） ──────────────────────────────────────
def _readable(path: Path) -> str:
    """只判断路径是否可读，绝不打开文件或回传内容。"""
    return 'readable' if os.access(path, os.R_OK) else 'not-readable'


def _probe_write(path: Path) -> str:
    """写一个探针文件再立刻删除，返回 wrote 或 refused: <异常类名>。"""
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('env-check probe\n')
    except Exception as exc:  # 权限不足 / 只读挂载等
        return f'refused: {type(exc).__name__}'
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # 写成功但删不掉，按不可用处理
        return f'refused: {type(exc).__name__}'
    return 'wrote'


def env_check() -> int:
    """打印五行沙箱边界事实；不读行情数据，也不输出凭据内容。"""
    print(f'cwd={Path.cwd()}')
    print(f'creds={_readable(Path("~/.dsh/.credentials.yaml").expanduser())}')
    print('statedb='
          f'{_readable(Path("/home/agent/state/superagent/orchestrator.sqlite"))}')
    print(f'outside-write={_probe_write(Path("/home/agent/probe-out.txt"))}')
    print(f'inside-write={_probe_write(Path.cwd() / ".env-check-probe.tmp")}')
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='本地 parquet 数据新鲜度检测（只读）')
    ap.add_argument('--json', action='store_true', help='输出机器可读 JSON')
    ap.add_argument('--strict', action='store_true',
                    help='任一表过期或分片截断时退出码 1')
    ap.add_argument('--as-of', dest='as_of',
                    help='按指定时间判定（YYYY-MM-DD 或 YYYY-MM-DDTHH:MM），'
                         '缺省用当前时间')
    ap.add_argument('--data-root', help='数据根目录，缺省用 queries.DATA_ROOT')
    ap.add_argument('--tables', nargs='+', help='只检查这些表（调试/测试用）')
    ap.add_argument('--env-check', action='store_true',
                    help='打印沙箱边界事实（cwd/creds/statedb/写权限）后退出')
    args = ap.parse_args(argv)

    if args.env_check:
        return env_check()

    now = None
    if args.as_of:
        try:
            now = datetime.fromisoformat(args.as_of)
        except ValueError:
            print(f'--as-of 格式错误: {args.as_of}', file=sys.stderr)
            return 2
        if now.tzinfo is not None:
            now = now.replace(tzinfo=None)  # 按本地墙钟时间判定
        if 'T' not in args.as_of and ' ' not in args.as_of:
            now = datetime.combine(now.date(), DEFAULT_AS_OF_TIME)

    result = analyze(Path(args.data_root) if args.data_root else None, now)
    if args.tables:
        wanted = set(args.tables)
        result['tables'] = [x for x in result['tables'] if x['table'] in wanted]
        # 重新汇总
        counts = {k: 0 for k in _STATUS_CN}
        stale, trunc = [], []
        for x in result['tables']:
            counts[x['status']] += 1
            if x['status'] == 'stale':
                stale.append(x['table'])
            if x['truncated']:
                trunc.append(x['table'])
        result['summary'].update(
            tables=len(result['tables']), ok=counts['ok'],
            stale=counts['stale'], missing=counts['missing'],
            error=counts['error'], truncated=len(trunc),
            stale_tables=stale, truncated_tables=trunc)
        result['strict_failed'] = bool(stale or trunc)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_text(result)

    if args.strict and result['strict_failed']:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
