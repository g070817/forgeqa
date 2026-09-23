"""站点扫描：从一个入口 URL 出发，发现接口并生成可运行的用例草稿。

信息来源三路（质量从高到低）：

1. **OpenAPI 文档** —— 依次探测 ``/openapi.json``、``/swagger.json`` 等常见路径，
   命中即得到精确的 path + method 清单。
2. **页面爬取** —— 抓取入口页面的 HTML（href/action/src）与内联 JS 里的路径字符串，
   归一化后作为候选接口。只爬同源地址，限制页数防失控。
3. **常见路径字典** —— ``/api/users``、``/health`` 等高频路径逐个 GET 试探；
   405 + ``Allow`` 头说明路径存在但不接受 GET，同样记录。

产出（``forgeqa scan <url>``）：

- ``config/schemas/<entity>.yaml``   从 JSON 响应反推的造数 Schema（**需人工核对**）
- ``cases/_generated/_scan_<host>.yaml``  可直接运行的 GET 冒烟用例 +
  注释形式的 POST 草稿。文件名 ``_`` 前缀保证默认 `--cases cases` 不会误跑草稿，
  显式传路径即可执行：``forgeqa run --cases cases/_generated/_scan_xxx.yaml``。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
import yaml

from .factory import infer_schema

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

STATIC_EXT = (
    ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map", ".pdf", ".zip",
)

#: OpenAPI/Swagger 文档的常见位置，按序探测
OPENAPI_CANDIDATES = (
    "/openapi.json", "/swagger.json", "/v3/api-docs",
    "/v2/swagger.json", "/api-docs", "/api/openapi.json", "/api/swagger.json",
)

#: 高频接口路径字典。扫描器没有读心术，字典决定「盲区」的大小。
WORDLIST = (
    "/health", "/healthz", "/ready", "/actuator/health", "/status",
    "/api", "/api/index", "/api/users", "/api/user", "/api/account", "/api/me",
    "/api/login", "/api/auth/login", "/api/logout", "/api/register", "/api/token",
    "/api/orders", "/api/order", "/api/items", "/api/item", "/api/products",
    "/api/list", "/api/search", "/api/config", "/api/info", "/api/version",
    "/api/docs", "/api/status", "/api/messages", "/api/comments", "/api/files",
    "/api/departments", "/api/depts", "/api/roles", "/api/menus", "/api/tasks",
)

#: HTML 属性里出现的地址
_HREF_RE = re.compile(r"""(?:href|action|src)\s*=\s*["']([^"']+)["']""", re.I)
#: 内联 JS 里写死的接口路径（fetch/axios 的第一个参数大多是这种形态）
_JS_API_RE = re.compile(r"""["'](/(?:api|v\d)(?:/[A-Za-z0-9_\-{}.:$?=&]+)+)["']""")

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_MAX_SAMPLE_BYTES = 200_000      # 超大响应不作为 schema 反推样本
_MAX_PATH_LEN = 120


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Endpoint:
    """一个被发现的接口路径。"""

    path: str
    methods: set[str] = field(default_factory=set)
    status: int | None = None          # GET 试探的状态码（None = 来自 openapi，未试探）
    content_type: str = ""
    source: str = "wordlist"           # openapi | crawl | wordlist
    sample: Any = None                 # GET 200 且 JSON 时的响应样本
    sample_too_large: bool = False

    @property
    def is_json(self) -> bool:
        return "json" in self.content_type.lower()

    @property
    def entity(self) -> str:
        return entity_for(self.path)


@dataclass
class ScanResult:
    base_url: str
    endpoints: list[Endpoint] = field(default_factory=list)
    pages_crawled: int = 0
    openapi_from: str | None = None    # openapi 文档的来源路径
    errors: list[str] = field(default_factory=list)

    def endpoint(self, path: str) -> Endpoint:
        for ep in self.endpoints:
            if ep.path == path:
                return ep
        raise KeyError(path)


# --------------------------------------------------------------------------- #
# 纯函数（单测覆盖这一层）
# --------------------------------------------------------------------------- #
def is_static(path: str) -> bool:
    """静态资源（css/图片/字体…）不作为接口候选。"""
    return path.lower().split("?")[0].endswith(STATIC_EXT)


def normalize_path(raw: str) -> str | None:
    """把页面里抓到的地址归一化为「同源路径」，不属于接口的返回 None。"""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith(("mailto:", "javascript:", "tel:", "data:", "#")):
        return None
    if raw.startswith(("http://", "https://")):
        return None                       # 绝对地址由调用方按同源过滤
    if not raw.startswith("/"):
        return None
    path = raw.split("#", 1)[0]
    if len(path) > _MAX_PATH_LEN or is_static(path):
        return None
    return path


def extract_paths(text: str) -> set[str]:
    """从 HTML/JS 文本里提取候选路径。JS 路径去掉查询串（不同参数不算不同接口）。"""
    found: set[str] = set()
    for m in _HREF_RE.finditer(text):
        p = normalize_path(m.group(1))
        if p:
            found.add(p)
    for m in _JS_API_RE.finditer(text):
        found.add(m.group(1).split("?", 1)[0])
    return found


def entity_for(path: str) -> str:
    """/api/users/42 → users。用作 schema 文件名与实体名。"""
    segs = [s for s in re.split(r"/+", path) if s and not s.startswith("{")]
    if not segs:
        return "root"
    last = segs[-1]
    if last.isdigit() or last.startswith("{"):        # 集合路径取倒数第二段
        last = segs[-2] if len(segs) >= 2 else "root"
    name = re.sub(r"[^A-Za-z0-9_]", "_", last).strip("_").lower()
    return name or "root"


def parse_openapi(spec: dict[str, Any]) -> list[Endpoint]:
    """解析 OpenAPI/Swagger 的 paths 段。"""
    endpoints: list[Endpoint] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        methods = {m.upper() for m in item if m.lower() in _HTTP_METHODS}
        if methods:
            endpoints.append(Endpoint(path=str(path), methods=methods, source="openapi"))
    return endpoints


def build_case_docs(result: ScanResult) -> list[dict[str, Any]]:
    """把扫描结果变成可直接运行的 GET 冒烟用例。"""
    docs: list[dict[str, Any]] = []
    for ep in sorted(result.endpoints, key=lambda e: e.path):
        if ep.status != 200:              # 只给「GET 通了」的路径生成冒烟
            continue
        assertions: list[dict[str, Any]] = [{"status": 200}, {"time_lt": 5000}]
        if ep.is_json:
            assertions.append({"jsonpath": "$", "op": "not_null", "label": "响应是合法 JSON"})
        docs.append({
            "id": f"TC-SCAN-{len(docs) + 1:03d}",
            "title": f"冒烟: GET {ep.path} 返回 200",
            "priority": "P2",
            "layer": "api",
            "tags": ["scan", "smoke"],
            "steps": [{
                "name": f"GET {ep.path}",
                "http": {"method": "GET", "path": ep.path},
                "assert": assertions,
            }],
        })
    return docs


# --------------------------------------------------------------------------- #
# 扫描主流程（带网络 IO）
# --------------------------------------------------------------------------- #
def _fetch(url: str, *, method: str = "GET", timeout: float = 4.0,
           headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
    """返回 (status, headers, text)；网络异常时 status=0、text=错误信息。"""
    try:
        resp = requests.request(method, url, headers=headers, timeout=timeout,
                                allow_redirects=False)
        return resp.status_code, dict(resp.headers), resp.text
    except requests.RequestException as exc:
        return 0, {}, str(exc)


def _same_host(base: str, url: str) -> bool:
    return urlsplit(url).netloc == urlsplit(base).netloc


def detect_openapi(base: str, *, timeout: float, headers: dict[str, str]) -> tuple[str, dict] | None:
    for cand in OPENAPI_CANDIDATES:
        status, hdrs, text = _fetch(base + cand, timeout=timeout, headers=headers)
        if status != 200 or "json" not in hdrs.get("Content-Type", "").lower():
            continue
        if len(text) > _MAX_SAMPLE_BYTES:
            continue
        try:
            spec = json.loads(text)
        except ValueError:
            continue
        if isinstance(spec, dict) and ("openapi" in spec or "swagger" in spec):
            return cand, spec
    return None


def scan_site(
    base: str,
    *,
    start_paths: tuple[str, ...] = ("/",),
    max_pages: int = 8,
    timeout: float = 4.0,
    headers: dict[str, str] | None = None,
    max_probes: int = 48,
) -> ScanResult:
    """扫描站点，返回发现的接口清单。只访问与 base 同源的地址。"""
    base = base.rstrip("/")
    headers = headers or {}
    result = ScanResult(base_url=base)

    # --- 1. OpenAPI 优先 ------------------------------------------------
    hit = detect_openapi(base, timeout=timeout, headers=headers)
    endpoints: dict[str, Endpoint] = {}
    if hit:
        from_path, spec = hit
        result.openapi_from = from_path
        for ep in parse_openapi(spec):
            endpoints[ep.path] = ep

    # --- 2. 爬页面收集候选 ----------------------------------------------
    candidates: set[str] = set()
    page_queue = [p if p.startswith("/") else "/" + p for p in start_paths]
    visited: set[str] = set()
    while page_queue and len(visited) < max_pages:
        page = page_queue.pop(0)
        if page in visited or page in candidates:
            continue
        visited.add(page)
        status, hdrs, text = _fetch(base + page, timeout=timeout, headers=headers)
        result.pages_crawled += 1
        if status != 200 or "html" not in hdrs.get("Content-Type", "").lower():
            continue
        for raw_path in extract_paths(text):
            abs_url = urljoin(base + page, raw_path)
            if not _same_host(base, abs_url):
                continue
            norm = normalize_path(urlsplit(abs_url).path) or (
                "/" if urlsplit(abs_url).path in ("", "/") else None)
            if norm:
                candidates.add(norm)
                # 页面本身也入爬取队列（限制在 max_pages 内）
                if not norm.startswith(("/api/", "/v")):
                    page_queue.append(norm)

    # --- 3. 候选 + 字典，逐个 GET 试探 -----------------------------------
    probes = sorted(candidates | set(WORDLIST))
    if len(probes) > max_probes:
        probes = probes[:max_probes]
    for path in probes:
        if path in endpoints:                      # openapi 已覆盖，只补试探信息
            ep = endpoints[path]
        else:
            ep = Endpoint(path=path, source="crawl" if path in candidates else "wordlist")
        status, hdrs, text = _fetch(base + path, timeout=timeout, headers=headers)
        ep.methods.add("GET")
        ep.status = status if status else ep.status
        ep.content_type = hdrs.get("Content-Type", "")
        if status == 405:                          # 方法不允许 → 路径存在
            allow = hdrs.get("Allow", "")
            ep.methods |= {m.strip().upper() for m in allow.split(",") if m.strip()}
            ep.methods.discard("GET")
        elif status == 200 and ep.is_json and len(text.encode()) <= _MAX_SAMPLE_BYTES:
            try:
                ep.sample = json.loads(text)
            except ValueError:
                pass
        elif status == 200 and ep.is_json:
            ep.sample_too_large = True
        endpoints.setdefault(path, ep)

    result.endpoints = sorted(endpoints.values(), key=lambda e: (e.path, e.source))
    return result


# --------------------------------------------------------------------------- #
# 产物生成
# --------------------------------------------------------------------------- #
def write_schemas(
    result: ScanResult,
    out_dir: Path,
    *,
    force: bool = False,
) -> list[tuple[Path, str]]:
    """对拿到 JSON 样本的接口反推造数 Schema。返回 (路径, 状态) 列表。"""
    written: list[tuple[Path, str]] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for ep in result.endpoints:
        if ep.sample is None or not isinstance(ep.sample, (dict, list)):
            continue
        try:
            schema = infer_schema(ep.sample, entity=ep.entity)
        except Exception:                          # 样本形态太怪就跳过，不挡整体扫描
            continue
        target = out_dir / f"{ep.entity}.yaml"
        if target.exists() and not force:
            backup = target.with_suffix(".inferred.yaml")
            backup.write_text(yaml.safe_dump(schema, allow_unicode=True, sort_keys=False),
                              encoding="utf-8")
            written.append((backup, "已存在，反推结果写入 .inferred.yaml（对比后合并）"))
        else:
            target.write_text(yaml.safe_dump(schema, allow_unicode=True, sort_keys=False),
                              encoding="utf-8")
            written.append((target, "写入"))
    return written


def write_case_file(
    result: ScanResult,
    out_dir: Path,
) -> Path | None:
    """生成 GET 冒烟用例 + POST 草稿（注释形式）。"""
    docs = build_case_docs(result)
    if not docs:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    host = re.sub(r"[^A-Za-z0-9_.-]", "_", urlsplit(result.base_url).netloc) or "site"
    target = out_dir / f"_scan_{host}.yaml"

    post_lines: list[str] = []
    for ep in result.endpoints:
        if "POST" in ep.methods:
            post_lines.append(f"#   forgeqa probe {result.base_url} --path {ep.path} "
                              f"--method POST --entity {ep.entity}")
    schema_lines: list[str] = []
    for ep in result.endpoints:
        if ep.sample is not None:
            schema_lines.append(f"#   config/schemas/{ep.entity}.yaml  ← GET {ep.path}")

    header = "\n".join([
        "# 由 `forgeqa scan` 自动生成 —— GET 冒烟可直接运行；POST 用例请人工编写后加入。",
        "# 运行本文件:  forgeqa run --cases " + str(target),
        "# 注意: 默认 `--cases cases` 不会加载本文件（`_` 前缀被目录扫描跳过）。",
        "",
        "# 反推的造数 Schema（请人工核对枚举值、长度等业务约束）:",
        *(schema_lines or ["#   （未拿到 JSON 样本，无 schema 产出）"]),
        "",
        "# POST 草稿——有接口文档时用 `forgeqa import <文档路径或URL>` 自动生成；",
        "# 否则用 probe 探测请求体结构后参考 cases/api_user_crud.yaml 手工编写:",
        *(post_lines or ["#   （未发现 POST 接口）"]),
        "",
    ])
    body = yaml.safe_dump({"cases": docs}, allow_unicode=True, sort_keys=False)
    target.write_text(header + body, encoding="utf-8")
    return target
