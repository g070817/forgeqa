"""forgeqa.cli — 命令行入口。

     forgeqa init                    生成项目脚手架（适配一个新站点的起点）
     forgeqa probe <url>             探测站点/接口，自动反推造数 schema 骨架
     forgeqa gen                     生成数据集（正常数据 + 边界/异常/极端变异数据）
     forgeqa seed                    造数入库（可回收）
     forgeqa run                     执行回归 + 生成报告
     forgeqa inventory               用例盘点与覆盖概览
     forgeqa db                      建表 / 跑 SQL / 查数据 / 快照 / 清理
     forgeqa demo                    启动内置演示站点
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from . import __version__
from .config import MISSING, ForgeConfig, coerce_scalar, deep_merge, deep_set
from .db import Database, Seeder, cleanup_from_ledger, diff_snapshots
from .errors import ForgeQAError
from .factory import DataFactory, Schema, infer_schema
from .report import console_summary, write_html, write_json, write_junit
from .runner import PRIORITY_ORDER, Runner, load_cases

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_FLAKY = 0, 1, 2, 3


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
def _setup_logging(verbose: int) -> logging.Logger:
    logger = logging.getLogger("forgeqa")
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    return logger


# 配置文件里的顶层段名。出现在 ``--set`` 的 key 开头时，按整个配置段处理；
# 否则视为「环境级键」（如 base_url），在文件模式下要落到具体环境下。
_SECTION_KEYS = frozenset({
    "http", "ui", "db", "auth", "generators", "runner",
    "report", "hooks", "envs", "defaults",
})


def _normalize_env_key(key: str) -> str:
    """``env.`` 与 ``envs.`` 同义，统一归一化为 ``envs.``。"""
    if key.startswith("env."):
        return "envs." + key[len("env."):]
    return key


def _overrides(pairs: Sequence[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ForgeQAError(f"--set 参数格式应为 key=value，收到: {item!r}")
        key, _, val = item.partition("=")
        _assign(out, key.strip(), coerce_scalar(val))
    return out


def _assign(target: dict[str, Any], key: str, value: Any) -> None:
    """把 ``--set`` 的 key=value 映射进 overrides 树。

    ``ForgeConfig.get()`` 查 overrides 优先于 ``defaults`` 段，所以
    overrides 里的键按原路径 1:1 存放即可同时支持两类写法：

    - 配置段键：``db.path=...`` / ``http.retries=...``
    - 环境级键：``base_url=...``

    早期实现把未匹配段名的裸键写成 ``envs.<key>``，但真实结构是
    ``envs.<环境名>.<key>``，于是 ``--set base_url=...`` 静默失效。
    这里按原路径存放即可，不要再往 ``envs.`` 底下塞。
    """
    deep_set(target, _normalize_env_key(key), value)


def _load_cfg(args) -> ForgeConfig:
    return ForgeConfig.load(
        getattr(args, "config", None),
        env=getattr(args, "env", None),
        overrides=_overrides(getattr(args, "set", None)),
        root=getattr(args, "root", None) or Path.cwd(),
    )


def _print(msg: str = "") -> None:
    print(msg)


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
ENV_TEMPLATE = """# ForgeQA 配置 —— 适配任意网站的入口
# 换站点只改这里：base_url / db / auth / ui，不需要改代码。
default_env: local

defaults:
  http:
    timeout: 15
    retries: 2
    headers: {User-Agent: forgeqa/1.0, Accept: application/json}
  ui:
    browser: chromium
    headless: true
    timeout: 15000
    viewport: {width: 1440, height: 900}
    screenshot_on_fail: true
  db:
    driver: sqlite
    path: ./out/forgeqa.db
  auth:
    type: none
    # 需要登录态时改成下面这样，bootstrap 会自动登录并把 token 注入后续请求：
    # type: bearer
    # login:
    #   method: POST
    #   path: /api/login
    #   json: {username: "${os:QA_USER:-admin}", password: "${os:QA_PASS:-admin123}"}
    #   extract: {token: "$.data.token"}

generators:
  locale: zh_CN
  seed: 20260922          # 固定 seed → 造数可复现；CI 里建议保持固定
  out_dir: ./out/data

runner:
  retries: 0              # 断言失败不重试（重试等于掩盖缺陷）；仅环境类异常会重试
  repeat: 1
  jobs: 1                 # UI/SQLite 场景建议 1
  fail_fast: false

report:
  out_dir: ./out/reports
  junit: true

hooks:
  ddl: config/db/schema.sql       # bootstrap 时执行的建表脚本
  seed: []                        # 造数入库计划，示例见下方注释
  cleanup: true                   # 跑完自动回收造数
  # seed:
  #   - {entity: dept, schema: dept.yaml, count: 3, table: dept,
  #      mapping: {code: dept_no}, defaults: {status: 1}}

envs:
  local:
    base_url: http://127.0.0.1:8000
  staging:
    base_url: https://staging.example.com
    db:
      driver: mysql
      dsn: mysql+pymysql://qa:qa_pass@127.0.0.1:3306/appdb
  prod_readonly:
    base_url: https://www.example.com
    http: {retries: 1}
    db: {driver: sqlite, path: ./out/readonly.db}
"""

SCHEMA_USER = '''# 造数 Schema —— 一个实体一份，字段即生成规则
# min_len / max_len 会驱动 mutate 自动生成「长度边界」用例。
# ${uniq} 是用例级唯一标记：同一用例内恒定、不同用例互不相同。
# 表上有唯一约束的字段（用户名/邮箱）务必带上它，否则用例之间会撞 409。
entity: user
count: 1
unique: [email, phone, username]
description: 用户实体（示例，可直接改字段适配你的站点）

fields:
  - {name: name,       gen: faker, method: name, min_len: 2, max_len: 20}
  - {name: username,   gen: pattern, pattern: "qa_????", transform: "suffix:${uniq}",
     min_len: 3, max_len: 20}
  - {name: email,      gen: pattern, pattern: "@@@@.####",
     transform: "suffix:${uniq}@example.com", min_len: 8, max_len: 64}
  - {name: phone,      gen: fake_phone, min_len: 5, max_len: 20}   # 明显虚构的 138 号段
  - {name: age,        gen: int, min: 18, max: 65}
  - {name: role,       gen: choice, values: [user, admin], weights: [9, 1]}
  - {name: dept,       gen: choice, values: [研发部, 测试部, 产品部]}
  - {name: vip,        gen: expr, value: "${age} >= 30"}
  - {name: created_at, gen: datetime, start: "-30d", end: now, fmt: "%Y-%m-%d %H:%M:%S"}
'''

SCHEMA_ORDER = '''entity: order
count: 1
unique: [order_no]
fields:
  - {name: order_no,  gen: pattern, pattern: "QA########", transform: "suffix:${uniq}"}
  - {name: user_id,   gen: int, min: 1, max: 1000}
  - {name: amount,    gen: float, min: 1.0, max: 9999.0, precision: 2}
  - {name: currency,  gen: choice, values: [CNY, USD]}
  - {name: status,    gen: choice, values: [created, paid, shipped], weights: [5, 3, 2]}
  - {name: created_at,gen: datetime, start: "-7d", end: now, fmt: "%Y-%m-%d %H:%M:%S"}
'''

SCHEMA_SQL = '''-- 造数与回归用表结构（示例，替换为你自己的库结构即可）
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS dept (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  dept_no  TEXT NOT NULL UNIQUE,
  name     TEXT NOT NULL,
  status   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  username   TEXT NOT NULL UNIQUE,
  email      TEXT NOT NULL UNIQUE,
  phone      TEXT,
  age        INTEGER,
  role       TEXT NOT NULL DEFAULT 'user',
  dept       TEXT,
  vip        INTEGER NOT NULL DEFAULT 0,
  status     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no   TEXT NOT NULL UNIQUE,
  user_id    INTEGER NOT NULL,
  amount     REAL NOT NULL,
  currency   TEXT NOT NULL DEFAULT 'CNY',
  status     TEXT NOT NULL DEFAULT 'created',
  created_at TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id)
);
'''

CASE_TEMPLATE = '''# 示例用例：接口 + 造数 + SQL 落库校验，三件套一次跑通
# 复制这个文件改接口路径就能适配你自己的站点。
id: TC-API-USER-001
title: 创建用户 → 接口返回校验 → 数据库落库校验
priority: P0
layer: api
tags: [api, smoke, user]
data:
  user: user.yaml            # 引用 config/schemas/user.yaml，count=1 时可直接 ${data.user.name}

steps:
  - name: 创建用户
    http:
      method: POST
      path: /api/users
      json:
        name:     "${data.user.name}"
        username: "${data.user.username}"
        email:    "${data.user.email}"
        phone:    "${data.user.phone}"
        age:      ${data.user.age}
        role:     "${data.user.role}"
        dept:     "${data.user.dept}"
    extract:
      uid: "$.data.id"                 # 提取后供后续步骤用 ${ctx.uid}
    assert:
      - {status: 201}
      - {jsonpath: "$.data.id", op: not_null, label: 返回了自增主键}
      - {jsonpath: "$.data.name", op: eq, value: "${data.user.name}", label: 名称回显一致}
      - {jsonpath: "$.code", op: eq, value: 0, label: 业务码为 0}
      - {time_lt: 3000, label: 接口响应小于 3s}

  - name: 落库校验（业务不变量：创建后库里必须查到 1 行）
    db:
      sql: >
        SELECT name, email, role, status
        FROM users
        WHERE id = :uid
      params: {uid: "${ctx.uid}"}
    assert:
      - {rows_count: 1}
      - {row: {field: name, op: eq, value: "${data.user.name}"}}
      - {each_row: {field: status, op: eq, value: 1}}
      - {sql: "SELECT COUNT(*) FROM users WHERE email = :email",
         params: {email: "${data.user.email}"}, op: eq, value: 1,
         label: 邮箱在库里唯一}

  - name: 清理本次造数
    db:
      mode: execute
      sql: "DELETE FROM users WHERE id = :uid"
      params: {uid: "${ctx.uid}"}
    assert:
      - {sql: "SELECT COUNT(*) FROM users WHERE id = :uid",
         params: {uid: "${ctx.uid}"}, op: eq, value: 0}
'''

CASE_MUTATION = '''# 变异造数回归：把自动生成的边界/异常/极端数据灌进接口，验证错误处理
# 这类用例是「AI 造数」性价比最高的地方——覆盖度提升快，几乎不用手写。
id: TC-API-USER-MUT-001
title: 创建用户接口的边界与异常输入回归
priority: P1
layer: api
tags: [api, boundary, negative]
data:
  muts:
    schema: user.yaml
    mutate: true
    categories: [boundary, abnormal]
  user: user.yaml

steps:
  - name: 变异数据逐条打接口（断言以「不 5xx + 有明确错误响应」为准）
    loop:
      over: "${data.muts}"
      as: m
      on_fail: continue
      steps:
        - name: "${m.case_id} ${m.description}"
          http:
            method: POST
            path: /api/users
            json: "${m.data}"
            retries: 0
          assert:
            - {status: [200, 201, 400, 409, 422], label: 不得出现 5xx / 超时}
            - {time_lt: 5000}
'''

GITIGNORE = '''__pycache__/
*.pyc
out/
.forgeqa_seed_ledger.json
.venv/
'''


def cmd_init(args) -> int:
    root = Path(args.root or Path.cwd())
    # 守卫：在 ForgeQA 自己的包目录里跑 init 会把脚手架生成进包内（污染源码树）。
    # 这是实际发生过两次的事故（init 以 cwd 为根，人在包目录里一敲就中招）。
    if (root / "runner.py").exists() and (root / "cli.py").exists() and (root / "__init__.py").exists():
        _print("✗ 当前目录是 ForgeQA 源码包本身，拒绝在这里生成脚手架。")
        _print("  → 修复建议: cd 到你的目标项目目录再运行 forgeqa init，"
               "或用 --root 指定目标目录")
        return EXIT_USAGE
    created: list[Path] = []

    def write(rel: str, content: str, force: bool = False) -> None:
        p = root / rel
        if p.exists() and not force:
            _print(f"  跳过（已存在）: {rel}")
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        created.append(p)

    write("config/env.yaml", ENV_TEMPLATE)
    write("config/schemas/user.yaml", SCHEMA_USER)
    write("config/schemas/order.yaml", SCHEMA_ORDER)
    write("config/db/schema.sql", SCHEMA_SQL)
    write("cases/api_user_crud.yaml", CASE_TEMPLATE)
    write("cases/api_user_boundary.yaml", CASE_MUTATION)
    write(".gitignore", GITIGNORE)

    env_file = root / "config" / "env.yaml"
    # 与运行时 --set 不同：init 是把值写进 YAML 文件，所以「环境级键」（如 base_url）
    # 必须落到 envs.<环境名> 下，否则写出来的是个无人读取的顶层死键。
    env_name = "local"
    patch: dict[str, Any] = {}
    for pair in args.set or []:
        if "=" not in pair:
            raise ForgeQAError(f"--set 参数格式应为 key=value，收到: {pair!r}")
        key, _, val = pair.partition("=")
        key = _normalize_env_key(key.strip())
        if key.startswith("envs.") or key.split(".")[0] in _SECTION_KEYS:
            deep_set(patch, key, coerce_scalar(val))
        else:
            deep_set(patch, f"envs.{env_name}.{key}", coerce_scalar(val))
    if args.base_url:
        deep_set(patch, f"envs.{env_name}.base_url", args.base_url)
    if patch:
        data = yaml.safe_load(env_file.read_text(encoding="utf-8")) or {}
        data = deep_merge(data, patch)
        env_file.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    if created:
        _print(f"\nForgeQA 脚手架已生成于 {root}")
        for p in created:
            _print(f"  + {p.relative_to(root)}")
    else:
        _print(f"\n{root} 下脚手架文件均已存在，未新增任何文件（无需重复初始化）。")
    _print("""
下一步：
  1. 编辑 config/env.yaml，把 base_url 改成你的站点
  2. forgeqa probe http://你的站点/api/xxx --entity user   # 用真实响应反推造数 schema
  3. forgeqa gen --schema config/schemas/user.yaml         # 先看造出来的数据长什么样
  4. forgeqa db init && forgeqa run --cases cases          # 跑一遍，打开 out/reports/*.html
""")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def cmd_probe(args) -> int:
    import requests

    cfg = _load_cfg(args) if args.config else None
    base = (args.url or (cfg.get("base_url") if cfg else "") or "").rstrip("/")
    if not base:
        _print("请提供 URL：forgeqa probe http://127.0.0.1:8000/api/users")
        return EXIT_USAGE
    url = base if args.url and args.url.startswith("http") else base + (args.path or "")
    if args.path and not url.endswith(args.path):
        url = base.rstrip("/") + args.path

    headers = dict(cfg.get("http.headers") if cfg else {}) or {"Accept": "application/json"}
    token = os.environ.get("FORGEQA_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    _print(f"探测 {url} …")
    try:
        resp = requests.request(args.method, url, headers=headers,
                                json=json.loads(args.body) if args.body else None,
                                timeout=args.timeout)
    except requests.RequestException as exc:
        _print(f"请求失败: {exc}")
        _print("检查：站点是否已启动 / URL 是否正确 / 是否需要代理")
        return EXIT_FAIL

    _print(f"状态码 {resp.status_code}   耗时 {resp.elapsed.total_seconds() * 1000:.0f} ms   "
           f"Content-Type {resp.headers.get('Content-Type', '-')}")
    _print(f"响应头: {json.dumps({k: v for k, v in resp.headers.items() if k.lower() in ('content-type', 'location', 'set-cookie', 'x-request-id')}, ensure_ascii=False)}")

    body: Any = resp.text
    try:
        body = resp.json()
        shape = _shape_of(body)
        _print(f"响应结构:\n{shape}")
    except ValueError:
        _print(f"响应体（前 500 字符，非 JSON）:\n{resp.text[:500]}")

    entity = args.entity or "entity"
    if not isinstance(body, (dict, list)):
        _print("\n非 JSON 响应，跳过 schema 反推。")
        return EXIT_OK

    try:
        schema = infer_schema(body, entity=entity)
    except ForgeQAError as exc:
        _print(f"\nSchema 反推失败: {exc.render()}")
        return EXIT_OK

    out = Path(args.out) if args.out else Path("config/schemas") / f"{entity}.yaml"
    if args.print_only:
        _print("\n--- 反推的造数 Schema ---")
        _print(yaml.safe_dump(schema, allow_unicode=True, sort_keys=False))
        return EXIT_OK

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not args.force:
        backup = out.with_suffix(".inferred.yaml")
        backup.write_text(yaml.safe_dump(schema, allow_unicode=True, sort_keys=False), encoding="utf-8")
        _print(f"\n{out} 已存在，反推结果写入 {backup}（对比后手动合并）")
    else:
        out.write_text(yaml.safe_dump(schema, allow_unicode=True, sort_keys=False), encoding="utf-8")
        _print(f"\n造数 Schema 已写入 {out}")
    _print("提示：字段类型是推导出来的，请人工核对枚举值、长度限制等业务约束后再使用。")
    return EXIT_OK


def cmd_scan(args) -> int:
    from .scan import scan_site, write_case_file, write_schemas

    # 扫描不强依赖项目配置：没有 config/env.yaml 也能对任意 URL 跑
    try:
        cfg = _load_cfg(args)
    except ForgeQAError:
        cfg = None
    base = (args.url or (cfg.get("base_url") if cfg else "") or "").rstrip("/")
    if not base:
        _print("请提供 URL：forgeqa scan http://127.0.0.1:8000")
        return EXIT_USAGE
    headers = dict(cfg.get("http.headers") if cfg else {}) or {"Accept": "application/json"}
    token = os.environ.get("FORGEQA_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    _print(f"扫描 {base}（OpenAPI → 页面爬取 → 路径字典，最多 {args.max_pages} 页 / "
           f"{args.max_probes} 个试探）…\n")
    result = scan_site(base, start_paths=tuple(args.path) or ("/",),
                       max_pages=args.max_pages, timeout=args.timeout,
                       headers=headers, max_probes=args.max_probes)

    if result.openapi_from:
        _print(f"✓ 发现 OpenAPI 文档: {result.openapi_from}（接口清单来自文档，最可靠）")
    _print(f"爬取页面 {result.pages_crawled} 个，发现接口 {len(result.endpoints)} 个\n")

    header = ["路径", "方法", "GET", "Content-Type", "schema", "来源"]
    rows = []
    for ep in result.endpoints:
        has_schema = "✓" if ep.sample is not None else "-"
        rows.append([ep.path, "/".join(sorted(ep.methods)) or "-",
                     str(ep.status) if ep.status else "-",
                     (ep.content_type.split(";")[0] if ep.content_type else "-"),
                     has_schema, ep.source])
    widths = [max(len(r[i]) for r in rows + [header]) + 2 for i in range(len(header))]
    _print("".join(_pad(h, w) for h, w in zip(header, widths)))
    _print("─" * sum(widths))
    for r in rows:
        _print("".join(_pad(c, w) for c, w in zip(r, widths)))
    for err in result.errors:
        _print(f"⚠ {err}")

    if args.print_only:
        return EXIT_OK

    root = Path(args.root) if args.root else Path.cwd()
    schema_writes = write_schemas(result, root / "config" / "schemas", force=args.force)
    if schema_writes:
        _print("\n--- 造数 Schema（人工核对后使用）---")
        for path, note in schema_writes:
            _print(f"  {path}  {note}")

    case_file = None
    if not args.no_cases:
        case_file = write_case_file(result, root / "cases" / "_generated")
        if case_file:
            _print(f"\n--- 冒烟用例草稿 ---\n  {case_file}")
            _print(f"  运行: forgeqa run --cases {case_file}")
        else:
            _print("\n没有 GET 可通的接口，未生成用例草稿。"
                   "若站点需要登录，先 export FORGEQA_TOKEN=<token> 再扫。")

    if not schema_writes and not case_file:
        _print("\n没有产出。排查：站点是否可访问 / 接口是否都在登录墙后 / "
               "是否是纯前端单页应用（接口路径不在页面与 JS 里，需要靠 OpenAPI 或手工补充）")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# import —— 从接口文档（OpenAPI/Swagger）生成写接口用例草稿
# --------------------------------------------------------------------------- #
def cmd_import(args) -> int:
    from .apidoc import import_spec, load_spec

    spec = load_spec(args.source, timeout=args.timeout)
    root = Path(args.root) if args.root else Path.cwd()
    name = args.name or Path(args.source if not args.source.startswith(("http://", "https://"))
                             else args.source.split("?")[0]).stem
    result = import_spec(spec, name=name,
                         cases_dir=root / "cases" / "_generated",
                         schemas_dir=root / "config" / "schemas",
                         force=args.force)

    _print(f"接口文档导入完成: {args.source}")
    _print(f"  接口操作 {result.op_total} 个 → POST 用例 {result.post_cases} 条"
           f"（各带 1 条边界变异），GET 冒烟 {result.get_cases} 条")
    if result.schemas:
        _print("\n--- 造数 Schema（枚举含义/必填语义/长度上限请人工核对）---")
        for path, note in result.schemas:
            _print(f"  {path}  {note}")
    if result.case_file:
        _print(f"\n--- 用例草稿 ---\n  {result.case_file}")
        _print(f"  运行: forgeqa run --cases {result.case_file}")
    if result.drafts:
        _print("\n--- 需要人工编写的接口（已写入草稿头注释）---")
        for line in result.drafts:
            _print(line.replace("# ", "  ", 1) if line.startswith("# ") else line)
    if not result.schemas and not result.case_file:
        _print("\n没有可执行产出。排查：文档里 POST 是否有 requestBody 的 properties 定义；"
               "带路径参数的接口（如 /api/users/{id}）需要先造资源，请手工编写。")
    return EXIT_OK


def _shape_of(body: Any, depth: int = 0, max_depth: int = 3) -> str:
    pad = "  " * depth
    if isinstance(body, dict):
        lines = []
        for k, v in list(body.items())[:30]:
            if isinstance(v, (dict, list)) and depth < max_depth:
                lines.append(f"{pad}{k}: {type(v).__name__}")
                lines.append(_shape_of(v, depth + 1, max_depth))
            else:
                lines.append(f"{pad}{k}: {type(v).__name__} = {str(v)[:60]!r}")
        return "\n".join(lines)
    if isinstance(body, list):
        lines = [f"{pad}[{len(body)} 项]"]
        if body and depth < max_depth:
            lines.append(_shape_of(body[0], depth + 1, max_depth))
        return "\n".join(lines)
    return f"{pad}{type(body).__name__} = {str(body)[:60]!r}"


# --------------------------------------------------------------------------- #
# gen
# --------------------------------------------------------------------------- #
def cmd_gen(args) -> int:
    cfg = _load_cfg(args)
    logger = _setup_logging(args.verbose)
    ctx = cfg.context()
    factory = DataFactory(ctx, schema_dir=cfg.root / "config" / "schemas")
    out_dir = Path(args.out) if args.out else cfg.path("generators.out_dir", "./out/data")

    if args.probe_url:
        import requests

        resp = requests.get(args.probe_url, timeout=15)
        resp.raise_for_status()
        schema_dict = infer_schema(resp.json(), entity=args.entity or "entity", count=args.count)
        schema: Any = schema_dict
        name = args.entity or "entity"
    else:
        if not args.schema:
            _print("请指定 --schema（或 --probe-url 从接口反推）")
            return EXIT_USAGE
        schema = args.schema
        name = Path(args.schema).stem if isinstance(args.schema, str) else (args.entity or "entity")

    rows = factory.generate(schema, count=args.count)
    _print(f"已生成 {len(rows)} 条 {name} 记录（seed={ctx.seed}，可复现）")
    for r in rows[: args.preview]:
        _print("  " + json.dumps({k: v for k, v in r.items() if not k.startswith("_")},
                                 ensure_ascii=False))

    files: list[Path] = []
    if args.save:
        files.append(factory.dump([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
                                  out_dir, name, args.format))

    if args.mutate:
        muts = factory.mutate(schema, base={k: v for k, v in rows[0].items() if not k.startswith("_")},
                              categories=args.categories)
        _print(f"\n已生成 {len(muts)} 条变异数据（边界/异常/极端），分布：")
        dist: dict[str, int] = {}
        for m in muts:
            dist[m["category"]] = dist.get(m["category"], 0) + 1
        for k, v in sorted(dist.items()):
            _print(f"  {k}: {v}")
        for m in muts[:5]:
            _print(f"  · {m['case_id']} [{m['category']}] {m['description']}")
        if args.save:
            files.append(factory.dump(muts, out_dir, f"{name}_mutations", args.format))

    for f in files:
        _print(f"\n已写入 {f}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# seed
# --------------------------------------------------------------------------- #
def cmd_seed(args) -> int:
    cfg = _load_cfg(args)
    _setup_logging(args.verbose)
    plan = list(cfg.get("hooks.seed") or [])
    if args.plan:
        plan = yaml.safe_load(Path(args.plan).read_text(encoding="utf-8")) or []
    if not plan:
        _print("seeding 计划为空。在 env.yaml 的 hooks.seed 下配置，或用 --plan 指定文件。")
        return EXIT_USAGE

    ctx = cfg.context()
    with Database.from_opts(cfg.get("db") or {}, cfg.root) as db:
        ddl = cfg.get("hooks.ddl")
        if ddl and not args.no_ddl:
            db.script_file(cfg.root / str(ddl))
            _print(f"已执行建表脚本 {ddl}")
        factory = DataFactory(ctx, schema_dir=cfg.root / "config" / "schemas")
        ledger = cfg.root / "out" / ".forgeqa_seed_ledger.json"
        seeder = Seeder(db, factory, ctx, ledger_path=ledger)
        counts = seeder.run(plan)
        _print("造数入库完成：")
        for table, n in counts.items():
            total = db.scalar(f"SELECT COUNT(*) FROM {table}")
            _print(f"  {table}: 本次插入 {n} 行，当前共 {total} 行")
        _print(f"\n回收登记已写入 {ledger}")
        _print("回收命令：forgeqa db cleanup")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def cmd_run(args) -> int:
    cfg = _load_cfg(args)
    logger = _setup_logging(args.verbose)
    cases = load_cases(args.cases or ["cases"], cfg.root)
    selected = Runner.select(
        cases,
        tags=args.tags or (),
        exclude_tags=args.exclude_tags or (),
        priorities=args.priority or (),
        ids=args.id or (),
        keyword=args.keyword or "",
    )
    _print(f"配置: {cfg.describe()}")
    _print(f"用例: 共加载 {len(cases)} 条，本次选中 {len(selected)} 条")
    if not selected:
        _print("没有选中任何用例，检查 --tags / --priority / --keyword 过滤条件")
        return EXIT_USAGE

    runner = Runner(cfg, logger=logger)
    bootstrap_info, bootstrap_steps = runner.bootstrap()
    bad_boot = [s for s in bootstrap_steps if s.status in ("FAILED", "ERROR")]
    if bad_boot:
        _print(f"⚠ 引导阶段失败：{bad_boot[0].name} — {bad_boot[0].error}")
        _print(f"  修复建议：{bad_boot[0].hint or '检查 hooks.bootstrap 配置'}")
        return EXIT_FAIL
    if bootstrap_info:
        _print(f"引导完成: {json.dumps(bootstrap_info, ensure_ascii=False)}")

    suite = runner.run(
        selected,
        jobs=args.jobs,
        repeat=args.repeat,
        retries=args.retries,
        fail_fast=args.fail_fast,
        baseline=args.baseline,
    )
    suite.bootstrap = bootstrap_info
    try:
        suite.db_cleanup = runner.teardown()
    except Exception as exc:
        logger.debug(f"清理异常: {exc}")

    out_dir = Path(args.report_dir) if args.report_dir else cfg.path("report.out_dir", "./out/reports")
    stamp = suite.started_at.replace(":", "").replace("-", "").replace("T", "-")
    html_path = write_html(suite, out_dir / f"report-{stamp}.html")
    write_html(suite, out_dir / "latest.html")
    write_json(suite, out_dir / f"result-{stamp}.json")
    if cfg.get("report.junit", True):
        write_junit(suite, out_dir / f"junit-{stamp}.xml")

    _print(console_summary(suite))
    _print(f"  HTML 报告: {html_path}")
    _print(f"  最新报告: {out_dir / 'latest.html'}")

    if args.open:
        import webbrowser

        webbrowser.open(html_path.resolve().as_uri())

    if suite.failed or suite.errors:
        if args.fail_on_flaky and suite.flaky:
            _print(f"  门禁不通过：存在 {suite.flaky} 条 flaky 用例")
        return EXIT_FAIL
    if suite.flaky and args.fail_on_flaky:
        _print(f"  门禁不通过：存在 {suite.flaky} 条 flaky 用例（不稳定用例会掩盖真实缺陷）")
        return EXIT_FLAKY
    return EXIT_OK


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #
def cmd_inventory(args) -> int:
    cfg = _load_cfg(args)
    cases = load_cases(args.cases or ["cases"], cfg.root)
    rows = [(c.priority, c.id, c.layer, ",".join(c.tags), c.title,
             *_count_steps(c.steps)) for c in cases]
    rows.sort(key=lambda r: (PRIORITY_ORDER.get(r[0], 9), r[1]))

    _print(f"\n用例盘点（共 {len(rows)} 条）  {cfg.describe()}\n")
    header = ["优先级", "用例 ID", "层", "标签", "接口", "SQL", "UI", "标题"]
    widths = [8, 26, 6, 26, 6, 5, 4, 0]
    _print("".join(_pad(h, w) for h, w in zip(header, widths)))
    _print("─" * 118)
    for r in rows:
        cells = [r[0], r[1], r[2], r[3], str(r[5]), str(r[6]), str(r[7]), r[4][:44]]
        _print("".join(_pad(c, w) for c, w in zip(cells, widths)))

    by_pri: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    for r in rows:
        by_pri[r[0]] = by_pri.get(r[0], 0) + 1
        by_layer[r[2]] = by_layer.get(r[2], 0) + 1
    _print("─" * 118)
    _print(f"按优先级: {dict(sorted(by_pri.items()))}")
    _print(f"按分层:   {by_layer}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.safe_dump(
            [{"priority": r[0], "id": r[1], "layer": r[2], "tags": r[3],
              "title": r[4], "http_steps": r[5], "db_steps": r[6], "ui_steps": r[7]} for r in rows],
            allow_unicode=True, sort_keys=False), encoding="utf-8")
        _print(f"\n清单已写入 {out}")
    _print("\n覆盖度自查（按维度，不给笼统百分比）：")
    dims = {
        "正常流": any("smoke" in c.tags or "positive" in c.tags for c in cases),
        "参数边界": any("boundary" in c.tags for c in cases),
        "异常分支": any({"negative", "abnormal"} & set(c.tags) for c in cases),
        "数据一致性(SQL)": any(any("db" in s for s in c.steps) for c in cases),
        "UI 端到端": any(any("ui" in s for s in c.steps) for c in cases),
    }
    for k, v in dims.items():
        _print(f"  {'✓' if v else '✗'} {k}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# db
# --------------------------------------------------------------------------- #
def cmd_db(args) -> int:
    cfg = _load_cfg(args)
    _setup_logging(args.verbose)
    with Database.from_opts(cfg.get("db") or {}, cfg.root) as db:
        _print(f"数据库: {db.label}")
        if args.action == "init":
            ddl = args.ddl or cfg.get("hooks.ddl")
            if not ddl:
                _print("未指定建表脚本（--ddl 或 hooks.ddl）")
                return EXIT_USAGE
            db.script_file(cfg.root / str(ddl))
            _print(f"已执行 {ddl}")
        elif args.action == "script":
            db.script_file(args.sql_file)
            _print(f"已执行 {args.sql_file}")
        elif args.action == "tables":
            for t in db.tables():
                n = db.scalar(f"SELECT COUNT(*) FROM {t}", default=0)
                cols = db.columns(t)
                _print(f"  {t}  ({n} 行, {len(cols)} 列)  PK={db.primary_key(t) or '-'}")
                _print(f"    列: {', '.join(cols)}")
        elif args.action == "query":
            sql = args.sql or Path(args.sql_file).read_text(encoding="utf-8")
            rows = db.query(sql, _kv(args.param or []))
            if not rows:
                _print("查询返回 0 行")
                return EXIT_OK
            cols = list(rows[0].keys())
            _print(" | ".join(cols))
            _print("─" * 80)
            for r in rows[: args.limit]:
                _print(" | ".join(str(r.get(c, ""))[:40] for c in cols))
            _print(f"\n共 {len(rows)} 行" + (f"（展示前 {args.limit} 行）" if len(rows) > args.limit else ""))
        elif args.action == "snapshot":
            table = args.table
            snap = db.snapshot(table, key=args.key, fields=args.fields, where=args.where)
            base = Path(args.out) if args.out else cfg.root / "out" / "baselines" / f"db_{table}.json"
            base.parent.mkdir(parents=True, exist_ok=True)
            if args.diff and base.exists():
                old = json.loads(base.read_text(encoding="utf-8"))
                diff = diff_snapshots(old, snap)
                _print(f"对比 {base}:")
                _print(f"  新增 {len(diff['added'])} 行, 删除 {len(diff['removed'])} 行, 修改 {len(diff['changed'])} 行")
                for k in diff["added"][:10]:
                    _print(f"  + {k}")
                for k in diff["removed"][:10]:
                    _print(f"  - {k}")
                for k, v in list(diff["changed"].items())[:10]:
                    _print(f"  ~ {k}: {json.dumps(v, ensure_ascii=False)[:200]}")
                return EXIT_FAIL if diff["has_diff"] else EXIT_OK
            base.write_text(json.dumps(snap, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            _print(f"快照已写入 {base}（{len(snap)} 行）")
        elif args.action == "cleanup":
            ledger = cfg.root / "out" / ".forgeqa_seed_ledger.json"
            deleted = cleanup_from_ledger(db, ledger)
            _print(f"已回收造数: {deleted or '（无登记数据）'}")
    return EXIT_OK


def _pad(text: str, width: int) -> str:
    """按终端显示宽度补齐（中文按 2 列计），让表格真正对齐。"""
    if width <= 0:
        return str(text)
    s = str(text)
    shown = sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)
    return s + " " * max(width - shown, 1)


def _count_steps(steps: Sequence[Any]) -> tuple[int, int, int]:
    """递归统计步骤里的 http / db / ui 数量（含 loop 内部）。"""
    http = db = ui = 0
    for s in steps or []:
        if not isinstance(s, Mapping):
            continue
        http += 1 if "http" in s else 0
        db += 1 if "db" in s else 0
        ui += 1 if "ui" in s else 0
        loop = s.get("loop")
        if isinstance(loop, Mapping):
            a, b, c = _count_steps(loop.get("steps") or [])
            http += a
            db += b
            ui += c
    return http, db, ui


def _kv(pairs: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in pairs:
        k, _, v = p.partition("=")
        out[k.strip()] = coerce_scalar(v)
    return out


# --------------------------------------------------------------------------- #
# demo
# --------------------------------------------------------------------------- #
def cmd_demo(args) -> int:
    import importlib.util

    root = Path(args.root or Path.cwd())
    server = root / "examples" / "demo_server.py"
    if not server.exists():
        _print(f"演示站点脚本不存在: {server}（可执行 forgeqa init 生成）")
        return EXIT_USAGE
    spec = importlib.util.spec_from_file_location("forgeqa_demo_server", server)
    if spec is None or spec.loader is None:
        _print("无法加载演示站点")
        return EXIT_FAIL
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _print(f"演示站点启动: http://127.0.0.1:{args.port}")
    _print("  接口: POST /api/login  POST/GET /api/users  GET /api/users/<id>  GET /api/orders")
    _print("  页面: http://127.0.0.1:{0}/ui/form   健康检查: /health".format(args.port))
    _print("  Ctrl+C 结束")
    module.main(host=args.host, port=args.port, db=args.db)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="forgeqa",
        description="ForgeQA — 通用造数与自动回归工具（Python Requests/Playwright/Faker + SQL，适配任意网站）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""典型流程:
  forgeqa init --base-url http://127.0.0.1:8000
  forgeqa probe http://127.0.0.1:8000/api/users --entity user
  forgeqa gen --schema config/schemas/user.yaml --mutate --save --preview 3
  forgeqa db init && forgeqa seed
  forgeqa run --cases cases --baseline update
  forgeqa run --cases cases --baseline diff --fail-on-flaky
""",
    )
    p.add_argument("--version", action="version", version=f"ForgeQA {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, *, config=True):
        sp.add_argument("--root", help="项目根目录（默认当前目录）")
        if config:
            sp.add_argument("--config", default="config/env.yaml", help="配置文件路径")
            sp.add_argument("--env", help="环境名，覆盖配置里的 default_env")
            sp.add_argument("--set", action="append", metavar="KEY=VALUE",
                            help="临时覆盖配置（优先级最高）。如 --set base_url=http://x、"
                                 "--set db.path=./a.db、--set envs.staging.base_url=http://y")
        sp.add_argument("-v", "--verbose", action="count", default=0, help="日志详细程度（可叠加）")

    sp = sub.add_parser("init", help="生成项目脚手架")
    sp.add_argument("--root", help="项目根目录")
    sp.add_argument("--base-url", help="写入 local 环境的 base_url")
    sp.add_argument("--set", action="append", metavar="KEY=VALUE")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("probe", help="探测站点/接口并反推造数 schema")
    common(sp)
    sp.add_argument("url", nargs="?", help="完整 URL 或配合 --path 使用 base_url")
    sp.add_argument("--path", help="接口路径，如 /api/users")
    sp.add_argument("--method", default="GET")
    sp.add_argument("--body", help="请求体（JSON 字符串）")
    sp.add_argument("--entity", help="实体名，决定 schema 文件名")
    sp.add_argument("--out", help="schema 输出路径")
    sp.add_argument("--timeout", type=float, default=15)
    sp.add_argument("--print-only", action="store_true", help="只打印不写文件")
    sp.add_argument("--force", action="store_true", help="覆盖已存在的 schema")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("scan", help="扫描站点发现接口，生成冒烟用例草稿与造数 Schema")
    common(sp)
    sp.add_argument("url", nargs="?", help="站点入口 URL，如 http://127.0.0.1:8000")
    sp.add_argument("--path", action="append", default=[], metavar="PATH",
                    help="额外的起始页面路径（可多次指定），默认从 / 开始爬")
    sp.add_argument("--max-pages", type=int, default=8, help="最多爬取的页面数（默认 8）")
    sp.add_argument("--timeout", type=float, default=4.0, help="单个请求超时秒数（默认 4）")
    sp.add_argument("--max-probes", type=int, default=48, help="路径试探上限（默认 48）")
    sp.add_argument("--print-only", action="store_true", help="只打印扫描结果，不写任何文件")
    sp.add_argument("--force", action="store_true", help="覆盖已存在的 schema 文件")
    sp.add_argument("--no-cases", action="store_true", help="只反推 schema，不生成用例草稿")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("import", help="从 OpenAPI/Swagger 接口文档生成写接口（POST 等）用例草稿")
    common(sp)
    sp.add_argument("source", help="接口文档路径或 URL（OpenAPI 3 / Swagger 2，JSON 或 YAML）")
    sp.add_argument("--name", help="导入名称，决定用例文件名（默认取文档文件名）")
    sp.add_argument("--timeout", type=float, default=15, help="下载文档的超时秒数（默认 15）")
    sp.add_argument("--force", action="store_true", help="覆盖已存在的 schema 文件")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("gen", help="生成数据集（含变异造数）")
    common(sp)
    sp.add_argument("--schema", help="schema 文件路径（或 schema 目录下的文件名）")
    sp.add_argument("--probe-url", help="从该接口响应反推 schema 后生成")
    sp.add_argument("--entity", help="实体名")
    sp.add_argument("--count", type=int, default=None, help="生成条数（默认取 schema 的 count）")
    sp.add_argument("--mutate", action="store_true", help="同时生成边界/异常/极端变异数据")
    sp.add_argument("--categories", nargs="*", default=["boundary", "abnormal", "extreme"])
    sp.add_argument("--out", help="输出目录")
    sp.add_argument("--format", default="json", choices=["json", "yaml"])
    sp.add_argument("--save", action="store_true", help="写文件（否则只打印预览）")
    sp.add_argument("--preview", type=int, default=3, help="预览条数")
    sp.set_defaults(func=cmd_gen)

    sp = sub.add_parser("seed", help="按计划造数入库")
    common(sp)
    sp.add_argument("--plan", help="seeding 计划 YAML（默认取 hooks.seed）")
    sp.add_argument("--no-ddl", action="store_true", help="不执行建表脚本")
    sp.set_defaults(func=cmd_seed)

    sp = sub.add_parser("run", help="执行回归并生成报告")
    common(sp)
    sp.add_argument("--cases", action="append", help="用例文件或目录（可多次指定，默认 cases）")
    sp.add_argument("--tags", action="append", help="只跑含这些标签的用例（可多次）")
    sp.add_argument("--exclude-tags", action="append", help="排除这些标签")
    sp.add_argument("--priority", action="append", help="只跑这些优先级，如 P0")
    sp.add_argument("--id", action="append", help="只跑这些用例 ID")
    sp.add_argument("-k", "--keyword", help="按 ID/标题/标签模糊匹配")
    sp.add_argument("--jobs", type=int, help="并发数（UI 与 SQLite 写入会自动串行）")
    sp.add_argument("--repeat", type=int, help="重复轮次，用于稳定性探测")
    sp.add_argument("--retries", type=int, help="环境类失败重试次数（断言失败不重试）")
    sp.add_argument("--fail-fast", action="store_true", help="首个失败即停止")
    sp.add_argument("--baseline", default="off", choices=["off", "update", "diff"],
                    help="基线模式：update 写基线，diff 做回归比对")
    sp.add_argument("--fail-on-flaky", action="store_true", help="存在 flaky 用例即视为门禁不通过")
    sp.add_argument("--report-dir", help="报告输出目录")
    sp.add_argument("--open", action="store_true", help="跑完自动打开 HTML 报告")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("inventory", help="用例盘点与覆盖概览")
    common(sp)
    sp.add_argument("--cases", action="append")
    sp.add_argument("--out", help="清单导出路径（YAML）")
    sp.set_defaults(func=cmd_inventory)

    sp = sub.add_parser("db", help="数据库操作")
    common(sp)
    sp.add_argument("action", choices=["init", "script", "tables", "query", "snapshot", "cleanup"])
    sp.add_argument("--ddl", help="建表脚本（action=init）")
    sp.add_argument("sql_file", nargs="?", help="SQL 文件（action=script）")
    sp.add_argument("--sql", help="直接给 SQL（action=query）")
    sp.add_argument("--param", action="append", metavar="K=V", help="具名参数（action=query）")
    sp.add_argument("--limit", type=int, default=50, help="查询展示行数上限")
    sp.add_argument("--table", help="表名（action=snapshot）")
    sp.add_argument("--key", default="id", help="快照键列（action=snapshot）")
    sp.add_argument("--fields", nargs="*", help="快照比对列（action=snapshot）")
    sp.add_argument("--where", help="快照过滤条件（action=snapshot）")
    sp.add_argument("--out", help="快照输出路径（action=snapshot）")
    sp.add_argument("--diff", action="store_true", help="与已有基线对比（action=snapshot）")
    sp.set_defaults(func=cmd_db)

    sp = sub.add_parser("demo", help="启动内置演示站点")
    sp.add_argument("--root", help="项目根目录")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--db", default=None, help="演示站点使用的 SQLite 路径")
    sp.set_defaults(func=cmd_demo)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or EXIT_OK)
    except KeyboardInterrupt:
        _print("\n已中断")
        return 130
    except ForgeQAError as exc:
        _print(f"\n[{exc.kind}] {exc.render()}")
        return EXIT_USAGE
    except FileNotFoundError as exc:
        _print(f"\n文件不存在: {exc}")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
