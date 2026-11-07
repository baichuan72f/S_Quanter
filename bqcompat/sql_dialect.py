"""兼容外壳：老代码 `from bqcompat import sql_dialect` 仍然可用。

实现已按职责拆到 `bqcompat/daisql/`（registry / parse / seq / cross / ta /
runtime / udfs / selftest）。这里只做 re-export，避免调用方跟着改。
"""
from __future__ import annotations

from .daisql import FUNCS, SUPPORTED, UNSUPPORTED, describe
from .daisql import translate  # noqa: F401
from .daisql.errors import DialectError  # noqa: F401
from .daisql.udfs import UDFS  # noqa: F401

__all__ = ['translate', 'DialectError', 'FUNCS', 'SUPPORTED', 'UNSUPPORTED',
           'describe', 'UDFS']
