"""ForgeQA — 通用造数与自动回归工具。

一句话：**用 YAML 描述「造什么数据、打哪个接口、查哪条 SQL、点哪个按钮」，
剩下的交给引擎。** 换站点 = 换配置，不改代码。

四层能力
--------
- ``factory``  Schema 驱动造数：Faker + 派生字段 + 边界/异常/极端变异数据，固定 seed 可复现
- ``db``       SQL 层：造数入库、SQL 断言、快照回归、精确回收
- ``httpclient`` 接口层：requests 封装、变量提取串联、结构断言、基线 diff
- ``uiauto``   UI 层：Playwright 声明式动作、多策略选择器、失败取证、视觉回归
"""
from .config import Context, ForgeConfig, deep_get, deep_merge, deep_set
from .errors import (
    AssertFailed, CaseError, ConfigError, DataError, DbError, DependencyError,
    ForgeQAError, HttpError, UiError,
)
from .factory import DataFactory, Schema, infer_schema
from .runner import Case, Executor, Runner, SuiteResult, load_cases
from .report import console_summary, render_html, write_html, write_junit

__version__ = "1.0.0"
__all__ = [
    "Context", "ForgeConfig", "deep_get", "deep_set", "deep_merge",
    "ForgeQAError", "ConfigError", "CaseError", "DataError", "DbError",
    "HttpError", "UiError", "AssertFailed", "DependencyError",
    "DataFactory", "Schema", "infer_schema",
    "Case", "Executor", "Runner", "SuiteResult", "load_cases",
    "console_summary", "render_html", "write_html", "write_junit",
    "__version__",
]
