"""bigmodule.M 兼容层：`from bigmodule import M` 后的模块调用
==============================================================

平台策略里最常见的一句是：

    m5 = M.bigtrader.v35(
        data={"instruments": ["600900.SH"]},
        start_date=..., end_date=...,
        initialize=initialize, handle_data=handle_data,
        capital_base=1000000,
        order_price_field_buy="open", order_price_field_sell="close",
    )

`M.bigtrader.vNN(...)` 在平台上会真的启动一次回测。本地把它**转调**
`smalltrader.run(**kwargs)`：

* 跑批时（`run_daily.py`）→ 正常执行当日回测；
* 加载策略读元信息时（`paper/runner.py::_CaptureRun` 已把 `run` 换成 no-op）
  → 只把 kwargs 捕获下来当参数来源，不会真的跑一遍。

版本号（v22/v35/v40/v47/v52/v58…）不做语义区分：都按同一套 kwargs 转发，
本地 `run()` 不认识的参数会显式抛 TypeError，而不是静默忽略。

## 模块输入 `data=` 的特殊处理

平台里 `data={"instruments": [...]}` 是**模块输入**（节点连线传进来的数据），
不是行情表；策略内通过 `context.data["instruments"][0]` 读。而本地 `run(data=…)`
期望的是 `dict[symbol, DataFrame]`。所以这里要分辨：

* `data` 是 dict 且**所有值都是 DataFrame** → 当作行情数据，原样透传；
* 其它情况的 dict → 当作模块输入：把 `instruments` 提出来当标的池，
  `data` 置 None 走引擎自动取数，并给回调套一层壳，让 `context.data` 在每次
  调用前等于那份输入字典。
"""
from __future__ import annotations

import functools
import re
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from . import NotSupportedError

__all__ = ['build_M']

_VERSION_RE = re.compile(r'^v\d+$')
_CALLBACK_KEYS = ('initialize', 'before_trading_start', 'handle_data',
                  'after_trading', 'handle_trade', 'handle_order')


class _Unsupported:
    """平台上有、本地没实现的模块节点：导入/属性访问放过，调用时报清楚。"""

    def __init__(self, path: str):
        self._path = path

    def __call__(self, *args, **kwargs):
        raise NotSupportedError(
            f'{self._path} 是 BigQuant 平台侧的计算模块，本地没有对应实现。\n'
            '可选做法：①把这段计算改写成策略内的 pandas/numpy 代码；'
            '②改用本地已支持的路径（bigtrader 交易模块 + dai 取数）。\n'
            '支持范围见 reports/07-bq-compat.md')

    def __getattr__(self, item: str):
        return _Unsupported(f'{self._path}.{item}')

    def __repr__(self) -> str:
        return f'<bigmodule 未实现的模块: {self._path}>'


def _split_module_input(data: Any) -> Tuple[Optional[Dict[str, Any]], Any]:
    """分辨 `data=` 是模块输入还是行情表。返回 (模块输入 or None, 行情数据)。"""
    if not isinstance(data, dict):
        return None, data
    if data and all(isinstance(v, pd.DataFrame) for v in data.values()):
        return None, data          # 真正的行情表
    return data, None              # 模块输入（含空 dict）


def _wrap_callbacks(kwargs: Dict[str, Any], inputs: Dict[str, Any]) -> None:
    """给回调套壳：调用前把模块输入挂到 context.data。"""
    for key in _CALLBACK_KEYS:
        fn = kwargs.get(key)
        if not callable(fn) or getattr(fn, '_bq_inputs_wrapped', False):
            continue

        @functools.wraps(fn)
        def _wrapped(*args, _fn=fn, **kw):
            ctx = args[0] if args else kw.get('context')
            if ctx is not None:
                try:
                    ctx.data = inputs
                except Exception:   # noqa: BLE001 - 上下文不许写也不该拦死策略
                    pass
            return _fn(*args, **kw)

        _wrapped._bq_inputs_wrapped = True
        kwargs[key] = _wrapped


def _module_run(bt, version: str, kwargs: Dict[str, Any]):
    """`M.bigtrader.vNN(...)` 的本地实现：转调 smalltrader.run。"""
    call_kwargs = dict(kwargs)
    inputs, market_data = _split_module_input(call_kwargs.get('data'))
    if inputs is not None:
        call_kwargs['data'] = market_data
        if call_kwargs.get('instruments') is None:
            inst = inputs.get('instruments')
            if inst:
                call_kwargs['instruments'] = list(inst)
        _wrap_callbacks(call_kwargs, inputs)
    return bt.run(**call_kwargs)


class _BigTraderNamespace:
    """`M.bigtrader.*`：认版本号（v22/v35/v47/v52…），别的转未实现。"""

    def __init__(self, bt):
        self._bt = bt

    def __getattr__(self, item: str):
        if _VERSION_RE.match(item):
            # 用 **kw 收集全部关键字参数（平台模块调用一律是关键字调用）
            return lambda **kw: _module_run(self._bt, item, kw)
        return _Unsupported(f'M.bigtrader.{item}')

    def __repr__(self) -> str:
        return '<M.bigtrader（本地兼容：vNN 转调 smalltrader.run）>'


class _M:
    """`from bigmodule import M` 的模块命名空间。"""

    def __init__(self, bt):
        self.bigtrader = _BigTraderNamespace(bt)

    def __getattr__(self, item: str):
        return _Unsupported(f'M.{item}')

    def __repr__(self) -> str:
        return '<bigmodule.M（本地兼容层）>'


def build_M(bt) -> _M:
    return _M(bt)
