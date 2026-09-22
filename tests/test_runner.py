"""用例引擎测试：加载、选择、步骤调度、断言、报告数据。

关键的 http 断言在合成响应上验证（不依赖网络）；
套件级流程用临时项目 + SQLite 做端到端集成验证。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forgeqa.config import ForgeConfig
from forgeqa.errors import CaseError, AssertFailed
from forgeqa.httpclient import Response, Recorder, eval_http_assertions, extract_value
from forgeqa.report import classify, console_summary, write_html, write_junit
from forgeqa.runner import (
    FAILED, PASSED, ERROR, SKIPPED, Case, Executor, Runner, load_cases, _retryable,
)

DDL = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  username TEXT NOT NULL UNIQUE,
  status INTEGER NOT NULL DEFAULT 1
);
"""

ENV = {
    "default_env": "local",
    "defaults": {"db": {"driver": "sqlite", "path": "./out/t.db"},
                 "ui": {"headless": True}, "report": {"out_dir": "./out/reports"}},
    "generators": {"seed": 42, "locale": "zh_CN"},
    "hooks": {"ddl": "config/db/schema.sql", "cleanup": True},
    "envs": {"local": {"base_url": "http://127.0.0.1:1"}},
}

SCHEMA = {
    "entity": "user", "count": 1,
    "fields": [
        {"name": "name", "gen": "faker", "method": "name"},
        {"name": "username", "gen": "pattern", "pattern": "u????",
         "transform": "suffix:${uniq}"},
    ],
}

CASE = {
    "id": "TC-IT-001",
    "title": "端到端：造数入库 → SQL 校验 → 脚本计算 → 清理",
    "priority": "P0",
    "tags": ["it", "smoke"],
    "data": {"user": SCHEMA},
    "steps": [
        {"name": "插入用户", "db": {
            "mode": "execute",
            "sql": "INSERT INTO users (name, username) VALUES (:n, :u)",
            "params": {"n": "${data.user.name}", "u": "${data.user.username}"}},
         "assert": [{"sql": "SELECT COUNT(*) FROM users WHERE username = :u",
                     "params": {"u": "${data.user.username}"}, "op": "eq", "value": 1}]},
        {"name": "查询校验", "db": {
            "sql": "SELECT name, status FROM users WHERE username = :u",
            "params": {"u": "${data.user.username}"}},
         "assert": [
             {"rows_count": 1},
             {"row": {"field": "name", "op": "eq", "value": "${data.user.name}"}},
             {"each_row": {"field": "status", "op": "eq", "value": 1}},
             # scalar 取的是首行首列（name），不是 status
             {"scalar": {"op": "eq", "value": "${data.user.name}"}},
         ]},
        # ${} 先替换成字面量，再当 Python 表达式求值
        {"name": "脚本计算", "script": {"expr": "${data.user.name} + '!'", "var": "greeting"}},
        {"name": "断言脚本结果", "script": {"expr": "${ctx.greeting}", "var": "val"},
         "assert": [{"var": "val", "op": "endswith", "value": "!"}]},
        {"name": "清理", "db": {"mode": "execute",
                                "sql": "DELETE FROM users WHERE username = :u",
                                "params": {"u": "${data.user.username}"}},
         "assert": [{"rows_count": {"op": "gte", "value": 0}}]},
    ],
}

CASE_LOOP = {
    "id": "TC-IT-LOOP",
    "title": "循环步骤：逐条校验",
    "data": {"items": [1, 2, 3]},
    "steps": [{"name": "循环", "loop": {
        "over": "${data.items}", "as": "it",
        "steps": [{"name": "校验 ${it}", "script": {"expr": "${it}", "var": "v"},
                   "assert": [{"var": "v", "op": "lte", "value": 3}]}]}}],
}


def _project(tmp_path: Path, cases: dict[str, dict] | None = None) -> Path:
    (tmp_path / "config" / "db").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cases").mkdir(exist_ok=True)
    (tmp_path / "config" / "env.yaml").write_text(
        yaml.safe_dump(ENV, allow_unicode=True), encoding="utf-8")
    (tmp_path / "config" / "db" / "schema.sql").write_text(DDL, encoding="utf-8")
    for name, body in (cases or {"it": CASE}).items():
        (tmp_path / "cases" / f"{name}.yaml").write_text(
            yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def cfg(tmp_path):
    return ForgeConfig.load(_project(tmp_path) / "config" / "env.yaml", root=tmp_path)


# --------------------------------------------------------------------------- #
# 合成响应用来验证接口断言
# --------------------------------------------------------------------------- #
def _resp(status=200, body=None, text=None, headers=None, ms=120.0) -> Response:
    import json

    body = {"code": 0, "data": {"id": 7, "name": "张三", "tags": ["a"]}} if body is None else body
    return Response(status=status, headers=headers or {"Content-Type": "application/json"},
                    text=text if text is not None else json.dumps(body, ensure_ascii=False),
                    url="http://t/api", method="GET", elapsed_ms=ms)


class TestLoadCases:
    def test_load_from_dir(self, cfg):
        cases = load_cases(["cases"], cfg.root)
        assert len(cases) == 1 and cases[0].id == "TC-IT-001"

    def test_defaults_inferred(self):
        c = Case.from_dict({"id": "X", "steps": [{"http": {"path": "/a"}}]})
        assert c.priority == "P1" and c.layer == "api" and c.title == "X"

    def test_layer_guessed_from_steps(self):
        assert Case.from_dict({"id": "A", "steps": [{"ui": [{"action": "goto"}]}]}).layer == "ui"
        assert Case.from_dict({"id": "B", "steps": [{"db": {"sql": "select 1"}}]}).layer == "db"

    def test_multi_doc_and_cases_key(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text(yaml.safe_dump({"cases": [{"id": "a", "steps": [{"sleep": 0}]},
                                               {"id": "b", "steps": [{"sleep": 0}]}]},
                                    allow_unicode=True), encoding="utf-8")
        assert len(load_cases([p], tmp_path)) == 2

    def test_invalid_yaml_gives_actionable_hint(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("id: x\nsteps:\n  - {value: ${a.b}}\n", encoding="utf-8")
        with pytest.raises(CaseError) as exc:
            load_cases([p], tmp_path)
        assert "引号" in (exc.value.hint or "")

    def test_missing_path(self, tmp_path):
        with pytest.raises(CaseError) as exc:
            load_cases([tmp_path / "nope"], tmp_path)
        assert "forgeqa init" in (exc.value.hint or "")

    def test_empty_dir(self, tmp_path):
        (tmp_path / "cases").mkdir()
        with pytest.raises(CaseError):
            load_cases(["cases"], tmp_path)


class TestSelect:
    def _cases(self):
        return [
            Case.from_dict({"id": "A", "priority": "P0", "tags": ["smoke"], "steps": [{"sleep": 0}]}),
            Case.from_dict({"id": "B", "priority": "P1", "tags": ["ui"], "steps": [{"sleep": 0}]}),
            Case.from_dict({"id": "C", "priority": "P2", "tags": ["smoke", "db"],
                            "title": "边界用例", "steps": [{"sleep": 0}]}),
        ]

    def test_filter_by_tag(self):
        assert [c.id for c in Runner.select(self._cases(), tags=["smoke"])] == ["A", "C"]

    def test_exclude_tag(self):
        assert [c.id for c in Runner.select(self._cases(), exclude_tags=["smoke"])] == ["B"]

    def test_filter_by_priority_and_keyword_and_id(self):
        assert [c.id for c in Runner.select(self._cases(), priorities=["p0"])] == ["A"]
        assert [c.id for c in Runner.select(self._cases(), keyword="边界")] == ["C"]
        assert [c.id for c in Runner.select(self._cases(), ids=["B"])] == ["B"]

    def test_sorted_by_priority(self):
        assert [c.priority for c in Runner.select(self._cases())] == ["P0", "P1", "P2"]


class TestHttpAssertions:
    def test_status_forms(self):
        r = _resp(201)
        assert all(x.passed for x in eval_http_assertions(r, [{"status": 201}]))
        assert eval_http_assertions(r, [{"status": [200, 201]}])[0].passed
        assert eval_http_assertions(r, [{"status": {"min": 200, "max": 299}}])[0].passed
        assert not eval_http_assertions(r, [{"status": 404}])[0].passed

    def test_jsonpath_assertions(self):
        r = _resp()
        checks = eval_http_assertions(r, [
            {"jsonpath": "$.data.id", "op": "eq", "value": 7},
            {"jsonpath": "$.data.name", "op": "eq", "value": "张三"},
            {"jsonpath": "$.data.tags", "op": "contains", "value": "a"},
            {"jsonpath": "$.data.missing", "op": "absent"},
            {"jsonpath": "$.data.missing2", "op": "exists"},
        ])
        assert [c.passed for c in checks] == [True, True, True, True, False]

    def test_jsonpath_all(self):
        r = _resp(body={"list": [{"s": 1}, {"s": 1}]})
        ok = eval_http_assertions(r, [{"jsonpath": "$.list[*].s", "op": "all", "value": 1}])
        assert all(c.passed for c in ok)

        bad = eval_http_assertions(r, [{"jsonpath": "$.list[*].s", "op": "all", "value": 2}])
        assert not all(c.passed for c in bad)

    def test_missing_jsonpath_reports_clearly(self):
        c = eval_http_assertions(_resp(), [{"jsonpath": "$.nope", "op": "eq", "value": 1}])[0]
        assert not c.passed and "无匹配值" in c.message

    def test_schema_and_header_and_text_and_time(self):
        r = _resp(headers={"Content-Type": "application/json; charset=utf-8"})
        checks = eval_http_assertions(r, [
            {"schema": {"type": "object", "required": ["code", "data"]}},
            {"header": "Content-Type", "op": "contains", "value": "json"},
            {"text_contains": "张三"},
            {"time_lt": 1000},
        ])
        assert all(c.passed for c in checks)

    def test_variable_interpolation_in_assertions(self, cfg):
        ctx = cfg.context()
        ctx.set("expect_name", "张三")
        checks = eval_http_assertions(_resp(), [{"jsonpath": "$.data.name", "op": "eq",
                                                 "value": "${expect_name}"}], ctx=ctx)
        assert checks[0].passed

    def test_unknown_assertion_key_gives_hint(self):
        with pytest.raises(AssertFailed) as exc:
            eval_http_assertions(_resp(), [{"没这个键": 1}])
        assert "可用键" in (exc.value.hint or "")

    def test_assertion_without_http_step_is_reported_clearly(self):
        from forgeqa.runner import _NullResponse
        c = eval_http_assertions(_NullResponse(), [{"status": 200}])[0]
        assert not c.passed


class TestExtract:
    def test_jsonpath_and_cast(self):
        r = _resp()
        assert extract_value(r, "$.data.id") == 7
        assert extract_value(r, {"jsonpath": "$.data.id", "cast": "int"}) == 7
        assert extract_value(r, {"jsonpath": "$.data.id", "cast": "str"}) == "7"

    def test_regex_and_header(self):
        r = _resp(text="session=abc123; Path=/")
        assert extract_value(r, {"regex": r"session=(\w+)"}) == "abc123"
        assert extract_value(r, {"header": "Content-Type"}) == "application/json"

    def test_default_when_missing(self):
        assert extract_value(_resp(), {"jsonpath": "$.nope", "default": "dflt"}) == "dflt"

    def test_missing_without_default_raises(self):
        from forgeqa.errors import HttpError
        with pytest.raises(HttpError) as exc:
            extract_value(_resp(), "$.nope")
        assert "没有匹配到" in str(exc.value)

    def test_unknown_rule(self):
        from forgeqa.errors import HttpError
        with pytest.raises(HttpError):
            extract_value(_resp(), {"未知键": 1})


class TestExecutorIntegration:
    def test_full_suite_passes_and_cleans_up(self, cfg):
        runner = Runner(cfg)
        info, boot = runner.bootstrap()
        assert not [s for s in boot if s.status in (FAILED, ERROR)]
        assert info["ddl"].endswith("schema.sql")

        cases = load_cases(["cases"], cfg.root)
        suite = runner.run(cases)
        result = suite.cases[0]
        assert result.status == PASSED, result.error
        assert suite.passed == 1 and suite.errors == 0
        assert all(s.status == PASSED for s in result.steps)

    def test_loop_step(self, cfg):
        (cfg.root / "cases" / "loop.yaml").write_text(
            yaml.safe_dump(CASE_LOOP, allow_unicode=True), encoding="utf-8")
        cases = load_cases(["cases"], cfg.root)
        loop_case = [c for c in cases if c.id == "TC-IT-LOOP"][0]
        ex = Executor(loop_case, cfg, suite_ctx=cfg.context())
        ex._prepare()
        result = ex.run()
        ex._finish()
        assert result.status == PASSED, result.error
        assert len(result.steps[0].checks) == 3

    def test_failing_assertion_marks_failed_not_error(self, cfg):
        bad = dict(CASE)
        bad["id"] = "TC-IT-FAIL"
        # 用不依赖建表的查询，专注验证「断言失败 → FAILED（而非 ERROR）」的分拣逻辑
        bad["steps"] = [{"name": "故意错", "db": {"sql": "SELECT 0 AS v"},
                         "assert": [{"scalar": {"op": "eq", "value": -1}}]}]
        (cfg.root / "cases" / "bad.yaml").write_text(
            yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
        cases = load_cases(["cases"], cfg.root)
        case = [c for c in cases if c.id == "TC-IT-FAIL"][0]
        result = Executor(case, cfg, suite_ctx=cfg.context()).run()
        assert result.status == FAILED
        assert "期望等于" in (result.error or "")
        assert classify(result)["category"] in ("缺陷", "数据")

    def test_tool_error_marks_error(self, cfg):
        bad = {"id": "TC-IT-ERR", "title": "未知动作", "steps": [{"没这个动作": 1}]}
        (cfg.root / "cases" / "err.yaml").write_text(
            yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
        cases = load_cases(["cases"], cfg.root)
        case = [c for c in cases if c.id == "TC-IT-ERR"][0]
        result = Executor(case, cfg, suite_ctx=cfg.context()).run()
        assert result.status == ERROR and result.hint

    def test_skip_flag(self, cfg):
        c = Case.from_dict({"id": "S", "skip": "环境未就绪", "steps": [{"sleep": 0}]})
        result = Executor(c, cfg, suite_ctx=cfg.context()).run()
        assert result.status == SKIPPED and "环境" in (result.error or "")

    def test_step_if_condition(self, cfg):
        c = Case.from_dict({"id": "I", "steps": [
            {"name": "不该跑", "if": "1 == 2", "sleep": 0.001, "assert": [{"actual": 1, "op": "eq", "value": 2}]}]})
        result = Executor(c, cfg, suite_ctx=cfg.context()).run()
        assert result.status == PASSED and result.steps[0].status == SKIPPED

    def test_on_fail_continue(self, cfg):
        c = Case.from_dict({"id": "C", "steps": [
            {"name": "失败但继续", "on_fail": "continue", "assert": [{"actual": 1, "op": "eq", "value": 2}]},
            {"name": "后续步骤", "sleep": 0.001}]})
        result = Executor(c, cfg, suite_ctx=cfg.context()).run()
        assert result.status == FAILED and len(result.steps) == 2

    def test_case_isolation_within_run(self, cfg):
        """同一份造数 schema 在两个用例里必须产出不同数据，否则会互相撞唯一约束。"""
        runner = Runner(cfg)
        runner.bootstrap()
        cases = [Case.from_dict(dict(CASE, id=f"TC-ISO-{i}")) for i in range(2)]
        suite = runner.run(cases)
        assert suite.passed == 2, [c.error for c in suite.cases]

    def test_fail_fast(self, cfg):
        runner = Runner(cfg)
        runner.bootstrap()
        c1 = Case.from_dict({"id": "F1", "priority": "P0",
                             "steps": [{"assert": [{"actual": 1, "op": "eq", "value": 2}]}]})
        c2 = Case.from_dict({"id": "F2", "priority": "P1", "steps": [{"sleep": 0.001}]})
        suite = runner.run([c1, c2], fail_fast=True)
        assert suite.total == 1

    def test_repeat(self, cfg):
        runner = Runner(cfg)
        runner.bootstrap()
        suite = runner.run([Case.from_dict({"id": "R", "steps": [{"sleep": 0.001}]})], repeat=3)
        assert suite.total == 3


class TestRetryPolicy:
    def test_only_environment_errors_are_retryable(self):
        from forgeqa.runner import CaseResult
        assert _retryable(CaseResult(id="x", title="x", status=ERROR, error="Connection timeout"))
        assert not _retryable(CaseResult(id="x", title="x", status=FAILED, error="期望等于 1"))
        assert not _retryable(CaseResult(id="x", title="x", status=ERROR, error="YAML 解析失败"))


class TestBaseline:
    def test_recorder_update_then_diff(self, tmp_path):
        path = tmp_path / "http.json"
        rec = Recorder(path, mode="update")
        rec.record("TC-1", "步骤A", _resp(200))
        rec.commit()

        rec2 = Recorder(path, mode="diff")
        assert rec2.record("TC-1", "步骤A", _resp(200)) is None
        assert rec2.diff()["has_diff"] is False

        rec3 = Recorder(path, mode="diff")
        rec3.record("TC-1", "步骤A", _resp(500))
        assert rec3.diff()["has_diff"] is True

    def test_recorder_off_does_nothing(self, tmp_path):
        rec = Recorder(tmp_path / "n.json", mode="off")
        rec.record("a", "b", _resp())
        rec.commit()
        assert not (tmp_path / "n.json").exists()


class TestReport:
    def test_classify_by_pattern(self):
        from forgeqa.runner import CaseResult
        assert classify(CaseResult(id="a", title="a", status=FAILED,
                                   error="数据快照与基线不一致"))["category"] == "缺陷"
        assert classify(CaseResult(id="a", title="a", status=ERROR,
                                   error="Connection refused"))["category"] == "环境"
        assert classify(CaseResult(id="a", title="a", status=ERROR,
                                   error="no such table: users"))["category"] == "数据"
        assert classify(CaseResult(id="a", title="a", status=ERROR,
                                   error="用例 YAML 解析失败"))["category"] == "脚本"
        assert classify(CaseResult(id="a", title="a", status=PASSED))["category"] == "-"

    def test_html_and_junit_and_console(self, cfg):
        runner = Runner(cfg)
        runner.bootstrap()
        suite = runner.run(load_cases(["cases"], cfg.root))
        html = write_html(suite, cfg.root / "out" / "r.html")
        text = html.read_text(encoding="utf-8")
        assert "TC-IT-001" in text and "<table" in text and "回归结果" not in text
        assert "PASSED" in text

        junit = write_junit(suite, cfg.root / "out" / "j.xml")
        xml = junit.read_text(encoding="utf-8")
        assert 'tests="1"' in xml and "testcase" in xml

        summary = console_summary(suite)
        assert "总计 1" in summary and "通过率" in summary
