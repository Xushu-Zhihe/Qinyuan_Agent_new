"""错误分类的快速单测：毫秒级，不落盘。"""

from __future__ import annotations

import pytest

from qinyuan_core import RETRYABLE, Category, CodedError, Layer, QinyuanError, classify, wrap


def test_unknown_by_default_and_not_retryable():
    err = QinyuanError("boom")
    assert err.category is Category.UNKNOWN
    assert err.retryable is False


@pytest.mark.parametrize(
    ("exc", "category", "retryable"),
    [
        (ValueError("x"), Category.INPUT, False),
        (KeyError("x"), Category.INPUT, False),
        (PermissionError("x"), Category.POLICY, False),
        (TimeoutError("x"), Category.CAPACITY, True),
        (ConnectionError("x"), Category.DEPENDENCY, True),
        (OSError("x"), Category.DEPENDENCY, True),
    ],
)
def test_classify_builtin(exc, category, retryable):
    got_category, code, got_retryable = classify(exc)
    assert got_category is category
    assert got_retryable is retryable
    assert code.startswith("unclassified.")


def test_classify_never_marks_unknown_retryable():
    got_category, _, retryable = classify(RuntimeError("?"))
    assert got_category is Category.UNKNOWN
    assert retryable is False


def test_subclass_declares_code_and_layer():
    class FactSourceMismatch(QinyuanError):
        category = Category.POLICY
        layer = Layer.MODEL
        code = "fact.source_mismatch"

    err = FactSourceMismatch("来源不符", fact="f1")
    assert classify(err) == (Category.POLICY, "fact.source_mismatch", False)
    assert err.as_dict()["fact"] == "f1"
    assert err.as_dict()["layer"] == "model"


def test_message_is_not_decorated():
    # 既有代码与测试可能断言异常文案，__str__ 不能偷偷加前缀
    assert str(QinyuanError("原始文案")) == "原始文案"


def test_retryable_can_be_overridden():
    err = QinyuanError("x", code="k", retryable=True)
    assert err.retryable is True
    assert Category.INPUT in RETRYABLE


def test_wrap_keeps_cause_and_code():
    root = TimeoutError("读超时")
    err = wrap(root, code="model.timeout", layer=Layer.MODEL, run="r1")
    assert err.__cause__ is root
    assert err.code == "model.timeout"
    assert err.as_dict()["run"] == "r1"
    assert err.retryable is False  # QinyuanError 默认 UNKNOWN


def test_coded_error_takes_code_from_raise_site():
    class ProtectedEvidenceError(CodedError):
        layer = Layer.STORE

    err = ProtectedEvidenceError("candidate_evidence_unavailable")
    assert err.code == "candidate_evidence_unavailable"
    assert err.as_dict()["layer"] == "store"
    assert err.category is Category.POLICY
    assert err.retryable is False
    # 既有代码里有 except RuntimeError 的兜底，换基类不能放空这些捕获点
    assert isinstance(err, RuntimeError)
