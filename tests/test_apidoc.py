"""apidoc 模块的单元测试：OpenAPI/Swagger 文档 → 写接口用例草稿。网络层用 mock 覆盖。"""
from __future__ import annotations

import json
import re
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
from forgeqa.config import ForgeConfig
from forgeqa.errors import CaseError, DataError
from forgeqa.factory import Context, DataFactory
from forgeqa.runner import _eval_condition, load_cases
from forgeqa.scan import AUTH_GUARD


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


class TestOpenApi31Shapes:
    """OpenAPI 3.1 / Pydantic v2 的写法：可选字段是 anyOf: [T, null]，不是 type+nullable。

    不摊平的话 `type` 键缺失 → 一律按字符串处理 → 数组被造成长单词、对象被造成长单词。
    """

    def _fields(self, sch):
        return {f["name"]: f for f in schema_to_fields("t", sch, {})["fields"]}

    def _one(self, prop):
        return self._fields({"type": "object", "properties": {"f": prop}})["f"]

    def test_nullable_string_keeps_string_gen(self):
        f = self._one({"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Key"})
        assert f["gen"] == "faker" and f["method"] == "word"

    def test_nullable_array_is_not_a_string(self):
        f = self._one({"anyOf": [{"items": {"type": "string", "format": "uri"},
                                  "type": "array"}, {"type": "null"}]})
        assert f["gen"] == "list"                       # 修复前是 faker word
        assert f["item"]["method"] == "url"

    def test_nullable_object_is_placeholder_not_string(self):
        f = self._one({"anyOf": [{"type": "object", "properties": {"a": {"type": "string"}}},
                                 {"type": "null"}]})
        assert f["gen"] == "const" and f["value"] == {}
        assert "嵌套对象" in f["description"]

    def test_nullable_boolean(self):
        f = self._one({"anyOf": [{"type": "boolean"}, {"type": "null"}]})
        assert f["gen"] == "bool"

    def test_type_as_list(self):
        f = self._one({"type": ["integer", "null"], "minimum": 3})
        assert f["gen"] == "int" and f["min"] == 3

    def test_null_only_type(self):
        f = self._one({"type": "null"})
        assert f["gen"] == "const" and f["value"] is None

    def test_const(self):
        f = self._one({"const": "normal"})
        assert f == {"name": "f", "gen": "const", "value": "normal"}

    def test_pattern_becomes_regex(self):
        f = self._one({"type": "string", "pattern": "^[A-Z0-9-]+$",
                       "minLength": 6, "maxLength": 32})
        assert f["gen"] == "regex" and f["pattern"] == "^[A-Z0-9-]+$"
        assert "method" not in f                        # 不再残留 faker provider
        # 长度约束保留：值生成用不到，但变异造数靠它产出长度边界用例
        assert f["min_len"] == 6 and f["max_len"] == 32

    def test_complex_pattern_annotated_not_faked(self):
        f = self._one({"type": "string", "pattern": "^(a|b)-\\d+$"})
        assert f["gen"] != "regex"                      # 展开器不保证满足，宁可不换
        assert "正则" in f["description"]

    def test_array_of_scalars_counts_from_min_max_items(self):
        f = self._one({"type": "array", "items": {"type": "string"},
                       "minItems": 2, "maxItems": 4})
        assert f["gen"] == "list" and f["count"] == [2, 4]

    def test_array_of_objects_is_placeholder(self):
        f = self._one({"type": "array", "items": {"type": "object",
                                                 "properties": {"a": {"type": "string"}}}})
        assert f["gen"] == "const" and f["value"] == []

    def test_all_of_merges_properties_and_required(self):
        f = self._one({"allOf": [
            {"type": "object", "properties": {"a": {"type": "string"}},
             "required": ["a"]},
            {"type": "object", "properties": {"b": {"type": "integer"}},
             "required": ["b"]},
        ]})
        assert f["gen"] == "const"                      # 合并后是对象 → 占位
        out = schema_to_fields("t", {"type": "object", "properties": {
            "f": {"allOf": [{"type": "object", "properties": {"a": {"type": "string"}}},
                            {"type": "object", "properties": {"b": {"type": "integer"}}}]}}}, {})
        assert {x["name"] for x in out["fields"]} == {"f"}   # allOf 不炸，外层照常产出


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

    def test_openapi31_end_to_end(self, tmp_path):
        """3.1 文档（FastAPI 形态）导入后，造出的数据类型正确，长度边界变异仍在。"""
        spec = {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1"},
            "paths": {"/api/orders": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "$ref": "#/components/schemas/Order"}}}},
                "responses": {"200": {"description": "ok"}}}}},
            "components": {"schemas": {"Order": {
                "type": "object",
                "required": ["sku"],
                "properties": {
                    "sku": {"type": "string", "pattern": "^[A-Z0-9-]+$",
                            "minLength": 6, "maxLength": 32},
                    "priority": {"const": "normal"},
                    "tags": {"anyOf": [{"type": "array", "items": {"type": "string"}},
                                       {"type": "null"}]},
                    "config": {"anyOf": [{"type": "object",
                                          "properties": {"a": {"type": "string"}}},
                                         {"type": "null"}]},
                    "enabled": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
                }}}},
        }
        import_spec(spec, name="owui", cases_dir=tmp_path / "c", schemas_dir=tmp_path / "s")
        schema = yaml.safe_load((tmp_path / "s" / "orders.yaml").read_text(encoding="utf-8"))

        factory = DataFactory(Context(seed=20260922, faker_locale="zh_CN"))
        row = factory.generate(schema)[0]
        assert re.fullmatch(r"[A-Z0-9-]+", row["sku"])        # 文档正则真的被展开
        assert row["priority"] == "normal"                    # const 原样落地
        assert isinstance(row["tags"], list)                  # 修复前：被造成一个字符串
        assert isinstance(row["enabled"], bool)               # 修复前：被造成一个字符串
        assert row["config"] == {}                            # 嵌套对象仍占位，待人工补

        muts = factory.mutate(schema, categories=["boundary"])
        assert any("sku" in m["description"] and "长度" in m["description"] for m in muts)


# --------------------------------------------------------------------------- #
# Operation 基本属性
# --------------------------------------------------------------------------- #
class TestOperation:
    def test_entity_naming(self):
        assert Operation(path="/api/users", method="POST").entity == "users"
        # operationId 仅在路径推导不出实体时使用
        assert Operation(path="/", method="POST", op_id="createOrder").entity == "createorder"
        assert Operation(path="/x", method="POST", op_id="createOrder").entity == "x"


# --------------------------------------------------------------------------- #
# 鉴权守卫：文档 security → 生成用例的 skip_if
# --------------------------------------------------------------------------- #
def _auth_spec(security_op=None, security_root=None) -> dict:
    """一份带 security 的最小文档：/api/open 公开，/api/secret 需鉴权。"""
    spec: dict = {
        "openapi": "3.1.0",
        "info": {"title": "auth demo", "version": "1"},
        "paths": {
            "/api/open": {"get": {"operationId": "open"}},
            "/api/secret": {"get": {"operationId": "secret"}},
            "/api/secret/items": {
                "post": {
                    "operationId": "createItem",
                    "requestBody": {"content": {"application/json": {
                        "schema": {"type": "object",
                                   "properties": {"name": {"type": "string"}},
                                   "required": ["name"]}}}},
                },
            },
        },
    }
    if security_op is not None:
        spec["paths"]["/api/secret"]["get"]["security"] = security_op
    if security_root is not None:
        spec["security"] = security_root
    return spec


class TestNeedsAuthDetection:
    def test_operation_level_security(self):
        ops = {o.path: o for o in iter_operations(_auth_spec(security_op=[{"HTTPBearer": []}]))}
        assert ops["/api/secret"].needs_auth is True
        assert ops["/api/open"].needs_auth is False

    def test_global_security_is_inherited(self):
        ops = {o.path: o for o in iter_operations(_auth_spec(security_root=[{"HTTPBearer": []}]))}
        assert ops["/api/secret"].needs_auth is True
        assert ops["/api/open"].needs_auth is True

    def test_empty_security_opts_out_of_global(self):
        """`security: []` 是「本接口公开」的显式声明，不能被全文级覆盖。"""
        spec = _auth_spec(security_op=[], security_root=[{"HTTPBearer": []}])
        ops = {o.path: o for o in iter_operations(spec)}
        assert ops["/api/secret"].needs_auth is False
        assert ops["/api/open"].needs_auth is True

    def test_no_security_anywhere(self):
        ops = {o.path: o for o in iter_operations(_auth_spec())}
        assert not any(o.needs_auth for o in ops.values())


class TestAuthGuardEmission:
    """没配 auth 时，需鉴权的生成用例必须整条跳过，而不是假通过。"""

    def _docs(self, spec):
        return build_import_docs(iter_operations(spec), spec, "authdemo")[0]

    @staticmethod
    def _path(doc: dict) -> str:
        """取用例打的目标路径：普通步骤在 step.http，变异用例嵌在 loop.steps[0]。"""
        step = doc["steps"][0]
        if "http" in step:
            return step["http"]["path"]
        return step["loop"]["steps"][0]["http"]["path"]

    def test_guarded_cases_carry_skip_if(self):
        docs = self._docs(_auth_spec(security_root=[{"HTTPBearer": []}]))
        guarded = [d for d in docs if d.get("skip_if")]
        assert guarded, "文档标了 security，生成用例必须带守卫"
        assert all(d["skip_if"] == AUTH_GUARD for d in guarded)
        # 守卫要覆盖该接口的全部生成用例（GET 冒烟 + POST 正常路径 + 变异）
        paths = {self._path(d) for d in guarded}
        assert {"/api/secret", "/api/secret/items"} <= paths

    def test_public_cases_have_no_guard(self):
        docs = self._docs(_auth_spec(security_op=[{"HTTPBearer": []}]))
        open_docs = [d for d in docs if self._path(d) == "/api/open"]
        assert open_docs and all("skip_if" not in d for d in open_docs)

    def test_none_needs_auth_means_no_guard_anywhere(self):
        docs = self._docs(_auth_spec())
        assert all("skip_if" not in d for d in docs)

    def test_guard_survives_roundtrip_into_case_file(self, tmp_path):
        """守卫要真的落进 YAML 并被用例加载器读出来。"""
        spec = _auth_spec(security_root=[{"HTTPBearer": []}])
        result = import_spec(spec, name="authdemo",
                             cases_dir=tmp_path / "c", schemas_dir=tmp_path / "s")
        cases = load_cases([str(result.case_file)], tmp_path)
        assert cases and all(c.skip_if for c in cases)
        assert all(c.skip_if == AUTH_GUARD for c in cases)

    # ------------------------------------------------------------------ #
    # 求值层面：守卫必须真的「为真」，只比字符串是抓不到 bug 的
    # ------------------------------------------------------------------ #
    @staticmethod
    def _config(tmp_path, env_yaml: str, env: str = "local"):
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "env.yaml").write_text(env_yaml, encoding="utf-8")
        return ForgeConfig.load(tmp_path / "config" / "env.yaml", env=env, root=tmp_path)

    #: 最常见的写法：auth 挂在 defaults 下面（不是顶层）
    _AUTH_IN_DEFAULTS = """
default_env: local
defaults:
  auth:
    type: {kind}
envs:
  local:
    base_url: http://127.0.0.1:8080
"""

    def test_truthy_when_auth_written_under_defaults(self, tmp_path):
        """回归锁：auth 写在 defaults 下时 ${cfg.auth.type} 必须取得到值。

        原先 cfg 层只挂配置文件顶层，取不到的后果不是「取到默认值」而是
        `None == 'none'` 恒假 —— 守卫静默失效，整批用例照跑并报一堆 401。
        """
        cfg = self._config(tmp_path, self._AUTH_IN_DEFAULTS.format(kind="none"))
        assert _eval_condition(cfg.context(), AUTH_GUARD) is True

    def test_falsy_when_auth_configured(self, tmp_path):
        cfg = self._config(tmp_path, self._AUTH_IN_DEFAULTS.format(kind="bearer"))
        assert _eval_condition(cfg.context(), AUTH_GUARD) is False

    def test_truthy_when_auth_absent_entirely(self, tmp_path):
        cfg = self._config(tmp_path, "default_env: local\nenvs:\n  local:\n"
                                     "    base_url: http://127.0.0.1:8080\n")
        assert _eval_condition(cfg.context(), AUTH_GUARD) is True

    def test_auth_set_by_cli_override(self, tmp_path):
        """--set auth.type=bearer 也要能让守卫放行（走 CLI 的真实解析路径）。"""
        from forgeqa.cli import _overrides

        self._config(tmp_path, "default_env: local\nenvs:\n  local:\n"
                               "    base_url: http://127.0.0.1:8080\n")
        cfg = ForgeConfig.load(tmp_path / "config" / "env.yaml", env="local",
                               overrides=_overrides(["auth.type=bearer"]), root=tmp_path)
        assert _eval_condition(cfg.context(), AUTH_GUARD) is False
