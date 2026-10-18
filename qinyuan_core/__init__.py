"""重构后共用的小内核：错误分类与日志分类。

这里只放跨层复用的基础件，不放业务。业务仍然留在 agent_core / agent_adapters。
"""

from .errors import (
    RETRYABLE,
    Category,
    CodedError,
    Layer,
    QinyuanError,
    classify,
    wrap,
)
from .logging import (
    CHANNELS,
    Channel,
    bind,
    configure,
    get_logger,
    unbind,
)

__all__ = [
    "RETRYABLE",
    "Category",
    "CodedError",
    "Layer",
    "QinyuanError",
    "classify",
    "wrap",
    "CHANNELS",
    "Channel",
    "bind",
    "configure",
    "get_logger",
    "unbind",
]
