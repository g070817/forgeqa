"""forgeqa.config — 多环境配置 + 变量插值 + 运行时变量池。

设计要点
--------
1. **一切皆配置**：换一个被测站点 = 换一份 ``envs`` 配置，不需要改代码。
2. **``defaults`` 合并**：所有环境共享的配置写在 ``defaults``，各环境只写差异项。
3. **``${}`` 迷你模板语言**：用例 YAML 里任何位置都能插值，支持函数、
   默认值、以及「整串就是一个表达式时保留原始类型」。

插值语法速查::

    ${env.base_url}                 配置/上下文取值（点号路径）
    ${ctx.token}                    运行时提取到的变量
    ${data.user.email}              数据工厂生成的字段
    ${cfg.generators.seed}          原始配置树
    ${uniq}  ${uniq:6}              用例级唯一标记（跨用例数据隔离用）
    ${os:HOME} / ${os:FOO:-默认}   环境变量（带默认值）
    ${now} ${now:%Y%m%d} ${today}   时间
    ${ts} ${uuid}                   时间戳 / UUID4
    ${faker:name} ${faker:random_int:1:100}
    ${randint:1:100} ${choice:a|b|c}
    ${seq:order_no:1000:1}          自增序列（name:起始:步长）
    ${md5:abc} ${b64:abc} ${upper:abc} ${lower:abc}
"""
from __future__ import annotations

import base64
import copy
import hashlib
import itertools
import os
import time
import random
import re
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping

import yaml

from .errors import ConfigError


class _Missing:
    """哨兵值：区分「取到 None」和「没取到」。"""

    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<MISSING>"


MISSING = _Missing()

_EXPR_RE = re.compile(r"\$\{([^{}]+)\}")

# 用例级唯一标记：每个 Context 一个，用于跨用例数据隔离。
# 不受 seed 影响 —— 它的作用就是「每次运行都不撞车」，而不是可复现。
_UNIQ_COUNTER = itertools.count(1)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def deep_get(obj: Any, path: str, default: Any = MISSING) -> Any:
    """点号路径取值，支持 ``a.b.0.c``。"""
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        elif isinstance(cur, (list, tuple)) and part.lstrip("-").isdigit():
            idx = int(part)
            if -len(cur) <= idx < len(cur):
                cur = cur[idx]
            else:
                return default
        else:
            return default
    return cur


def deep_set(obj: MutableMapping[str, Any], path: str, value: Any) -> None:
    """点号路径写入，中间层不存在则自动创建 dict。"""
    parts = str(path).split(".")
    cur: MutableMapping[str, Any] = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, MutableMapping):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def deep_merge(base: Any, override: Any) -> Any:
    """递归合并；override 中的 None 视为「显式清空」以外的普通值。"""
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        out = dict(base)
        for k, v in override.items():
            out[k] = deep_merge(out[k], v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(override)


def coerce_scalar(text: str) -> Any:
    """把字符串还原成 bool / int / float / None，否则原样返回。"""
    s = text.strip()
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return text


# --------------------------------------------------------------------------- #
# 运行时变量池
# --------------------------------------------------------------------------- #
class Context:
    """分层变量池 + 模板插值引擎。

    层（layer）按优先级从高到低搜索：
    ``ctx`` > ``data`` > ``case`` > ``env`` > ``cfg`` > ``os``
    首段若是已知层名，则直接在该层内下钻。
    """

    SEARCH_ORDER = ("ctx", "data", "case", "env", "cfg")

    def __init__(
        self,
        layers: Mapping[str, Any] | None = None,
        *,
        faker_locale: str = "zh_CN",
        seed: int | None = None,
        base_dir: Path | str | None = None,
    ):
        self.layers: dict[str, Any] = {
            "ctx": {},
            "data": {},
            "case": {},
            "env": {},
            "cfg": {},
            "os": dict(os.environ),
        }
        for key, val in (layers or {}).items():
            self.layers[key] = val

        self.faker_locale = faker_locale
        self.seed = seed
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self._rng = random.Random(seed)
        self._in_progress: list[str] = []   # 循环引用检测栈
        self._faker = None
        self._counters: dict[str, int] = {}
        # 用例级唯一标记：同一 Context 内恒定，不同用例不同，用于数据隔离
        # 时间分量（毫秒）保证跨运行唯一，计数器保证同进程内唯一
        self.uniq = (f"{int(time.time() * 1000) % 10 ** 7:07d}"
                     f"{next(_UNIQ_COUNTER):03d}")
        self.trace: list[str] = []  # 记录动态函数求值，便于报告里回溯造数来源

    # ---------------- 基础读写 ----------------
    def set(self, path: str, value: Any, layer: str = "ctx") -> None:
        self.layers.setdefault(layer, {})
        deep_set(self.layers[layer], path, value)

    def set_many(self, mapping: Mapping[str, Any], layer: str = "ctx") -> None:
        for k, v in mapping.items():
            self.set(k, v, layer)

    def get(self, path: str, default: Any = MISSING) -> Any:
        parts = str(path).split(".")
        head = parts[0]
        if head in self.layers:
            val = deep_get(self.layers[head], ".".join(parts[1:]), MISSING) if len(parts) > 1 else self.layers[head]
            if val is not MISSING:
                return val
        for layer in self.SEARCH_ORDER:
            val = deep_get(self.layers.get(layer, {}), path, MISSING)
            if val is not MISSING:
                return val
        return default

    def snapshot(self) -> dict[str, Any]:
        """可序列化的变量快照（用于报告与基线），跳过环境变量层。"""
        return {k: copy.deepcopy(v) for k, v in self.layers.items() if k != "os"}

    # ---------------- 动态函数 ----------------
    @property
    def faker(self):
        if self._faker is None:
            try:
                from faker import Faker
            except ImportError as exc:  # pragma: no cover
                raise ConfigError(
                    "使用了 ${faker:...} 但未安装 Faker",
                    hint="pip install faker  （或改用 ${randint:1:100} 等内置函数）",
                ) from exc
            self._faker = Faker(self.faker_locale)
            if self.seed is not None:
                self._faker.seed_instance(self.seed)
        return self._faker

    def _eval_function(self, name: str, args: list[str], default: Any = MISSING) -> Any:
        raw = args[0] if args else ""
        if name == "now":
            return datetime.now().strftime(raw) if raw else datetime.now().isoformat(timespec="seconds")
        if name == "today":
            return datetime.now().strftime(raw or "%Y-%m-%d")
        if name == "ts":
            offset = int(raw or 0)
            return int(datetime.now().timestamp()) + offset
        if name == "uuid":
            return str(_uuid.uuid4())
        if name == "uniq":
            return self.uniq[: int(raw)] if raw and raw.isdigit() else self.uniq
        if name == "os":
            key = raw
            fallback = args[1] if len(args) > 1 else (default if default is not MISSING else MISSING)
            val = os.environ.get(key)
            if val is None and fallback is MISSING:
                raise ConfigError(
                    f"环境变量 {key!r} 未设置且未提供默认值",
                    hint=f"用 ${{os:{key}:-你的默认值}} 提供默认值，或先 export {key}=...",
                )
            return val if val is not None else fallback
        if name == "randint":
            lo = int(args[0]) if args else 0
            hi = int(args[1]) if len(args) > 1 else 100
            return self._rng.randint(lo, hi)
        if name == "randfloat":
            lo = float(args[0]) if args else 0.0
            hi = float(args[1]) if len(args) > 1 else 1.0
            prec = int(args[2]) if len(args) > 2 else 2
            return round(self._rng.uniform(lo, hi), prec)
        if name == "choice":
            pool = [a for a in raw.split("|") if a != ""]
            return coerce_scalar(self._rng.choice(pool)) if pool else None
        if name == "seq":
            key = args[0] if args else "default"
            start = int(args[1]) if len(args) > 1 else 1
            step = int(args[2]) if len(args) > 2 else 1
            cur = self._counters.get(key, start - step) + step
            self._counters[key] = cur
            return cur
        if name == "md5":
            return hashlib.md5(raw.encode()).hexdigest()
        if name == "sha1":
            return hashlib.sha1(raw.encode()).hexdigest()
        if name == "b64":
            return base64.b64encode(raw.encode()).decode()
        if name == "upper":
            return raw.upper()
        if name == "lower":
            return raw.lower()
        if name == "faker":
            method = args[0] if args else "name"
            fn = getattr(self.faker, method, None)
            if fn is None:
                raise ConfigError(
                    f"Faker 没有 {method!r} 这个 provider",
                    hint="常见可用：name / email / phone_number / company / address / "
                    "random_int / date_time / user_name / ssn / postcode",
                )
            call_args = [coerce_scalar(a) for a in args[1:]]
            self.trace.append(f"faker.{method}({','.join(map(str, call_args))})")
            return fn(*call_args)
        return MISSING

    _FUNCS = {
        "now", "today", "ts", "uuid", "os", "randint", "randfloat",
        "choice", "seq", "md5", "sha1", "b64", "upper", "lower", "faker",
        "uniq",
    }

    def resolve_expr(self, expr: str) -> Any:
        """按表达式语义求值（区别于 resolve：不会把裸字符串原样返回）。

        ``resolve("ctx.token")`` → 字符串 "ctx.token"
        ``resolve_expr("ctx.token")`` → 变量池里 token 的真实值
        """
        return self._resolve_expr(str(expr).strip())

    def _resolve_expr(self, expr: str) -> Any:
        expr = expr.strip()
        # 循环引用检测：a → b → a 会无限递归，必须在这里截断并给出可读报错
        if expr in self._in_progress:
            chain = " → ".join([*self._in_progress, expr])
            raise ConfigError(
                f"变量循环引用: {chain}",
                hint="检查是否 a 引用了 b、b 又引用回 a；改成静态值或拆开定义",
            )
        self._in_progress.append(expr)
        try:
            return self._resolve_expr_inner(expr)
        finally:
            self._in_progress.pop()

    def _resolve_expr_inner(self, expr: str) -> Any:
        expr = expr.strip()
        # 默认值语法： ${os:FOO:-bar} / ${env.not_exist:-http://x}
        default = MISSING
        if ":-" in expr:
            expr, _, dflt = expr.partition(":-")
            default = coerce_scalar(dflt)
            expr = expr.strip()

        head, sep, rest = expr.partition(":")
        if sep and head in self._FUNCS:
            args = rest.split(":") if rest else []
            val = self._eval_function(head, args, default)
            if val is MISSING:
                val = default
            return val

        if head in self._FUNCS and not sep:
            val = self._eval_function(head, [], default)
            if val is not MISSING:
                return val

        val = self.get(expr, MISSING)
        if val is MISSING:
            if default is not MISSING:
                return default
            raise ConfigError(
                f"变量 {expr!r} 未定义",
                hint="检查拼写；造数变量来自数据工厂，运行时变量来自 extract 或 setup 步骤",
            )
        return val

    # ---------------- 递归插值 ----------------
    def resolve(self, node: Any, _depth: int = 0) -> Any:
        if _depth > 12:
            raise ConfigError("变量插值递归过深（超过 12 层），可能存在循环引用")
        if isinstance(node, str):
            return self._resolve_str(node)
        if isinstance(node, Mapping):
            return {k: self.resolve(v, _depth + 1) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [self.resolve(v, _depth + 1) for v in node]
        return node

    def _resolve_str(self, text: str, _depth: int = 0) -> Any:
        # 递归发生在函数自身（解析出的值可能还是个模板），所以在这里按深度截断
        if _depth > 12:
            raise ConfigError(
                "变量解析递归过深，疑似循环引用",
                hint="检查 ${} 引用链，例如 a 引用了 b、b 又引用回 a",
            )
        matches = list(_EXPR_RE.finditer(text))
        if not matches:
            return text
        # 整串就是一个表达式 → 保留原始类型（int / dict / list / bool）
        if len(matches) == 1 and matches[0].group(0) == text:
            val = self._resolve_expr(matches[0].group(1))
            if isinstance(val, str):
                return self._resolve_str(val, _depth + 1) if _EXPR_RE.search(val) else val
            return val

        def _sub(m: re.Match) -> str:
            val = self._resolve_expr(m.group(1))
            if val is None:
                return ""
            if isinstance(val, (dict, list)):
                import json

                return json.dumps(val, ensure_ascii=False)
            return str(val)

        out = _EXPR_RE.sub(_sub, text)
        return self._resolve_str(out, _depth + 1) if _EXPR_RE.search(out) else out


# --------------------------------------------------------------------------- #
# 配置对象
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: dict[str, Any] = {
    "default_env": "local",
    "defaults": {
        "http": {"timeout": 15, "retries": 2, "backoff": 0.4, "verify_ssl": True,
                 "headers": {"User-Agent": "forgeqa/1.0", "Accept": "application/json"}},
        "ui": {"browser": "chromium", "headless": True, "timeout": 15000,
               "viewport": {"width": 1440, "height": 900}, "screenshot_on_fail": True},
        "db": {"driver": "sqlite", "path": "./out/forgeqa.db"},
        "auth": {"type": "none"},
    },
    "generators": {"locale": "zh_CN", "seed": None, "out_dir": "./out/data"},
    "runner": {"retries": 0, "repeat": 1, "jobs": 1, "fail_fast": False,
               "stop_on_flaky": False, "flaky_threshold": 0.01},
    "report": {"out_dir": "./out/reports", "open": False, "junit": True},
    "envs": {"local": {"base_url": "http://127.0.0.1:8000"}},
}


@dataclass
class ForgeConfig:
    """加载后的配置。``env`` 是 defaults 与环境覆盖合并后的结果。"""

    raw: dict[str, Any]
    env_name: str
    env: dict[str, Any]
    root: Path = field(default_factory=Path.cwd)
    source: Path | None = None
    overrides: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    def effective(self) -> dict[str, Any]:
        """三层合并后的完整视图：``raw`` ← ``env`` ← ``overrides``。

        必须做成一个「合并后的整体」而不是在 ``get()`` 里取第一个命中的层：
        否则局部覆盖会整段替换掉整个配置段。例如 ``--set db.path=X`` 会让
        ``get("db")`` 只返回 ``{"path": X}``，把同段的 ``driver`` 一起弄丢，
        数据库连接就被静默禁用了。

        合并结果按 ``raw`` → ``env`` → ``overrides`` 的顺序逐层递归叠加，
        低优先级在前、高优先级覆盖，dict 递归合并、标量直接覆盖。
        """
        if self._cache is None:
            merged = copy.deepcopy(self.raw)
            merged = deep_merge(merged, self.env)
            merged = deep_merge(merged, self.overrides)
            self._cache = merged
        return self._cache

    # ---------------- 加载 ----------------
    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        env: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        root: str | Path | None = None,
    ) -> "ForgeConfig":
        root_path = Path(root) if root else Path.cwd()
        raw = copy.deepcopy(DEFAULT_CONFIG)
        src: Path | None = None

        if path:
            src = Path(path)
            if not src.is_absolute():
                src = root_path / src
            if not src.exists():
                raise ConfigError(
                    f"配置文件不存在: {src}",
                    hint="运行 `forgeqa init` 生成一份脚手架配置",
                )
            loaded = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
            raw = deep_merge(raw, loaded)

        if overrides:
            raw = deep_merge(raw, dict(overrides))

        env_name = env or os.environ.get("FORGEQA_ENV") or raw.get("default_env") or "local"
        envs = raw.get("envs") or {}
        if env_name not in envs:
            raise ConfigError(
                f"环境 {env_name!r} 未定义，可用环境: {sorted(envs)}",
                hint="在配置文件的 envs 下补充该环境，或用 --env 指定已有环境",
            )
        merged_env = deep_merge(raw.get("defaults") or {}, envs[env_name] or {})

        cfg = cls(raw=raw, env_name=env_name, env=merged_env, root=root_path,
                  source=src, overrides=dict(overrides or {}))
        cfg._apply_env_vars()
        return cfg

    def _apply_env_vars(self) -> None:
        """``FORGEQA_BASE_URL`` / ``FORGEQA_TARGET`` 之类的环境变量覆盖，便于 CI。"""
        mapping = {
            "FORGEQA_BASE_URL": "base_url",
            "FORGEQA_DB_PATH": "db.path",
            "FORGEQA_DB_DSN": "db.dsn",
            "FORGEQA_DB_DRIVER": "db.driver",
            "FORGEQA_UI_HEADLESS": "ui.headless",
            "FORGEQA_UI_BROWSER": "ui.browser",
        }
        for env_key, cfg_path in mapping.items():
            if env_key in os.environ:
                deep_set(self.env, cfg_path, coerce_scalar(os.environ[env_key]))
        # env 被就地修改，合并视图必须重建
        self._cache = None

    # ---------------- 取值 ----------------
    def get(self, path: str, default: Any = MISSING) -> Any:
        """按 ``overrides`` > 环境配置 > 原始配置 的优先级取值。

        读的是 ``effective()`` 的三层合并视图，所以取配置段（值是 dict）时
        拿到的是各层合并后的完整段，而不是某一层的残缺子树。
        """
        return deep_get(self.effective(), path, default)

    @property
    def base_url(self) -> str:
        url = str(self.get("base_url", "")).rstrip("/")
        if not url:
            raise ConfigError(
                f"环境 {self.env_name!r} 未配置 base_url",
                hint="在 env.yaml 的 envs.<环境>.base_url 里填写被测站点根地址",
            )
        return url

    def path(self, key: str, default: str | None = None) -> Path:
        """把配置里的相对路径解析成相对项目根目录的绝对路径。"""
        raw = self.get(key, default)
        if raw is None:
            raise ConfigError(f"配置项 {key!r} 未设置")
        p = Path(str(raw)).expanduser()
        return p if p.is_absolute() else (self.root / p)

    # ---------------- 上下文档 ----------------
    def context(self) -> Context:
        gen = self.raw.get("generators") or {}
        seed = gen.get("seed")
        ctx = Context(
            layers={"env": self.env, "cfg": self.raw},
            faker_locale=gen.get("locale", "zh_CN"),
            seed=int(seed) if seed is not None else None,
            base_dir=self.root,
        )
        ctx.set("env_name", self.env_name)
        return ctx

    def describe(self) -> str:
        return (
            f"env={self.env_name} base_url={self.get('base_url', '(未设置)')} "
            f"db={self.get('db.driver')} source={self.source or '(内置默认)'}"
        )
