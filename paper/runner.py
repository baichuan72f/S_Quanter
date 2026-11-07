"""
单日跑批主流程 —— 本地模拟盘的 PaperTradingRunner
==================================================

对应平台 PaperTradingRunner（_pre_run 状态装载 → 跑当日一根 bar → _post_run
save_state）。运行模式由调用方显式指定（平台用环境变量 RUN_MODE，本地是
函数参数），撮合/除权除息等引擎语义与回测完全共用同一个 smalltrader。

每日时间线（A 股日频，方案文档第 5 节）：
  T 日 15:00 后日线数据到位 → run_one_day(strategy_id, T)：
    装载 T-1 状态 → 数据校验 → 单日引擎（撮合昨日计划单 + 生成 T 日信号）
    → 增量绩效 → 状态落库 → 输出 T+1 委托清单。

策略契约（strategies/*.py 或 *.ipynb）：
    STRATEGY = dict(
        strategy_id='demo', strategy_name='示例', capital_base=1_000_000,
        instruments=[...], benchmark='000300.SH',       # 可选，默认 000300.SH
        order_price_field_buy='open', order_price_field_sell='open',
        warmup_days=365,                                 # 可选，行情预热自然日
        initialize=..., before_trading_start=...,        # 平台同形回调
        handle_data=..., after_trading=...,
    )

两种文件都支持（提交任务时两者都会 copy 进 strategies/）：
  .py     有 STRATEGY dict 的直接用；脚本式（模块末尾调 bigtrader.run）的
          参数从 run 调用里捕获
  .ipynb  code cell 拼接后 exec；同样拦下末尾的 bigtrader.run
没写 STRATEGY 的（含 notebook）由同目录 <同名>.meta.json 提供参数，优先级
见 StrategySpec 文档串。

注意平台语义：模拟盘每日冷启动、initialize 每天重跑 —— 策略里跨日状态必须放
context.user_store（本地为普通 dict，每日从状态库装回）。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / 'engine')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402

import smalltrader as bt  # noqa: E402
import bqcompat  # noqa: E402 - BigQuant 平台包名兼容层（bigquant/dai/bigmodule）
from . import checks, state  # noqa: E402
from .constants import RUN_MODE_PAPERTRADING  # noqa: E402
from .local_data import LocalParquetDataSource  # noqa: E402
from .notify import write_order_list  # noqa: E402
from .perf import evaluate as evaluate_perf  # noqa: E402
from .store import StateStore  # noqa: E402

DEFAULT_WARMUP_DAYS = 365      # 覆盖 200 交易日因子预热（与自动取数一致）
DEFAULT_BENCHMARK = '000300.SH'


# 策略参数键（提交表单 / <stem>.meta.json / STRATEGY dict 共用）
_PARAM_KEYS = ('strategy_id', 'strategy_name', 'capital_base', 'instruments',
               'benchmark', 'order_price_field_buy', 'order_price_field_sell',
               'warmup_days')
_CALLBACK_KEYS = ('initialize', 'before_trading_start', 'handle_data',
                  'after_trading')
# 平台风格标的代码（用于从模块常量里猜标的池）
_INSTRUMENT_RE = re.compile(r'^\d{6}\.(SH|SZ|SHA|SZA|SHE|SSE)$')


class RunStopped(Exception):
    """跑批被用户中止（看板「停止部署任务」）。

    只在日与日之间、以及单日内的几个检查点抛出：一天跑到一半停下来会留下
    写了一半的账户状态，所以宁可把当天跑完，也不做日内中断。
    """


def meta_json_path(path) -> Path:
    """策略文件对应的参数文件：strategies/x.py|ipynb → strategies/x.meta.json。"""
    return Path(path).with_suffix('.meta.json')


def _read_meta_json(path: Path) -> Dict[str, Any]:
    mp = meta_json_path(path)
    if not mp.exists():
        return {}
    try:
        data = json.loads(mp.read_text(encoding='utf-8'))
    except (ValueError, OSError) as e:
        raise ValueError(f'策略参数文件损坏: {mp}（{e}）')
    return data if isinstance(data, dict) else {}


def _unify_engine_module(bt) -> None:
    """让包式写法 ``import engine.smalltrader`` 与顶层 ``import smalltrader``
    共用同一份模块实例。

    策略文件常用包式写法（``import engine.smalltrader as bigtrader``）。若真去
    导入 engine.smalltrader，sys.modules 里会多出一个键、一个独立对象，
    _CaptureRun 只换掉顶层模块的 run，拦不住它 —— 「读元信息」就变成真跑一遍
    完整回测（看板列表页每次刷新卡十几秒，日志停在 prepare data ..）。
    预先登记别名即可，两处 run 从此是同一个函数属性。
    """
    sys.modules.setdefault('engine.smalltrader', bt)


class _CaptureRun:
    """执行策略文件期间把 smalltrader.run 换成捕获式 no-op。

    策略程序（尤其 notebook 与脚本式 .py）常在模块末尾直接调用
    ``bigtrader.run(...)`` 跑一次回测；导入成模拟盘策略时必须拦下来，
    否则一 import 就启动完整回测。捕获到的 kwargs 同时作为参数缺省值
    来源（capital_base / 价格字段 / 回调函数等）。

    兜底：万一 engine.smalltrader 已被别的写法先导入过（别名没生效，存在
    两份实例），这里连同已加载的同名模块一起替换，避免漏拦。
    """

    def __init__(self, bt):
        self.bt = bt
        self.kwargs: Dict[str, Any] = {}
        self._orig = None
        self._patched: List[Tuple[Any, Any]] = []

    def __enter__(self):
        self._orig = self.bt.run
        captured = self.kwargs

        # notebook 常在末尾对 run() 的返回值再调 .render() / .get_stats()。
        # run() 被捕获时若直接回 None，exec 阶段就会抛
        # ``NoneType has no attribute 'render'``。给个 no-op 占位对象，让
        # 捕获式执行对这类 notebook 也稳健（render 逻辑不在这里跑）。
        class _RunStub:
            def render(self, *a, **k): return None
            def get_stats(self, *a, **k): return {}
            def __repr__(self): return '<captured run>'

        def _noop_run(*args, **kwargs):
            captured.update(kwargs)
            return _RunStub()

        for name, mod in list(sys.modules.items()):
            if mod is None or mod is self.bt or not hasattr(mod, 'run'):
                continue
            if name == 'smalltrader' or name.endswith('.smalltrader'):
                self._patched.append((mod, mod.run))
                mod.run = _noop_run

        self.bt.run = _noop_run
        return self

    def __exit__(self, *exc):
        self.bt.run = self._orig
        for mod, orig in self._patched:
            mod.run = orig
        self._patched = []
        return False


# IPython 魔法命令：%time / %matplotlib inline / %%time / !pip ...
# 只认「% 或 %% 紧跟标识符」，避免误伤行首的取模运算符（如 "% (a, b)" 续行）
_MAGIC_RE = re.compile(r'^\s*(?:%{1,2}[A-Za-z_]|!)')

# 默认预热 365 自然日大约覆盖这么多个交易日（含长假与停牌）
DEFAULT_WARMUP_TRADING_DAYS = 240


def _const_int(node) -> int:
    return node.value if isinstance(node, ast.Constant) \
        and isinstance(node.value, int) else 0


def _call_lookback(node: ast.Call) -> int:
    """从一次调用里读回看窗口（交易日）。认得出才返回，认不出给 0。"""
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else (
        fn.id if isinstance(fn, ast.Name) else '')
    args = list(node.args)
    kw = {k.arg: k.value for k in node.keywords if k.arg}
    if name == 'add_trading_days' and len(args) >= 2:
        n = args[1]                        # add_trading_days(date, -N)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return _const_int(n.operand)
        return 0
    if name == 'history':                  # data.history(inst, 'close', N, '1d')
        cands = [args[2]] if len(args) >= 3 else []
        cands += [kw[k] for k in ('bar_count', 'count') if k in kw]
        return max((_const_int(c) for c in cands), default=0)
    if name in ('rolling', 'tail', 'head'):
        cands = list(args[:1]) + [kw[k] for k in ('window',) if k in kw]
        return max((_const_int(c) for c in cands), default=0)
    return 0


def scan_source(source_text: str) -> Dict[str, Any]:
    """静态扫策略源码：最长回看窗口 + 是否真的用到 trading_day_index。

    用 AST 而非正则 —— 注释/文档串里提到这些词不该算数（`dual_ma_demo`
    的文档串就写着「不要依赖 trading_day_index」，正则会把这条正经建议
    误报成告警）。
    """
    out: Dict[str, Any] = {'lookback': 0, 'uses_trading_day_index': False}
    try:
        tree = ast.parse(source_text or '')
    except (SyntaxError, ValueError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == 'trading_day_index':
            out['uses_trading_day_index'] = True
        elif isinstance(node, ast.Name) and node.id == 'trading_day_index':
            out['uses_trading_day_index'] = True
        elif isinstance(node, ast.Call):
            out['lookback'] = max(out['lookback'], _call_lookback(node))
    return out


def _source_text(path: Path) -> str:
    """策略源码（.ipynb 为拼接后的 code cell）。"""
    return (_notebook_source(path) if path.suffix == '.ipynb'
            else path.read_text(encoding='utf-8', errors='replace'))


def detect_warmup_days(source_text: str) -> int:
    """从策略源码推断行情预热窗口（自然日）。

    平台模拟盘的回看窗口由数据层自动补齐，本地取数得自己定。策略里显式
    写了的（``bigtrader.run(warmup_days=...)`` / STRATEGY / meta.json）优先，
    这里只在其都缺席时兜底：扫描源码里最长的回看窗口（交易日），换算成自然日
    并留余量。**宁可多取**（只是取数慢一点），不可少取 —— 窗口不够时因子会
    算成 NaN，策略静默不下单，不报错但结果全错。
    """
    longest = scan_source(source_text)['lookback']
    if longest <= DEFAULT_WARMUP_TRADING_DAYS:
        return DEFAULT_WARMUP_DAYS
    return int(longest * 1.55) + 30       # 交易日 → 自然日（含长假余量）


# notebook「环境专用」代码单元的标记：这些单元只在 VS Code / Jupyter / Colab 里
# 有意义（取当前 cell 的元数据、读本地 notebook 路径、挂载盘等），当脚本执行必然
# 报错（如 `get_ipython()` 返回 None → `'NoneType' object has no attribute
# 'get_parent'`）。克隆自平台的 notebook 里常残留这类单元，跑批时应跳过。
_NB_ENV_MARKERS = ('get_ipython(', 'vscode-notebook-cell://', 'google.colab',
                   'nbformat.read(')
# 只要单元里出现这些，就认定它在定义策略本体，不能跳
_NB_KEEP_MARKERS = ('def handle_data', 'def initialize', 'def before_trading_start',
                    'bigtrader.run(', 'M.bigtrader', 'STRATEGY')

# 最近一次 _notebook_source 跳过的环境单元数（供日志提示）
LAST_SKIPPED_CELLS = 0

# 「只由一个名字/属性链构成」的单元 = notebook 的展示单元
_DISPLAY_CELL_RE = re.compile(r'[A-Za-z_][\w\.]*(?:\[\d+\])?')


def _notebook_source(path: Path) -> str:
    """拼接 .ipynb 全部 code cell 源码（仅供静态分析：warmup 探测、源码扫描）。

    执行路径不再用整段 exec，见 _exec_notebook（逐格执行 + 环境单元容错）；
    这里保留全量拼接，避免静态分析漏掉被容错跳过的单元里的窗口参数。
    """
    return '\n\n'.join(src for _idx, src in _notebook_cells(path))


def _is_env_only_cell(body: str) -> bool:
    if not any(mark in body for mark in _NB_ENV_MARKERS):
        return False
    return not any(mark in body for mark in _NB_KEEP_MARKERS)


def _notebook_cells(path: Path):
    """逐个 yield (单元序号, 源码)，只做魔法行清理，不做整格丢弃。"""
    nb = json.loads(path.read_text(encoding='utf-8'))
    for idx, cell in enumerate(nb.get('cells', [])):
        if cell.get('cell_type') != 'code':
            continue
        src = ''.join(cell.get('source') or [])
        lines = [ln for ln in src.splitlines() if not _MAGIC_RE.match(ln)]
        if lines:
            yield idx, '\n'.join(lines)


def _exec_notebook(module, path: Path) -> int:
    """逐格 exec notebook；环境专用单元真报错时才跳过它。

    为什么不预先整格丢弃：这类单元里常顺带定义了后面要用的变量（`desc`、
    `path` 之类），整格扔掉会让后续单元 NameError。这里按「先执行、失败了
    且确认是环境类单元」的顺序处理，保留报错之前已生效的部分。

    返回跳过的单元数。
    """
    skipped = 0
    for idx, src in _notebook_cells(path):
        # 「展示单元」：整个单元只有一个变量名/属性链（notebook 里靠它把结果打印
        # 出来）。当脚本跑没有意义，变量未定义时还会 NameError，直接跳过。
        if _DISPLAY_CELL_RE.fullmatch(src.strip()):
            continue
        try:
            exec(compile(src, f'{path}#cell{idx}', 'exec'), module.__dict__)
        except Exception as exc:  # noqa: BLE001
            env_related = _is_env_only_cell(src) or (
                isinstance(exc, (NameError, AttributeError))
                and any(m in src for m in _NB_ENV_MARKERS))
            if env_related:
                skipped += 1
                print(f'[WARN] 跳过 notebook 第 {idx} 个代码单元（环境专用，'
                      f'{type(exc).__name__}: {exc}）')
                continue
            if isinstance(exc, ModuleNotFoundError):
                raise _missing_dep_error(exc) from exc
            raise
    return skipped


def _load_module(path: Path) -> Tuple[Any, Dict[str, Any]]:
    """执行策略文件，返回 (module, 捕获到的 bigtrader.run kwargs)。

    .py 走 importlib（与原实现一致）；.ipynb 抽取 code cell 后 exec 到模块
    命名空间。两者都在 smalltrader.run 被替换为 no-op 的上下文里执行。

    先 import 本项目的 smalltrader / queries / data_source，使 notebook 里
    ``import smalltrader as bigtrader`` 一定命中本工程 engine/smalltrader.py，
    而不是 notebook 自己（如 examples/）或其它路径下的同名文件（notebook
    里的 sys.path.insert 会把 CWD 插到最前，存在串味风险）。
    """
    import smalltrader as bt          # engine/ 已在本模块 sys.path 中
    import data_source              # noqa: F401  engine/data_source.py
    import queries                  # noqa: F401  项目根 queries.py

    # 平台原味策略（克隆自 BigQuant）写的是平台包名：
    #   from bigquant import bigtrader, dai
    #   from bigmodule import M
    #   from bigtrader.finance.commission import PerOrder
    # 这些包本地没有，会抛 ModuleNotFoundError。装兼容层把它们指到本地实现
    # （bigtrader → 本引擎模块，dai → 本地 DuckDB，M.bigtrader.vNN → run）。
    # 必须在 exec 策略之前装：策略 import 发生在模块执行阶段。
    bqcompat.install(bt)

    # 包式写法（import engine.smalltrader）与顶层写法共用同一份模块，
    # 否则 no-op 拦截会漏掉前者的 run —— 一次「读元信息」= 一次完整回测。
    _unify_engine_module(bt)

    name = f'strategy_{path.stem}'
    if path.suffix == '.ipynb':
        module = types.ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        with _CaptureRun(bt) as cap:
            _exec_notebook(module, path)
        return module, cap.kwargs

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with _CaptureRun(bt) as cap:
        try:
            spec.loader.exec_module(module)
        except ModuleNotFoundError as e:
            raise _missing_dep_error(e) from e
    return module, cap.kwargs


def _missing_dep_error(exc: ModuleNotFoundError) -> ImportError:
    """把裸的 ModuleNotFoundError 翻成「装哪个包、跑哪条命令」。

    平台策略 import 的包（scipy 等）随包 .venv 默认没装；直接抛原异常时，看板
    与命令行只显示一行 `No module named 'scipy'`，看不出该怎么修。
    """
    mod = (exc.name or '').split('.')[0]
    if not mod:
        return ImportError(str(exc))
    single = f'.venv\\Scripts\\python.exe -m pip install {mod}'
    return ImportError(
        f"策略需要第三方包 '{mod}'，但当前 .venv 里没装。\n"
        f"  · 只装这一个：{single}\n"
        f"  · 一次装齐常用依赖（推荐，需联网，约 300 MB）：双击 install_deps.bat，或执行\n"
        f"      .venv\\Scripts\\python.exe -m pip install -r requirements-extra.txt "
        f"-c constraints.txt\n"
        f"  （随包 .venv 只带 pandas/numpy/duckdb；清单见 requirements-extra.txt）")


def _guess_instruments(module) -> Optional[List[str]]:
    """从模块命名空间猜标的池（如 OFFENSIVE_ETFS / INSTRUMENTS 常量）。"""
    for key, val in vars(module).items():
        if key.startswith('_') or not isinstance(val, (list, tuple, set)) or not val:
            continue
        items = [str(x) for x in val]
        if all(_INSTRUMENT_RE.match(x) for x in items):
            return items
    return None


def _default_pool(captured: Dict[str, Any]) -> str:
    """识别不出标的池时的默认：按 run(market=…) 判断，缺省全市场股票。

    notebook 常写 ``bigtrader.run(market=bigtrader.Market.CN_FUND)`` 跑全市场
    —— 本地用「全市场股票 / 全市场基金」预设表达，跑批时再展开成具体代码。
    """
    market = captured.get('market')
    val = str(getattr(market, 'value', market) or '').lower()
    return '全市场基金' if 'fund' in val else '全市场股票'


def as_instruments(val) -> List[str]:
    """标的池归一化：list/tuple 原样；字符串按换行/逗号/空格切分。

    ⚠ 字符串不可直接迭代 —— 「全市场股票」这种预设必须整体保留，
    否则会被逐字拆成 ['全','市','场','股','票']。
    """
    if isinstance(val, str):
        return [x for x in re.split(r'[\s,;]+', val) if x]
    return [str(x) for x in (val or [])]


class StrategySpec:
    """策略模块的规格。

    参数来源优先级（后者覆盖前者）：
        捕获的 bigtrader.run kwargs → 模块属性 → 文件内 STRATEGY dict
        → 同目录 <stem>.meta.json → load(params=...) 显式传入
    ``.py`` 里写了 STRATEGY 的按老规矩来；``.ipynb``（以及没写 STRATEGY 的
    ``.py``）由提交任务时生成的 ``<stem>.meta.json`` 补齐参数。
    """

    def __init__(self, module, source: str, meta: Dict[str, Any]):
        if not isinstance(meta, dict) or not callable(meta.get('handle_data')):
            raise ValueError(
                f'{source} 缺少可用的策略定义：需提供 handle_data。'
                f'.py 可写 STRATEGY dict，或由「提交任务」生成 '
                f'{meta_json_path(Path(source)).name}')
        missing = [k for k in ('strategy_id', 'capital_base', 'instruments')
                   if meta.get(k) in (None, '', [])]
        if missing:
            raise ValueError(
                f'{source} 缺少必要参数: {", ".join(missing)}'
                f'（可在提交任务时填写，或写进 STRATEGY dict）')
        self.module = module
        self.source = source
        self.meta = dict(meta)
        self.strategy_id = str(meta['strategy_id'])
        self.strategy_name = str(meta.get('strategy_name') or self.strategy_id)
        self.capital_base = float(meta['capital_base'])
        self.instruments: List[str] = as_instruments(meta['instruments'])
        self.benchmark = str(meta.get('benchmark') or DEFAULT_BENCHMARK)
        self.order_price_field_buy = str(
            meta.get('order_price_field_buy') or 'open')
        self.order_price_field_sell = str(
            meta.get('order_price_field_sell') or 'open')
        self.warmup_days = int(meta.get('warmup_days') or DEFAULT_WARMUP_DAYS)
        self.initialize = meta.get('initialize')
        self.before_trading_start = meta.get('before_trading_start')
        self.handle_data = meta['handle_data']
        self.after_trading = meta.get('after_trading')

    @staticmethod
    def _compose_meta(module, captured: Dict[str, Any], path: Path,
                      params: Dict[str, Any],
                      source_text: Optional[str] = None) -> Dict[str, Any]:
        meta: Dict[str, Any] = {}

        # 1. 捕获到的 run(...) kwargs（脚本式/notebook 策略的参数来源）
        #    instruments 也在这里：平台写法的标的池常藏在
        #    ``M.bigtrader.vNN(data={"instruments": [...]})`` 里，由兼容层提取后
        #    以 run(instruments=...) 的形式出现在捕获 kwargs 中。
        for k in ('capital_base', 'benchmark', 'order_price_field_buy',
                  'order_price_field_sell', 'warmup_days', 'instruments',
                  *_CALLBACK_KEYS):
            if captured.get(k) is not None:
                meta[k] = captured[k]

        # 2. 模块属性里的回调（只定义了函数、没调 run 的脚本式 .py）
        #    不覆盖第 1 步已捕获的：平台原味策略的回调可能在捕获阶段被包装过
        #    （bqcompat 给回调套壳以注入 context.data），用模块级原函数覆盖
        #    会把包装丢掉。
        for k in _CALLBACK_KEYS:
            fn = getattr(module, k, None)
            if callable(fn) and k not in meta:
                meta[k] = fn

        # 3. 文件内 STRATEGY dict
        declared = getattr(module, 'STRATEGY', None)
        if isinstance(declared, dict):
            meta.update(declared)

        # 4. 提交任务时生成的参数文件
        meta.update(_read_meta_json(path))

        # 5. 显式传入（提交表单）
        meta.update({k: v for k, v in (params or {}).items() if v is not None})

        # 缺省补齐
        if not meta.get('strategy_id'):
            meta['strategy_id'] = path.stem
        if not meta.get('strategy_name'):
            meta['strategy_name'] = str(meta['strategy_id'])
        if not meta.get('instruments'):
            guessed = _guess_instruments(module)
            if guessed:
                meta['instruments'] = guessed
            else:
                # 都识别不出 → 默认全市场（按 run(market=…) 判股票还是基金）
                meta['instruments'] = _default_pool(captured)
                meta['_instruments_auto'] = True
        if not meta.get('warmup_days'):
            # 谁都没指定 → 按源码里的回看窗口自动推断（平台侧由数据层代劳）
            if source_text is None:
                source_text = _source_text(path)
            meta['warmup_days'] = detect_warmup_days(source_text)
            meta['_warmup_auto'] = True
        return meta

    @classmethod
    def load(cls, path: str, params: Dict[str, Any] = None) -> 'StrategySpec':
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f'策略文件不存在: {p}')
        if p.suffix not in ('.py', '.ipynb'):
            raise ValueError(
                f'不支持的策略文件类型: {p.suffix}（仅 .py / .ipynb）')
        module, captured = _load_module(p)
        return cls(module, str(p), cls._compose_meta(module, captured, p,
                                                     params or {}))

    @classmethod
    def inspect(cls, path: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """解析策略文件但**不强制完整** —— 缺参数也返回，供提交表单预填。

        返回 dict(meta, captured, missing, source_text)：
            meta        合成后的参数（可能缺 strategy_id/capital_base/instruments）
            captured    从 bigtrader.run(...) 捕获到的 kwargs
            missing     仍缺的必要参数名列表
            source_text 策略源码（.ipynb 为拼接后的 code cell），用于静态检查
        """
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f'策略文件不存在: {p}')
        if p.suffix not in ('.py', '.ipynb'):
            raise ValueError(
                f'不支持的策略文件类型: {p.suffix}（仅 .py / .ipynb）')
        module, captured = _load_module(p)
        source_text = _source_text(p)
        meta = cls._compose_meta(module, captured, p, params or {}, source_text)
        missing = [k for k in ('strategy_id', 'capital_base', 'instruments')
                   if meta.get(k) in (None, '', [])]
        if not callable(meta.get('handle_data')):
            missing.append('handle_data')
        return dict(meta=meta, captured=captured, missing=missing,
                    source_text=source_text)


def register_spec(store, spec: StrategySpec) -> Dict[str, Any]:
    """把策略规格登记成状态库账户（已存在则按传入字段更新）。

    提交任务时同步调用一次：看板提交成功后立刻跳 /tasks/<id>，若等后台跑批
    线程进 run_one_day 才建账户，详情页这段窗口查不到策略，会 404
    「任务 xxx 不存在」。PaperRunner 里也复用本函数，字段只有这一份定义。
    """
    return store.upsert_strategy(
        strategy_id=spec.strategy_id,
        strategy_name=spec.strategy_name,
        capital_base=spec.capital_base,
        benchmark_instrument=spec.benchmark,
        market='cn_stock_fund',
        frequency='1d',
        order_price_field_buy=spec.order_price_field_buy,
        order_price_field_sell=spec.order_price_field_sell,
        config={'instruments': spec.instruments,
                'source': spec.source,
                'warmup_days': spec.warmup_days})


class PaperRunner:
    """单日跑批器。一个进程跑一个交易日，跑完即退（与平台同构）。"""

    def __init__(self, store: StateStore,
                 ds: LocalParquetDataSource = None,
                 out_dir: str = None,
                 should_stop: Callable[[], bool] = None):
        self.store = store
        self.ds = ds or LocalParquetDataSource()
        # T+1 委托清单输出目录；None = 默认 notifications/<strategy_id>/
        # （测试用临时目录隔离，避免覆盖真实清单）
        self.out_dir = out_dir
        # 中止询问（看板「停止部署任务」）：返回 True 则在下一个检查点抛 RunStopped。
        # 只在这几处「天与天/阶段与阶段」的边界检查，不做日内中断 ——
        # 单日跑到一半停下会在状态库里留下写了一半的账户。
        self.should_stop = should_stop

    def _check_stop(self) -> None:
        if self.should_stop is not None and self.should_stop():
            raise RunStopped('已按请求停止部署任务（当前交易日未受影响）')

    # ── 策略注册 ────────────────────────────────────────────────
    def register_strategy(self, spec: StrategySpec) -> Dict[str, Any]:
        return register_spec(self.store, spec)

    # ── 单日主流程 ──────────────────────────────────────────────
    def run_one_day(self, spec: StrategySpec, trading_day: str,
                    skip_checks: bool = False) -> Dict[str, Any]:
        td = str(trading_day)
        logs: List[Dict[str, str]] = []

        def note(msg: str, level: str = 'INFO'):
            logs.append({'level': level, 'msg': msg})
            print(f'[{level}] {msg}')

        self._check_stop()
        cfg = self.store.get_strategy(spec.strategy_id)
        if cfg is None:
            self.register_strategy(spec)
            note(f'注册策略 {spec.strategy_id}（初始资金 {spec.capital_base:,.0f}）')
        elif abs(float(cfg['capital_base']) - spec.capital_base) > 1e-6:
            raise ValueError('capital_base 与状态库不一致，请检查策略定义')

        # ── 1. 装载 T-1 状态 ────────────────────────────────────
        ls = state.load_state(self.store, spec.strategy_id, td)
        st = ls['state']
        prev_day = ls['prev_day']
        if ls['is_first_day']:
            note('首日运行：以 capital_base 起始，无前序状态')
        else:
            note(f'装载 T-1 状态：{prev_day}（现金 {st["cash"]:,.2f}，'
                 f'持仓 {len(st["positions"])} 只，计划单 {len(st["planned_orders"])} 笔）')
            # 缺口告警（不阻断）：漏跑那天的信号已错过，净值曲线有跳跃
            days = self.ds.get_trading_days(prev_day, td)
            if len(days) > 2:
                note(f'⚠ 状态缺口：{prev_day} 与 {td} 之间存在未跑的交易日，'
                     f'漏跑信号已错过，净值曲线将出现跳跃', 'WARN')

        # 标的池：把「全市场股票/基金」预设展开成具体代码（实例内缓存，一次查询）
        instruments = self.ds.expand_pool(spec.instruments)
        if not instruments:
            raise checks.DataCheckError(
                f'{spec.strategy_id} 标的池为空（预设展开后无标的），中止跑批')

        # ── 2. 数据完整性校验（不过即中止，不留快照） ───────────
        held = [p['instrument'] for p in st['positions']]
        if not skip_checks:
            checks.check_data_ready(self.ds, trading_day=td,
                                    instruments=instruments,
                                    held_instruments=held)
            note('数据完整性校验通过')

        # ── 3. 取数（单日模式必须前推预热窗口，因子/均线才有历史） ──
        qs = (pd.Timestamp(td) - pd.Timedelta(days=spec.warmup_days)) \
            .strftime('%Y-%m-%d')
        universe = list(dict.fromkeys(instruments + held))
        data = self.ds.get_bar_data(universe, qs, td)
        idx_df = self.ds.get_index_bar_data(spec.benchmark, qs, td)
        if idx_df.empty:
            raise checks.DataCheckError(
                f'{td} 基准 {spec.benchmark} 无行情，中止跑批')
        data[spec.benchmark] = idx_df
        dividend_events = self.ds.get_dividend_events(universe, qs, td)
        instrument_names = self.ds.get_instrument_names(universe)
        note(f'取数完成：{len(data)} 标的，窗口 {qs} ~ {td}，'
             f'分红事件 {len(dividend_events)} 条')

        # ── 4. 单日引擎（状态注入 + 昨日计划单今日撮合） ─────────
        # 引擎启动前是最后一个廉价检查点：过了这里就让当天完整跑完（见 RunStopped）
        self._check_stop()
        # paper 跨日交易日序号：从上一条绩效递增（首日 0）。注入引擎，使
        # context.trading_day_index%N 类调仓调度与「以首日为起点的整窗回测」
        # 同节拍（否则单日窗口里恒为 0，策略会每天调仓，与回测每 N 日不符）。
        all_perfs = self.store.read_performances(spec.strategy_id)
        prev_perf = all_perfs[-1] if all_perfs else None
        trading_day_index = (int(prev_perf['trading_day_index']) + 1
                             if prev_perf else 0)
        perf = bt.run(
            instruments=universe,
            start_date=td, end_date=td,
            data=data,
            capital_base=spec.capital_base,
            initialize=spec.initialize,
            before_trading_start=spec.before_trading_start,
            handle_data=spec.handle_data,
            after_trading=spec.after_trading,
            benchmark=spec.benchmark,
            order_price_field_buy=spec.order_price_field_buy,
            order_price_field_sell=spec.order_price_field_sell,
            dividend_events=dividend_events if len(dividend_events) else None,
            instrument_names=instrument_names,
            render='none',
            run_mode=bt.RunMode.PAPER_TRADING,
            initial_positions=st['positions'],
            initial_cash=st['cash'],
            planned_orders=st['planned_orders'],
            user_store=st['user_store'],
            initial_trading_day_index=trading_day_index,
        )
        export = perf.paper_export
        note(f'当日跑完：总资产 {export["portfolio_value"]:,.2f}，'
             f'成交 {len(export["trades"])} 笔，'
             f'新计划单 {len(export["new_pending_orders"])} 笔')

        # ── 5. 增量绩效（照平台 papertradingperf 口径，当天只算当天） ──
        prev_pv = ls['prev_pv']
        if prev_pv:
            today_return = (export['portfolio_value'] - prev_pv) / prev_pv
        else:
            today_return = ((export['portfolio_value'] - spec.capital_base)
                            / spec.capital_base)
        bm_close, bm_return = self._benchmark_metrics(idx_df, td)
        day_raw = dict(
            portfolio_value=export['portfolio_value'],
            ending_cash=export['cash'],
            long_value=export['total_market_value'],
            today_return=today_return,
            today_sum_buy_value=export['today_sum_buy_value'],
            today_sum_sell_value=export['today_sum_sell_value'],
            commission=export['commission'],
            realized_pnl=export['realized_pnl'],
            profit_count=export['profit_count'],
            loss_count=export['loss_count'],
            benchmark_close=bm_close,
            benchmark_returns=bm_return,
        )
        perf_row = evaluate_perf(
            day_raw=day_raw, trading_day=td,
            trading_day_index=trading_day_index,
            capital_base=spec.capital_base,
            benchmark_instrument=spec.benchmark,
            all_performances=all_perfs)

        # ── 6. 落库（先删后写，幂等）+ T+1 清单 ────────────────
        next_day = self.ds.next_trading_day(td)
        # 重跑守卫：T 已跑过且 T+1 也跑过时，本批 T+1 新计划单写入后不会被
        # 撮合（T+1 不会重跑），会悬置 pending；先警告再落库。
        if next_day and self.store.has_run(spec.strategy_id, td) \
                and self.store.has_run(spec.strategy_id, next_day):
            note(f'⚠ 重跑 {td} 时下一交易日 {next_day} 已跑过：本批 T+1 新'
                 f'计划单将不会被撮合，状态与 {next_day} 已存快照不一致。'
                 f'如需修正历史，请 --rollback-from {next_day} 后重跑区间',
                 'WARN')
        # 引擎订单日志（提交/失败/缩量）与跑批日志合并一次写入
        # （write_run_logs 先删后写，分开写会互相覆盖）
        logs.extend({'level': lg.get('level', 'INFO'),
                     'msg': lg.get('msg', '')}
                    for lg in getattr(perf, '_order_logs', []))
        state.save_state(self.store, strategy_id=spec.strategy_id,
                         trading_day=td, next_trading_day=next_day,
                         export=export, perf_row=perf_row, logs=logs)
        list_path = None
        if next_day:
            list_path = write_order_list(self.store, spec.strategy_id,
                                         td, next_day, out_dir=self.out_dir)
            note(f'T+1 委托清单: {list_path}')
        else:
            note('交易日历未覆盖 T+1，委托清单未生成', 'WARN')
        return dict(trading_day=td, portfolio_value=export['portfolio_value'],
                    today_return=today_return,
                    planned_orders=len(export['new_pending_orders']),
                    order_list_path=list_path, perf=perf_row)

    # ── 区间补跑 ────────────────────────────────────────────────
    def run_range(self, spec: StrategySpec, start: str, end: str) -> List[dict]:
        """逐日跑 [start, end]。已跑过的交易日跳过（幂等）；断点续跑。"""
        days = self.ds.get_trading_days(start, end)
        if not days:
            raise checks.DataCheckError(f'{start}~{end} 日历内无交易日')
        results = []
        for d in days:
            # 区间补跑可能持续很久：每跨一天问一次是否该停（当天停在这一步之外）
            self._check_stop()
            if self.store.has_run(spec.strategy_id, d):
                print(f'[SKIP] {d} 已跑过')
                continue
            results.append(self.run_one_day(spec, d))
        return results

    # ── 基准 ────────────────────────────────────────────────────
    def _benchmark_metrics(self, idx_df: pd.DataFrame,
                           trading_day: str) -> tuple:
        """返回 (基准收盘, 基准日收益率)。首日/无前收盘时收益率为 0。"""
        idx = idx_df.sort_index()
        close = float(idx['close'].iloc[-1]) if len(idx) else 0.0
        ts = pd.Timestamp(trading_day)
        hist = idx[idx.index < ts]
        if hist.empty or close <= 0:
            return close, 0.0
        prev = float(hist['close'].iloc[-1])
        return close, (close / prev - 1.0) if prev > 0 else 0.0
