"""
状态库 —— 模拟盘跨日状态的本地持久化（SQLite，零外部依赖）
============================================================

为什么需要它
------------
平台模拟盘**不是常驻进程**：每个交易日冷启动跑一天、跑完退出，进程内存里的
持仓 / 现金 / 挂单 / 策略自定义变量全部消失。能连续跑下去的唯一原因是这些状态
被外置到数据库，次日开盘前再装回引擎。本模块就是这套「外置状态」的本地实现，
用一个 SQLite 文件替代平台的 tradingapiserver HTTP 服务。

表结构与字段命名
----------------
字段名一律沿用 BigQuant SDK 模拟交易 API 文档（见 constants.py 顶部注释），
使本地库的 positions / orders / planned_orders / performances 与平台
`Strategy.get_positions()` 等返回结构可直接互认，便于日后对拍或迁移。

两种写入粒度（对齐平台 AccountOnePerDay / AccountNPerDay）
  · 一天一行 : account_cash、account_performances     → 主键 (strategy_id, trading_day)
  · 一天多行 : positions / orders / trades / planned_orders / run_logs

幂等
----
所有「一天多行」表的写入都是**先删当日再整批插入**（remove-then-insert，
平台同款语义）。同一交易日重复跑不会产生重复记录，跑挂了直接重跑即可。
`rollback_from()` 提供更粗的回滚：抹掉某日（含）之后的全部状态。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

# ── DDL ─────────────────────────────────────────────────────────
# 说明：数值统一用 REAL，数量用 INTEGER；嵌套结构（risk_indicator、benchmark、
# order_params 等）以 JSON 文本存在 *_json 列，读出时自动反序列化。
_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS strategies (
    strategy_id           TEXT PRIMARY KEY,
    strategy_name         TEXT NOT NULL,
    capital_base          REAL NOT NULL,
    benchmark_instrument  TEXT DEFAULT '000300.SH',
    market                TEXT DEFAULT 'cn_stock',
    frequency             TEXT DEFAULT '1d',
    first_trading_day     TEXT,
    last_trading_day      TEXT,
    order_price_field_buy  TEXT DEFAULT 'open',
    order_price_field_sell TEXT DEFAULT 'open',
    status                TEXT DEFAULT 'running',
    config_json           TEXT DEFAULT '{}',
    created_at            TEXT,
    updated_at            TEXT
);

CREATE TABLE IF NOT EXISTS strategy_user_stores (
    strategy_id  TEXT PRIMARY KEY,
    trading_day  TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS account_cash (
    strategy_id        TEXT NOT NULL,
    trading_day        TEXT NOT NULL,
    balance            REAL NOT NULL,
    total_market_value REAL NOT NULL DEFAULT 0,
    portfolio_value    REAL NOT NULL,
    frozen_cash        REAL NOT NULL DEFAULT 0,
    created_at         TEXT,
    PRIMARY KEY (strategy_id, trading_day)
);

CREATE TABLE IF NOT EXISTS account_positions (
    strategy_id     TEXT NOT NULL,
    trading_day     TEXT NOT NULL,
    instrument      TEXT NOT NULL,
    posi_direction  INTEGER NOT NULL DEFAULT 1,
    name            TEXT DEFAULT '',
    exchange        TEXT DEFAULT '',
    current_qty     INTEGER NOT NULL DEFAULT 0,
    available_qty   INTEGER NOT NULL DEFAULT 0,
    today_qty       INTEGER NOT NULL DEFAULT 0,
    cost_price      REAL NOT NULL DEFAULT 0,
    last_price      REAL NOT NULL DEFAULT 0,
    market_value    REAL NOT NULL DEFAULT 0,
    position_pnl    REAL NOT NULL DEFAULT 0,
    profit_ratio    REAL NOT NULL DEFAULT 0,
    sum_buy_value   REAL NOT NULL DEFAULT 0,
    sum_sell_value  REAL NOT NULL DEFAULT 0,
    commission      REAL NOT NULL DEFAULT 0,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    dividend_qty    INTEGER NOT NULL DEFAULT 0,
    dividend_cash   REAL NOT NULL DEFAULT 0,
    open_date       TEXT DEFAULT '',
    open_price      REAL NOT NULL DEFAULT 0,
    hold_days       INTEGER NOT NULL DEFAULT 0,
    initial_qty     INTEGER NOT NULL DEFAULT 0,
    hedge_flag      INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT,
    PRIMARY KEY (strategy_id, trading_day, instrument, posi_direction)
);

CREATE TABLE IF NOT EXISTS account_orders (
    strategy_id   TEXT NOT NULL,
    trading_day   TEXT NOT NULL,
    order_key     TEXT NOT NULL,
    instrument    TEXT NOT NULL,
    name          TEXT DEFAULT '',
    exchange      TEXT DEFAULT '',
    direction     INTEGER NOT NULL,
    offset_flag   INTEGER NOT NULL DEFAULT 0,
    order_qty     INTEGER NOT NULL DEFAULT 0,
    order_price   REAL NOT NULL DEFAULT 0,
    filled_qty    INTEGER NOT NULL DEFAULT 0,
    average_price REAL NOT NULL DEFAULT 0,
    order_status  INTEGER NOT NULL DEFAULT 0,
    status_msg    TEXT DEFAULT '',
    order_type    TEXT DEFAULT '0',
    hedge_flag    INTEGER NOT NULL DEFAULT 1,
    order_dt      TEXT DEFAULT '',
    filled_dt     TEXT DEFAULT '',
    commission    REAL NOT NULL DEFAULT 0,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    created_at    TEXT,
    PRIMARY KEY (strategy_id, trading_day, order_key)
);

CREATE TABLE IF NOT EXISTS account_trades (
    strategy_id   TEXT NOT NULL,
    trading_day   TEXT NOT NULL,
    trade_id      TEXT NOT NULL,
    order_key     TEXT DEFAULT '',
    instrument    TEXT NOT NULL,
    name          TEXT DEFAULT '',
    direction     INTEGER NOT NULL,
    offset_flag   INTEGER NOT NULL DEFAULT 0,
    filled_qty    INTEGER NOT NULL DEFAULT 0,
    filled_price  REAL NOT NULL DEFAULT 0,
    filled_money  REAL NOT NULL DEFAULT 0,
    commission    REAL NOT NULL DEFAULT 0,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    cost_price    REAL NOT NULL DEFAULT 0,
    filled_dt     TEXT DEFAULT '',
    created_at    TEXT,
    PRIMARY KEY (strategy_id, trading_day, trade_id)
);

-- 计划订单：模拟盘与回测唯一的实质差异所在。
-- T 日 handle_data 产生的委托当天并不成交（日频撮合在 T+1 开盘），而进程当天
-- 就退出了，因此必须落库；T+1 装载后作为待撮合队列注入引擎。
-- trading_day = 计划生效（待撮合）的那一天，即下单日的下一交易日。
CREATE TABLE IF NOT EXISTS account_planned_orders (
    strategy_id        TEXT NOT NULL,
    trading_day        TEXT NOT NULL,
    planned_order_id   TEXT NOT NULL,
    signal_day         TEXT NOT NULL,
    instrument         TEXT NOT NULL,
    name               TEXT DEFAULT '',
    exchange           TEXT DEFAULT '',
    direction          INTEGER NOT NULL,
    offset_flag        INTEGER NOT NULL DEFAULT 0,
    original_order_qty INTEGER NOT NULL DEFAULT 0,
    order_qty          INTEGER NOT NULL DEFAULT 0,
    order_price        REAL NOT NULL DEFAULT 0,
    order_type         TEXT DEFAULT '0',
    order_status       INTEGER NOT NULL DEFAULT 0,
    status_msg         TEXT DEFAULT '',
    order_dt           TEXT DEFAULT '',
    order_placed_dt    TEXT DEFAULT '',
    order_key          TEXT DEFAULT '',
    order_params_json  TEXT DEFAULT '{}',
    created_at         TEXT,
    PRIMARY KEY (strategy_id, trading_day, planned_order_id)
);

-- 绩效：一天一行。字段集对齐平台 papertradingperf.evaluate() 的输出 ——
-- 增量算法要靠历史行里的 max_portfolio_value / total_* 累计量往前推，
-- 所以这些"中间量"必须落库，不能只存展示指标。
CREATE TABLE IF NOT EXISTS account_performances (
    strategy_id          TEXT NOT NULL,
    trading_day          TEXT NOT NULL,
    trading_day_index    INTEGER NOT NULL DEFAULT 0,
    portfolio_value      REAL NOT NULL,
    balance              REAL NOT NULL DEFAULT 0,
    total_market_value   REAL NOT NULL DEFAULT 0,
    today_return         REAL NOT NULL DEFAULT 0,
    cumulative_return    REAL NOT NULL DEFAULT 0,
    annualized_return    REAL NOT NULL DEFAULT 0,
    max_drawdown         REAL NOT NULL DEFAULT 0,
    max_portfolio_value  REAL NOT NULL DEFAULT 0,
    ten_day_return       REAL NOT NULL DEFAULT 0,
    week_return          REAL NOT NULL DEFAULT 0,
    month_return         REAL NOT NULL DEFAULT 0,
    three_month_return   REAL NOT NULL DEFAULT 0,
    six_month_return     REAL NOT NULL DEFAULT 0,
    year_return          REAL NOT NULL DEFAULT 0,
    win_percent          REAL NOT NULL DEFAULT 0,
    pnl_ratio            REAL NOT NULL DEFAULT 0,
    today_commission     REAL NOT NULL DEFAULT 0,
    today_realized_pnl   REAL NOT NULL DEFAULT 0,
    today_sum_buy_value  REAL NOT NULL DEFAULT 0,
    today_sum_sell_value REAL NOT NULL DEFAULT 0,
    total_commission     REAL NOT NULL DEFAULT 0,
    total_buy_value      REAL NOT NULL DEFAULT 0,
    total_sell_value     REAL NOT NULL DEFAULT 0,
    total_close_count    INTEGER NOT NULL DEFAULT 0,
    total_profit_count   INTEGER NOT NULL DEFAULT 0,
    total_profit_value   REAL NOT NULL DEFAULT 0,
    total_loss_value     REAL NOT NULL DEFAULT 0,
    risk_indicator_json  TEXT DEFAULT '{}',
    benchmark_json       TEXT DEFAULT '{}',
    created_at           TEXT,
    PRIMARY KEY (strategy_id, trading_day)
);

-- 每日运行留痕：一天一行，记录该日运行结果与数据校验结论，便于事后审计
-- 「哪天跑了、跑成什么样、有没有降级」。
CREATE TABLE IF NOT EXISTS run_logs (
    strategy_id  TEXT NOT NULL,
    trading_day  TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    level        TEXT NOT NULL DEFAULT 'INFO',
    msg          TEXT NOT NULL DEFAULT '',
    created_at   TEXT,
    PRIMARY KEY (strategy_id, trading_day, seq)
);

-- 任务运行历史：每次「提交/跑批」一行，供任务管理器的运行历史面板与
-- 状态徽标（运行中/已完成/失败）。与 account_* 快照不同，它不参与回滚
-- （审计留痕），仅 reset_strategy 时随策略一起清空。
CREATE TABLE IF NOT EXISTS task_runs (
    strategy_id  TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'run',      -- run / range / submit
    status       TEXT NOT NULL DEFAULT 'running',  -- running / ok / error
    trading_day  TEXT,                             -- 本次跑的交易日（区间为首日）
    mode         TEXT,                             -- auto / date / range
    params_json  TEXT DEFAULT '{}',                -- 提交/跑批参数留痕
    summary      TEXT DEFAULT '',                  -- 结果摘要（如「总资产 …」）
    error        TEXT DEFAULT '',
    log_json     TEXT DEFAULT '[]',                -- 控制台输出逐行
    started_at   TEXT,
    finished_at  TEXT,
    PRIMARY KEY (strategy_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_task_runs_sid
    ON task_runs(strategy_id, started_at);
CREATE INDEX IF NOT EXISTS idx_pos_sid_day
    ON account_positions(strategy_id, trading_day);
CREATE INDEX IF NOT EXISTS idx_ord_sid_day
    ON account_orders(strategy_id, trading_day);
CREATE INDEX IF NOT EXISTS idx_trd_sid_day
    ON account_trades(strategy_id, trading_day);
CREATE INDEX IF NOT EXISTS idx_plan_sid_day
    ON account_planned_orders(strategy_id, trading_day);
CREATE INDEX IF NOT EXISTS idx_perf_sid_day
    ON account_performances(strategy_id, trading_day);
"""

# 「一天多行」表 → 其 JSON 列名（读出时反序列化）
_JSON_COLUMNS = {
    'account_planned_orders': {'order_params_json': 'order_params'},
    'account_performances': {'risk_indicator_json': 'risk_indicator',
                             'benchmark_json': 'benchmark'},
    'task_runs': {'params_json': 'params', 'log_json': 'log'},
}

# 一天多行表清单（幂等删除 / 回滚时遍历）
N_PER_DAY_TABLES = (
    'account_positions',
    'account_orders',
    'account_trades',
    'account_planned_orders',
    'run_logs',
)

# 一天一行表清单
ONE_PER_DAY_TABLES = (
    'account_cash',
    'account_performances',
)


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class StateStore:
    """模拟盘状态库（SQLite）。

    用法
    ----
        with StateStore('state/paper.db') as st:
            st.upsert_strategy(strategy_id='demo', strategy_name='双均线',
                               capital_base=1_000_000)
            st.write_positions('demo', '2026-09-07', rows)

    线程/进程模型：单进程单连接。模拟盘每日冷启动跑完即退出，无并发写需求；
    WAL 模式下允许另一个进程并发读（例如一边跑一边查报表）。
    """

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        d = os.path.dirname(os.path.abspath(self.db_path))
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ── 上下文管理 ───────────────────────────────────────────────
    def __enter__(self) -> 'StateStore':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    # ── 内部工具 ─────────────────────────────────────────────────
    def _columns(self, table: str) -> List[str]:
        cur = self.conn.execute(f'PRAGMA table_info({table})')
        return [r['name'] for r in cur.fetchall()]

    def _insert_rows(self, table: str, rows: Iterable[Dict[str, Any]],
                     base: Dict[str, Any]) -> int:
        """整批插入。rows 里的键按表列名过滤（多余键忽略、缺失列取表默认值），
        base 提供 strategy_id / trading_day 等公共列。dict/list 值自动 JSON 化。"""
        rows = list(rows)
        if not rows:
            return 0
        cols = set(self._columns(table))
        json_map = _JSON_COLUMNS.get(table, {})
        # 业务键 -> JSON 列名 的反向映射（允许调用方传 order_params 而非 *_json）
        alias = {v: k for k, v in json_map.items()}
        payloads = []
        for r in rows:
            item = dict(base)
            item.update(r)
            rec: Dict[str, Any] = {}
            for k, v in item.items():
                key = alias.get(k, k)
                if key not in cols:
                    continue
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False, default=str)
                rec[key] = v
            rec.setdefault('created_at', _now())
            payloads.append(rec)
        keys = sorted({k for rec in payloads for k in rec})
        sql = (f'INSERT OR REPLACE INTO {table} ({", ".join(keys)}) '
               f'VALUES ({", ".join("?" * len(keys))})')
        self.conn.executemany(sql, [[rec.get(k) for k in keys] for rec in payloads])
        self.conn.commit()
        return len(payloads)

    def _fetch(self, table: str, strategy_id: str,
               trading_day: Optional[str] = None,
               order_by: str = '') -> List[Dict[str, Any]]:
        sql = f'SELECT * FROM {table} WHERE strategy_id = ?'
        args: List[Any] = [strategy_id]
        if trading_day is not None:
            sql += ' AND trading_day = ?'
            args.append(trading_day)
        if order_by:
            sql += f' ORDER BY {order_by}'
        cur = self.conn.execute(sql, args)
        json_map = _JSON_COLUMNS.get(table, {})
        out = []
        for row in cur.fetchall():
            d = dict(row)
            for col, name in json_map.items():
                raw = d.pop(col, None)
                try:
                    d[name] = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    d[name] = {}
            out.append(d)
        return out

    # ── 策略元信息 ───────────────────────────────────────────────
    def upsert_strategy(self, strategy_id: str, strategy_name: str,
                        capital_base: float, **kwargs) -> None:
        """注册/更新策略。已存在时只更新传入字段，capital_base 变更会拒绝
        （初始资金是绩效反推的基准，中途改会让历史累计收益率失去意义）。"""
        old = self.get_strategy(strategy_id)
        if old is not None and abs(float(old['capital_base']) - float(capital_base)) > 1e-6:
            raise ValueError(
                f'策略 {strategy_id} 已存在且 capital_base 不同'
                f'（库内 {old["capital_base"]} vs 传入 {capital_base}）。'
                f'初始资金是累计收益率的计算基准，不允许中途变更；'
                f'如需重来请换 strategy_id 或先 reset_strategy()。')
        cols = set(self._columns('strategies'))
        rec: Dict[str, Any] = {'strategy_id': strategy_id,
                               'strategy_name': strategy_name,
                               'capital_base': float(capital_base)}
        for k, v in kwargs.items():
            key = 'config_json' if k == 'config' else k
            if key not in cols:
                continue
            rec[key] = json.dumps(v, ensure_ascii=False, default=str) \
                if isinstance(v, (dict, list)) else v
        if old is None:
            rec['created_at'] = _now()
        else:
            merged = {k: v for k, v in old.items() if k in cols}
            merged.update(rec)
            rec = merged
        rec['updated_at'] = _now()
        keys = sorted(rec)
        self.conn.execute(
            f'INSERT OR REPLACE INTO strategies ({", ".join(keys)}) '
            f'VALUES ({", ".join("?" * len(keys))})',
            [rec[k] for k in keys])
        self.conn.commit()

    @staticmethod
    def _strategy_row(row) -> Dict[str, Any]:
        """strategies 行 → dict，把 config_json 解成 config（与 get_strategy 同形）。

        list_strategies 以前直接返回原始行，config_json 还是字符串，于是
        strategy_summary 读 `s['config']['source']` 恒为空串 —— 列表页「跑批」
        对任何账户都退化成「缺少源文件路径」告警。两处必须同形。
        """
        d = dict(row)
        try:
            d['config'] = json.loads(d.pop('config_json', None) or '{}')
        except ValueError:
            d['config'] = {}
        return d

    def get_strategy(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            'SELECT * FROM strategies WHERE strategy_id = ?', (strategy_id,))
        row = cur.fetchone()
        return None if row is None else self._strategy_row(row)

    def list_strategies(self) -> List[Dict[str, Any]]:
        cur = self.conn.execute('SELECT * FROM strategies ORDER BY created_at')
        return [self._strategy_row(r) for r in cur.fetchall()]

    def set_last_trading_day(self, strategy_id: str, trading_day: str) -> None:
        self.conn.execute(
            'UPDATE strategies SET last_trading_day = ?, updated_at = ? '
            'WHERE strategy_id = ?', (trading_day, _now(), strategy_id))
        self.conn.commit()

    def set_first_trading_day_if_empty(self, strategy_id: str,
                                       trading_day: str) -> None:
        self.conn.execute(
            'UPDATE strategies SET first_trading_day = ?, updated_at = ? '
            'WHERE strategy_id = ? AND (first_trading_day IS NULL '
            "OR first_trading_day = '')",
            (trading_day, _now(), strategy_id))
        self.conn.commit()

    # ── user_store（策略自定义跨日变量） ────────────────────────
    def read_user_store(self, strategy_id: str) -> Dict[str, Any]:
        cur = self.conn.execute(
            'SELECT payload_json FROM strategy_user_stores WHERE strategy_id = ?',
            (strategy_id,))
        row = cur.fetchone()
        if row is None:
            return {}
        try:
            return json.loads(row['payload_json'] or '{}')
        except ValueError:
            return {}

    def write_user_store(self, strategy_id: str, trading_day: str,
                         payload: Dict[str, Any],
                         max_bytes: int = 1_000_000) -> None:
        """写入 user_store。必须能 JSON 序列化 —— 存不下去就报错，不静默丢弃
        （平台同样限制大小并直接报 'context.user_store too large'）。
        丢状态会让次日策略行为悄悄改变，属于必须暴露的错误。"""
        try:
            text = json.dumps(payload or {}, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f'context.user_store 无法 JSON 序列化：{e}。'
                f'模拟盘的跨日状态必须可序列化，请只往 user_store 放'
                f'基础类型 / list / dict（DataFrame 请转 to_dict 或改存文件）。') from e
        if len(text.encode('utf-8')) > max_bytes:
            raise ValueError(
                f'context.user_store too large: {len(text.encode("utf-8"))} bytes '
                f'> {max_bytes}。请精简跨日状态。')
        self.conn.execute(
            'INSERT OR REPLACE INTO strategy_user_stores '
            '(strategy_id, trading_day, payload_json, updated_at) VALUES (?,?,?,?)',
            (strategy_id, trading_day, text, _now()))
        self.conn.commit()

    # ── 现金 ────────────────────────────────────────────────────
    def write_cash(self, strategy_id: str, trading_day: str, balance: float,
                   total_market_value: float, portfolio_value: float,
                   frozen_cash: float = 0.0) -> None:
        self.conn.execute(
            'INSERT OR REPLACE INTO account_cash (strategy_id, trading_day, '
            'balance, total_market_value, portfolio_value, frozen_cash, created_at) '
            'VALUES (?,?,?,?,?,?,?)',
            (strategy_id, trading_day, float(balance), float(total_market_value),
             float(portfolio_value), float(frozen_cash), _now()))
        self.conn.commit()

    def read_cash(self, strategy_id: str,
                  trading_day: str) -> Optional[Dict[str, Any]]:
        rows = self._fetch('account_cash', strategy_id, trading_day)
        return rows[0] if rows else None

    def latest_cash_before(self, strategy_id: str,
                           trading_day: str) -> Optional[Dict[str, Any]]:
        """取 trading_day 之前（不含）最近一天的现金快照。"""
        cur = self.conn.execute(
            'SELECT * FROM account_cash WHERE strategy_id = ? AND trading_day < ? '
            'ORDER BY trading_day DESC LIMIT 1', (strategy_id, trading_day))
        row = cur.fetchone()
        return dict(row) if row else None

    # ── 持仓 / 订单 / 成交（一天多行，先删后写） ────────────────
    def write_positions(self, strategy_id: str, trading_day: str,
                        rows: Iterable[Dict[str, Any]]) -> int:
        self.remove_positions(strategy_id, trading_day)
        return self._insert_rows('account_positions', rows,
                                 {'strategy_id': strategy_id,
                                  'trading_day': trading_day})

    def remove_positions(self, strategy_id: str, trading_day: str) -> None:
        self.conn.execute('DELETE FROM account_positions WHERE strategy_id = ? '
                          'AND trading_day = ?', (strategy_id, trading_day))
        self.conn.commit()

    def read_positions(self, strategy_id: str,
                       trading_day: str) -> List[Dict[str, Any]]:
        return self._fetch('account_positions', strategy_id, trading_day,
                           order_by='instrument')

    def latest_positions_before(self, strategy_id: str,
                                trading_day: str) -> List[Dict[str, Any]]:
        """取 trading_day 之前（不含）最近一个有现金快照的交易日的持仓。

        以 account_cash 定位「最后一个成功跑完的交易日」，而不是以
        account_positions 定位 —— 空仓日没有持仓行，若按持仓表回溯会误取到更早
        的一天，把已清掉的仓位重新装回来。
        """
        cash = self.latest_cash_before(strategy_id, trading_day)
        if cash is None:
            return []
        return self.read_positions(strategy_id, str(cash['trading_day']))

    def write_orders(self, strategy_id: str, trading_day: str,
                     rows: Iterable[Dict[str, Any]]) -> int:
        # 引擎导出的订单行 trading_day 可能为空串（计划单注入时引擎的
        # current_dt 尚未设置）—— 空串会覆盖下面的 base 值，导致委托记录
        # 整列没有日期。这里丢掉空值，让 base 的 trading_day 生效。
        clean = []
        for r in rows:
            r = dict(r)
            if not r.get('trading_day'):
                r.pop('trading_day', None)
            clean.append(r)
        self.conn.execute('DELETE FROM account_orders WHERE strategy_id = ? '
                          'AND trading_day = ?', (strategy_id, trading_day))
        return self._insert_rows('account_orders', clean,
                                 {'strategy_id': strategy_id,
                                  'trading_day': trading_day})

    def read_orders(self, strategy_id: str,
                    trading_day: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._fetch('account_orders', strategy_id, trading_day,
                           order_by='trading_day, order_key')

    def write_trades(self, strategy_id: str, trading_day: str,
                     rows: Iterable[Dict[str, Any]]) -> int:
        self.conn.execute('DELETE FROM account_trades WHERE strategy_id = ? '
                          'AND trading_day = ?', (strategy_id, trading_day))
        return self._insert_rows('account_trades', rows,
                                 {'strategy_id': strategy_id,
                                  'trading_day': trading_day})

    def read_trades(self, strategy_id: str,
                    trading_day: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._fetch('account_trades', strategy_id, trading_day,
                           order_by='trading_day, trade_id')

    # ── 计划订单 ────────────────────────────────────────────────
    def write_planned_orders(self, strategy_id: str, trading_day: str,
                             rows: Iterable[Dict[str, Any]]) -> int:
        """写入「在 trading_day 待撮合」的计划订单。

        删除范围**仅限该日名下仍待执行（pending）的行**：已终结（撮合/取消）
        的历史计划单是审计记录。若按交易日全删，重跑前一晚（该日已跑完）会把
        已撮合历史删掉、再插一批永不撮合的新单 —— 曾造成 pending 悬置与
        重复注入（重跑后应 rollback 生效日起再续跑，见 runner 的 WARN）。
        """
        self.conn.execute(
            'DELETE FROM account_planned_orders WHERE strategy_id = ? '
            'AND trading_day = ? AND order_status IN (0, 1)',
            (strategy_id, trading_day))
        return self._insert_rows('account_planned_orders', rows,
                                 {'strategy_id': strategy_id,
                                  'trading_day': trading_day})

    def read_planned_orders(self, strategy_id: str,
                            trading_day: Optional[str] = None,
                            only_pending: bool = False) -> List[Dict[str, Any]]:
        rows = self._fetch('account_planned_orders', strategy_id, trading_day,
                           order_by='trading_day, planned_order_id')
        if only_pending:
            from .constants import PLANNED_PENDING_STATUSES
            rows = [r for r in rows
                    if int(r['order_status']) in PLANNED_PENDING_STATUSES]
        return rows

    def update_planned_order_status(self, strategy_id: str, trading_day: str,
                                    planned_order_id: str, order_status: int,
                                    status_msg: str = '',
                                    order_key: str = '',
                                    order_placed_dt: str = '') -> int:
        """更新计划单撮合结果，返回受影响行数（0 = 未匹配到行，调用方须告警）。"""
        cur = self.conn.execute(
            'UPDATE account_planned_orders SET order_status = ?, status_msg = ?, '
            'order_key = ?, order_placed_dt = ? WHERE strategy_id = ? '
            'AND trading_day = ? AND planned_order_id = ?',
            (int(order_status), status_msg, order_key, order_placed_dt,
             strategy_id, trading_day, planned_order_id))
        self.conn.commit()
        return cur.rowcount

    def mark_planned_order_terminal(self, strategy_id: str, trading_day: str,
                                    planned_order_id: str, order_status: int,
                                    status_msg: str = '', order_key: str = '',
                                    order_placed_dt: str = '') -> None:
        """兜底：撮合回写 0 行命中时，直接覆盖写一行终态计划单。

        防止行找不到（写入日与回写日错位）时被持续当作 pending 反复注入。
        INSERT OR REPLACE 会整行替换，主键同 (strategy, trading_day, pid)。
        """
        self.conn.execute(
            'INSERT OR REPLACE INTO account_planned_orders '
            '(strategy_id, trading_day, planned_order_id, order_status, '
            'status_msg, order_key, order_placed_dt, created_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (strategy_id, trading_day, planned_order_id, int(order_status),
             status_msg, order_key, order_placed_dt, _now()))
        self.conn.commit()

    # ── 绩效 ────────────────────────────────────────────────────
    def write_performance(self, strategy_id: str, trading_day: str,
                          perf: Dict[str, Any]) -> None:
        rec = dict(perf)
        rec.pop('strategy_id', None)
        rec.pop('trading_day', None)
        self.conn.execute('DELETE FROM account_performances WHERE strategy_id = ? '
                          'AND trading_day = ?', (strategy_id, trading_day))
        self._insert_rows('account_performances', [rec],
                          {'strategy_id': strategy_id,
                           'trading_day': trading_day})

    def read_performances(self, strategy_id: str) -> List[Dict[str, Any]]:
        """按交易日升序返回全部绩效行（增量绩效计算需要历史序列）。"""
        return self._fetch('account_performances', strategy_id,
                           order_by='trading_day')

    def last_performance(self, strategy_id: str,
                         before: Optional[str] = None) -> Optional[Dict[str, Any]]:
        sql = 'SELECT * FROM account_performances WHERE strategy_id = ?'
        args: List[Any] = [strategy_id]
        if before:
            sql += ' AND trading_day < ?'
            args.append(before)
        sql += ' ORDER BY trading_day DESC LIMIT 1'
        cur = self.conn.execute(sql, args)
        row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        for col, name in _JSON_COLUMNS['account_performances'].items():
            raw = d.pop(col, None)
            try:
                d[name] = json.loads(raw) if raw else {}
            except ValueError:
                d[name] = {}
        return d

    # ── 运行日志 ────────────────────────────────────────────────
    def write_run_logs(self, strategy_id: str, trading_day: str,
                       logs: Iterable[Dict[str, Any]]) -> int:
        self.conn.execute('DELETE FROM run_logs WHERE strategy_id = ? '
                          'AND trading_day = ?', (strategy_id, trading_day))
        rows = [{'seq': i, 'level': str(x.get('level', 'INFO')),
                 'msg': str(x.get('msg', ''))} for i, x in enumerate(logs)]
        return self._insert_rows('run_logs', rows,
                                 {'strategy_id': strategy_id,
                                  'trading_day': trading_day})

    def read_run_logs(self, strategy_id: str,
                      trading_day: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._fetch('run_logs', strategy_id, trading_day,
                           order_by='trading_day, seq')

    # ── 任务运行历史（任务管理器） ──────────────────────────────
    def write_task_run(self, strategy_id: str, run_id: str, **fields) -> None:
        """写入/覆盖一条任务运行记录（同 run_id 幂等覆盖）。

        fields 支持 kind / status / trading_day / mode / params / summary /
        error / log / started_at / finished_at；dict/list 自动 JSON 化。
        """
        rec: Dict[str, Any] = {'strategy_id': strategy_id, 'run_id': run_id}
        rec.update({k: v for k, v in fields.items() if v is not None})
        cols = set(self._columns('task_runs'))
        # 业务名 -> 列名（params -> params_json）
        alias = {v: k for k, v in _JSON_COLUMNS['task_runs'].items()}
        payload: Dict[str, Any] = {}
        for k, v in rec.items():
            key = alias.get(k, k)
            if key not in cols:
                continue
            payload[key] = json.dumps(v, ensure_ascii=False, default=str) \
                if isinstance(v, (dict, list)) else v
        keys = sorted(payload)
        self.conn.execute(
            f'INSERT OR REPLACE INTO task_runs ({", ".join(keys)}) '
            f'VALUES ({", ".join("?" * len(keys))})',
            [payload[k] for k in keys])
        self.conn.commit()

    def update_task_run(self, strategy_id: str, run_id: str, **fields) -> int:
        """回填已有运行记录（跑完写 status/summary/log/finished_at）。返回命中行数。"""
        cols = set(self._columns('task_runs'))
        alias = {v: k for k, v in _JSON_COLUMNS['task_runs'].items()}
        payload: Dict[str, Any] = {}
        for k, v in fields.items():
            key = alias.get(k, k)
            if key not in cols or v is None:
                continue
            payload[key] = json.dumps(v, ensure_ascii=False, default=str) \
                if isinstance(v, (dict, list)) else v
        if not payload:
            return 0
        keys = sorted(payload)
        cur = self.conn.execute(
            f'UPDATE task_runs SET {", ".join(f"{k} = ?" for k in keys)} '
            f'WHERE strategy_id = ? AND run_id = ?',
            [payload[k] for k in keys] + [strategy_id, run_id])
        self.conn.commit()
        return cur.rowcount

    def read_task_runs(self, strategy_id: str,
                       limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """该策略的运行记录，最新在前。"""
        rows = self._fetch('task_runs', strategy_id,
                           order_by='started_at DESC, run_id DESC')
        for r in rows:
            if not isinstance(r.get('log'), list):
                r['log'] = []
        return rows[:limit] if limit else rows

    def latest_task_run(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        runs = self.read_task_runs(strategy_id, limit=1)
        return runs[0] if runs else None

    def latest_run_times(self) -> Dict[str, Dict[str, Any]]:
        """每个策略的最近一次运行（列表页「最近运行时间」列用）。

        列表页每行都要这一列，逐个策略调 latest_task_run 是 N 次查询；
        这里一次全表扫（task_runs 行数 = 跑批次数，量级很小），
        按 started_at 倒序取每个 strategy_id 的第一条。
        """
        cur = self.conn.execute(
            'SELECT strategy_id, run_id, kind, status, mode, trading_day, '
            'started_at, finished_at FROM task_runs '
            'ORDER BY started_at DESC, run_id DESC')
        out: Dict[str, Dict[str, Any]] = {}
        for r in cur.fetchall():
            sid = str(r['strategy_id'])
            if sid not in out:
                out[sid] = dict(r)
        return out

    def set_strategy_status(self, strategy_id: str, status: str) -> None:
        """更新任务状态徽标（running / ok / error）。"""
        self.conn.execute(
            'UPDATE strategies SET status = ?, updated_at = ? '
            'WHERE strategy_id = ?', (str(status), _now(), strategy_id))
        self.conn.commit()

    # ── 已跑交易日 / 回滚 ───────────────────────────────────────
    def ran_trading_days(self, strategy_id: str) -> List[str]:
        """已成功跑完的交易日列表（以现金快照为准，升序）。"""
        cur = self.conn.execute(
            'SELECT trading_day FROM account_cash WHERE strategy_id = ? '
            'ORDER BY trading_day', (strategy_id,))
        return [str(r['trading_day']) for r in cur.fetchall()]

    def has_run(self, strategy_id: str, trading_day: str) -> bool:
        return self.read_cash(strategy_id, trading_day) is not None

    def rollback_from(self, strategy_id: str, trading_day: str) -> Dict[str, int]:
        """删除 trading_day（含）之后的全部状态，用于重跑一段历史。

        注意：user_store 只保留最后一份快照（对齐平台单行存储），无法按日回滚 ——
        回滚后 user_store 仍是最新那天的内容。若策略依赖 user_store，回滚后应
        从首日重跑，或自行清空 user_store。此行为在返回值里以 user_store_kept 标明。
        """
        counts: Dict[str, int] = {}
        for t in N_PER_DAY_TABLES + ONE_PER_DAY_TABLES:
            cur = self.conn.execute(
                f'DELETE FROM {t} WHERE strategy_id = ? AND trading_day >= ?',
                (strategy_id, trading_day))
            counts[t] = cur.rowcount
        self.conn.commit()
        days = self.ran_trading_days(strategy_id)
        self.conn.execute(
            'UPDATE strategies SET last_trading_day = ?, updated_at = ? '
            'WHERE strategy_id = ?',
            (days[-1] if days else None, _now(), strategy_id))
        self.conn.commit()
        counts['user_store_kept'] = 1
        return counts

    def reset_strategy(self, strategy_id: str) -> None:
        """清空该策略的全部状态（含 user_store、运行历史与策略元信息）。"""
        for t in N_PER_DAY_TABLES + ONE_PER_DAY_TABLES + (
                'strategy_user_stores', 'task_runs', 'strategies'):
            self.conn.execute(f'DELETE FROM {t} WHERE strategy_id = ?',
                              (strategy_id,))
        self.conn.commit()
