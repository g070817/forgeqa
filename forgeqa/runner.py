"""forgeqa.runner — 用例引擎。

一个用例 = 一段 YAML，由若干步骤组成，步骤是四种能力（http / db / ui / script）的编排。
引擎负责：变量池、步骤调度、断言判定、失败分类、重试与 flaky 识别、并发、清理。

**失败分类**（对应缺陷根因四分类的第一步）
- ``FAILED``  断言未通过 → 优先怀疑被测系统（缺陷 / 数据不一致）
- ``ERROR``   工具层异常 → 优先怀疑脚本、环境、数据准备（不是缺陷）

用例 YAML 结构::

    id: TC-API-001
    title: 创建用户并校验落库
    priority: P0
    tags: [api, smoke]
    data:                                  # 造数（Faker 驱动）
      user: schemas/user.yaml              # count=1 → ${data.user.name}
      depts: {schema: schemas/dept.yaml, count: 3}
    steps:
      - name: 创建用户
        http: {method: POST, path: /api/users, json: {name: "${data.user.name}"}}
        extract: {uid: "$.data.id"}
        assert:
          - {status: 201}
          - {jsonpath: "$.data.name", op: eq, value: "${data.user.name}"}
      - name: 落库校验
        db:
          sql: "SELECT name, status FROM users WHERE id = :uid"
          params: {uid: "${ctx.uid}"}
        assert:
          - {rows_count: 1}
          - {row: {field: name, op: eq, value: "${data.user.name}"}}
          - {sql: "SELECT COUNT(*) FROM users WHERE id = :uid", op: eq, value: 1}
"""
from __future__ import annotations

import copy
import importlib.util
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

from .assertions import CheckResult, check
from .config import MISSING, Context, ForgeConfig, deep_get
from .db import Database, Seeder, cleanup_from_ledger, diff_snapshots
from .errors import AssertFailed, CaseError, DbError, ForgeQAError, HttpError, UiError
from .factory import DataFactory
from .httpclient import HttpClient, Recorder, eval_http_assertions, extract_value, jsonpath_first
from .uiauto import UiDriver, eval_ui_assertions

_EXPR_SUB_RE = re.compile(r"\$\{([^}]+)\}")
_SAFE_BUILTINS = {"__builtins__": {}, "len": len, "abs": abs, "round": round,
                  "int": int, "float": float, "str": str, "bool": bool,
                  "min": min, "max": max, "sum": sum, "sorted": sorted,
                  "list": list, "dict": dict, "set": set, "any": any, "all": all}

PASSED, FAILED, ERROR, SKIPPED, FLAKY = "PASSED", "FAILED", "ERROR", "SKIPPED", "FLAKY"
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

_DB_LOCK = threading.Lock()
_UI_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #
@dataclass
class StepResult:
    name: str
    kind: str
    status: str = PASSED
    ms: float = 0.0
    checks: list[dict[str, Any]] = dc_field(default_factory=list)
    error: str | None = None
    hint: str | None = None
    detail: dict[str, Any] = dc_field(default_factory=dict)
    artifacts: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "status": self.status,
                "ms": round(self.ms, 1), "checks": self.checks, "error": self.error,
                "hint": self.hint, "detail": self.detail, "artifacts": self.artifacts}


@dataclass
class CaseResult:
    id: str
    title: str
    priority: str = "P1"
    tags: list[str] = dc_field(default_factory=list)
    layer: str = "api"
    status: str = PASSED
    ms: float = 0.0
    steps: list[StepResult] = dc_field(default_factory=list)
    error: str | None = None
    hint: str | None = None
    attempts: int = 1
    flaky: bool = False
    started_at: str = ""
    source: str = ""
    data_snapshot: dict[str, Any] = dc_field(default_factory=dict)
    baseline_diff: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status in (PASSED, SKIPPED)

    @property
    def failed_checks(self) -> list[dict[str, Any]]:
        return [c for s in self.steps for c in s.checks if not c.get("passed")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "priority": self.priority,
            "tags": self.tags, "layer": self.layer, "status": self.status,
            "ms": round(self.ms, 1), "attempts": self.attempts, "flaky": self.flaky,
            "error": self.error, "hint": self.hint, "started_at": self.started_at,
            "source": self.source, "steps": [s.to_dict() for s in self.steps],
            "data_snapshot": self.data_snapshot, "baseline_diff": self.baseline_diff,
        }


@dataclass
class SuiteResult:
    cases: list[CaseResult] = dc_field(default_factory=list)
    env: str = ""
    base_url: str = ""
    started_at: str = ""
    duration_ms: float = 0.0
    seed: int | None = None
    bootstrap: dict[str, Any] = dc_field(default_factory=dict)
    db_cleanup: dict[str, int] = dc_field(default_factory=dict)
    options: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.status == PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if c.status == FAILED)

    @property
    def errors(self) -> int:
        return sum(1 for c in self.cases if c.status == ERROR)

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.cases if c.status == SKIPPED)

    @property
    def flaky(self) -> int:
        return sum(1 for c in self.cases if c.flaky)

    @property
    def pass_rate(self) -> float:
        denom = self.total - self.skipped
        return (self.passed / denom) if denom else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {"total": self.total, "passed": self.passed, "failed": self.failed,
                        "errors": self.errors, "skipped": self.skipped, "flaky": self.flaky,
                        "pass_rate": round(self.pass_rate, 4)},
            "env": self.env, "base_url": self.base_url, "started_at": self.started_at,
            "duration_ms": round(self.duration_ms, 1), "seed": self.seed,
            "bootstrap": self.bootstrap, "db_cleanup": self.db_cleanup,
            "options": self.options,
            "cases": [c.to_dict() for c in self.cases],
        }


# --------------------------------------------------------------------------- #
# 用例加载
# --------------------------------------------------------------------------- #
@dataclass
class Case:
    id: str
    title: str
    steps: list[dict[str, Any]]
    priority: str = "P1"
    tags: list[str] = dc_field(default_factory=list)
    layer: str = "api"
    data: dict[str, Any] = dc_field(default_factory=dict)
    setup: list[dict[str, Any]] = dc_field(default_factory=list)
    teardown: list[dict[str, Any]] = dc_field(default_factory=list)
    retries: int | None = None
    skip: Any = None
    baseline: dict[str, Any] | None = None
    source: str = ""
    raw: dict[str, Any] = dc_field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source: str = "") -> "Case":
        if "steps" not in data and "id" not in data:
            raise CaseError(f"用例缺少 id/steps: {source}")
        cid = str(data.get("id") or Path(source).stem)
        return cls(
            id=cid,
            title=str(data.get("title") or cid),
            steps=list(data.get("steps") or []),
            priority=str(data.get("priority", "P1")).upper(),
            tags=[str(t) for t in (data.get("tags") or [])],
            layer=str(data.get("layer") or _guess_layer(data.get("steps") or [])),
            data=dict(data.get("data") or {}),
            setup=list(data.get("setup") or []),
            teardown=list(data.get("teardown") or []),
            retries=data.get("retries"),
            skip=data.get("skip"),
            baseline=dict(data["baseline"]) if isinstance(data.get("baseline"), Mapping) else None,
            source=source,
            raw=dict(data),
        )


def _guess_layer(steps: Sequence[Mapping[str, Any]]) -> str:
    for s in steps:
        for k in ("http", "db", "ui", "script", "sleep", "log"):
            if k in s:
                return {"http": "api", "db": "db", "ui": "ui"}.get(k, "api")
    return "api"


def load_cases(paths: Sequence[str | Path], root: Path) -> list[Case]:
    files: list[Path] = []
    for p in paths:
        pp = Path(p)
        if not pp.is_absolute():
            pp = root / pp
        if pp.is_dir():
            files.extend(sorted(x for x in pp.rglob("*.y*ml")
                                if not x.name.startswith(("_", ".")) and "schema" not in x.parts))
        elif pp.exists():
            files.append(pp)
        else:
            raise CaseError(f"用例路径不存在: {pp}",
                            hint="检查 --cases 参数，或运行 `forgeqa init` 生成示例用例")

    cases: list[Case] = []
    for f in files:
        try:
            raw = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise CaseError(
                f"用例 YAML 解析失败: {f}\n{exc}",
                hint="常见原因：缩进不一致、冒号后缺少空格、未转义的引号；"
                     "另外 ${...} 出现在 { } 流式写法里必须加引号，"
                     '例如 {value: "${data.user.age}"} 而不是 {value: ${data.user.age}}',
            ) from exc
        if raw is None:
            continue
        docs = raw if isinstance(raw, list) else [raw]
        for doc in docs:
            if isinstance(doc, Mapping) and "cases" in doc:
                for sub in doc["cases"]:
                    cases.append(Case.from_dict(sub, source=str(f.relative_to(root))))
            elif isinstance(doc, Mapping) and "steps" in doc:
                cases.append(Case.from_dict(doc, source=str(f.relative_to(root))))
    if not cases:
        raise CaseError(
            f"在 {[str(p) for p in paths]} 下没有找到任何用例",
            hint="用例文件需要顶层包含 steps 关键字（或 cases 列表）",
        )
    return cases


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #
class Executor:
    """单个用例的执行器。每个用例独立一个实例，保证线程安全。"""

    def __init__(self, case: Case, cfg: ForgeConfig, *, suite_ctx: Context,
                 recorder: Recorder | None = None, logger=None,
                 artifacts_dir: Path | None = None):
        self.case = case
        self.cfg = cfg
        self.logger = logger
        self.artifacts_dir = artifacts_dir or (cfg.path("report.out_dir", "./out/reports") / "artifacts")
        self.recorder = recorder
        self.suite_ctx = suite_ctx
        self.ctx = self._fork_context(suite_ctx)
        self.factory = DataFactory(self.ctx, schema_dir=cfg.root / "config" / "schemas")
        self._last_resp: Any = None
        self.http: HttpClient | None = None
        self.db: Database | None = None
        self.ui: UiDriver | None = None
        self.result = CaseResult(id=case.id, title=case.title, priority=case.priority,
                                 tags=list(case.tags), layer=case.layer, source=case.source)

    def _fork_context(self, parent: Context) -> Context:
        """用例隔离：env/cfg 共享，ctx/data/case 独立。"""
        child = Context(
            layers={
                "env": parent.layers.get("env", {}),
                "cfg": parent.layers.get("cfg", {}),
                "suite": parent.layers.get("suite", {}),
            },
            faker_locale=parent.faker_locale,
            seed=parent.seed,
            base_dir=parent.base_dir,
        )
        # 引导阶段（登录、造数）产出的变量让用例可直接引用
        child.layers["ctx"].update(copy.deepcopy(parent.layers.get("ctx", {})))
        child.set("case_id", self.case.id, layer="case")
        return child

    # ---------------- 主流程 ----------------
    def run(self) -> CaseResult:
        started = time.perf_counter()
        self.result.started_at = datetime.now().isoformat(timespec="seconds")
        try:
            if self.case.skip:
                self.result.status = SKIPPED
                self.result.error = str(self.case.skip) if not isinstance(self.case.skip, bool) else "用例标记为跳过"
                return self.result

            self._prepare()
            failures: list[StepResult] = []
            aborted = False
            for step in [*self.case.setup, *self.case.steps]:
                sr = self._run_step(step)
                self.result.steps.append(sr)
                if sr.status in (FAILED, ERROR):
                    failures.append(sr)
                    if str(step.get("on_fail", "abort")).lower() != "continue":
                        self.result.status = sr.status
                        self.result.error = sr.error
                        self.result.hint = sr.hint
                        aborted = True
                        break
            # on_fail=continue 只是「继续往下跑」，不是「失败被原谅」：
            # 用例最终状态必须仍然反映失败，否则 CI 门禁会漏放
            if not aborted and failures:
                first = failures[0]
                self.result.status = first.status
                self.result.error = (
                    f"共 {len(failures)} 个步骤失败（on_fail=continue，已继续执行）：{first.error}")
                self.result.hint = first.hint
        except (AssertFailed, ForgeQAError) as exc:
            self.result.status = FAILED if isinstance(exc, AssertFailed) else ERROR
            self.result.error = exc.render() if hasattr(exc, "render") else str(exc)
            self.result.hint = getattr(exc, "hint", None)
        except Exception as exc:  # 引擎自身异常也算 ERROR，但要留完整栈
            self.result.status = ERROR
            self.result.error = f"{type(exc).__name__}: {exc}"
            self.result.hint = "这是引擎层异常，请检查用例 YAML 结构或提 issue"
            if self.logger:
                self.logger.debug(traceback.format_exc())
        finally:
            self._finish()
            self.result.ms = (time.perf_counter() - started) * 1000
            self.result.data_snapshot = {
                k: v for k, v in self.ctx.layers.get("data", {}).items()
            }
        return self.result

    def _prepare(self) -> None:
        # 1) 造数
        #    data 段三种含义：字面量（列表/标量，直接可用）｜schema 文件路径｜内联 schema
        for name, spec in self.case.data.items():
            if isinstance(spec, (list, tuple, int, float, bool)) or spec is None:
                self.ctx.set(name, list(spec) if isinstance(spec, tuple) else spec, layer="data")
                continue
            if isinstance(spec, str) and spec.startswith(("$", "{")):
                self.ctx.set(name, self.ctx.resolve(spec), layer="data")
                continue
            rows = self._load_dataset(name, spec)
            self.ctx.set(name, rows if len(rows) != 1 else rows[0], layer="data")
            self.ctx.set(f"{name}_list", rows, layer="data")

        # 2) 数据库连接
        db_opts = self.cfg.get("db") or {}
        if db_opts.get("driver"):
            try:
                self.db = Database.from_opts(db_opts, self.cfg.root)
                self.db.driver.connect()
            except Exception as exc:
                self.db = None
                if self._needs_db():
                    raise DbError(f"数据库连接失败: {exc}", hint="检查 db 配置") from exc

        # 3) HTTP 客户端 + 鉴权引导
        http_opts = dict(self.cfg.get("http") or {})
        auth_opts = dict(self.cfg.get("auth") or {})
        http_opts["auth"] = auth_opts
        self.http = HttpClient(self.ctx, http_opts, base_url=self.cfg.get("base_url", ""),
                               logger=self.logger)
        # 需要登录态但变量池里还没有 token → 本用例自行完成一次登录（用例之间保持独立）
        if str(auth_opts.get("type", "")).lower() == "bearer" and auth_opts.get("login") \
                and not self.ctx.get(str(auth_opts.get("token_var", "ctx.token")), None):
            self.http.bootstrap_auth()

    def _needs_db(self) -> bool:
        return any("db" in s for s in [*self.case.setup, *self.case.steps, *self.case.teardown])

    def _needs_ui(self) -> bool:
        return any("ui" in s for s in [*self.case.setup, *self.case.steps, *self.case.teardown])

    def _load_dataset(self, name: str, spec: Any) -> list[dict[str, Any]]:
        """data 段的三种写法：

        ``user: user.yaml``                                 引用 schema 文件
        ``user: {schema: user.yaml, count: 3, overrides: {}}``  带参数
        ``user: {entity: user, fields: [...]}``               直接内联
        """
        if isinstance(spec, str):
            return self.factory.generate(spec)
        if not isinstance(spec, Mapping):
            raise CaseError(
                f"data.{name} 的类型不支持: {type(spec).__name__}",
                hint="字面量数据请用列表/标量，造数请用 schema 文件路径或内联 {entity, fields}",
            )
        if "fields" in spec:            # 内联 schema 定义
            return self.factory.generate(spec)
        schema = spec.get("schema")
        if schema is None:
            raise CaseError(
                f"data.{name} 的定义无法识别: {spec!r}",
                hint="三种写法：user: user.yaml ｜ user: {schema: user.yaml, count: 3} ｜ "
                     "user: {entity: user, fields: [...]}（内联）",
            )
        overrides = self.ctx.resolve(spec["overrides"]) if spec.get("overrides") else None
        if spec.get("mutate"):
            base = spec.get("base")
            if isinstance(base, str):
                base = self.ctx.get(base, None)
                if isinstance(base, Mapping) and "_index" in base:
                    base = {k: v for k, v in base.items() if not k.startswith("_")}
            return self.factory.mutate(
                schema, base=base,
                categories=spec.get("categories") or ("boundary", "abnormal", "extreme"),
            )
        return self.factory.generate(schema, count=spec.get("count"), overrides=overrides)

    def _finish(self) -> None:
        # 用例级清理：只删本用例造的数据，避免污染被测环境
        for step in self.case.teardown:
            try:
                self._run_step(step)
            except Exception as exc:
                if self.logger:
                    self.logger.debug(f"teardown 失败: {exc}")
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.ui is not None:
            try:
                self.ui.stop()
            except Exception:
                pass
            self.ui = None

    # ---------------- 步骤分发 ----------------
    def _run_step(self, step: Mapping[str, Any]) -> StepResult:
        name = str(step.get("name") or _step_label(step))
        kind = _step_kind(step)
        sr = StepResult(name=name, kind=kind)
        started = time.perf_counter()
        self._last_resp = None

        cond = step.get("if")
        if cond is not None and not _eval_condition(self.ctx, cond):
            sr.status = SKIPPED
            sr.detail["reason"] = f"if 条件不满足: {cond}"
            return sr

        try:
            if "loop" in step:
                self._run_loop(step, sr, name)
            else:
                self._dispatch(step, kind, sr)
                checks = self._collect_checks(step, kind)
                sr.checks = [c.to_dict() for c in checks]
                failed = [c for c in checks if not c.passed]
                if failed:
                    sr.status = FAILED
                    sr.error = failed[0].message
                    sr.hint = self._assert_hint(failed[0], kind)
        except AssertFailed as exc:
            sr.status = FAILED
            sr.error = exc.render()
            sr.hint = exc.hint
        except (DbError, HttpError, UiError, CaseError) as exc:
            sr.status = ERROR
            sr.error = exc.render()
            sr.hint = exc.hint
        except ForgeQAError as exc:
            sr.status = ERROR
            sr.error = exc.render()
            sr.hint = exc.hint
        except Exception as exc:
            sr.status = ERROR
            sr.error = f"{type(exc).__name__}: {exc}"
            sr.hint = "工具层异常，排查用例 YAML 与站点实际结构"
            if self.logger:
                self.logger.debug(traceback.format_exc())
        finally:
            sr.ms = (time.perf_counter() - started) * 1000
        return sr

    def _run_loop(self, step: Mapping[str, Any], sr: StepResult, name: str) -> None:
        spec = step["loop"]
        items = self.ctx.resolve(spec.get("over") or spec.get("items") or [])
        alias = str(spec.get("as") or "item")
        body = list(spec.get("steps") or [])
        if not isinstance(items, (list, tuple)):
            raise CaseError(f"loop.over 必须是列表，实际是 {type(items).__name__}",
                            hint="例如 loop: {over: '${data.users_list}', as: user, steps: [...]}")
        saved = copy.deepcopy(self.ctx.layers["ctx"].get(alias))
        failures = 0
        for idx, item in enumerate(items):
            self.ctx.set(alias, item)
            self.ctx.set(f"{alias}_index", idx)
            for sub in body:
                s = copy.deepcopy(dict(sub))
                s["name"] = f"{name}[{idx}] {s.get('name', '')}".strip()
                subres = self._run_step(s)
                sr.checks.extend(subres.checks)
                if subres.status in (FAILED, ERROR):
                    failures += 1
                    if str(spec.get("on_fail", "abort")).lower() == "abort":
                        sr.status = subres.status
                        sr.error = f"循环第 {idx} 次迭代失败: {subres.error}"
                        self.ctx.set(alias, saved)
                        return
        if failures and sr.status == PASSED:
            sr.status = FAILED
            sr.error = f"循环中有 {failures} 次迭代存在断言失败"
        self.ctx.set(alias, saved)

    def _dispatch(self, step: Mapping[str, Any], kind: str, sr: StepResult) -> None:
        if kind == "http":
            self._do_http(step, sr)
        elif kind == "db":
            self._do_db(step, sr)
        elif kind == "ui":
            self._do_ui(step, sr)
        elif kind == "script":
            self._do_script(step, sr)
        elif kind == "sleep":
            secs = float(self.ctx.resolve(step["sleep"] if not isinstance(step["sleep"], Mapping)
                                          else step["sleep"].get("seconds", 1)))
            time.sleep(max(secs, 0))
            sr.detail["sleep"] = secs
        elif kind == "log":
            sr.detail["message"] = str(self.ctx.resolve(step["log"]))
        elif kind == "assert":
            pass      # 断言在 _collect_checks 里统一执行
        else:
            raise CaseError(
                f"无法识别的步骤: {step!r}",
                hint="每个步骤必须包含 http / db / ui / script / sleep / log 之一",
            )

    # ---------------- HTTP ----------------
    def _do_http(self, step: Mapping[str, Any], sr: StepResult) -> None:
        spec = self.ctx.resolve(step["http"]) if isinstance(step["http"], Mapping) else {}
        exec_spec = self.ctx.resolve(step.get("request") or {})
        spec = {**spec, **exec_spec}
        method = str(spec.get("method", "GET"))
        path = spec.get("path") or spec.get("url")
        if not path:
            raise CaseError(f"步骤 {step.get('name')!r} 的 http 缺少 path",
                            hint="例如 http: {method: GET, path: /api/users}")
        resp = self.http.request(
            method, str(path),
            params=spec.get("params"),
            json_body=spec.get("json"),
            data=spec.get("data"),
            headers=spec.get("headers"),
            cookies=spec.get("cookies"),
            timeout=spec.get("timeout"),
            retries=spec.get("retries"),
            auth=spec.get("auth"),
            raw_url=bool(spec.get("raw_url")),
        )
        sr.detail["request"] = {"method": method.upper(), "url": resp.url,
                                "params": spec.get("params"),
                                "json": spec.get("json"), "data": spec.get("data"),
                                "headers": spec.get("headers")}
        sr.detail["response"] = resp.to_record()
        if resp.attempts > 1:
            sr.detail["retried"] = resp.attempts - 1
        self._last_resp = resp

        if self.recorder:
            self.recorder.record(self.case.id, sr.name, resp)

        for var, rule in (step.get("extract") or {}).items():
            val = extract_value(resp, self.ctx.resolve(rule))
            self.ctx.set(str(var), val)
            sr.detail.setdefault("extracted", {})[str(var)] = _brief(val)

        for var, path_or_spec in (step.get("store") or {}).items():
            rule = {"jsonpath": path_or_spec} if isinstance(path_or_spec, str) else dict(path_or_spec)
            self.ctx.set(str(var), extract_value(resp, rule))

    # ---------------- SQL ----------------
    def _do_db(self, step: Mapping[str, Any], sr: StepResult) -> None:
        if self.db is None:
            raise DbError("本步骤需要数据库，但连接未建立",
                          hint="检查 env.yaml 里的 db.driver 配置")
        raw = step["db"]
        spec = dict(self.ctx.resolve(raw)) if isinstance(raw, Mapping) else {"sql": self.ctx.resolve(raw)}
        mode = str(spec.get("mode") or spec.get("action") or "query").lower()
        sql = str(spec.get("sql") or spec.get("query") or "")
        if not sql:
            raise CaseError(f"步骤 {step.get('name')!r} 的 db 缺少 sql")
        params = spec.get("params") or {}
        # 具名参数容错：允许 params 里写 "${ctx.x}" 之外的裸值
        parsed = _parse_sql_params(sql, params)

        sr.detail["sql"] = {"text": " ".join(sql.split())[:2000], "params": _brief(parsed), "mode": mode}

        if mode == "script":
            with _DB_LOCK:
                self.db.script(sql)
            sr.detail["sql"]["result"] = "script executed"
            return
        if mode == "file":
            with _DB_LOCK:
                self.db.script_file(self.cfg.root / sql)
            sr.detail["sql"]["result"] = "script file executed"
            return
        if mode == "execute":
            with _DB_LOCK:
                affected = self.db.execute(sql, parsed)
            sr.detail["rows"] = affected
            sr.detail["sql"]["rows_affected"] = affected
            self.ctx.set("db_affected", affected)
            return

        rows = self.db.query(sql, parsed)
        sr.detail["rows"] = [_brief(r, 400) for r in rows[:50]]
        sr.detail["row_count"] = len(rows)
        self.ctx.set("db_rows", rows)
        self.ctx.set("db_row_count", len(rows))
        if rows:
            self.ctx.set("db_row", rows[0])
            self.ctx.set("db_scalar", next(iter(rows[0].values())) if rows[0] else None)

        for var, expr in (step.get("extract") or {}).items():
            self.ctx.set(str(var), _extract_from_rows(rows, self.ctx.resolve(expr), self.db, parsed))

        if step.get("baseline") or spec.get("baseline"):
            self._db_baseline(step.get("baseline") or spec.get("baseline"), sr)

    def _db_baseline(self, spec: Any, sr: StepResult) -> None:
        if not isinstance(spec, Mapping):
            raise CaseError(f"baseline 需要对象写法: {spec!r}",
                            hint="baseline: {table: users, key: id, where: \"status=1\"}")
        mode = str(spec.get("mode") or self._baseline_mode)
        table = str(spec["table"])
        key = str(spec.get("key") or "id")
        snap = self.db.snapshot(table, key=key, fields=spec.get("fields"),
                                where=spec.get("where"), order_by=spec.get("order_by"))
        bp = self.cfg.root / "out" / "baselines" / f"db_{table}.json"
        import json

        if mode == "update" or not bp.exists():
            bp.parent.mkdir(parents=True, exist_ok=True)
            bp.write_text(json.dumps(snap, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            sr.detail["baseline"] = {"mode": mode, "status": "written", "path": str(bp), "rows": len(snap)}
            return
        baseline = json.loads(bp.read_text(encoding="utf-8"))
        diff = diff_snapshots(baseline, snap)
        self.result.baseline_diff = {"table": table, **diff}
        sr.detail["baseline"] = {"mode": mode, "status": "diff", "path": str(bp),
                                 "has_diff": diff["has_diff"],
                                 "added": diff["added"][:10], "removed": diff["removed"][:10],
                                 "changed": dict(list(diff["changed"].items())[:10])}
        if diff["has_diff"]:
            sr.checks.append(CheckResult(
                False, f"数据快照回归({table})", "deep_eq", "与基线一致",
                {"added": len(diff["added"]), "removed": len(diff["removed"]),
                 "changed": len(diff["changed"])},
                f"表 {table} 数据相对基线发生变化：新增 {len(diff['added'])} 行、"
                f"删除 {len(diff['removed'])} 行、修改 {len(diff['changed'])} 行").to_dict())
            sr.status = FAILED
            sr.error = f"表 {table} 数据快照与基线不一致"
            sr.hint = "若变化是预期的，用 `forgeqa run --baseline update` 刷新基线"

    # ---------------- UI ----------------
    def _do_ui(self, step: Mapping[str, Any], sr: StepResult) -> None:
        if self.ui is None:
            self.ui = UiDriver(
                self.ctx, {**(self.cfg.get("ui") or {}), "base_url": self.cfg.get("base_url", "")},
                artifacts_dir=self.artifacts_dir,
                baseline_dir=self.cfg.root / "out" / "baselines" / "ui",
                logger=self.logger, baseline_mode=self._baseline_mode,
            )
            self.ui.start()
        actions = step["ui"]
        if isinstance(actions, Mapping):
            actions = [actions]
        with _UI_LOCK:   # 浏览器会话串行，避免多线程互抢焦点
            for act_spec in actions:
                action, payload = _parse_ui_action(self.ctx, act_spec)
                try:
                    out = self.ui.act(action, payload)
                    sr.detail.setdefault("ui_actions", []).append({**out, "target": _brief(payload)})
                except (UiError, ForgeQAError):
                    self._capture_ui_failure(sr, action)
                    raise

            spec = step.get("ui_assert") or step.get("assert") or []
            checks = eval_ui_assertions(self.ui, self.ctx.resolve(spec))
            sr.detail["url"] = self.ui.page.url
            sr.checks.extend([c.to_dict() for c in checks])
            failed = [c for c in checks if not c.passed]
            if failed:
                self._capture_ui_failure(sr, "assert")
                sr.status = FAILED
                sr.error = "; ".join(c.message for c in failed[:3])
                sr.hint = "页面结构或文案可能已变更；截图与 DOM 摘要已保存到报告"

    def _capture_ui_failure(self, sr: StepResult, action: str) -> None:
        if self.ui is None or not self.ui.screenshot_on_fail:
            return
        try:
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{self.case.id}_{sr.name}")
            shot = self.ui.screenshot(f"FAIL_{safe}_{action}")
            sr.artifacts.append(str(shot))
            sr.detail["dom_digest"] = self.ui.dom_digest(3000)
            sr.detail["page_url"] = self.ui.page.url
        except Exception:
            pass

    # ---------------- 脚本逃生舱 ----------------
    def _do_script(self, step: Mapping[str, Any], sr: StepResult) -> None:
        spec = step["script"]
        if isinstance(spec, str):
            spec = {"expr": spec}
        if not isinstance(spec, Mapping):
            raise CaseError(f"script 步骤格式错误: {spec!r}")

        if "expr" in spec:
            raw_expr = str(spec["expr"])
            # ${var} 先替换成字面量，再当 Python 表达式求值：
            #   "${ctx.before} + 1"  →  0 + 1  →  1
            text = _EXPR_SUB_RE.sub(
                lambda m: repr(self.ctx.resolve_expr(m.group(1).strip())), raw_expr)
            try:
                value = eval(text, _SAFE_BUILTINS, {})
            except Exception:
                value = self.ctx.resolve(raw_expr)   # 不是表达式就按普通插值处理
            target = str(spec.get("var") or "value")
            self.ctx.set(target, value)
            sr.detail["expr"] = raw_expr
            sr.detail["result"] = _brief(value)
            return

        if "file" in spec:
            path = Path(str(spec["file"]))
            if not path.is_absolute():
                path = self.cfg.root / path
            if not path.exists():
                raise CaseError(
                    f"脚本文件不存在: {path}",
                    hint="脚本目录约定为项目根下的 scripts/，文件内需提供 run(ctx, args) 函数",
                )
            module = _load_module(path)
            fn = getattr(module, "run", None)
            if not callable(fn):
                raise CaseError(f"脚本 {path.name} 缺少 run(ctx, args) 函数",
                                hint="def run(ctx, args): return {'变量名': 值}")
            out = fn(self.ctx, self.ctx.resolve(spec.get("args") or {}))
            if isinstance(out, Mapping):
                self.ctx.set_many(out)
            sr.detail["script"] = str(path.relative_to(self.cfg.root))
            sr.detail["result"] = _brief(out)
            return

        if "import" in spec:
            raise CaseError("script 不支持 import 关键字，请用 file 指定脚本路径")

        if "code" in spec:
            value = eval(str(spec["code"]),
                         {"__builtins__": {}, "len": len, "int": int, "float": float, "str": str},
                         dict(self.ctx.layers.get("ctx", {})))
            self.ctx.set(str(spec.get("var") or "value"), value)
            sr.detail["code"] = str(spec["code"])
            return

        raise CaseError(f"script 步骤必须提供 expr / file / code 之一: {spec!r}")

    # ---------------- 断言汇总 ----------------
    def _collect_checks(self, step: Mapping[str, Any], kind: str) -> list[CheckResult]:
        if kind == "http":
            return eval_http_assertions(self._last_response(step), step.get("assert") or [], ctx=self.ctx)
        if kind == "db":
            return self._db_checks(step)
        if kind == "script":
            return self._value_checks(step)
        if kind == "log":
            return []
        return self._value_checks(step)

    def _last_response(self, step: Mapping[str, Any]):
        """取回本步骤刚发出的响应对象（_do_http 在执行时缓存）。"""
        return self._last_resp if self._last_resp is not None else _NullResponse()

    def _db_checks(self, step: Mapping[str, Any]) -> list[CheckResult]:
        specs = step.get("assert") or []
        rows: list[dict[str, Any]] = self.ctx.get("db_rows", []) or []
        out: list[CheckResult] = []
        for spec in specs:
            if isinstance(spec, str):
                spec = {"rows_count": {"op": "gte", "value": 1}}
            # 断言里也可能写 ${...}（尤其 sql 断言的 params），先整体插值
            spec = self.ctx.resolve(spec)
            label = spec.get("label") or ""
            if "rows_count" in spec:
                v = spec["rows_count"]
                if isinstance(v, Mapping):
                    out.append(check(len(rows), str(v.get("op", "eq")), v.get("value"),
                                     target=label or "结果行数"))
                else:
                    out.append(check(len(rows), "eq", v, target=label or "结果行数"))
            if "row" in spec:
                r = spec["row"]
                if not rows:
                    out.append(CheckResult(False, label or "首行断言", "not_empty", "至少 1 行",
                                           [], "SQL 返回 0 行，无法校验首行字段"))
                else:
                    out.append(check(rows[0].get(r["field"]), str(r.get("op", "eq")),
                                     self.ctx.resolve(r.get("value")),
                                     target=label or f"首行.{r['field']}"))
            if "each_row" in spec:
                r = spec["each_row"]
                if not rows:
                    out.append(CheckResult(False, label or "逐行断言", "not_empty", "至少 1 行", [],
                                           "SQL 返回 0 行"))
                for i, row in enumerate(rows[:200]):
                    out.append(check(row.get(r["field"]), str(r.get("op", "not_null")),
                                     self.ctx.resolve(r.get("value")),
                                     target=label or f"行{i}.{r['field']}"))
            if "scalar" in spec:
                s = spec["scalar"]
                val = next(iter(rows[0].values())) if rows else None
                out.append(check(val, str(s.get("op", "eq")), self.ctx.resolve(s.get("value")),
                                 target=label or "标量结果"))
            if "sql" in spec:
                sql = str(spec["sql"])
                parsed = _parse_sql_params(sql, spec.get("params") or {})
                val = self.db.scalar(sql, parsed)
                out.append(check(val, str(spec.get("op", "eq")), self.ctx.resolve(spec.get("value")),
                                 target=label or "业务不变量 SQL"))
            if "not_empty" in spec:
                out.append(check(rows, "not_empty" if spec["not_empty"] else "empty",
                                 target=label or "结果集"))
            if "table_exists" in spec:
                ok = spec["table_exists"] in self.db.tables()
                out.append(CheckResult(ok, label or f"表 {spec['table_exists']}", "is_true", True, ok,
                                       "" if ok else f"表 {spec['table_exists']} 不存在"))
        return out

    def _value_checks(self, step: Mapping[str, Any]) -> list[CheckResult]:
        out: list[CheckResult] = []
        for spec in step.get("assert") or []:
            if isinstance(spec, str):
                continue
            if "actual" in spec or "expr" in spec:
                actual = self.ctx.resolve(spec.get("actual", spec.get("expr")))
            elif "var" in spec:
                actual = self.ctx.get(str(spec["var"]), None)
            else:
                continue
            out.append(check(actual, str(spec.get("op", "eq")), self.ctx.resolve(spec.get("value")),
                             target=spec.get("label") or str(spec.get("var") or "值")))
        return out

    def _assert_hint(self, failed: CheckResult, kind: str) -> str | None:
        if kind == "http":
            return ("接口返回与预期不符：确认是需求变更（改断言）还是缺陷（提单修复）。"
                    "若为新接口首次跑通，先用 --record 重录基线。")
        if kind == "db":
            return ("数据断言失败：核对造数隔离条件（where 是否只覆盖本用例数据），"
                    "以及被测系统写入逻辑。")
        if kind == "ui":
            return "UI 断言失败：优先怀疑页面结构/文案变更，截图见报告附件。"
        return None

    _baseline_mode = "off"


def _step_kind(step: Mapping[str, Any]) -> str:
    for k in ("http", "db", "ui", "script", "sleep", "log"):
        if k in step:
            return k
    if "loop" in step:
        return "loop"
    if "assert" in step:
        return "assert"      # 纯断言步骤：对前面步骤留在变量池里的值做判断
    raise CaseError(f"步骤缺少动作类型: {step!r}",
                    hint="每个步骤必须包含 http / db / ui / script / sleep / log / assert 之一")


def _step_label(step: Mapping[str, Any]) -> str:
    try:
        kind = _step_kind(step)
    except CaseError:
        return "未命名步骤"
    if kind == "assert":
        return "断言"
    body = step.get(kind)
    if isinstance(body, Mapping):
        return str(body.get("name") or body.get("path") or body.get("url") or body.get("sql", ""))[:60] or kind
    if isinstance(body, list) and body:
        first = body[0]
        if isinstance(first, Mapping):
            return str(next(iter(first.values()), kind))[:60]
    return kind.upper()


def _eval_condition(ctx: Context, cond: Any) -> bool:
    """步骤级 if 条件：支持布尔字面量、${} 插值、以及 Python 表达式。

    ``if: "${env.feature_x}"`` / ``if: "${ctx.count} > 0"`` / ``if: "1 == 2"``
    """
    if isinstance(cond, bool):
        return cond
    text = str(cond)
    subbed = _EXPR_SUB_RE.sub(
        lambda m: repr(ctx.resolve_expr(m.group(1).strip())), text)
    try:
        return bool(eval(subbed, _SAFE_BUILTINS, {}))
    except Exception:
        return _truthy(ctx.resolve(cond))


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "none", "null")
    return bool(v)


def _brief(v: Any, limit: int = 300) -> Any:
    import json

    if isinstance(v, (dict, list)):
        text = json.dumps(v, ensure_ascii=False, default=str)
        return text if len(text) <= limit else text[:limit] + "…"
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + "…"
    return v


def _parse_ui_action(ctx: Context, spec: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(spec, str):
        return "goto", {"url": ctx.resolve(spec)}
    if not isinstance(spec, Mapping):
        raise CaseError(f"UI 动作格式错误: {spec!r}",
                        hint="写法：- {action: click, target: '#submit'}")
    if "action" in spec:
        payload = {k: v for k, v in spec.items() if k != "action"}
        return str(spec["action"]), payload
    if len(spec) == 1:
        action, payload = next(iter(spec.items()))
        return str(action), (payload if isinstance(payload, Mapping) else {"value": payload})
    if "target" in spec:
        for guess in ("click", "fill", "check", "hover"):
            if guess in spec:
                return guess, {k: v for k, v in spec.items() if k != guess}
    raise CaseError(f"无法从 {spec!r} 推断 UI 动作",
                    hint="请显式写 action，例如 {action: fill, target: '#email', value: '${data.u.email}'}")


def _parse_sql_params(sql: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """只传 SQL 里真正引用到的具名参数，避免多余键导致驱动报错。"""
    import re

    used = set(re.findall(r"[:@$]([A-Za-z_]\w*)", sql))
    return {k: v for k, v in (params or {}).items() if k in used}


def _extract_from_rows(rows: Sequence[Mapping[str, Any]], expr: Any, db: Database,
                       params: Mapping[str, Any]) -> Any:
    """SQL 步骤的 extract：支持 ``rows.0.id``、``scalar``、``count`` 或直接子查询 SQL。"""
    if isinstance(expr, Mapping):
        if "sql" in expr:
            return db.scalar(str(expr["sql"]), _parse_sql_params(str(expr["sql"]), expr.get("params") or params))
        if "field" in expr:
            idx = int(expr.get("index", 0))
            return rows[idx].get(str(expr["field"])) if len(rows) > idx else None
        return None
    text = str(expr)
    if text == "count":
        return len(rows)
    if text == "scalar":
        return next(iter(rows[0].values())) if rows else None
    if text in ("rows", "all"):
        return [dict(r) for r in rows]
    parts = text.split(".")
    if parts[0] in ("rows", "row"):
        idx = 0
        rest = parts[1:]
        if rest and rest[0].isdigit():
            idx = int(rest[0])
            rest = rest[1:]
        if idx >= len(rows):
            return None
        val: Any = rows[idx]
        for p in rest:
            val = val.get(p) if isinstance(val, Mapping) else None
        return val
    if rows and isinstance(rows[0], Mapping) and text in rows[0]:
        return rows[0].get(text)
    raise DbError(f"无法解析 db 步骤的 extract 表达式: {expr!r}",
                  hint="支持 count / scalar / rows / rows.0.field / {field: name, index: 0} / {sql: ...}")


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"forgeqa_user_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise CaseError(f"无法加载脚本: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _NullResponse:
    """没执行 http 却写了 assert 时的兜底，给出清晰报错而不是 AttributeError。"""

    status = 0
    elapsed_ms = 0.0
    text = ""
    url = ""
    headers: dict[str, str] = {}

    def json(self, default=None):
        return default

    @property
    def body(self):
        return None


# --------------------------------------------------------------------------- #
# 套件调度
# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, cfg: ForgeConfig, *, logger=None):
        self.cfg = cfg
        self.logger = logger
        self.suite_ctx = cfg.context()
        self.suite_ctx.layers.setdefault("suite", {})

    # ---------------- 引导 / 收尾 ----------------
    def bootstrap(self) -> tuple[dict[str, Any], list[StepResult]]:
        """执行 hooks.bootstrap：建表、造数入库、登录等一次性准备。"""
        info, results = {}, []
        hooks = self.cfg.get("hooks") or {}
        steps = list(hooks.get("bootstrap") or [])
        data_plan = hooks.get("seed") or []

        # 登录引导：一次登录，token 进入 suite 变量池，所有用例复用
        auth_opts = dict(self.cfg.get("auth") or {})
        if str(auth_opts.get("type", "")).lower() == "bearer" and auth_opts.get("login"):
            http_opts = dict(self.cfg.get("http") or {})
            http_opts["auth"] = auth_opts
            client = HttpClient(self.suite_ctx, http_opts, base_url=self.cfg.get("base_url", ""),
                                logger=self.logger)
            try:
                found = client.bootstrap_auth()
                info["auth"] = {k: (v[:8] + "…" if isinstance(v, str) and len(v) > 12 else v)
                                for k, v in found.items()}
                for k, v in found.items():
                    self.suite_ctx.set(k, v, layer="ctx")
            except ForgeQAError as exc:
                results.append(StepResult(name="登录引导", kind="http", status=ERROR,
                                          error=exc.render(), hint=exc.hint))
                return info, results

        # 建表：与是否有 seeding 计划无关，永远是引导的第一步
        ddl = self.cfg.get("db.ddl") or hooks.get("ddl")
        if ddl:
            try:
                with Database.from_opts(self.cfg.get("db") or {}, self.cfg.root) as db:
                    db.script_file(self.cfg.root / str(ddl))
                info["ddl"] = str(ddl)
            except ForgeQAError as exc:
                results.append(StepResult(name="执行建表脚本", kind="db", status=ERROR,
                                          error=exc.render(), hint=exc.hint))
                return info, results

        if data_plan:
            db_opts = self.cfg.get("db") or {}
            with Database.from_opts(db_opts, self.cfg.root) as db:
                factory = DataFactory(
                    self.suite_ctx,
                    schema_dir=(self.cfg.root / str(self.cfg.get("generators.schema_dir", "config/schemas"))),
                )
                seeder = Seeder(db, factory, self.suite_ctx,
                                ledger_path=self.cfg.root / "out" / ".forgeqa_seed_ledger.json")
                counts = seeder.run(data_plan)
                info["seeded"] = counts

        if steps:
            probe = Case(id="_bootstrap", title="引导", steps=steps)
            ex = Executor(probe, self.cfg, suite_ctx=self.suite_ctx, logger=self.logger)
            ex.result.started_at = datetime.now().isoformat(timespec="seconds")
            ex._prepare()
            for step in steps:
                sr = ex._run_step(step)
                results.append(sr)
                if sr.status in (FAILED, ERROR):
                    break
            ex._finish()
            info["bootstrap_vars"] = sorted(
                k for k in self.suite_ctx.layers.get("ctx", {}) if not k.startswith("_"))
        return info, results

    def teardown(self) -> dict[str, int]:
        hooks = self.cfg.get("hooks") or {}
        cleanup: dict[str, int] = {}
        ledger = self.cfg.root / "out" / ".forgeqa_seed_ledger.json"
        if hooks.get("cleanup", True) and ledger.exists():
            try:
                with Database.from_opts(self.cfg.get("db") or {}, self.cfg.root) as db:
                    cleanup = cleanup_from_ledger(db, ledger)
            except Exception as exc:
                if self.logger:
                    self.logger.debug(f"清理失败: {exc}")
        for step in hooks.get("post") or []:
            probe = Case(id="_teardown", title="收尾", steps=[step])
            ex = Executor(probe, self.cfg, suite_ctx=self.suite_ctx, logger=self.logger)
            ex._prepare()
            ex._run_step(step)
            ex._finish()
        return cleanup

    # ---------------- 选择 ----------------
    @staticmethod
    def select(cases: Sequence[Case], *, tags: Sequence[str] = (), exclude_tags: Sequence[str] = (),
               priorities: Sequence[str] = (), ids: Sequence[str] = (), keyword: str = "") -> list[Case]:
        out: list[Case] = []
        for c in cases:
            if tags and not (set(tags) & set(c.tags)):
                continue
            if exclude_tags and (set(exclude_tags) & set(c.tags)):
                continue
            if priorities and c.priority not in {p.upper() for p in priorities}:
                continue
            if ids and c.id not in ids:
                continue
            if keyword and keyword.lower() not in (c.id + c.title + " ".join(c.tags)).lower():
                continue
            out.append(c)
        return sorted(out, key=lambda c: (PRIORITY_ORDER.get(c.priority, 9), c.layer, c.id))

    # ---------------- 主循环 ----------------
    def run(
        self,
        cases: Sequence[Case],
        *,
        jobs: int | None = None,
        repeat: int | None = None,
        retries: int | None = None,
        fail_fast: bool = False,
        baseline: str = "off",
        record_dir: str | Path | None = None,
    ) -> SuiteResult:
        opts = self.cfg.get("runner") or {}
        jobs = int(jobs if jobs is not None else opts.get("jobs", 1))
        repeat = int(repeat if repeat is not None else opts.get("repeat", 1))
        retries = int(retries if retries is not None else opts.get("retries", 0))
        gen = self.cfg.raw.get("generators") or {}

        suite = SuiteResult(env=self.cfg.env_name, base_url=self.cfg.get("base_url", ""),
                            started_at=datetime.now().isoformat(timespec="seconds"),
                            seed=gen.get("seed"),
                            options={"jobs": jobs, "repeat": repeat, "retries": retries,
                                     "baseline": baseline, "fail_fast": fail_fast,
                                     "case_count": len(cases)})
        recorder = None
        if baseline in ("update", "diff"):
            recorder = Recorder(self.cfg.root / "out" / "baselines" / "http.json", mode=baseline)

        t0 = time.perf_counter()
        queue: list[Case] = [c for _ in range(repeat) for c in cases]

        def _one(case: Case) -> CaseResult:
            return self._run_case(case, retries=retries, baseline=baseline, recorder=recorder)

        try:
            if jobs > 1 and len(queue) > 1:
                with ThreadPoolExecutor(max_workers=jobs) as pool:
                    futures = {pool.submit(_one, c): c for c in queue}
                    for fut in as_completed(futures):
                        res = fut.result()
                        suite.cases.append(res)
                        if fail_fast and not res.ok:
                            for f in futures:
                                f.cancel()
                            break
            else:
                for c in queue:
                    res = _one(c)
                    suite.cases.append(res)
                    if fail_fast and not res.ok:
                        break
        finally:
            if recorder:
                if baseline == "diff":
                    suite.options["baseline_diff"] = recorder.diff()
                recorder.commit()

        suite.duration_ms = (time.perf_counter() - t0) * 1000
        suite.cases.sort(key=lambda r: (PRIORITY_ORDER.get(r.priority, 9), r.id))
        return suite

    def _run_case(self, case: Case, *, retries: int, baseline: str, recorder) -> CaseResult:
        attempts = (case.retries if case.retries is not None else retries) + 1
        result: CaseResult | None = None
        for i in range(attempts):
            ex = Executor(case, self.cfg, suite_ctx=self.suite_ctx, recorder=recorder,
                          logger=self.logger)
            ex._baseline_mode = baseline
            result = ex.run()
            result.attempts = i + 1
            if result.status == PASSED:
                if i > 0:
                    # 重试后通过 → 标记 flaky：这类用例必须收敛，否则会掩盖真实缺陷
                    result.flaky = True
                break
            if result.status == ERROR and not _retryable(result):
                break
        assert result is not None
        return result


def _retryable(result: CaseResult) -> bool:
    """只有环境/网络类失败才值得重试；断言失败重试等于掩盖缺陷。"""
    if result.status == ERROR:
        text = (result.error or "").lower()
        return any(k in text for k in ("timeout", "connection", "超时", "无法连接", "5", "locked"))
    return False
