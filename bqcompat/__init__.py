"""BigQuant 平台包名兼容层（bqcompat）
=========================================

## 解决的问题

从 BigQuant 平台克隆下来的策略，写的是平台包名：

    from bigquant import bigtrader, dai
    import dai
    from bigmodule import M
    from bigtrader.finance.commission import PerOrder
    from bigtrader.constant import Direction, OrderType

本机没有这些包，于是 import 阶段就抛 `ModuleNotFoundError: No module named
'bigquant'`，还没走到引擎。

但**引擎本身已经是 bigtrader 的形状**（engine/smalltrader.py 里的 IContext /
IBarData / PerOrder / TradingDaysRebalance / run() 参数都逐字对齐平台）。所以
这里不重写引擎，只做两件事：

1. 把平台包名注册进 `sys.modules`，指向本地实现（`bigtrader` → 真实引擎模块，
   `dai` → 本地 DuckDB 查询层，`bigmodule.M` → 转调 `smalltrader.run`）。
2. 补齐平台侧的子模块路径与少量属性（`bigtrader.finance.commission`、
   `bigtrader.constant`、`context.capital_base`、`context.log`）。

## 用法

策略加载前调用一次即可（`paper/runner.py::_load_module` 已内置）：

    import bqcompat
    bqcompat.install()          # 自动 import engine/smalltrader 作为 bigtrader

装完后 `import bigquant` / `from bigmodule import M` 等写法与平台一致，策略文件
**不需要改任何一行**。

## 边界

只做「导入可得 + 接口转调」，不做平台功能复刻：`M.*` 的因子/训练模块、分钟级
行情、`bigquant.api` 的账号接口都是「导入不炸、真调用报清晰错误」。详见
reports/06-bq-interface-map.md 与 reports/07-bq-compat.md。
"""
from __future__ import annotations

import sys
import types
from typing import Any

__all__ = ['install', 'NotSupportedError']

# 已经装过哪些 bt 实例（重复 install 时做增量，不重复造模块）
_INSTALLED: dict = {}


class NotSupportedError(RuntimeError):
    """平台能力本地未实现。"

    只在**真正调用**平台专有功能时抛出（导入阶段不抛），避免把一个只是想
    import 的策略直接拦死。
    """


def _mk(name: str, **attrs: Any) -> types.ModuleType:
    """造/覆盖一个模块对象并登记进 sys.modules。"""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    mod.__doc__ = f'{name}（bigquant 兼容层提供，本地实现）'
    sys.modules[name] = mod
    return mod


def _noop(*a, **k):
    return None


def _install_bigtrader(bt) -> None:
    """bigtrader → 真实引擎模块，并补子模块路径。"""
    sys.modules['bigtrader'] = bt

    finance = _mk('bigtrader.finance')
    commission = _mk(
        'bigtrader.finance.commission',
        PerOrder=bt.PerOrder,
        PerContract=bt.PerContract,
        # 平台还有 PerShare（按股数计费），本地没有独立模型，按 PerContract 语义处理
        PerShare=getattr(bt, 'PerContract', bt.PerOrder),
    )
    finance.commission = commission
    bt.finance = finance

    constant = _mk(
        'bigtrader.constant',
        Direction=bt.Direction,
        OrderType=bt.OrderType,
        OrderStatus=bt.OrderStatus,
        OffsetFlag=getattr(bt, 'OffsetFlag', None),
        HedgeFlag=getattr(bt, 'HedgeFlag', None),
    )
    bt.constant = constant

    # 绩效图渲染（平台侧 bigtrader._v2.performancechartrender），本地空实现
    _v2 = _mk('bigtrader._v2')
    perf = _mk(
        'bigtrader._v2.performancechartrender',
        render=_noop,
        render_performance_chart=_noop,
        PerformanceChartRender=_noop,
    )
    _v2.performancechartrender = perf
    bt._v2 = _v2

    # 少量策略会 import bigtrader.utils
    utils = _mk('bigtrader.utils')
    bt.utils = utils


def _install_context_extras(bt) -> None:
    """给本地 IContext 补平台上有、本地没挂的属性。"""
    ctx = getattr(bt, 'IContext', None)
    if ctx is None:
        return
    if not hasattr(ctx, 'capital_base'):
        # 平台 IContext.capital_base = 初始资金；本地存在 portfolio.starting_cash
        ctx.capital_base = property(
            lambda self: float(getattr(self.portfolio, 'starting_cash', 0.0)))
    if not hasattr(ctx, 'log'):
        # 平台两种写法都存在：context.logger / context.log
        ctx.log = property(lambda self: self.logger)


def _install_bigquant(bt, dai_mod, M) -> None:
    """bigquant 包本体：`from bigquant import bigtrader, dai`。"""
    pkg = _mk('bigquant', bigtrader=bt, dai=dai_mod, __version__='local-compat')
    sys.modules['bigquant.bigtrader'] = bt
    sys.modules['bigquant.dai'] = dai_mod
    pkg.bigtrader = bt
    pkg.dai = dai_mod


def _install_stubs() -> None:
    """只用于绘图/可视化的平台包：导入可得，调用空实现。"""
    class _ChartStub:
        def __init__(self, *a, **k):
            self.options = []

        def add(self, *a, **k):
            return self

        def render(self, *a, **k):
            return self

        def show(self, *a, **k):
            return self

        def plot(self, *a, **k):
            return self

    class _LineStub(_ChartStub):
        pass

    class _BarStub(_ChartStub):
        pass

    charts = _mk('bigcharts', Chart=_ChartStub, Line=_LineStub, Bar=_BarStub,
                 render=_noop)
    sys.modules.setdefault('bigcharts', charts)

    # bigalpha：平台因子/alpha 工具，本地无实现
    # 注意：模块级 __getattr__ 只在**真正调用**时抛错，属性访问返回可调用占位。
    # 否则 `hasattr(mod, 'run')` 这类探测（paper/runner.py::_CaptureRun 会遍历
    # sys.modules 做探测）会被意外打断。
    class _AlphaStub:
        def __init__(self, path: str):
            self._path = path

        def __call__(self, *a, **k):
            raise NotSupportedError(
                f'{self._path} 是平台侧的因子/alpha 计算服务，本地没有对应实现；'
                '请把用到它的计算搬到策略内用 pandas/numpy 完成')

        def __getattr__(self, item: str):
            return _AlphaStub(f'{self._path}.{item}')

        def __repr__(self) -> str:
            return f'<bigalpha 未实现: {self._path}>'

    alpha = types.ModuleType('bigalpha')
    alpha.__doc__ = 'bigquant 兼容层：仅保证导入，调用会明确报错'
    # 版本/路径这类元信息要给出真实值：策略里常见
    # `if bigalpha.__file__.endswith(...)` / `bigalpha.__version__` 之类的判断，
    # 若把 __file__ 也做成占位对象，字符串方法一调用就炸。
    alpha.__file__ = '<bqcompat: bigalpha stub>'
    alpha.__version__ = '0.0.0+local-compat'

    def _alpha_getattr(name: str):
        # dunder 一律不接管（else `hasattr(mod, '__path__')` 之类全变真）
        if name.startswith('__'):
            raise AttributeError(name)
        return _AlphaStub(f'bigalpha.{name}')

    alpha.__getattr__ = _alpha_getattr   # PEP 562
    sys.modules.setdefault('bigalpha', alpha)


def install(bt=None, *, force: bool = False) -> types.ModuleType:
    """装好全部平台包名别名，返回 bigtrader 模块（= 本地引擎模块）。

    bt 缺省时 `import smalltrader`（调用方需保证 engine/ 在 sys.path 上）。
    """
    if bt is None:
        import smalltrader as bt  # noqa: PLC0415 - 延迟导入，避免循环

    key = id(bt)
    if key in _INSTALLED and not force:
        return bt

    from . import bigmodule_compat, dai_compat  # noqa: PLC0415

    _install_bigtrader(bt)
    _install_context_extras(bt)
    dai_mod = dai_compat.install()
    M = bigmodule_compat.build_M(bt)
    _install_bigquant(bt, dai_mod, M)
    _mk('bigmodule', M=M)
    _install_stubs()

    _INSTALLED[key] = True
    return bt
