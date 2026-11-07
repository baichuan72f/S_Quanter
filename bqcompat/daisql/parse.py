"""SQL 扫描器 + 参数切分 + 递归重写（纯语法层，不含任何函数语义）。

为什么要自己扫而不是正则替换：方言函数可以嵌套（`m_avg(m_lag(close,1),5)`）、
参数里可能带逗号的函数调用或字符串（`m_corr(a, coalesce(b,0), 20)`），正则处理
不了平衡括号。这里做一遍线性扫描：

    找到 名字(  →  往后数括号到匹配处  →  切参数  →  递归处理每个参数
                 →  交给 registry 里的构造器生成新 SQL  →  继续往后扫
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .errors import DialectError
from .registry import FUNCS, UNSUPPORTED

# 平台方言函数名：m_xxx / c_xxx（大小写不敏感）
_CALL_RE = re.compile(r'\b([mc]_[A-Za-z0-9_]+)\s*\(', re.I)
_IDENT_RE = re.compile(r'^[A-Za-z_]\w*$')


def split_args(text: str) -> Tuple[List[str], Dict[str, str]]:
    """按顶层逗号切参数，识别平台的关键字写法 `name := value`。"""
    positional: List[str] = []
    named: Dict[str, str] = {}
    buf: List[str] = []
    depth = 0
    quote = ''

    def flush():
        raw = ''.join(buf).strip()
        buf.clear()
        if not raw:
            return
        m = re.match(r'^([A-Za-z_]\w*)\s*:=\s*(.+)$', raw, re.S)
        if m:
            named[m.group(1)] = m.group(2).strip()
        else:
            positional.append(raw)

    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ''
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
        elif ch == '(':
            depth += 1
            buf.append(ch)
        elif ch == ')':
            depth -= 1
            buf.append(ch)
        elif ch == ',' and depth == 0:
            flush()
        else:
            buf.append(ch)
    flush()
    return positional, named


def translate(sql: str) -> Tuple[str, List[str]]:
    """把 SQL 里的平台方言函数翻译成本地等价表达式。

    返回 (新 SQL, 用到的方言函数名列表)。遇到 UNCSUPPORTED 或未知的 m_/c_ 函数
    抛 DialectError（绝不静默放过：静默会让策略算出错的数还照常下单）。
    """
    used: List[str] = []
    out: List[str] = []
    i, n = 0, len(sql)

    while i < n:
        m = _CALL_RE.search(sql, i)
        if not m:
            out.append(sql[i:])
            break
        name = m.group(1)
        low = name.lower()
        spec = FUNCS.get(low)
        if spec is None and low not in UNSUPPORTED:
            # 不是已知方言函数：可能是用户自定义/普通函数，原样保留
            out.append(sql[i:m.end()])
            i = m.end()
            continue

        # 找匹配的右括号（跳过字符串内的括号）
        depth, j, quote = 1, m.end(), ''
        while j < n and depth > 0:
            ch = sql[j]
            if quote:
                if ch == quote:
                    quote = ''
            elif ch in ('"', "'"):
                quote = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            j += 1
        if depth != 0:
            raise DialectError(f'{name}( 的括号不匹配，无法解析 SQL')

        out.append(sql[i:m.start()])
        inner = sql[m.end():j - 1]
        positional, named = split_args(inner)

        if spec is None:
            raise DialectError(
                f'SQL 里用了平台方言函数 {name}()，本地映射模块还不支持。\n'
                f'原因：{UNSUPPORTED[low]}\n'
                '处置：①把这段计算搬到策略里用 pandas/numpy/talib 算；'
                '②在 bqcompat/daisql/ 对应模块里补一个实现（加 registry 一条即可）。\n'
                '已支持的函数：.venv\\Scripts\\python.exe -m bqcompat.daisql.selftest --list')

        # 参数里可能还有方言调用 → 递归翻译
        positional = [translate(a)[0] for a in positional]
        named = {k: translate(v)[0] for k, v in named.items()}
        try:
            spec.check_arity(positional, named)
        except ValueError as exc:
            raise DialectError(str(exc)) from exc

        used.append(low)
        out.append(spec.builder(positional, named))
        i = j

    return ''.join(out), used
