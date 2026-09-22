"""scan 模块的单元测试：纯函数 + 产物生成。网络层不做单测（由演示站点端到端覆盖）。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forgeqa.errors import CaseError
from forgeqa.runner import load_cases
from forgeqa.scan import (
    Endpoint,
    ScanResult,
    build_case_docs,
    entity_for,
    extract_paths,
    is_static,
    normalize_path,
    parse_openapi,
    write_case_file,
    write_schemas,
)


# --------------------------------------------------------------------------- #
# 路径归一化
# --------------------------------------------------------------------------- #
class TestNormalizePath:
    @pytest.mark.parametrize("raw", [
        "/api/users", "/api/users?size=10", "/health", "/ui/form",
    ])
    def test_keeps_api_paths(self, raw):
        assert normalize_path(raw) == raw

    @pytest.mark.parametrize("raw", [
        "", "api/users",                    # 不以 / 开头
        "mailto:a@b.com", "javascript:void(0)", "tel:123", "data:image/png", "#top",
        "https://other.com/api",            # 绝对地址交给同源过滤
        "/static/app.js", "/logo.png",      # 静态资源
        "/" + "a" * 200,                    # 超长
    ])
    def test_rejects(self, raw):
        assert normalize_path(raw) is None

    def test_is_static(self):
        assert is_static("/a/b.JS")
        assert not is_static("/api/js")     # 无扩展名结尾不算静态


# --------------------------------------------------------------------------- #
# 提取
# --------------------------------------------------------------------------- #
class TestExtractPaths:
    def test_html_links_and_forms(self):
        html = """
        <a href="/ui/form">表单</a>
        <a href="https://other.com/api/x">外链</a>
        <form action="/api/login"></form>
        <link rel="stylesheet" href="/static/app.css">
        <img src="/logo.png">
        <a href="#top">锚点</a>
        """
        assert extract_paths(html) == {"/ui/form", "/api/login"}

    def test_inline_js_api_paths(self):
        js = """
        fetch('/api/users?page=1');
        axios.post("/api/orders/42/items");
        const notApi = "/docs/intro";        // 不以 /api 或 /v数字 开头，不收
        """
        assert extract_paths(js) == {"/api/users", "/api/orders/42/items"}

    def test_dedupe(self):
        html = '<a href="/api/users"></a><a href="/api/users"></a>'
        assert extract_paths(html) == {"/api/users"}


# --------------------------------------------------------------------------- #
# 实体名
# --------------------------------------------------------------------------- #
class TestEntityFor:
    @pytest.mark.parametrize("path, expect", [
        ("/api/users", "users"),
        ("/api/users/42", "users"),          # 取集合段
        ("/api/users/{id}", "users"),
        ("/health", "health"),
        ("/", "root"),
        ("/api/user-profiles", "user_profiles"),
    ])
    def test_basic(self, path, expect):
        assert entity_for(path) == expect


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
class TestParseOpenapi:
    def test_paths_and_methods(self):
        spec = {"openapi": "3.0.0", "paths": {
            "/users": {"get": {}, "post": {}},
            "/users/{id}": {"get": {}, "delete": {}},
        }}
        eps = parse_openapi(spec)
        assert {e.path: e.methods for e in eps} == {
            "/users": {"GET", "POST"},
            "/users/{id}": {"GET", "DELETE"},
        }
        assert all(e.source == "openapi" for e in eps)

    def test_ignores_non_method_keys(self):
        spec = {"paths": {"/x": {"parameters": [], "summary": "s"}}}
        assert parse_openapi(spec) == []


# --------------------------------------------------------------------------- #
# 用例生成
# --------------------------------------------------------------------------- #
def _result() -> ScanResult:
    r = ScanResult(base_url="http://127.0.0.1:8000")
    r.endpoints = [
        Endpoint(path="/api/users", methods={"GET"}, status=200,
                 content_type="application/json", sample={"code": 0, "data": []}),
        Endpoint(path="/health", methods={"GET"}, status=200,
                 content_type="application/json", sample={"status": "ok"}),
        Endpoint(path="/api/login", methods={"POST"}, status=405),   # GET 不通 → 不生成冒烟
        Endpoint(path="/api/users", methods={"POST"}),               # 无 GET 200
    ]
    return r


class TestBuildCaseDocs:
    def test_only_get_200(self):
        docs = build_case_docs(_result())
        assert [d["id"] for d in docs] == ["TC-SCAN-001", "TC-SCAN-002"]
        paths = [d["steps"][0]["http"]["path"] for d in docs]
        assert paths == ["/api/users", "/health"]

    def test_json_gets_jsonpath_assertion(self):
        docs = build_case_docs(_result())
        user_case = next(d for d in docs if d["steps"][0]["http"]["path"] == "/api/users")
        ops = [list(a)[0] for a in user_case["steps"][0]["assert"]]
        assert "jsonpath" in ops


class TestWriteArtifacts:
    def test_case_file_loadable_but_hidden_from_dir_scan(self, tmp_path):
        target = write_case_file(_result(), tmp_path / "cases" / "_generated")
        assert target is not None and target.name.startswith("_scan_")

        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert len(raw["cases"]) == 2
        assert "# POST" in target.read_text(encoding="utf-8")       # POST 草稿以注释提示

        # 显式传文件路径可加载
        cases = load_cases([str(target)], tmp_path)
        assert {c.id for c in cases} == {"TC-SCAN-001", "TC-SCAN-002"}

        # 目录扫描会跳过 `_` 前缀文件 —— 默认 --cases cases 不会误跑草稿
        # （目录里只有草稿时，load_cases 直接报"没找到用例"）
        with pytest.raises(CaseError):
            load_cases([tmp_path / "cases"], tmp_path)

    def test_write_schemas_and_backup(self, tmp_path):
        result = _result()
        schemas = tmp_path / "schemas"
        written = write_schemas(result, schemas)
        # users 与 health 各一份
        assert {p.name for p, _ in written} == {"users.yaml", "health.yaml"}

        # 已存在且不 force → 写 .inferred.yaml，不覆盖人工维护的文件
        written2 = write_schemas(result, schemas, force=False)
        assert {p.name for p, _ in written2} == {"users.inferred.yaml", "health.inferred.yaml"}
        assert (schemas / "users.yaml").exists()

        # force → 直接覆盖
        written3 = write_schemas(result, schemas, force=True)
        assert {p.name for p, _ in written3} == {"users.yaml", "health.yaml"}

    def test_skip_non_dict_sample(self, tmp_path):
        result = ScanResult(base_url="http://x")
        result.endpoints = [Endpoint(path="/text", methods={"GET"}, status=200,
                                     content_type="text/plain", sample=None)]
        assert write_schemas(result, tmp_path / "s") == []
