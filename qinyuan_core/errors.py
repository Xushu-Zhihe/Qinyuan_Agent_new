"""统一的错误分类。

每个错误只回答两个问题：**该谁管**（category + layer）和**能不能重试**（retryable）。
具体业务含义放在 code 里，用点分小写串表示，便于回溯时按前缀聚合。

约定：
  * 没命中任何已知分类的异常一律归 UNKNOWN 且不可重试——
    对应既有的“未知结果必须核实、禁止直接重发”约束。
  * 只放 ID 和短字段，不要把命令原文、模型原文塞进 context。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class Category(StrEnum):
    """冲突的性质。决定处理方式，不描述业务。"""

    INPUT = "input"  # 参数/格式不合法，调用方的错，重试无意义
    POLICY = "policy"  # 授权、范围、预算、Deadline 拒绝，需要人介入
    CONFLICT = "conflict"  # 状态冲突、前置不满足、幂等冲突，重新加载再判断
    CAPACITY = "capacity"  # 容量/额度耗尽，扩额后可重试
    DEPENDENCY = "dependency"  # 模型、沙箱、DB、网络失败，通常可重试
    UNKNOWN = "unknown"  # 结果不明，必须先核实


class Layer(StrEnum):
    """错误首次出现的位置，用来把问题分派给正确的排查方向。"""

    CTRL = "ctrl"
    MODEL = "model"
    EXEC = "exec"
    STORE = "store"
    IO = "io"


RETRYABLE: dict[Category, bool] = {
    Category.INPUT: False,
    Category.POLICY: False,
    Category.CONFLICT: False,
    Category.CAPACITY: True,
    Category.DEPENDENCY: True,
    Category.UNKNOWN: False,
}

# 内建异常的分类兜底。顺序敏感：子类在前。
_BUILTIN: tuple[tuple[type[BaseException], Category], ...] = (
    (PermissionError, Category.POLICY),
    (TimeoutError, Category.CAPACITY),
    (ConnectionError, Category.DEPENDENCY),
    (OSError, Category.DEPENDENCY),
    (ValueError, Category.INPUT),
    (TypeError, Category.INPUT),
    (KeyError, Category.INPUT),
)


class QinyuanError(Exception):
    """全部自有异常的基类。

    子类只需覆盖 category / layer / code 三个类属性：

        class FactSourceMismatch(QinyuanError):
            category = Category.POLICY
            layer = Layer.MODEL
            code = "fact.source_mismatch"

    __str__ 保持纯消息，不额外加前缀，避免改变既有文案断言。
    """

    category: Category = Category.UNKNOWN
    layer: Layer = Layer.CTRL
    code: str = "error"

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        layer: Layer | None = None,
        retryable: bool | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or type(self).code
        self.layer = layer or type(self).layer
        self.retryable = RETRYABLE[self.category] if retryable is None else retryable
        self.context: dict[str, Any] = dict(context)

    def as_dict(self) -> dict[str, Any]:
        """给结构化日志用的扁平字典。"""
        payload: dict[str, Any] = {
            "code": self.code,
            "category": str(self.category),
            "layer": str(self.layer),
            "retryable": self.retryable,
            "message": self.message,
        }
        payload.update(self.context)
        return payload

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class CodedError(QinyuanError, RuntimeError):
    """带固定安全码的拒绝类错误。

    这类错误不携带正文，只带一个可对外暴露的码，用于证据保护、请求暂存、
    候选视图等边界。码由 raise 点决定，所以 code 是实例属性而不是类属性。
    原先 12 个模块各写一遍 ``__init__(self, code)``，现在共用这一处。

    同时继承 RuntimeError：既有代码里有 ``except RuntimeError`` 的兜底，
    换基类不能把这些捕获点悄悄放空。
    """

    category = Category.POLICY
    layer = Layer.CTRL

    def __init__(self, code: str, message: str | None = None, **context: Any) -> None:
        super().__init__(message or code, code=code, **context)


def classify(exc: BaseException) -> tuple[Category, str, bool]:
    """把任意异常映射成 (category, code, retryable)。

    用于在边界处给尚未分类的异常归类，而不是要求 1386 个 raise 点全部重写。
    """
    if isinstance(exc, QinyuanError):
        return exc.category, exc.code, exc.retryable
    name = type(exc).__name__
    code = f"unclassified.{name.lower()}"
    for cls, category in _BUILTIN:
        if isinstance(exc, cls):
            return category, code, RETRYABLE[category]
    return Category.UNKNOWN, code, False


def wrap(
    exc: BaseException,
    *,
    code: str,
    layer: Layer | None = None,
    message: str | None = None,
    **context: Any,
) -> QinyuanError:
    """把外部异常转成本项目异常，保留 __cause__。"""
    if isinstance(exc, QinyuanError) and message is None and not context:
        return exc
    wrapped = QinyuanError(message or str(exc) or type(exc).__name__, code=code, layer=layer, **context)
    wrapped.__cause__ = exc
    return wrapped
