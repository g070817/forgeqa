"""forgeqa.httpclient — requests 封装 + 变量提取 + 接口断言 + 基线录制。

适配任意网站的接口，只需要在用例 YAML 里描述请求，不需要写 Python。
本模块提供四件事：

1. **统一请求**：超时、重试退避、默认头、鉴权注入、耗时统计、全量日志。
2. **变量提取**：从响应里抽出值塞进变量池，供后续步骤引用（接口串联）。
3. **接口断言**：状态码 / 字段 / 结构 / 响应头 / 响应时间 / 业务不变量。
4. **基线录制与回归**：把响应落成基线，后续运行做结构化 diff，
   捕捉「接口悄悄改了返回结构」这类回归。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .assertions import CheckResult, check, jsonpath, jsonpath_first, loose_eq, validate_schema
from .config import MISSING, Context
from .errors import AssertFailed, HttpError

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError as exc:  # pragma: no cover
    raise SystemExit("forgeqa 需要 requests：pip install requests") from exc


# --------------------------------------------------------------------------- #
# 响应包装
# --------------------------------------------------------------------------- #
@dataclass
class Response:
    status: int
    headers: dict[str, str]
    text: str
    url: str
    method: str
    elapsed_ms: float
    request_body: Any = None
    _json: Any = dc_field(default=None, repr=False)
    _json_ok: bool = False
    error: str | None = None
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def json(self, default: Any = None) -> Any:
        if not self._json_ok:
            try:
                self._json = json.loads(self.text) if self.text else None
                self._json_ok = True
            except (json.JSONDecodeError, TypeError):
                self._json = default
                self._json_ok = True
        return self._json

    @property
    def body(self) -> Any:
        j = self.json()
        return j if j is not None else self.text

    def brief(self, limit: int = 1200) -> str:
        body = self.text or ""
        if len(body) > limit:
            body = body[:limit] + f"…(共 {len(self.text)} 字符)"
        return f"{self.method} {self.url} → {self.status} ({self.elapsed_ms}ms)\n{body}"

    def to_record(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "headers": {k: v for k, v in self.headers.items()
                        if k.lower() in ("content-type", "location", "content-length",
                                         "x-request-id", "set-cookie")},
            "body": self.body if isinstance(self.body, (dict, list)) else self.text[:2000],
            "elapsed_ms": round(self.elapsed_ms, 1),
            "url": self.url,
        }


def _to_response(resp: "requests.Response", method: str, url: str,
                 req_body: Any, started: float, attempts: int) -> Response:
    return Response(
        status=resp.status_code,
        headers=dict(resp.headers),
        text=resp.text or "",
        url=str(resp.url or url),
        method=method,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        request_body=req_body,
        attempts=attempts,
    )


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class HttpClient:
    def __init__(self, ctx: Context, opts: Mapping[str, Any], *, base_url: str = "",
                 logger=None):
        self.ctx = ctx
        self.opts = dict(opts or {})
        self.base_url = (base_url or self.opts.get("base_url") or "").rstrip("/") + ("/" if base_url else "")
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = float(self.opts.get("timeout", 15))
        self.retries = int(self.opts.get("retries", 2))
        self.backoff = float(self.opts.get("backoff", 0.4))
        self.verify = bool(self.opts.get("verify_ssl", True))
        self.retry_status = set(self.opts.get("retry_status", [408, 425, 429, 500, 502, 503, 504]))
        # 代理行为：企业网络 / CI 常设 HTTP_PROXY，若不排除回环与内网地址，
        # 本地被测站点会被代理拦成 502。默认对回环+私网直连。
        self.trust_env = bool(self.opts.get("trust_env", True))
        self.no_proxy = [str(h).lower() for h in (
            self.opts.get("no_proxy") or
            ["localhost", "127.0.0.1", "::1", "0.0.0.0", "*.local", "10.*", "192.168.*", "172.16.*"]
        )]
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update({str(k): str(v) for k, v in (self.opts.get("headers") or {}).items()})
        # auth 段里的值同样可能是模板：token / username / password / value 的常见
        # 写法是 ${os:XXX}（凭证不进仓库）。配置层只做三层合并、不做插值，必须
        # 在这里解析——否则发出去的是字面量 "${os:XXX}"，服务端一律 401，
        # 而报错信息里看不到任何线索。
        self.auth_spec: dict[str, Any] = dict(self.ctx.resolve(self.opts.get("auth") or {}) or {})
        self.log: list[dict[str, Any]] = []
        self._apply_auth()

    # ---------------- 鉴权 ----------------
    def _apply_auth(self) -> None:
        """把 auth 配置注入 session。支持 bearer / basic / header / api_key / cookie。"""
        spec = self.auth_spec
        kind = str(spec.get("type", "none")).lower()
        self.session.auth = None
        if kind == "bearer":
            token = spec.get("token") or self.ctx.get(str(spec.get("token_var", "ctx.token")), MISSING)
            if token is MISSING or not token:
                token = None
            self.session.headers.pop("Authorization", None)
            if token:
                self.set_token(str(token), scheme=str(spec.get("scheme", "Bearer")))
            self._pending_bearer = token
        elif kind == "basic":
            self.session.auth = (str(spec.get("username", "")), str(spec.get("password", "")))
        elif kind in ("header", "api_key", "api_key_header"):
            name = str(spec.get("name", "X-API-Key"))
            val = spec.get("value") or self.ctx.get(str(spec.get("value_var", "")), "")
            self.session.headers[name] = str(val)
        elif kind in ("cookie",):
            for k, v in (spec.get("cookies") or {}).items():
                self.session.cookies.set(k, str(v))
        elif kind in ("login", "form"):
            pass  # 由 bootstrap 执行 login 用例
        elif kind in ("none", ""):
            pass
        else:
            raise HttpError(
                f"未知鉴权类型 {kind!r}",
                hint="支持: none / bearer / basic / header / api_key / cookie / login",
            )

    def set_token(self, token: str, *, scheme: str = "Bearer") -> None:
        self.session.headers["Authorization"] = f"{scheme} {token}".strip()

    # ---------------- 请求 ----------------
    def request(self, method: str, path: str, *, params=None, json_body=None, data=None,
                headers=None, cookies=None, files=None, timeout=None, allow_redirects=True,
                retries=None, auth=None, raw_url=False, **kwargs) -> Response:
        method = method.upper()
        url = path if (raw_url or str(path).startswith(("http://", "https://"))) else self._join(path)
        self._apply_proxy_policy(url)
        if auth and str(auth).lower() != "none":
            self._apply_call_auth(auth)

        merged_headers = dict(headers or {})
        body_for_log = json_body if json_body is not None else data
        attempts = int(retries if retries is not None else self.retries) + 1
        last_exc: Exception | None = None

        for i in range(attempts):
            started = time.perf_counter()
            try:
                resp = self.session.request(
                    method, url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=merged_headers or None,
                    cookies=cookies,
                    files=files,
                    timeout=timeout or self.timeout,
                    allow_redirects=allow_redirects,
                    verify=self.verify,
                    **kwargs,
                )
                out = _to_response(resp, method, url, body_for_log, started, i + 1)
                if resp.status_code in self.retry_status and i + 1 < attempts:
                    self._sleep(i)
                    continue
                self._log(out)
                return out
            except requests.RequestException as exc:
                last_exc = exc
                if i + 1 < attempts:
                    self._sleep(i)
                    continue
                self._log_error(method, url, exc, attempts)
                raise HttpError(
                    f"请求失败（已重试 {attempts} 次）: {method} {url} → {exc}",
                    hint=_net_hint(exc),
                ) from exc

        raise HttpError(f"请求失败: {method} {url}") from last_exc

    def _apply_call_auth(self, auth: Any) -> None:
        """步骤级鉴权覆盖，如 ``auth: none``（测未授权场景）。"""
        if isinstance(auth, Mapping):
            kind = str(auth.get("type", "")).lower()
            if kind == "bearer" and auth.get("token"):
                self.set_token(str(auth["token"]))
        elif str(auth).lower() in ("none", "off", "false"):
            self.session.headers.pop("Authorization", None)

    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw) -> Response:
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw) -> Response:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw) -> Response:
        return self.request("DELETE", path, **kw)

    def _apply_proxy_policy(self, url: str) -> None:
        """决定本次请求是否走环境代理：命中 no_proxy 白名单则直连。"""
        host = (urlparse(url).hostname or "").lower()
        bypass = (not self.trust_env) or any(_host_match(host, pat) for pat in self.no_proxy)
        self.session.trust_env = not bypass

    def _join(self, path: str) -> str:
        if not self.base_url:
            raise HttpError(
                "未配置 base_url，无法拼请求地址",
                hint="在 env.yaml 的 envs.<环境>.base_url 填写被测站点根地址",
            )
        if not str(path).startswith("/"):
            path = "/" + str(path)
        return self.base_url + str(path)

    def _sleep(self, i: int) -> None:
        time.sleep(self.backoff * (2 ** i))

    def _log(self, resp: Response) -> None:
        entry = {"method": resp.method, "url": resp.url, "status": resp.status,
                 "ms": round(resp.elapsed_ms, 1), "attempts": resp.attempts}
        self.log.append(entry)
        if self.logger:
            self.logger.debug(f"HTTP {resp.status} {resp.method} {resp.url} ({entry['ms']}ms)")

    def _log_error(self, method: str, url: str, exc: Exception, attempts: int) -> None:
        self.log.append({"method": method, "url": url, "error": str(exc), "attempts": attempts})

    # ---------------- 登录引导 ----------------
    def bootstrap_auth(self) -> dict[str, Any]:
        """按配置里的 ``auth.login`` 定义完成登录并注入凭证。

        除登录请求本身外还有两个可选项：

        - ``login.prepare``：登录前的预备请求。CSRF token、``wordpress_test_cookie``
          这类「必须先握一次手」的凭证靠它拿到——缺了它 WordPress 会直接拒绝登录。
        - ``login.expect``：校验登录结果。登录失败必须显式报错，不能静默带着
          匿名身份继续跑，否则后面的 401 会被误判成「接口坏了」。
        """
        spec = self.auth_spec
        login = spec.get("login")
        if not supports_bootstrap(spec):
            return {}

        for step in login.get("prepare") or []:
            if not isinstance(step, Mapping):
                continue
            self.request(
                str(step.get("method", "GET")),
                str(self.ctx.resolve(step.get("path", "/"))),
                json_body=self.ctx.resolve(step.get("json")),
                data=self.ctx.resolve(step.get("data")),
                headers=self.ctx.resolve(step.get("headers")),
                retries=step.get("retries"),
            )

        resp = self.request(
            str(login.get("method", "POST")),
            str(self.ctx.resolve(login.get("path", "/login"))),
            json_body=self.ctx.resolve(login.get("json")),
            data=self.ctx.resolve(login.get("data")),
            headers=self.ctx.resolve(login.get("headers")),
            retries=login.get("retries"),
        )

        expect = login.get("expect") or {}
        want = expect.get("status")
        if want is not None:
            wanted = want if isinstance(want, (list, tuple, set)) else [want]
            if resp.status not in {int(w) for w in wanted}:
                raise HttpError(
                    f"登录失败：期望状态 {sorted(int(w) for w in wanted)}，实际 {resp.status}",
                    hint=("检查 auth.login 的 path / data 字段名与凭证是否正确；"
                          f"响应片段: {(resp.text or '')[:200]}"),
                )
        extract = login.get("extract") or {}
        found: dict[str, Any] = {}
        for name, rule in extract.items():
            val = extract_value(resp, rule)
            if val is not None:
                found[name] = val
                self.ctx.set(name, val)
                self.ctx.set(f"login.{name}", val)
        if str(spec.get("type", "")).lower() == "bearer":
            token = found.get(str(spec.get("token_field", "token"))) or \
                    found.get("token") or spec.get("token")
            if token:
                self.set_token(str(token))
        else:
            for c in self.session.cookies:
                found.setdefault("_cookie_" + c.name, c.value)
        return found


#: 需要「先登录再跑用例」的鉴权类型。配置里同时给了 auth.login 时，
#: 引擎会在套件启动（或单条用例启动）时自动完成一次表单登录。
BOOTSTRAP_AUTH_TYPES = ("login", "form", "bearer")


def supports_bootstrap(spec: Any) -> bool:
    """配置是否声明了「自动登录引导」——auth.type 是登录类且给了 auth.login。"""
    if not isinstance(spec, Mapping):
        return False
    return (str(spec.get("type", "")).lower() in BOOTSTRAP_AUTH_TYPES
            and bool(spec.get("login")))


def _host_match(host: str, pattern: str) -> bool:
    """支持 localhost / 127.0.0.1 / *.local / 10.* 这类简单通配。"""
    if not host:
        return False
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) or host == pattern[2:]
    if pattern.endswith(".*"):
        return host.startswith(pattern[:-1])
    return host == pattern


def _net_hint(exc: Exception) -> str:
    text = str(exc).lower()
    if "connection refused" in text or "max retries" in text:
        return "目标未启动或端口不通：先启动被测站点，或用 `forgeqa probe <url>` 验证连通性"
    if "proxy" in text:
        return ("代理拦截：确认被测地址是否在 http.no_proxy 白名单里（回环与私网地址默认直连），"
                "必要时设 http.trust_env: false")
    if "name or service not known" in text or "nodename" in text or "getaddrinfo" in text:
        return "域名解析失败：检查 base_url 拼写与网络代理"
    if "timed out" in text:
        return "请求超时：调大 http.timeout，或确认被测服务是否卡住"
    if "certificate" in text or "ssl" in text:
        return "证书校验失败：自签证书环境可设 http.verify_ssl: false（仅限测试环境）"
    return "检查被测服务状态与网络出口"


# --------------------------------------------------------------------------- #
# 变量提取
# --------------------------------------------------------------------------- #
def extract_value(resp: Response, rule: Any) -> Any:
    """从响应里提取一个值。rule 支持字符串简写或对象写法。"""
    if isinstance(rule, str):
        rule = {"jsonpath": rule}
    if not isinstance(rule, Mapping):
        raise HttpError(f"extract 规则格式错误: {rule!r}",
                        hint="写法：uid: $.data.id  或  uid: {jsonpath: $.data.id, default: ''}")

    data = resp.body
    default = rule.get("default", None)

    if "jsonpath" in rule:
        val = jsonpath_first(resp.json(), str(rule["jsonpath"]), MISSING)
        if val is MISSING:
            if "default" in rule:
                return default
            raise HttpError(
                f"提取失败：JSONPath {rule['jsonpath']!r} 没有匹配到任何值",
                hint=f"实际响应片段: {resp.text[:300]}",
            )
        return _cast(val, rule)

    if "header" in rule:
        val = next((v for k, v in resp.headers.items() if k.lower() == str(rule["header"]).lower()), MISSING)
        if val is MISSING:
            if "default" in rule:
                return default
            raise HttpError(f"提取失败：响应头 {rule['header']!r} 不存在",
                            hint=f"实际响应头: {sorted(resp.headers)}")
        return _cast(val, rule)

    if "cookie" in rule:
        jar = getattr(resp, "cookies", None)
        return _cast(jar.get(str(rule["cookie"])) if jar else default, rule)

    if "regex" in rule:
        import re

        m = re.search(str(rule["regex"]), resp.text or "")
        if not m:
            if "default" in rule:
                return default
            raise HttpError(f"提取失败：正则 {rule['regex']!r} 无匹配",
                            hint=f"实际响应片段: {resp.text[:300]}")
        return m.group(1) if m.groups() else m.group(0)

    if rule.get("body") or "raw" in rule:
        return resp.text
    if "status" in rule:
        return resp.status
    if "url" in rule:
        return resp.url
    raise HttpError(f"无法识别的 extract 规则: {rule!r}",
                    hint="可用键：jsonpath / header / cookie / regex / body / status / url")


def _cast(val: Any, rule: Mapping[str, Any]) -> Any:
    if "cast" not in rule or val is None:
        return val
    kind = str(rule["cast"]).lower()
    try:
        return {"int": int, "float": float, "str": str, "bool": lambda v: str(v).lower() in ("1", "true", "yes")}[kind](val)
    except (KeyError, ValueError, TypeError) as exc:
        raise HttpError(f"extract 的 cast={kind!r} 转换失败: {val!r}") from exc


# --------------------------------------------------------------------------- #
# 接口断言
# --------------------------------------------------------------------------- #
def eval_http_assertions(resp: Response, specs: Sequence[Any], *,
                         ctx: Context | None = None) -> list[CheckResult]:
    """把 YAML 里的 assert 列表翻译成 CheckResult 列表（不抛错，由 runner 决定策略）。"""
    results: list[CheckResult] = []
    for spec in specs or []:
        results.extend(_one_assertion(resp, spec, ctx))
    return results


def _one_assertion(resp: Response, spec: Any, ctx: Context | None) -> list[CheckResult]:
    if isinstance(spec, str):
        spec = {"jsonpath": spec, "op": "not_null"}
    if isinstance(spec, Mapping) and "status" in spec and len(spec) == 1:
        spec = {"status": spec["status"]}

    if not isinstance(spec, Mapping):
        raise AssertFailed(f"断言格式错误: {spec!r}",
                           hint="写法：- {jsonpath: $.id, op: eq, value: 1} 或 - {status: 200}")

    out: list[CheckResult] = []
    label = spec.get("label") or spec.get("desc") or ""

    # 状态码：支持单值、列表、区间
    if "status" in spec:
        want = spec["status"]
        if isinstance(want, (list, tuple, set)):
            ok = resp.status in set(want)
            out.append(CheckResult(ok, label or "status", "in", sorted(set(want)), resp.status,
                                   "" if ok else f"状态码 {resp.status} 不在预期 {sorted(set(want))} 内"))
        elif isinstance(want, Mapping) and {"min", "max"} & set(want):
            lo, hi = int(want.get("min", 100)), int(want.get("max", 599))
            ok = lo <= resp.status <= hi
            out.append(CheckResult(ok, label or "status", "between", [lo, hi], resp.status,
                                   "" if ok else f"状态码 {resp.status} 不在 [{lo},{hi}]"))
        else:
            out.append(check(resp.status, "eq", int(want), target=label or "status"))

    # 响应时间
    for key, op in (("time_lt", "lt"), ("time_lte", "lte"), ("time_gt", "gt")):
        if key in spec:
            out.append(check(round(resp.elapsed_ms, 1), op, spec[key],
                             target=label or "响应时间(ms)"))

    # 响应头
    if "header" in spec:
        name = str(spec["header"])
        val = next((v for k, v in resp.headers.items() if k.lower() == name.lower()), None)
        out.append(check(val, str(spec.get("op", "eq")), _resolve(spec.get("value"), ctx),
                         target=label or f"响应头 {name}"))

    # 字段级断言（jsonpath 一网打尽）
    if "jsonpath" in spec:
        op = str(spec.get("op", "eq"))
        expected = _resolve(spec.get("value"), ctx)
        path = str(spec["jsonpath"])
        vals = jsonpath(resp.json(), path)
        if op in ("exists", "present"):
            out.append(check(vals[0] if vals else None, "present", None, target=label or path))
        elif op in ("absent",):
            out.append(check(vals[0] if vals else None, "absent", None, target=label or path))
        elif op == "count":
            out.append(check(len(vals), "eq", int(expected), target=label or f"{path} 匹配数"))
        elif op in ("all", "every"):
            sub = expected if isinstance(expected, Mapping) else {"op": "eq", "value": expected}
            sub_op = str(sub.get("op", "eq"))
            sub_val = _resolve(sub.get("value"), ctx)
            for i, v in enumerate(vals):
                out.append(check(v, sub_op, sub_val, target=label or f"{path}[{i}]"))
            if not vals:
                out.append(CheckResult(False, label or path, "not_empty", "至少 1 个匹配", [],
                                       "JSONPath 没有匹配到任何值"))
        else:
            if not vals:
                out.append(CheckResult(
                    False, label or path, op, expected, None,
                    f"JSONPath {path!r} 无匹配值，无法断言（实际响应: {resp.text[:200]}）"))
            else:
                for i, v in enumerate(vals):
                    tgt = label or (path if len(vals) == 1 else f"{path}[{i}]")
                    out.append(check(v, op, expected, target=tgt))

    # 整体结构
    if "schema" in spec:
        errs = validate_schema(resp.json(), _resolve(spec["schema"], ctx) or {})
        out.append(CheckResult(not errs, label or "response schema", "matches_schema",
                               spec["schema"], "ok" if not errs else errs,
                               "; ".join(errs)))

    # 文本
    if "text_contains" in spec:
        out.append(check(resp.text, "contains", _resolve(spec["text_contains"], ctx),
                         target=label or "响应文本"))
    if "text_regex" in spec:
        out.append(check(resp.text, "regex", _resolve(spec["text_regex"], ctx),
                         target=label or "响应文本"))
    if "body_len_gt" in spec:
        out.append(check(len(resp.text or ""), "gt", int(spec["body_len_gt"]),
                         target=label or "响应体长度"))
    if "body_empty" in spec:
        out.append(check(resp.text, "empty" if spec["body_empty"] else "not_empty",
                         target=label or "响应体"))

    # 通用：直接给值比较（配合 script 步骤产出的变量）
    if "actual" in spec:
        out.append(check(_resolve(spec["actual"], ctx), str(spec.get("op", "eq")),
                         _resolve(spec.get("value"), ctx), target=label or "值"))

    if not out:
        raise AssertFailed(
            f"断言里没有任何可识别字段: {spec!r}",
            hint="可用键：status / jsonpath / schema / header / text_contains / text_regex / "
                 "time_lt / body_len_gt / actual",
        )
    return out


def _resolve(value: Any, ctx: Context | None) -> Any:
    if ctx is None or value is None:
        return value
    return ctx.resolve(value)


# --------------------------------------------------------------------------- #
# 基线录制 / 回归
# --------------------------------------------------------------------------- #
class Recorder:
    """记录响应基线，支持跨运行结构化 diff。

    ``mode``:
      - ``off``    不记录
      - ``update`` 本次运行写基线
      - ``diff``   与基线对比，输出 added/removed/changed
    """

    def __init__(self, path: str | Path, mode: str = "off"):
        self.path = Path(path)
        self.mode = mode
        self.current: dict[str, Any] = {}
        self.baseline: dict[str, Any] = {}
        if mode == "diff" and self.path.exists():
            try:
                self.baseline = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.baseline = {}

    def record(self, case_id: str, step_name: str, resp: Response, extra: Mapping[str, Any] | None = None) -> None:
        if self.mode == "off":
            return
        key = f"{case_id}::{step_name}"
        rec = resp.to_record()
        if extra:
            rec.update(extra)
        self.current[key] = rec

    # 兼容旧命名
    def save(self, case_id: str, step_name: str, resp: Response) -> None:
        self.record(case_id, step_name, resp)

    def diff(self) -> dict[str, Any]:
        from .db import diff_snapshots

        return diff_snapshots(self.baseline, self.current)

    def commit(self) -> None:
        if self.mode != "update":
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        merged = dict(self.baseline)
        merged.update(self.current)
        self.path.write_text(json.dumps(merged, ensure_ascii=False, indent=2, default=str),
                             encoding="utf-8")
