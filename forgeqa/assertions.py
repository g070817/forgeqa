"""forgeqa.assertions — 统一断言算子 + 轻量 JSONPath。

为什么要自己写 JSONPath
----------------------
测试工具的依赖越少越稳。这里实现的是 JSONPath 的**实战子集**，
覆盖接口断言 95% 的场景，且不引入 jsonpath-ng 这类额外依赖：

    $.data.id              取字段
    $.list[0].name         取数组元素（支持 -1 表示最后一个）
    $.list[*].id           展开数组，返回多个值
    $..id                  递归下降，找所有同名 key
    $.list[?(@.age>30)]    过滤（支持 == != > >= < <= ）
    $.a.*                  取对象所有值

断言算子统一给 HTTP / SQL / UI 三层复用，避免每层各写一套比较逻辑。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .config import coerce_scalar
from .errors import AssertFailed

# --------------------------------------------------------------------------- #
# JSONPath 子集
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(
    r"""
    \$                              # 根
  | \.\.([A-Za-z_][\w\-]*)          # ..key 递归下降
  | \.([A-Za-z_][\w\-]*)            # .key
  | \.(\*)                          # .*
  | \[(\*)\]                        # [*]
  | \[(-?\d+)\]                     # [n]
  | \['([^']*)'\]                   # ['key']
  | \[\"([^\"]*)\"\]                # ["key"]
  | \[\?\(([^)]+)\)\]               # [?(...)]
    """,
    re.VERBOSE,
)

_FILTER_RE = re.compile(r"^\s*@\.?([\w\-\.]*)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")


def _walk(node: Any) -> Iterable[Any]:
    """递归下降遍历所有节点。"""
    yield node
    if isinstance(node, Mapping):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _walk(v)


def _apply_filter(items: Sequence[Any], expr: str) -> list[Any]:
    m = _FILTER_RE.match(expr)
    if not m:
        return []
    path, op, raw = m.group(1), m.group(2), m.group(3)
    expected = coerce_scalar(raw.strip().strip("'\""))

    def _val(item: Any) -> Any:
        if not path:
            return item
        cur = item
        for part in path.split("."):
            if isinstance(cur, Mapping):
                cur = cur.get(part)
            else:
                return None
        return cur

    out = []
    for it in items:
        val = _val(it)
        try:
            if op == "==" and val == expected:
                out.append(it)
            elif op == "!=" and val != expected:
                out.append(it)
            elif op in (">", ">=", "<", "<="):
                a, b = _num(val), _num(expected)
                if a is None or b is None:
                    continue
                if (op == ">" and a > b) or (op == ">=" and a >= b) or \
                   (op == "<" and a < b) or (op == "<=" and a <= b):
                    out.append(it)
        except TypeError:
            continue
    return out


def jsonpath(data: Any, path: str) -> list[Any]:
    """返回所有匹配值。语法错误会抛 AssertFailed（提示正确写法）。"""
    if path in ("$", ""):
        return [data]
    if not path.startswith("$"):
        raise AssertFailed(f"JSONPath 必须以 $ 开头: {path!r}",
                           hint="例如 $.data.id、$.list[0].name、$..id")

    nodes: list[Any] = [data]
    pos = 1
    while pos < len(path):
        m = _TOKEN_RE.match(path, pos)
        if not m:
            raise AssertFailed(
                f"JSONPath 语法无法解析: {path!r}（位置 {pos}）",
                hint="支持 $.a.b、$.a[0]、$.a[-1]、$.a[*]、$..a、$.a[?(@.k==1)]",
            )
        pos = m.end()
        rec, dotkey, dotstar, wc, idx, skey, dkey, filt = m.groups()
        out: list[Any] = []

        if rec is not None:
            # _walk 本身已递归到每个子节点，这里只挑含该 key 的对象即可；
            # 再单独处理 list 会把元素重复收集一遍
            for node in nodes:
                for sub in _walk(node):
                    if isinstance(sub, Mapping) and rec in sub:
                        out.append(sub[rec])
        elif dotkey is not None:
            out = [n[dotkey] for n in nodes if isinstance(n, Mapping) and dotkey in n]
        elif skey is not None or dkey is not None:
            key = skey if skey is not None else dkey
            out = [n[key] for n in nodes if isinstance(n, Mapping) and key in n]
        elif dotstar is not None or wc is not None:
            for n in nodes:
                if isinstance(n, Mapping):
                    out.extend(n.values())
                elif isinstance(n, (list, tuple)):
                    out.extend(n)
        elif idx is not None:
            i = int(idx)
            for n in nodes:
                if isinstance(n, (list, tuple)) and -len(n) <= i < len(n):
                    out.append(n[i])
        elif filt is not None:
            for n in nodes:
                if isinstance(n, (list, tuple)):
                    out.extend(_apply_filter(n, filt))
                elif isinstance(n, Mapping):
                    out.extend(_apply_filter([n], filt))
        nodes = out
        if not nodes:
            return []
    return nodes


def jsonpath_first(data: Any, path: str, default: Any = None) -> Any:
    vals = jsonpath(data, path)
    return vals[0] if vals else default


# --------------------------------------------------------------------------- #
# 数值/类型工具
# --------------------------------------------------------------------------- #
def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def loose_eq(actual: Any, expected: Any) -> bool:
    """宽松相等：容忍 "1" vs 1、"1.0" vs 1、大小写差异的 bool。"""
    if actual == expected:
        return True
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.strip() == expected.strip()
    a, b = _num(actual), _num(expected)
    if a is not None and b is not None:
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(actual, bool) or isinstance(expected, bool):
        try:
            return bool(actual) == bool(expected)
        except Exception:
            return False
    return str(actual) == str(expected)


# --------------------------------------------------------------------------- #
# 断言结果与算子
# --------------------------------------------------------------------------- #
@dataclass
class CheckResult:
    passed: bool
    target: str
    op: str
    expected: Any
    actual: Any
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "target": self.target, "op": self.op,
                "expected": _brief(self.expected), "actual": _brief(self.actual),
                "message": self.message}


def _brief(v: Any, limit: int = 300) -> Any:
    if isinstance(v, (dict, list)):
        import json

        text = json.dumps(v, ensure_ascii=False, default=str)
        return text if len(text) <= limit else text[:limit] + "…(截断)"
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + "…(截断)"
    return v


OPS = {
    "eq": "等于", "ne": "不等于", "gt": "大于", "gte": "大于等于",
    "lt": "小于", "lte": "小于等于", "contains": "包含", "not_contains": "不包含",
    "in": "属于", "not_in": "不属于", "regex": "匹配正则",
    "startswith": "以…开头", "endswith": "以…结尾",
    "is_null": "为空", "not_null": "非空", "is_true": "为真", "is_false": "为假",
    "len_eq": "长度等于", "len_gte": "长度>=", "len_lte": "长度<=",
    "between": "介于区间", "empty": "为空集", "not_empty": "非空集",
    "type_is": "类型为", "absent": "不存在", "present": "存在",
    "approx": "近似等于", "deep_eq": "深度相等", "matches_schema": "结构匹配",
}


def check(actual: Any, op: str, expected: Any = None, *, target: str = "value",
          tolerance: float = 0.01) -> CheckResult:
    """执行一次断言。返回 CheckResult；失败时 runner 会据此抛错并记录。"""
    op = (op or "eq").lower()
    if op not in OPS:
        raise AssertFailed(
            f"未知断言算子 {op!r}",
            hint="可用算子: " + ", ".join(sorted(OPS)),
        )

    ok = False
    detail = ""

    if op == "eq":
        ok = loose_eq(actual, expected)
    elif op == "ne":
        ok = not loose_eq(actual, expected)
    elif op in ("gt", "gte", "lt", "lte"):
        a, b = _num(actual), _num(expected)
        if a is None or b is None:
            detail = f"无法数值比较（actual={actual!r}, expected={expected!r}）"
        else:
            ok = {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    elif op == "contains":
        if isinstance(actual, (list, tuple, set)):
            ok = any(loose_eq(i, expected) for i in actual)
        elif isinstance(actual, Mapping):
            ok = str(expected) in actual
        elif actual is None:
            ok = False
        else:
            ok = str(expected) in str(actual)
    elif op == "not_contains":
        inner = check(actual, "contains", expected, target=target)
        ok = not inner.passed
    elif op == "in":
        pool = expected if isinstance(expected, (list, tuple, set)) else [expected]
        ok = any(loose_eq(actual, p) for p in pool)
    elif op == "not_in":
        ok = not check(actual, "in", expected, target=target).passed
    elif op == "regex":
        try:
            ok = re.search(str(expected), "" if actual is None else str(actual)) is not None
        except re.error as exc:
            raise AssertFailed(f"正则表达式非法: {expected!r} → {exc}",
                               hint="检查转义，例如匹配点号要写 \\. ") from exc
    elif op == "startswith":
        ok = str(actual).startswith(str(expected)) if actual is not None else False
    elif op == "endswith":
        ok = str(actual).endswith(str(expected)) if actual is not None else False
    elif op == "is_null":
        ok = actual is None or actual == ""
    elif op == "not_null":
        ok = not (actual is None or actual == "")
    elif op == "is_true":
        ok = actual is True or str(actual).lower() in ("true", "1", "yes")
    elif op == "is_false":
        ok = actual is False or str(actual).lower() in ("false", "0", "no")
    elif op in ("len_eq", "len_gte", "len_lte"):
        try:
            n, e = len(actual), int(expected)
            ok = {"len_eq": n == e, "len_gte": n >= e, "len_lte": n <= e}[op]
        except TypeError:
            detail = f"不支持取长度的类型: {type(actual).__name__}"
    elif op == "between":
        lo, hi = (expected or [None, None])[:2]
        a, x, y = _num(actual), _num(lo), _num(hi)
        ok = None not in (a, x, y) and x <= a <= y
    elif op == "empty":
        ok = actual is None or (hasattr(actual, "__len__") and len(actual) == 0)
    elif op == "not_empty":
        ok = not check(actual, "empty", target=target).passed
    elif op == "type_is":
        want = str(expected)
        mapping = {"str": str, "int": int, "float": float, "bool": bool,
                   "list": list, "dict": dict, "null": type(None), "number": (int, float)}
        t = mapping.get(want)
        if t is None:
            raise AssertFailed(f"type_is 不支持的类型名: {want!r}",
                               hint="可用: " + ", ".join(mapping))
        ok = isinstance(actual, t) and not (want != "bool" and isinstance(actual, bool))
    elif op == "absent":
        ok = actual is None
    elif op == "present":
        ok = actual is not None
    elif op == "approx":
        a, b = _num(actual), _num(expected)
        ok = a is not None and b is not None and math.isclose(a, b, rel_tol=tolerance, abs_tol=tolerance)
    elif op == "deep_eq":
        ok = _deep_eq(actual, expected)
    elif op == "matches_schema":
        errs = validate_schema(actual, expected or {})
        ok = not errs
        detail = "; ".join(errs[:4])

    msg = "" if ok else (detail or f"{target} 期望{OPS[op]} {_brief(expected)!r}，实际为 {_brief(actual)!r}")
    return CheckResult(passed=ok, target=target, op=op, expected=expected,
                       actual=actual, message=msg)


def _deep_eq(a: Any, b: Any) -> bool:
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return set(a) == set(b) and all(_deep_eq(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_deep_eq(x, y) for x, y in zip(a, b))
    return loose_eq(a, b)


# --------------------------------------------------------------------------- #
# 轻量结构校验（接口契约回归的核心）
# --------------------------------------------------------------------------- #
TYPE_MAP = {
    "string": str, "str": str, "integer": int, "int": int, "number": (int, float),
    "boolean": bool, "bool": bool, "object": dict, "dict": dict,
    "array": list, "list": list, "null": type(None), "any": object,
}


def validate_schema(data: Any, spec: Mapping[str, Any], *, path: str = "$") -> list[str]:
    """按简化 schema 校验结构，返回错误列表（空 = 通过）。

    spec 写法::

        type: object
        required: [id, name]
        properties:
          id: {type: integer}
          name: {type: string, min_len: 1}
          tags: {type: array, items: {type: string}}
    """
    errs: list[str] = []
    if not isinstance(spec, Mapping) or not spec:
        return errs

    want = spec.get("type")
    if want:
        t = TYPE_MAP.get(str(want).lower())
        if t is not None and not isinstance(data, t):
            errs.append(f"{path} 类型应为 {want}，实际 {type(data).__name__}")
            return errs

    if isinstance(data, Mapping):
        for key in spec.get("required") or []:
            if key not in data:
                errs.append(f"{path}.{key} 缺失（required）")
        for key, sub in (spec.get("properties") or {}).items():
            if key in data:
                errs.extend(validate_schema(data[key], sub, path=f"{path}.{key}"))
        if spec.get("additional_properties") is False and spec.get("properties"):
            extra = set(data) - set(spec["properties"])
            if extra:
                errs.append(f"{path} 出现未声明字段: {sorted(extra)}")
    elif isinstance(data, (list, tuple)):
        if spec.get("min_items") is not None and len(data) < int(spec["min_items"]):
            errs.append(f"{path} 元素数 {len(data)} < min_items {spec['min_items']}")
        if spec.get("max_items") is not None and len(data) > int(spec["max_items"]):
            errs.append(f"{path} 元素数 {len(data)} > max_items {spec['max_items']}")
        items = spec.get("items")
        if items:
            for i, it in enumerate(data[:20]):
                errs.extend(validate_schema(it, items, path=f"{path}[{i}]"))
    elif isinstance(data, str):
        if spec.get("min_len") is not None and len(data) < int(spec["min_len"]):
            errs.append(f"{path} 长度 {len(data)} < min_len {spec['min_len']}")
        if spec.get("max_len") is not None and len(data) > int(spec["max_len"]):
            errs.append(f"{path} 长度 {len(data)} > max_len {spec['max_len']}")
        if spec.get("pattern") and not re.search(str(spec["pattern"]), data):
            errs.append(f"{path} 不匹配 pattern {spec['pattern']!r}")
    elif isinstance(data, (int, float)) and not isinstance(data, bool):
        if spec.get("min") is not None and data < spec["min"]:
            errs.append(f"{path} 值 {data} < min {spec['min']}")
        if spec.get("max") is not None and data > spec["max"]:
            errs.append(f"{path} 值 {data} > max {spec['max']}")
    elif data is None and spec.get("nullable") is False:
        errs.append(f"{path} 不应为 null")
    return errs


def expect_schema(data: Any, spec: Mapping[str, Any], *, target: str = "response") -> CheckResult:
    errs = validate_schema(data, spec)
    return CheckResult(passed=not errs, target=target, op="matches_schema",
                       expected=spec, actual=data, message="; ".join(errs))
