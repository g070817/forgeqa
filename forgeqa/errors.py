"""forgeqa 统一异常体系。

所有异常都带 ``hint`` 字段，用于在报告里直接给出可执行的修复建议，
而不是让使用者去猜。
"""
from __future__ import annotations


class ForgeQAError(Exception):
    """基类。"""

    kind = "error"

    def __init__(self, message: str, *, hint: str | None = None, **extra):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.extra = extra

    def render(self) -> str:
        if self.hint:
            return f"{self.message}\n  → 修复建议: {self.hint}"
        return self.message


class ConfigError(ForgeQAError):
    kind = "config"


class CaseError(ForgeQAError):
    kind = "case"


class DataError(ForgeQAError):
    kind = "data"


class DbError(ForgeQAError):
    kind = "db"


class HttpError(ForgeQAError):
    kind = "http"


class UiError(ForgeQAError):
    kind = "ui"


class AssertFailed(ForgeQAError):
    """断言失败。这是「被测系统有问题」还是「用例写错了」的分界线。"""

    kind = "assert"

    def __init__(self, message: str, *, actual=None, expected=None, **extra):
        super().__init__(message, **extra)
        self.actual = actual
        self.expected = expected


class DependencyError(ForgeQAError):
    """可选依赖缺失。"""

    kind = "dependency"
