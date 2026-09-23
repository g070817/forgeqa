"""apidoc 模块的单元测试：OpenAPI/Swagger 文档 → 写接口用例草稿。网络层用 mock 覆盖。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from forgeqa.apidoc import (
    Operation,
    build_import_docs,
    import_spec,
    iter_operations,
    load_spec,
    resolve_ref,
    schema_to_fields,
)
from forgeqa.errors import CaseError, DataError
from forgeqa.factory import Context, DataFactory
from forgeqa.runner import load_cases


# --------------------------------------------------------------------------- #
# 测试用文档
# --------------------------------------------------------------------------- #
def _spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "demo", "version": "1.0"},
        "paths": {
            "/api/users": {
                "post": {
                    "operationId": "createUser",
                    "summary": "创建用户",
                    "requestBody": {
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/UserInput"}}},
                    },
                },
                "get": {"operationId": "listUsers"},
            },
            "/api/users/{id}": {
                "put": {
                    "operationId": "updateUser",
                    "requestBody": {
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/UserInput"}}},
                    },
                    "parameters": [{"name": "id", "in": "path", "required": True}],
                },
                "delete": {"summary": "删除用户"},
            },
            "/api/orders": {
                "post": {
                    "operationId": "createOrder",
                    "requestBody": {"content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/FreeForm"}}}},
                },
            },
        },
        "components": {"schemas": {
            "UserInput": {
                "type": "object",
                "required": ["name", "email"],
                "properties": {
                    "name": {"type": "string", "minLength": 2, "maxLength": 20},
                    "email": {"type": "string", "format": "email"},
                    "age": {"type": "integer", "minimum": 18, "maximum": 65},
                    "role": {"type": "string", "enum": ["user", "admin"]},
                    "vip": {"type": "boolean"},
                    "score": {"type": "number", "minimum": 0, "maximum": 100},
                    "dept_id": {"type": "integer"},
                    "profile": {"type": "object", "properties": {"bio": {"type": "string"}}},
                    "address": {"type": "string", "format": "date"},
                },
            },
            "FreeForm": {"type": "object"},
        }},
    }


# --------------------------------------------------------------------------- #
# load_spec
# --------------------------------------------------------------------------- #
class TestLoadSpec:
    def test_from_json_file(self, tmp_path):
        f = tmp_path / "api.json"
        f.write_text(json.dumps(_spec()), encoding="utf-8")
        assert load_spec(str(f))["openapi"] == "3.0.3"

    def test_from_yaml_file(self, tmp_path):
        f = tmp_path / "api.yaml"
        f.write_text(yaml.safe_dump(_spec(), allow_unicode=True), encoding="utf-8")
        assert load_spec(str(f))["paths"]

    def test_from_url(self):
        resp = type("R", (), {"status_code": 200, "text": json.dumps(_spec())})()
        with patch("forgeqa.apidoc.requests.get", return_value=resp):
            spec = load_spec("http://x/openapi.json")
        assert spec["info"]["title"] == "demo"

    def test_missing_file(self):
        with pytest.raises(DataError):
            load_spec("/no/such/file.yaml")

    def test_not_openapi(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text(json.dumps({"foo": 1}), encoding="utf-8")
        with pytest.raises(DataError):
            load_spec(str(f))

    def test_no_paths(self, tmp_path):
        f = tmp_path / "empty.json"
        f.write_text(json.dumps({"openapi": "3.0.0", "paths": {}}), encoding="utf-8")
        with pytest.raises(DataError):
            load_spec(str(f))

    def test_download_error(self):
        import requests as _requests
        with patch("forgeqa.apidoc.requests.get",
                   side_effect=_requests.ConnectionError("refused")):
            with pytest.raises(DataError):
                load_spec("http://x/openapi.json")


# --------------------------------------------------------------------------- #
# resolve_ref
# --------------------------------------------------------------------------- #
class TestResolveRef:
    def test_local_ref(self):
        spec = {"components": {"schemas": {"User": {"type": "object"}}}}
        assert resolve_ref(spec, {"$ref": "#/components/schemas/User"}) == {"type": "object"}

    def test_nested_ref_and_passthrough(self):
        spec = {"components": {"schemas": {"A": {"type": "string"}}}}
        node = {"type": "object", "properties": {"n": {"$ref": "#/components/schemas/A"}}}
        out = resolve_ref(spec, node)
        assert out["properties"]["n"] == {"type": "string"}
        assert out["type"] == "object"

    def test_circular_ref_guard(self):
        spec = {"components": {"schemas": {
            "A": {"type": "object", "properties": {"b": {"$ref": "#/components/schemas/A"}}}}}}
        assert resolve_ref(spec, {"$ref": "#/components/schemas/A"}) is not None

    def test_broken_ref(self):
        assert resolve_ref({}, {"$ref": "#/components/schemas/Nope"}) == {}


# --------------------------------------------------------------------------- #
# iter_operations
# --------------------------------------------------------------------------- #
class TestIterOperations:
    def test_methods_and_refs(self):
        ops = iter_operations(_spec())
        by = {(o.path, o.method): o for o in ops}
        assert set(by) == {("/api/users", "POST"), ("/api/users", "GET"),
                           ("/api/users/{id}", "PUT"), ("/api/users/{id}", "DELETE"),
                           ("/api/orders", "POST")}
        assert by[("/api/users", "POST")].body_schema["properties"]      # $ref 已解析
        assert by[("/api/users/{id}", "PUT")].path_params == ["id"]
        assert by[("/api/users/{id}", "DELETE")].has_path_param

    def test_path_param_from_template(self):
        spec = {"paths": {"/api/items/{itemId}": {"delete": {}}}}
        ops = iter_operations(spec)
        assert ops[0].path_params == ["itemId"]


# --------------------------------------------------------------------------- #
# schema_to_fields
# --------------------------------------------------------------------------- #
class TestSchemaToFields:
    def _fields(self, sch):
        return {f["name"]: f for f in schema_to_fields("t", sch, {})["fields"]}

    def test_mapping(self):
        spec = _spec()
        out = schema_to_fields("user", spec["components"]["schemas"]["UserInput"], spec)
        fs = {f["name"]: f for f in out["fields"]}
        assert out["required"] == ["name", "email"]
        assert fs["name"]["gen"] == "faker" and fs["name"]["min_len"] == 2
        assert fs["email"] == {"name": "email", "gen": "faker", "method": "email"}
        assert fs["age"]["min"] == 18 and fs["age"]["max"] == 65
        assert fs["role"] == {"name": "role", "gen": "choice", "values": ["user", "admin"]}
        assert fs["vip"] == {"name": "vip", "gen": "bool", "p": 0.5}
        assert fs["score"]["gen"] == "float"
        assert fs["dept_id"]["gen"] == "seq"                      # *_id → 自增序列
        assert fs["profile"]["gen"] == "const"                    # 嵌套对象 → 占位
        assert fs["address"]["gen"] == "datetime"                 # format: date

    def test_no_properties_raises(self):
        with pytest.raises(DataError):
            schema_to_fields("t", {"type": "object"}, {})

    def test_unknown_string_falls_back(self):
        fs = self._fields({"type": "object",
                           "properties": {"thing": {"type": "string"}}})
        assert fs["thing"]["gen"] == "faker"

    def test_phone_hint(self):
        fs = self._fields({"type": "object",
                           "properties": {"mobile_phone": {"type": "string"}}})
        assert fs["mobile_phone"]["gen"] == "fake_phone"


# --------------------------------------------------------------------------- #
# build_import_docs
# --------------------------------------------------------------------------- #
class TestBuildImportDocs:
    def test_doc_shapes(self):
        ops = iter_operations(_spec())
        docs, schemas, drafts = build_import_docs(ops, _spec(), "demo")

        ids = [d["id"] for d in docs]
        # POST /api/users → 正常 + 变异；POST /api/orders 的请求体无 properties → 不生成
        normal = next(d for d in docs
                      if d["steps"][0].get("http", {}).get("method") == "POST")
        mut = next(d for d in docs if "MUT" in d["id"])
        assert normal["id"] in ids and mut["id"] in ids
        assert normal["steps"][0]["http"] == {
            "method": "POST", "path": "/api/users", "json": "${data.users}"}
        assert set(schemas) == {"users"}                             # FreeForm 被跳过

        # GET 冒烟 + PUT/DELETE 草稿
        assert any(d["steps"][0]["http"]["method"] == "GET" for d in docs)
        assert any("PUT /api/users/{id}" in line for line in drafts)
        assert any("DELETE /api/users/{id}" in line for line in drafts)

    def test_post_with_path_param_is_draft(self):
        spec = {"paths": {"/api/users/{id}/roles": {
            "post": {"requestBody": {"content": {"application/json": {
                "schema": {"type": "object",
                           "properties": {"role": {"type": "string"}}}}}}}}}}
        docs, schemas, drafts = build_import_docs(iter_operations(spec), spec, "x")
        assert docs == [] and schemas == {}
        assert len(drafts) == 1 and "路径参数" in drafts[0]

    def test_post_without_body_goes_to_draft(self):
        spec = {"paths": {"/api/ping": {"post": {}}}}
        docs, schemas, drafts = build_import_docs(iter_operations(spec), spec, "x")
        assert docs == [] and len(drafts) == 1

    def test_entity_dedupe(self):
        spec = {"paths": {
            "/api/users": {"post": {"requestBody": {"content": {"application/json": {
                "schema": {"type": "object",
                           "properties": {"a": {"type": "string"}}}}}}}},
            "/api/accounts": {"post": {"requestBody": {"content": {"application/json": {
                "schema": {"type": "object",
                           "properties": {"a": {"type": "string"}}}}}}}},
        }}
        spec["paths"]["/api/accounts"]["post"]["operationId"] = "users"
        docs, schemas, _ = build_import_docs(iter_operations(spec), spec, "x")
        # 实体名以路径为准（operationId 被忽略），两个路径 → 两个实体
        assert sorted(schemas) == ["accounts", "users"]


# --------------------------------------------------------------------------- #
# import_spec 端到端（产物 + 可运行性）
# --------------------------------------------------------------------------- #
class TestImportSpec:
    def test_writes_files_and_loadable(self, tmp_path):
        result = import_spec(_spec(), name="demo-api",
                             cases_dir=tmp_path / "cases" / "_generated",
                             schemas_dir=tmp_path / "config" / "schemas")
        assert result.post_cases == 1 and result.get_cases == 1 and result.op_total == 5
        assert result.case_file is not None
        assert result.case_file.name == "_import_demo_api.yaml"
        assert (tmp_path / "config" / "schemas" / "users.yaml").exists()

        # 用例文件显式可加载；目录扫描默认跳过（_ 前缀）
        cases = load_cases([str(result.case_file)], tmp_path)
        assert len(cases) == 3                                  # GET 冒烟 + POST 正常 + 变异
        assert any("MUT" in c.id for c in cases)
        with pytest.raises(CaseError):
            load_cases([tmp_path / "cases"], tmp_path)

        # 导入的 schema 能真正造出数据（正常行 + 变异数据）
        schema_path = tmp_path / "config" / "schemas" / "users.yaml"
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        factory = DataFactory(Context(seed=20260922, faker_locale="zh_CN"))
        row = factory.generate(schema)[0]
        assert isinstance(row["name"], str) and row["role"] in ("user", "admin")
        assert isinstance(row["vip"], bool)
        muts = factory.mutate(schema, categories=["boundary", "abnormal"])
        assert muts and all("data" in m for m in muts)

    def test_existing_schema_backed_up(self, tmp_path):
        schemas_dir = tmp_path / "config" / "schemas"
        schemas_dir.mkdir(parents=True)
        (schemas_dir / "users.yaml").write_text("entity: users\nfields: []\n", encoding="utf-8")

        result = import_spec(_spec(), name="demo",
                             cases_dir=tmp_path / "c", schemas_dir=schemas_dir)
        assert {p.name for p, _ in result.schemas} == {"users.imported.yaml"}
        assert (schemas_dir / "users.yaml").read_text(encoding="utf-8").startswith("entity: users")

        result2 = import_spec(_spec(), name="demo",
                              cases_dir=tmp_path / "c", schemas_dir=schemas_dir, force=True)
        assert {p.name for p, _ in result2.schemas} == {"users.yaml"}

    def test_no_executable_output(self, tmp_path):
        spec = {"openapi": "3.0.0",
                "paths": {"/api/ping": {"post": {"requestBody": {"content": {
                    "application/json": {"schema": {"type": "object"}}}}}}}}
        result = import_spec(spec, name="x",
                             cases_dir=tmp_path / "c", schemas_dir=tmp_path / "s")
        assert result.case_file is None and result.schemas == []


# --------------------------------------------------------------------------- #
# Operation 基本属性
# --------------------------------------------------------------------------- #
class TestOperation:
    def test_entity_naming(self):
        assert Operation(path="/api/users", method="POST").entity == "users"
        # operationId 仅在路径推导不出实体时使用
        assert Operation(path="/", method="POST", op_id="createOrder").entity == "createorder"
        assert Operation(path="/x", method="POST", op_id="createOrder").entity == "x"
