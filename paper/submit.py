"""
提交任务后端 —— 文件浏览 / 策略探针 / 落盘入 strategies/
==========================================================

看板「提交模拟」两步弹窗的服务端：先在目录树里挑一个 .py / .ipynb，再填
参数，最后把文件**复制进 strategies/** 并写一份同名 ``<stem>.meta.json``。
此后跑批一律以 strategies/ 里这份为准（对齐用户口径「以后就用这里面的
策略来跑」）。

为什么是复制而不是引用
----------------------
- strategies/ 是唯一权威目录：run_daily.py / 看板 / 调度都从它取策略；
- 参数与代码同目录同名，``StrategySpec.load`` 无需额外配置就能读到；
- 源文件后续被改动不会悄悄改变线上任务的行为。

安全边界
--------
浏览器只能看到 ``browse_root`` 内的内容，且所有路径都经 ``resolve_within``
校验（resolve 后必须落在 root 内），越界一律 SubmitError。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .local_data import pool_preset
from .runner import (StrategySpec, as_instruments, detect_warmup_days,
                     meta_json_path, register_spec, scan_source)

# 可提交的策略文件后缀
STRATEGY_SUFFIXES = ('.py', '.ipynb')
# 目录浏览里跳过的噪音目录（含隐藏目录）
SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.ipynb_checkpoints',
             'node_modules', '.pytest_cache', '.claude', '.idea', '.vscode'}

# 提交时写进 meta.json 的参数键（回调函数不进 JSON）
_META_KEYS = ('strategy_id', 'strategy_name', 'capital_base', 'instruments',
              'benchmark', 'order_price_field_buy', 'order_price_field_sell',
              'warmup_days')


class SubmitError(RuntimeError):
    """提交/浏览过程中的可预期错误（前端直接展示文案）。"""


# ── 路径安全 ────────────────────────────────────────────────────
def resolve_within(root, rel: str = '') -> Path:
    """把相对路径解析成 root 内的绝对路径；越界或不存在则 SubmitError。"""
    root_p = Path(root).resolve()
    raw = str(rel or '').strip().replace('\\', '/')
    if raw.startswith('/'):
        raw = raw.lstrip('/')
    try:
        p = (root_p / raw).resolve() if raw else root_p
    except (OSError, ValueError) as e:
        raise SubmitError(f'路径无法解析: {rel}（{e}）')
    if p != root_p and root_p not in p.parents:
        raise SubmitError(f'路径越界，只能访问 {root_p} 内的文件')
    if not p.exists():
        raise SubmitError(f'路径不存在: {p}')
    return p


def _rel(root: Path, p: Path) -> str:
    try:
        rel = p.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return ''
    return '' if rel == '.' else rel


# ── 目录浏览 ────────────────────────────────────────────────────
def browse(root, rel: str = '') -> Dict[str, Any]:
    """列出 root 内某个目录的子目录与可提交文件（单层，不递归）。

    返回 dict(root, path, rel, parent, dirs[], files[], error)
    """
    root_p = Path(root).resolve()
    try:
        cur = resolve_within(root_p, rel)
    except SubmitError as e:
        return dict(root=str(root_p), path=None, rel='', parent=None,
                    dirs=[], files=[], error=str(e))
    if not cur.is_dir():
        return dict(root=str(root_p), path=str(cur), rel=_rel(root_p, cur),
                    parent=_rel(root_p, cur.parent), dirs=[], files=[],
                    error=f'不是目录: {cur.name}')

    dirs, files = [], []
    try:
        entries = sorted(cur.iterdir(), key=lambda x: x.name.lower())
    except OSError as e:
        return dict(root=str(root_p), path=str(cur), rel=_rel(root_p, cur),
                    parent=_rel(root_p, cur.parent), dirs=[], files=[],
                    error=f'无法读取目录: {e}')

    for ent in entries:
        name = ent.name
        if ent.is_dir():
            if name.startswith('.') or name in SKIP_DIRS:
                continue
            dirs.append(dict(name=name, rel=_rel(root_p, ent)))
        elif ent.is_file() and ent.suffix.lower() in STRATEGY_SUFFIXES:
            try:
                st = ent.stat()
            except OSError:
                continue
            files.append(dict(name=name, rel=_rel(root_p, ent),
                              size=st.st_size, mtime=st.st_mtime))

    parent = None
    if cur != root_p:
        parent = _rel(root_p, cur.parent)
    return dict(root=str(root_p), path=str(cur), rel=_rel(root_p, cur),
                parent=parent, dirs=dirs, files=files, error=None)


# ── 标的池 ──────────────────────────────────────────────────────
def parse_instruments(text) -> List[str]:
    """标的池文本 → 代码列表；「全市场」预设原样保留，跑批时才展开。

    一般不用填：策略里声明了就用声明，识别不出则默认全市场（见 runner）。
    这里只做解析，不查库。
    """
    if isinstance(text, (list, tuple)):
        return [str(x) for x in text]
    return [x for x in re.split(r'[\s,;]+', str(text or '').strip()) if x]


def pool_label(instruments) -> str:
    """标的池的展示名：预设显示「全市场股票」，其余显示「N 只」。"""
    items = as_instruments(instruments)
    if len(items) == 1 and pool_preset(items[0]):
        return items[0]
    return f'{len(items)} 只' if items else '未指定'


# ── 策略探针 ────────────────────────────────────────────────────
def probe(path, browse_root=None) -> Dict[str, Any]:
    """解析策略文件，返回提交表单可预填的参数 + 静态检查提示。"""
    try:
        p = resolve_within(browse_root, path) if browse_root else Path(path).resolve()
    except SubmitError as e:
        return dict(error=str(e), missing=[], notes=[])
    if p.suffix.lower() not in STRATEGY_SUFFIXES:
        return dict(error=f'只支持 {"/".join(STRATEGY_SUFFIXES)} 文件', missing=[],
                    notes=[])

    # rel 供提交表单回传（source）：与 browse() 返回的 rel 同构，否则填表后
    # source 为空、提交时 resolve 到 browse_root 本身，后缀检查就会误报。
    rel = _rel(Path(browse_root).resolve(), p) if browse_root else p.name
    out: Dict[str, Any] = dict(path=str(p), rel=rel, file=p.name,
                               suffix=p.suffix, missing=[], notes=[], error=None)
    try:
        info = StrategySpec.inspect(str(p))
    except Exception as e:  # 语法错误 / 导入失败 / 执行期异常
        out['error'] = f'{type(e).__name__}: {e}'
        return out

    meta = info['meta']
    # 预热窗口：策略里显式写了（STRATEGY / meta.json / run(warmup_days=…)）
    # 就带出来预填；没写则留空，由提交人确认后填写（见 _warmup_hint）
    auto = bool(meta.get('_warmup_auto'))
    explicit = None if auto else (int(meta['warmup_days'])
                                  if meta.get('warmup_days') else None)
    hint = int(meta.get('warmup_days') or 0) if auto else \
        detect_warmup_days(info.get('source_text') or '')
    out.update(
        strategy_id=str(meta.get('strategy_id') or p.stem),
        strategy_name=str(meta.get('strategy_name') or p.stem),
        capital_base=meta.get('capital_base'),
        instruments=as_instruments(meta.get('instruments')),
        pool=pool_label(meta.get('instruments')),
        benchmark=meta.get('benchmark') or '000300.SH',
        warmup_days=explicit,
        warmup_days_hint=hint,
        order_price_field_buy=meta.get('order_price_field_buy') or 'open',
        order_price_field_sell=meta.get('order_price_field_sell') or 'open',
        callbacks=[k for k in ('initialize', 'before_trading_start',
                               'handle_data', 'after_trading')
                   if callable(meta.get(k))],
        has_run_call=bool(info['captured']),
        missing=info['missing'],
    )
    if info['captured']:
        out['notes'].append(
            '文件末尾的 bigtrader.run(...) 已在导入时被拦下，'
            '模拟盘由看板按日调用引擎')
    if explicit:
        out['notes'].append(f'行情预热窗口 {explicit} 自然日（策略里显式指定）')
    else:
        out['notes'].append(
            f'策略里没写预热窗口 —— 按源码回看窗口推断需 ≥ {hint} 自然日，'
            f'请确认后填进「预热天数」再提交')
    if scan_source(info.get('source_text') or '')['uses_trading_day_index']:
        out['notes'].append(
            'ℹ 代码里用到 trading_day_index：模拟盘已跨日携带该序号（首日 0，'
            '每交易日 +1，由 paper.runner 从上一条绩效注入引擎），故 '
            'trading_day_index % N 类调仓调度与「以该策略自身首日为起点的'
            '回测」节拍一致。仅当你的回测从更早日期起步、依赖其绝对序号时'
            '两者相位才会错开')
    if meta.get('_instruments_auto'):
        out['notes'].append(
            f'标的池按 run(market=…) 自动判定为「{out["pool"]}」，'
            f'跑批时展开成具体代码（全市场策略取数较慢，首次跑批请耐心等）')
    return out


# ── 提交落盘 ────────────────────────────────────────────────────
def submit(source_path, params: Dict[str, Any], strategies_dir,
           browse_root=None, store=None) -> Dict[str, Any]:
    """复制策略文件到 strategies/ 并写 <stem>.meta.json，返回落库信息。

    params 里的键（_META_KEYS）会写进 meta.json，覆盖文件内同名参数。
    ``store`` 给定时同步建账户：看板提交成功后立刻跳任务详情页，若等后台
    跑批线程注册，页面会有一段时间 404「任务不存在」（账户是跑批时才建的）。
    """
    src = resolve_within(browse_root, source_path) if browse_root \
        else Path(source_path).resolve()
    if src.suffix.lower() not in STRATEGY_SUFFIXES:
        raise SubmitError(f'只支持 {"/".join(STRATEGY_SUFFIXES)} 文件')
    if not src.is_file():
        raise SubmitError(f'不是文件: {src}')

    params = dict(params or {})
    strategy_id = str(params.get('strategy_id') or src.stem).strip()
    if not strategy_id:
        raise SubmitError('任务名称/策略 ID 不能为空')
    try:
        capital_base = float(params.get('capital_base'))
    except (TypeError, ValueError):
        raise SubmitError('初始资金必须是数字')
    if capital_base <= 0:
        raise SubmitError('初始资金必须大于 0')
    # 标的池一般不用填：用策略自己声明的；识别不出则由 runner 默认全市场
    instruments = parse_instruments(params.get('instruments'))
    if not instruments:
        try:
            info = StrategySpec.inspect(str(src))
            instruments = as_instruments(info['meta'].get('instruments'))
        except Exception:
            instruments = []

    warmup = params.get('warmup_days')
    if warmup in (None, ''):
        # 提交人没填 → 策略里显式写了就用它，否则必须明确给出（不静默兜底：
        # 窗口不够会让因子算成 NaN、策略静默不下单）
        try:
            info = StrategySpec.inspect(str(src))
            if not info['meta'].get('_warmup_auto'):
                warmup = info['meta'].get('warmup_days')
        except Exception:
            warmup = None
    try:
        warmup = int(warmup)
    except (TypeError, ValueError):
        raise SubmitError('请填写预热天数（行情窗口前推自然日）')
    if warmup <= 0:
        raise SubmitError('预热天数必须是正整数')

    sdir = Path(strategies_dir).resolve()
    sdir.mkdir(parents=True, exist_ok=True)
    dst = sdir / f'{src.stem}{src.suffix}'
    if dst.resolve() != src:
        shutil.copy2(src, dst)

    meta = {k: params[k] for k in _META_KEYS if params.get(k) is not None}
    meta['strategy_id'] = strategy_id
    meta['strategy_name'] = str(params.get('strategy_name') or strategy_id)
    meta['capital_base'] = capital_base
    if instruments:
        meta['instruments'] = instruments
    meta['warmup_days'] = warmup
    meta['source_file'] = str(src)
    meta_path = meta_json_path(dst)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding='utf-8')

    # 落盘后立刻按正式口径加载一次：能加载成功才允许建任务
    spec = StrategySpec.load(str(dst))
    if store is not None and store.get_strategy(spec.strategy_id) is None:
        # 同步建账户（策略已存在则不动，资金口径冲突留给跑批报错）
        register_spec(store, spec)
    return dict(path=str(dst), meta_path=str(meta_path),
                strategy_id=spec.strategy_id, strategy_name=spec.strategy_name,
                capital_base=spec.capital_base, benchmark=spec.benchmark,
                instruments=spec.instruments, warmup_days=spec.warmup_days,
                pool=pool_label(spec.instruments),
                instrument_count=len(spec.instruments),
                source_file=str(src))
