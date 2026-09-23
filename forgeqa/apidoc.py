"""接口文档导入：从 OpenAPI/Swagger 文档生成写接口用例草稿。

与 `scan`（在线探测）互补：`scan` 只能自动生成 GET 冒烟，写接口（POST/PUT/PATCH/DELETE）
的请求体结构在线探测猜不出来；但如果拿到接口文档（OpenAPI 3 / Swagger 2，JSON 或 YAML，
文件路径或 URL 均可），请求体 Schema 就写在文档里——本模块把它翻译成 ForgeQA 的
造数 Schema 与用例草稿。

用法（``forgeqa import <文档路径或URL>``）：

- ``config/schemas/<entity>.yaml``           从 requestBody Schema 翻译的造数 Schema（需人工核对）
- ``cases/_generated/_import_<name>.yaml``   可直接运行的 POST 用例（正常路径 + 边界变异循环）
                                             + GET 冒烟 + 注释形式的带路径参数/其他方法草稿

翻译约定（JSON Schema → 造数 Schema）：

- ``enum``                     → ``gen: choice``
- ``const``                    → ``gen: const``（原样落地）
- ``string`` + format/name 语义 → faker 各 provider / fake_phone 等脱敏生成器
- ``string`` + pattern         → ``gen: regex``（引擎按简单正则子集展开；含分组/``|`` 时退回普通造数并标注）
- ``string`` + minLength/maxLength → ``min_len`` / ``max_len``（同时驱动变异造数）
- ``integer`` / ``number``     → ``gen: int`` / ``gen: float``（id 类字段用 ``gen: seq``）
- ``boolean``                  → ``gen: bool``
- ``array`` + 标量 items       → ``gen: list``（元素按 items 造数，条数取 minItems~maxItems）
- ``object`` / ``array`` + 对象 items → ``gen: const`` 占位（造数引擎暂不支持嵌套生成，需人工补全）

OpenAPI 3.1 / Pydantic v2 的写法（可选字段是 ``anyOf: [T, null]`` 而非 ``type`` + ``nullable``）
由 :func:`_unwrap_schema` 先摊平成单类型再走上面的约定——不摊平的话 ``type`` 键缺失，
数组和对象都会被当作普通字符串造数。

因为文档里的业务约束（必填语义、枚举含义）机器读不全，产出全部定位为**草稿**：
文件名 ``_`` 前缀保证默认 ``--cases cases`` 不会误跑，人工核对后再转正。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import yaml

from .errors import DataError
from .scan import AUTH_GUARD, entity_for

# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Operation:
    """文档里的一个接口操作。"""

    path: str
    method: str                       # 大写：POST / GET / ...
    op_id: str = ""                   # operationId（存在时用作实体名）
    summary: str = ""
    body_schema: dict[str, Any] | None = None   # application/json 请求体 schema（$ref 已解析）
    path_params: list[str] = field(default_factory=list)
    needs_auth: bool = False          # 文档标了 security（操作级或全文级）

    @property
    def entity(self) -> str:
        # 实体名优先从路径推导（/api/users → users），路径推导不出（root）再用 operationId
        from_path = entity_for(self.path)
        if from_path == "root" and self.op_id:
            return _safe_entity(self.op_id)
        return _safe_entity(from_path)

    @property
    def has_path_param(self) -> bool:
        return bool(self.path_params)


@dataclass
class ImportResult:
    name: str
    schemas: list[tuple[Path, str]] = field(default_factory=list)   # (路径, 状态)
    case_file: Path | None = None
    op_total: int = 0
    post_cases: int = 0
    get_cases: int = 0
    drafts: list[str] = field(default_factory=list)                 # 注释草稿行


# --------------------------------------------------------------------------- #
# 文档加载与 $ref 解析
# --------------------------------------------------------------------------- #
def load_spec(source: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """从文件路径或 URL 加载 OpenAPI/Swagger 文档（JSON 或 YAML）。"""
    text: str
    if source.startswith(("http://", "https://")):
        try:
            resp = requests.get(source, timeout=timeout)
        except requests.RequestException as exc:
            raise DataError(f"接口文档下载失败: {source}",
                            hint="检查 URL 是否可达；内网文档可先下载到本地再导入") from exc
        if resp.status_code != 200:
            raise DataError(f"接口文档下载失败: HTTP {resp.status_code} — {source}",
                            hint="检查 URL 与鉴权要求")
        text = resp.text
    else:
        p = Path(source)
        if not p.exists():
            raise DataError(f"接口文档不存在: {source}",
                            hint="传入 OpenAPI/Swagger 的 JSON/YAML 文件路径或 URL")
        text = p.read_text(encoding="utf-8")

    try:
        spec = json.loads(text)
    except ValueError:
        try:
            spec = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise DataError(f"接口文档解析失败（既不是合法 JSON 也不是合法 YAML）: {source}",
                            hint="确认导出格式：OpenAPI 3 / Swagger 2 的 JSON 或 YAML") from exc

    if not isinstance(spec, dict) or not ("openapi" in spec or "swagger" in spec):
        raise DataError("这不是 OpenAPI/Swagger 文档（缺少 openapi/swagger 版本键）",
                        hint="Apifox / Postman 可直接导出 OpenAPI 格式后再导入；"
                             "Postman Collection 暂不支持")
    if not spec.get("paths"):
        raise DataError("文档里没有 paths 段，没有可导入的接口",
                        hint="确认导出时包含了接口定义")
    return spec


def resolve_ref(spec: dict[str, Any], node: Any, _depth: int = 0) -> Any:
    """递归解析本地 $ref（#/components/schemas/...、#/definitions/...）。"""
    if _depth > 12:                       # 循环引用守卫
        return {}
    if isinstance(node, list):
        return [resolve_ref(spec, x, _depth) for x in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if not ref or not isinstance(ref, str) or not ref.startswith("#/"):
        return {k: resolve_ref(spec, v, _depth + 1) for k, v in node.items()}
    target: Any = spec
    for seg in ref[2:].split("/"):
        seg = seg.replace("~1", "/").replace("~0", "~")
        target = target.get(seg) if isinstance(target, dict) else None
        if target is None:
            return {}
    return resolve_ref(spec, target, _depth + 1)


def iter_operations(spec: dict[str, Any]) -> list[Operation]:
    """展开 paths 段为操作清单（忽略无 requestBody 的非 GET 不生成）。"""
    ops: list[Operation] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        path_params = [p.get("name") for p in resolve_ref(spec, item.get("parameters")) or []
                       if isinstance(p, dict) and p.get("in") == "path" and p.get("name")]
        for key, op in item.items():
            if key.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            if not isinstance(op, dict):
                continue
            op = resolve_ref(spec, op)
            body_schema: dict | None = None
            body = resolve_ref(spec, op.get("requestBody") or {})
            content = body.get("content") if isinstance(body, dict) else None
            if isinstance(content, dict):
                for ctype in ("application/json", "application/x-www-form-urlencoded",
                              "text/plain"):
                    if ctype in content:
                        sch = resolve_ref(spec, content[ctype].get("schema") or {})
                        body_schema = sch if isinstance(sch, dict) and sch else None
                        break
            params = [p.get("name") for p in resolve_ref(spec, op.get("parameters")) or []
                      if isinstance(p, dict) and p.get("in") == "path" and p.get("name")]
            # 鉴权要求：操作级 security 优先，未声明则继承全文级。
            # 显式写 `security: []` 表示「本接口公开」，不能被全文级覆盖。
            op_sec = op.get("security")
            needs_auth = bool(spec.get("security")) if op_sec is None else bool(op_sec)
            ops.append(Operation(
                path=str(path), method=key.upper(),
                op_id=str(op.get("operationId") or ""),
                summary=str(op.get("summary") or op.get("description") or ""),
                body_schema=body_schema,
                path_params=sorted(set(path_params) | {p for p in path_params if p in str(path)}),
                needs_auth=needs_auth,
            ))
    # 路径模板里的 {id} 本身就是路径参数
    for op in ops:
        op.path_params = sorted({*op.path_params, *re.findall(r"\{([^{}]+)\}", op.path)})
    return ops


# --------------------------------------------------------------------------- #
# JSON Schema → 造数 Schema
# --------------------------------------------------------------------------- #
_NAME_HINTS: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
    (("phone", "mobile", "tel"), {"gen": "fake_phone"}),
    (("email", "mail"), {"gen": "faker", "method": "email"}),
    (("city",), {"gen": "faker", "method": "city"}),
    (("address", "addr"), {"gen": "faker", "method": "address"}),
    (("company", "org"), {"gen": "faker", "method": "company"}),
    (("idcard", "id_card"), {"gen": "fake_id_card"}),
    (("nickname", "fullname", "full_name"), {"gen": "faker", "method": "name"}),
    (("url", "link", "href"), {"gen": "faker", "method": "url"}),
    (("title", "desc", "comment", "content", "remark"), {"gen": "faker", "method": "sentence"}),
    (("password", "passwd", "pwd"), {"gen": "pattern", "pattern": "Qa#??????"}),
    (("name", "username", "user"), {"gen": "faker", "method": "name"}),
)
_FALLBACK_STR = {"gen": "faker", "method": "word"}


def _string_field(name: str, sch: dict[str, Any]) -> dict[str, Any]:
    field_def: dict[str, Any]
    fmt = str(sch.get("format", "")).lower()
    if fmt == "email":
        field_def = {"gen": "faker", "method": "email"}
    elif fmt in ("date", "date-time"):
        field_def = {"gen": "datetime", "start": "-30d", "end": "now",
                     "fmt": "%Y-%m-%d" if fmt == "date" else "%Y-%m-%d %H:%M:%S"}
    elif fmt == "uuid":
        field_def = {"gen": "uuid"}
    elif fmt in ("uri", "url"):
        field_def = {"gen": "faker", "method": "url"}
    else:
        lname = name.lower()
        field_def = next((dict(hint) for keys, hint in _NAME_HINTS if any(k in lname for k in keys)),
                         dict(_FALLBACK_STR))
    for k in ("min_len", "max_len"):
        src = {"min_len": "minLength", "max_len": "maxLength"}[k]
        if sch.get(src):
            field_def[k] = int(sch[src])          # type: ignore[assignment]
    return field_def


def _unwrap_schema(sch: Any) -> dict[str, Any]:
    """把 OpenAPI 3.1 / Pydantic v2 的联合与继承写法摊平成单类型 schema。

    3.1（FastAPI 生成文档的主形态）里可选字段不是 ``type: string`` + ``nullable``，
    而是 ``anyOf: [{type: string}, {type: null}]``；不摊平的话 ``type`` 键缺失，
    会被当成普通字符串——数组被造成一个长单词、对象被造成一个长单词，全是错的。
    """
    if not isinstance(sch, dict) or not sch:
        return {}
    out = dict(sch)

    for key in ("anyOf", "oneOf"):
        branches = out.pop(key, None)
        if isinstance(branches, list) and branches:
            real = [b for b in branches
                    if not (isinstance(b, dict) and b.get("type") == "null")]
            picked = _unwrap_schema(real[0]) if real else {}
            for k, v in picked.items():            # 分支缺的键由外层补（外层约束更贴调用点）
                if out.get(k) is None:
                    out[k] = v

    branches = out.pop("allOf", None)
    if isinstance(branches, list) and branches:
        merged: dict[str, Any] = {k: v for k, v in out.items() if k != "allOf"}
        for b in branches:
            for k, v in _unwrap_schema(b).items():
                if k == "properties" and isinstance(merged.get("properties"), dict):
                    merged["properties"] = {**v, **merged["properties"]}
                elif k == "required" and isinstance(merged.get("required"), list):
                    merged["required"] = list(dict.fromkeys(list(merged["required"]) + list(v)))
                elif merged.get(k) is None:
                    merged[k] = v
        out = merged

    t = out.get("type")
    if isinstance(t, list):                        # 3.1 允许 type: ["string", "null"]
        real_t = [x for x in t if x != "null"]
        out["type"] = real_t[0] if real_t else "null"
    return out


#: 引擎的正则展开器只覆盖简单子集（字面量/字符类/\d\w/{n,m}/+*/?）。
#: 出现分组或选择分支时不保证满足约束，宁可退回普通造数并标注，也不造假通过。
_REGEX_TOO_COMPLEX = ("(", "|")


def _schema_to_field(name: str, sch: dict[str, Any]) -> dict[str, Any]:
    sch = _unwrap_schema(sch)
    if not sch:
        return {"name": name, "gen": "const", "value": None}
    if "const" in sch:
        return {"name": name, "gen": "const", "value": sch["const"]}
    if "enum" in sch and sch["enum"]:
        return {"name": name, "gen": "choice", "values": list(sch["enum"])}
    t = sch.get("type")
    if t == "null":
        return {"name": name, "gen": "const", "value": None}
    if t == "array":
        return _array_field(name, sch)
    if t == "object":
        return {"name": name, "gen": "const", "value": {},
                "description": "嵌套对象，造数引擎暂不支持自动生成，请人工补全"}
    if t == "string" or (t is None and "properties" not in sch):
        out = {"name": name, **_string_field(name, sch)}
        pat = str(sch.get("pattern", ""))
        if pat and not any(c in pat for c in _REGEX_TOO_COMPLEX):
            out["gen"] = "regex"                   # 引擎按简单正则子集展开，能真实满足约束
            out["pattern"] = pat
            out.pop("method", None)                # 换成 regex 后不再用 faker provider
            # min_len/max_len 保留：值生成用不到，但变异造数靠它产出长度边界用例
        elif pat:
            out["description"] = f"文档要求满足正则 {pat}，请人工核对"
        return out
    if t == "integer":
        out: dict[str, Any] = {"name": name, "gen": "int",
                               "min": int(sch.get("minimum", 0)),
                               "max": int(sch.get("maximum", 9999))}
        if name.lower() in ("id", "uid", "pk") or name.lower().endswith("_id"):
            out = {"name": name, "gen": "seq", "start": 1, "step": 1}
        return out
    if t == "number":
        return {"name": name, "gen": "float",
                "min": float(sch.get("minimum", 0)),
                "max": float(sch.get("maximum", 9999)), "precision": 2}
    if t == "boolean":
        return {"name": name, "gen": "bool", "p": 0.5}
    return {"name": name, "gen": "const", "value": None}


def _array_field(name: str, sch: dict[str, Any]) -> dict[str, Any]:
    """数组字段：元素可自动造数时用 ``gen: list`` 真实生成，元素是嵌套结构则占位。"""
    item = _schema_to_field(f"{name}[i]", sch.get("items") or {})
    if item.get("gen") == "const":
        why = "数组元素为嵌套结构" if sch.get("items") else "数组未声明元素类型"
        return {"name": name, "gen": "const", "value": [],
                "description": f"{why}，造数引擎暂不支持自动生成，请人工补全"}
    lo = max(1, int(sch.get("minItems", 1) or 1))
    hi = max(lo, int(sch.get("maxItems", max(lo, 3)) or lo))
    spec = {k: v for k, v in item.items() if k != "name"}
    return {"name": name, "gen": "list", "count": [lo, hi], "item": spec}


def schema_to_fields(entity: str, sch: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """requestBody 的 JSON Schema → ForgeQA 造数 Schema。"""
    sch = _unwrap_schema(resolve_ref(spec, sch))
    props = sch.get("properties") if isinstance(sch, dict) else None
    if not isinstance(props, dict) or not props:
        raise DataError(f"实体 {entity!r} 的请求体没有 properties 定义",
                        hint="文档请求体可能是自由格式（无 schema）；这类接口请参考 "
                             "cases/api_user_crud.yaml 手工编写用例")
    fields = [_schema_to_field(str(n), p or {}) for n, p in props.items()]
    required = [str(r) for r in (sch.get("required") or []) if isinstance(r, str)]
    out: dict[str, Any] = {"entity": entity, "count": 1, "fields": fields,
                           "description": "由接口文档 requestBody 翻译生成（枚举含义、必填"
                                          "语义、长度上限请人工核对）"}
    if required:
        out["required"] = required
    return out


# --------------------------------------------------------------------------- #
# 用例生成
# --------------------------------------------------------------------------- #
def _safe_entity(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", raw).strip("_").lower()
    return name or "entity"


def build_import_docs(ops: list[Operation], spec: dict[str, Any], name: str) \
        -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    """返回 (可执行用例, {实体名: 造数schema}, 注释草稿行)。

    文档标了 ``security`` 的操作（``needs_auth``），其生成用例统一加
    ``scan.AUTH_GUARD`` 守卫：没配 auth 时整条跳过，而不是被 401 打成红灯
    （GET 冒烟）或被宽断言算成通过（POST，假绿，最危险）。

    可执行：POST 无路径参数（正常路径 + 边界变异）、GET 无路径参数（冒烟）。
    草稿：带路径参数的写接口、PUT/PATCH/DELETE。
    """
    docs: list[dict[str, Any]] = []
    schemas: dict[str, dict[str, Any]] = {}
    drafts: list[str] = []
    seq = 0

    def unique_entity(op: Operation) -> str:
        base = op.entity
        final, n = base, 2
        while final in schemas:
            final = f"{base}_{n}"
            n += 1
        return final

    for op in sorted(ops, key=lambda o: (o.path, o.method)):
        mark = len(docs)                     # 本次循环新增的用例起点，循环末尾统一加守卫
        if op.method == "POST" and not op.has_path_param and op.body_schema:
            entity = unique_entity(op)
            try:
                schemas[entity] = schema_to_fields(entity, op.body_schema, spec)
            except DataError:
                drafts.append(f"# {op.method} {op.path} — 请求体无 properties，"
                              f"请参考 cases/api_user_crud.yaml 手工编写")
                continue
            seq += 1
            docs.append({
                "id": f"TC-IMP-{seq:03d}",
                "title": f"{op.method} {op.path} 正常路径（由接口文档生成）",
                "priority": "P1",
                "layer": "api",
                "tags": ["import", "smoke"],
                "data": {entity: f"{entity}.yaml"},
                "steps": [{
                    "name": f"{op.method} {op.path}",
                    "http": {"method": op.method, "path": op.path,
                             "json": "${data." + entity + "}"},
                    "assert": [
                        {"status": {"min": 200, "max": 499}, "label": "不应出现 5xx"},
                        {"time_lt": 5000},
                    ],
                }],
            })
            # 边界变异用例：同一实体只加一条
            docs.append({
                "id": f"TC-IMP-MUT-{seq:03d}",
                "title": f"{op.method} {op.path} 边界与异常输入",
                "priority": "P2",
                "layer": "api",
                "tags": ["import", "boundary", "negative"],
                "data": {
                    "muts": {"schema": f"{entity}.yaml", "mutate": True,
                             "categories": ["boundary", "abnormal"]},
                },
                "steps": [{
                    "name": "变异数据逐条打接口",
                    "loop": {
                        "over": "${data.muts}", "as": "m", "on_fail": "continue",
                        "steps": [{
                            "name": "${m.case_id} ${m.description}",
                            "http": {"method": op.method, "path": op.path,
                                     "json": "${m.data}", "retries": 0},
                            "assert": [
                                {"status": [200, 201, 400, 409, 422],
                                 "label": "不得出现 5xx / 超时"},
                                {"time_lt": 5000},
                            ],
                        }],
                    },
                }],
            })
        elif op.method == "GET" and not op.has_path_param:
            seq += 1
            docs.append({
                "id": f"TC-IMP-{seq:03d}",
                "title": f"冒烟: GET {op.path} 返回 200（由接口文档生成）",
                "priority": "P2",
                "layer": "api",
                "tags": ["import", "smoke"],
                "steps": [{
                    "name": f"GET {op.path}",
                    "http": {"method": "GET", "path": op.path},
                    "assert": [{"status": 200}, {"time_lt": 5000}],
                }],
            })
        elif op.method in ("POST", "PUT", "PATCH", "DELETE"):
            why = "存在路径参数，请先造出资源再调用" if op.has_path_param else ""
            drafts.append(f"# {op.method} {op.path}"
                          + (f"  （{why}）" if why else "")
                          + (f"  {op.summary}" if op.summary else ""))
        if op.needs_auth:
            for doc in docs[mark:]:
                doc["skip_if"] = AUTH_GUARD
    return docs, schemas, drafts


def import_spec(spec: dict[str, Any], *, name: str,
                cases_dir: Path, schemas_dir: Path, force: bool = False) -> ImportResult:
    """执行导入：写 schema 与用例草稿文件。"""
    ops = iter_operations(spec)
    docs, schemas, drafts = build_import_docs(ops, spec, name)
    def _is(doc: dict[str, Any], method: str) -> bool:
        step = doc["steps"][0]
        return step.get("http", {}).get("method") == method

    result = ImportResult(name=name, op_total=len(ops),
                          post_cases=sum(1 for d in docs if "MUT" not in d["id"] and _is(d, "POST")),
                          get_cases=sum(1 for d in docs if _is(d, "GET")),
                          drafts=drafts)

    schemas_dir.mkdir(parents=True, exist_ok=True)
    for entity, schema in schemas.items():
        target = schemas_dir / f"{entity}.yaml"
        if target.exists() and not force:
            backup = target.with_suffix(".imported.yaml")
            backup.write_text(yaml.safe_dump(schema, allow_unicode=True, sort_keys=False),
                              encoding="utf-8")
            result.schemas.append((backup, "已存在，导入结果写入 .imported.yaml（对比后合并）"))
        else:
            target.write_text(yaml.safe_dump(schema, allow_unicode=True, sort_keys=False),
                              encoding="utf-8")
            result.schemas.append((target, "写入"))

    if docs:
        cases_dir.mkdir(parents=True, exist_ok=True)
        target = cases_dir / f"_import_{_safe_entity(name)}.yaml"
        header = "\n".join([
            f"# 由 `forgeqa import` 从接口文档生成 —— 正常路径与变异用例可直接运行。",
            f"# 运行本文件:  forgeqa run --cases {target}",
            "# 注意: 默认 `--cases cases` 不会加载本文件（`_` 前缀被目录扫描跳过）。",
            "",
            "# 导入的造数 Schema（枚举含义、必填语义、长度上限请人工核对）:",
            *(f"#   config/schemas/{entity}.yaml" for entity in schemas),
            "",
            "# 需要人工编写的接口草稿:",
            *(drafts or ["#   （无）"]),
            "",
        ])
        target.write_text(header + yaml.safe_dump({"cases": docs}, allow_unicode=True,
                                                  sort_keys=False), encoding="utf-8")
        result.case_file = target
    return result
