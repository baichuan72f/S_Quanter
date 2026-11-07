"""daiSQL 函数注册表：名字 → 语义类别 + SQL 构造器。

设计要点：**加一个方言函数只需要在这里登记一条**，翻译器与自检自动带上。
构造器签名：

    builder(args: List[str], named: Dict[str, str]) -> str
        args  : 已递归翻译过的位置参数（SQL 文本）
        named : 关键字参数（平台写法 `fastperiod:=12`）
        返回  : 等价的 DuckDB SQL 表达式

语义类别（kind）：
    seq    时序（沿标的按日期滚动）
    cross  截面（同一天横切）
    ta     时序 + 需要逐行状态（EMA/RSI/MACD/KDJ/加权/相关）→ 走 Python UDF
    seq_udf / cross_udf  时序/截面 + UDF 辅助（滚动排名、回归残差）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

Args = List[str]
Named = Dict[str, str]


@dataclass
class FuncSpec:
    name: str
    kind: str
    builder: Callable[[Args, Named], str]
    doc: str = ''
    min_args: int = 1
    max_args: Optional[int] = None
    aliases: tuple = field(default_factory=tuple)

    def check_arity(self, args: Args, named: Named) -> None:
        n = len(args)
        if n < self.min_args or (self.max_args is not None and n > self.max_args):
            hi = '∞' if self.max_args is None else self.max_args
            raise ValueError(
                f'{self.name} 的参数个数不对：期望 {self.min_args}~{hi} 个位置参数，'
                f'实际 {n} 个（位置 {args}，关键字 {sorted(named)}）')


FUNCS: Dict[str, FuncSpec] = {}


def register(spec: FuncSpec) -> FuncSpec:
    FUNCS[spec.name] = spec
    for alias in spec.aliases:
        FUNCS[alias] = FuncSpec(name=alias, kind=spec.kind, builder=spec.builder,
                                doc=spec.doc, min_args=spec.min_args,
                                max_args=spec.max_args)
    return spec


# 平台上有、本地确实无法等价映射的（有状态递推 / 截面回归）
UNSUPPORTED: Dict[str, str] = {}


def unsupported(name: str, why: str) -> None:
    UNSUPPORTED[name] = why


def supported() -> List[str]:
    return sorted(FUNCS)


# 为兼容旧名字保留（原来 sql_dialect 暴露的 SUPPORTED 是列表）
SUPPORTED = supported()


def describe() -> str:
    """人读的函数清单（按类别分组）。"""
    groups: Dict[str, List[str]] = {}
    for name, spec in sorted(FUNCS.items()):
        groups.setdefault(spec.kind, []).append(name)
    lines = ['本地 daiSQL 映射支持：']
    for kind in ('seq', 'seq_udf', 'cross', 'cross_udf', 'ta'):
        if kind in groups:
            lines.append(f'  [{kind}] ' + ', '.join(groups[kind]))
    if UNSUPPORTED:
        lines.append('明确不支持（会抛 DialectError）：')
        for name, why in sorted(UNSUPPORTED.items()):
            lines.append(f'  {name}: {why}')
    return '\n'.join(lines)
