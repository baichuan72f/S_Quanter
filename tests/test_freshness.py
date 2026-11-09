"""数据新鲜度检测的测试
========================

用 DuckDB 造临时 parquet 分片，不碰仓库里的 `data/`，也不依赖 pandas/bigquant。
覆盖 issue #21 要求的四类 fixture：

1. 全部表停在「上周二」→ 必须按市场日历报出缺少的交易日数，不能报新鲜；
2. 某张表 max(date) 落后 2 个交易日 → `--strict` 退出码 1；
3. 分片被截断（distinct(date) 少于应有交易日）→ 必须报出；
4. 全部对齐 → `--strict` 退出码 0。

测试本身只依赖标准库 `unittest`（pytest 也能收集），这样在没有 pytest 的最小
`.venv` 里也能跑：

    .venv/bin/python -m pytest tests/test_freshness.py -q
    .venv/bin/python tests/test_freshness.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'datadownload' / 'check_freshness.py'
sys.path.insert(0, str(ROOT / 'datadownload'))

import check_freshness as cf  # noqa: E402

# 2026-10-26(一) ~ 2026-10-30(五) 是一个没有节假日的正常交易周
WEEK = [date(2026, 10, 26) + timedelta(days=i) for i in range(5)]
LAST_TUESDAY = [d for d in WEEK if d <= date(2026, 10, 27)]
AS_OF_FRIDAY_CLOSED = datetime(2026, 10, 30, 16, 0)


def _write_table(root: Path, table: str, shard: str, dates, *,
                 calendar: bool = False) -> None:
    """写一个只含 date 列（日历表再加 market_code）的 parquet 分片。"""
    out_dir = root / table
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / shard
    con = duckdb.connect()
    try:
        if calendar:
            con.execute('CREATE TABLE t(date DATE, market_code VARCHAR)')
            con.executemany('INSERT INTO t VALUES (?, ?)',
                            [(d.isoformat(), 'CN') for d in dates])
        else:
            con.execute('CREATE TABLE t(date DATE)')
            con.executemany('INSERT INTO t VALUES (?)',
                            [(d.isoformat(),) for d in dates])
        con.execute(f"COPY t TO '{out_file.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _run_cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), '--data-root', str(root), *args],
        capture_output=True, text=True, cwd=str(ROOT))


def _analyze(root: Path, now: datetime = AS_OF_FRIDAY_CLOSED) -> dict:
    return cf.analyze(root, now)


class FreshnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── fixture 1：全部表停在「上周二」 ─────────────────────────────────
    def test_all_tables_stuck_at_last_tuesday_is_not_fresh(self):
        for table in ('trading_days', 'cn_stock_bar1d', 'cn_stock_prefactors'):
            _write_table(self.root, table, '202610.parquet', LAST_TUESDAY,
                         calendar=(table == 'trading_days'))

        result = _analyze(self.root)
        self.assertEqual(result['market_latest_trading_day'], '2026-10-30')
        by_name = {x['table']: x for x in result['tables']}
        # 交易日历零容忍：上周二 10-27 到周五 10-30 缺 3 个交易日
        self.assertEqual(by_name['trading_days']['status'], 'stale')
        self.assertEqual(by_name['trading_days']['lag_days'], 3)
        self.assertEqual(by_name['cn_stock_bar1d']['lag_days'], 3)
        self.assertTrue(result['strict_failed'])
        # 不能因为日历自己也落后，就把整批数据说成新鲜
        self.assertIn('trading_days', result['summary']['stale_tables'])

    # ── fixture 2：某表落后 2 个交易日 → --strict 退出码 1 ──────────────
    def test_table_two_days_behind_strict_exit_1(self):
        _write_table(self.root, 'trading_days', '202610.parquet', WEEK,
                     calendar=True)
        _write_table(self.root, 'cn_stock_bar1d', '202610.parquet',
                     WEEK[:3])  # 到周三 10-28，落后周四/周五 2 个交易日

        proc = _run_cli(self.root, '--as-of', '2026-10-30T16:00', '--strict')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('cn_stock_bar1d', proc.stdout)
        self.assertIn('[过期]', proc.stdout)

    # ── fixture 3：分片被截断（distinct date 少于应有） ─────────────────
    def test_truncated_shard_is_reported(self):
        _write_table(self.root, 'trading_days', '202610.parquet', WEEK,
                     calendar=True)
        # 最大日期对得上（10-30），但中间缺了 10-28、10-29
        _write_table(self.root, 'cn_stock_bar1d', '202610.parquet',
                     [WEEK[0], WEEK[1], WEEK[4]])

        result = _analyze(self.root)
        row = {x['table']: x for x in result['tables']}['cn_stock_bar1d']
        self.assertEqual(row['max_date'], '2026-10-30')
        self.assertEqual(row['lag_days'], 0)
        self.assertTrue(row['truncated'])
        self.assertEqual(row['missing_days'], 2)
        self.assertIn('cn_stock_bar1d', result['summary']['truncated_tables'])

        proc = _run_cli(self.root, '--as-of', '2026-10-30T16:00')
        self.assertIn('分片缺 2 个交易日', proc.stdout)

    # ── fixture 4：全部对齐 → --strict 退出码 0 ──────────────────────────
    def test_all_aligned_strict_exit_0(self):
        _write_table(self.root, 'trading_days', '202610.parquet', WEEK,
                     calendar=True)
        for table in ('cn_stock_bar1d', 'cn_stock_real_bar1d',
                      'cn_stock_prefactors'):
            _write_table(self.root, table, '202610.parquet', WEEK)

        proc = _run_cli(self.root, '--as-of', '2026-10-30T16:00', '--strict')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn('[过期]', proc.stdout)
        self.assertNotIn('分片缺', proc.stdout)

    # ── 附加：bar1d 与 real_bar1d 最大日期差 > 1 个交易日 → 单独告警 ──
    def test_bar_and_real_bar_gap_warning(self):
        _write_table(self.root, 'trading_days', '202610.parquet', WEEK,
                     calendar=True)
        _write_table(self.root, 'cn_stock_bar1d', '202610.parquet', WEEK[:1])
        _write_table(self.root, 'cn_stock_real_bar1d', '202610.parquet', WEEK)

        result = _analyze(self.root)
        self.assertTrue(any('cn_stock_bar1d' in w and 'cn_stock_real_bar1d' in w
                            for w in result['warnings']))

    # ── 附加：--json 可解析且字段稳定 ────────────────────────────────────
    def test_json_output_is_machine_readable(self):
        _write_table(self.root, 'trading_days', '202610.parquet', WEEK,
                     calendar=True)
        _write_table(self.root, 'cn_stock_bar1d', '202610.parquet', WEEK[:3])

        proc = _run_cli(self.root, '--as-of', '2026-10-30T16:00', '--json')
        data = json.loads(proc.stdout)
        self.assertEqual(data['market_latest_trading_day'], '2026-10-30')
        row = {x['table']: x for x in data['tables']}['cn_stock_bar1d']
        self.assertEqual(row['status'], 'stale')
        self.assertEqual(row['lag_days'], 2)
        self.assertEqual(row['shard'], '202610.parquet')
        # 不加 --strict 时退出码仍为 0，方便 cron 只看 JSON
        self.assertEqual(proc.returncode, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
