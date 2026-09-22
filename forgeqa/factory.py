"""forgeqa.factory — Schema 驱动的造数工厂。

能力
----
1. **声明式实体**：YAML 描述字段 → 生成任意条记录（Faker + 内置生成器 + 派生字段）。
2. **可复现**：固定 seed，同一份 schema 每次产出完全一致的数据文件。
3. **脱敏**：手机号/身份证一律用明显虚构的格式化值，不出现任何真实个人信息。
4. **变异造数**：自动产出「边界 / 异常 / 极端 / 恶意」四类变异数据集，
   直接喂给接口回归，不需要手写脏数据。
5. **反推 schema**：``infer_schema`` 从真实接口响应样本自动推导字段定义，
   这是「适配任意网站」的第一块拼图。

Schema 示例::

    entity: user
    count: 5
    unique: [email, phone]
    fields:
      - {name: id,        gen: seq, start: 1001}
      - {name: name,      gen: faker, method: name}
      - {name: email,     gen: faker, method: email}
      - {name: phone,     gen: fake_phone}
      - {name: age,       gen: int, min: 18, max: 65}
      - {name: role,      gen: choice, values: [user, admin], weights: [9, 1]}
      - {name: vip,       gen: expr, value: "${age} >= 30"}
      - {name: order_no,  gen: pattern, pattern: "QA-????-####"}
      - {name: created_at,gen: datetime, start: "-30d", end: now, fmt: "%Y-%m-%d %H:%M:%S"}
      - {name: dept_id,   gen: ref, entity: dept, field: id}
"""
from __future__ import annotations

import copy
import json
import re
import string
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

from .config import MISSING, Context, coerce_scalar
from .errors import DataError

# --------------------------------------------------------------------------- #
# 脱敏常量：所有生成值都是明显虚构的，禁止出现真实个人信息
# --------------------------------------------------------------------------- #
FAKE_PHONE_PREFIX = "1380000"          # 1380000xxxx → 共 11 位，明显虚构
FAKE_ID_PREFIX = "11010119900101"      # 1990-01-01 北京东城，校验位故意不合法
FAKE_BANK_CARD = "6222020000000000"
FAKE_COMPANY_SUFFIX = ["测试科技有限公司", "样例数据有限公司", "造数实验有限公司"]

# 恶意/异常字符串池（固定，不随机 —— 保证可复现）
NASTY_STRINGS: list[tuple[str, str]] = [
    ("empty", ""),
    ("blank", " "),
    ("null_literal", "null"),
    ("undefined_literal", "undefined"),
    ("type_confusion", "true"),
    ("xss", "<script>alert(1)</script>"),
    ("sqli", "'; DROP TABLE users;--"),
    ("sqli_union", "1' UNION SELECT NULL,NULL--"),
    ("path_traversal", "../../etc/passwd"),
    ("null_byte", "%00"),
    ("crlf", "a\r\nb"),
    ("template_inject", "{{7*7}}"),
    ("emoji_bomb", "😀🎉🚀" * 20),
    ("cjk_long", "中文字符串测试" * 30),
    ("overlong", "a" * 4096),
    ("overlong_huge", "A" * 65535),
]

_RANGE_RE = re.compile(r"^(-?\d+)([smhdwMy])$")


def parse_time_bound(spec: Any, *, now: datetime | None = None) -> datetime:
    """支持 ``now`` / ``-30d`` / ``2026-01-01`` / ``2026-01-01 08:00:00``。"""
    now = now or datetime.now()
    if isinstance(spec, datetime):
        return spec
    if spec is None or str(spec).strip().lower() in ("now", ""):
        return now
    text = str(spec).strip()
    low = text.lower()
    if low in ("now", "today"):
        return now
    if low == "yesterday":
        return now - timedelta(days=1)
    m = _RANGE_RE.match(low)
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        delta = {
            "s": timedelta(seconds=amount), "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount), "d": timedelta(days=amount),
            "w": timedelta(weeks=amount), "M": timedelta(days=30 * amount),
            "y": timedelta(days=365 * amount),
        }[unit]
        return now + delta
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise DataError(f"无法解析时间边界 {spec!r}",
                    hint="支持 now / yesterday / -30d / -2h / 2026-01-01 三种写法")


# --------------------------------------------------------------------------- #
# 转换器
# --------------------------------------------------------------------------- #
class _Seq:
    """按字段名独立计数的序列发生器（跨批次保持连续）。"""

    def __init__(self, start: int = 1, step: int = 1, width: int = 0, prefix: str = ""):
        self.start, self.step, self.width, self.prefix = start, step, width, prefix
        self._cur = start - step

    def next(self) -> str | int:
        self._cur += self.step
        if self.width:
            return f"{self.prefix}{self._cur:0{self.width}d}"
        return f"{self.prefix}{self._cur}" if self.prefix else self._cur


class Transforms:
    """字段级后处理。

    写法：``transform: "name"``、``transform: "truncate:8"``、
    ``transform: "suffix:@example.com"``，多个转换用 ``|`` 串联：``"strip|lower"``。
    """

    def __init__(self):
        self._seq_phone = _Seq(start=1, width=4)
        self._seq_id = _Seq(start=1, width=4)
        self._seq_card = _Seq(start=1, width=8)

    def apply(self, spec: Any, value: Any) -> Any:
        """应用一条或一组转换。

        ``spec`` 可以是字符串（``"truncate:8"``）、用 ``|`` 串联的字符串
        （``"strip|lower"``）、列表、或 ``{md5: null}`` 形式的映射。
        """
        if not spec:
            return value
        if isinstance(spec, (list, tuple)):
            for s in spec:
                value = self.apply(s, value)
            return value
        if isinstance(spec, Mapping):
            for name, arg in spec.items():
                value = self._one(str(name), value, *([str(arg)] if arg is not None else []))
            return value
        for piece in str(spec).split("|"):
            value = self._one(piece, value)
        return value

    def _one(self, piece: str, value: Any, *extra: str) -> Any:
        piece = piece.strip()
        if not piece:
            return value
        name, _, argtext = piece.partition(":")
        args = argtext.split(":") if argtext else []
        args.extend(extra)
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            raise DataError(
                f"未知 transform: {name!r}",
                hint="可用: fake_phone fake_id_card fake_bank_card fake_company lower upper "
                     "strip title truncate:N prefix:X suffix:X mask:H:T md5 sha1 b64 "
                     "to_str to_int to_float default_if_blank:X json dump",
            )
        return fn(value, *args)

    # --- 脱敏类（全部明显虚构） ---
    def t_fake_phone(self, _v: Any, *_a: str) -> str:
        return f"{FAKE_PHONE_PREFIX}{self._seq_phone.next()}"

    def t_fake_id_card(self, _v: Any, *_a: str) -> str:
        return f"{FAKE_ID_PREFIX}{self._seq_id.next()}"

    def t_fake_bank_card(self, _v: Any, *_a: str) -> str:
        return f"{FAKE_BANK_CARD[:8]}{self._seq_card.next()}"

    def t_fake_company(self, v: Any, *_a: str) -> str:
        idx = len(str(v)) % len(FAKE_COMPANY_SUFFIX)
        return f"造数{idx + 1}号{FAKE_COMPANY_SUFFIX[idx]}"

    # --- 字符串类 ---
    def t_lower(self, v: Any, *_a: str) -> str:
        return str(v).lower()

    def t_upper(self, v: Any, *_a: str) -> str:
        return str(v).upper()

    def t_strip(self, v: Any, *_a: str) -> str:
        return str(v).strip()

    def t_title(self, v: Any, *_a: str) -> str:
        return str(v).title()

    def t_truncate(self, v: Any, n: str = "8", *_a: str) -> str:
        return str(v)[: int(n)]

    def t_prefix(self, v: Any, p: str = "", *_a: str) -> str:
        return f"{p}{v}"

    def t_suffix(self, v: Any, s: str = "", *_a: str) -> str:
        return f"{v}{s}"

    def t_mask(self, v: Any, keep_head: str = "3", keep_tail: str = "2", *_a: str) -> str:
        s = str(v)
        h, t = int(keep_head), int(keep_tail)
        if len(s) <= h + t:
            return s
        return f"{s[:h]}{'*' * (len(s) - h - t)}{s[-t:]}"

    # --- 编码/哈希类 ---
    def t_md5(self, v: Any, *_a: str) -> str:
        import hashlib

        return hashlib.md5(str(v).encode()).hexdigest()

    def t_sha1(self, v: Any, *_a: str) -> str:
        import hashlib

        return hashlib.sha1(str(v).encode()).hexdigest()

    def t_b64(self, v: Any, *_a: str) -> str:
        import base64

        return base64.b64encode(str(v).encode()).decode()

    # --- 类型类 ---
    def t_to_str(self, v: Any, *_a: str) -> str:
        return "" if v is None else str(v)

    def t_to_int(self, v: Any, *_a: str) -> int:
        return int(float(v))

    def t_to_float(self, v: Any, *_a: str) -> float:
        return float(v)

    def t_default_if_blank(self, v: Any, d: str = "", *_a: str) -> Any:
        return d if v is None or str(v).strip() == "" else v

    def t_json(self, v: Any, *_a: str) -> str:
        return json.dumps(v, ensure_ascii=False)

    def t_dump(self, v: Any, *_a: str) -> str:
        return str(v)


# --------------------------------------------------------------------------- #
# Schema 模型
# --------------------------------------------------------------------------- #
@dataclass
class Field:
    name: str
    gen: str = "faker"
    raw: dict[str, Any] = dc_field(default_factory=dict)
    transform: str | None = None
    nullable: bool = False

    @classmethod
    def of(cls, spec: Mapping[str, Any]) -> "Field":
        if "name" not in spec:
            raise DataError(f"字段定义缺少 name: {spec}")
        return cls(
            name=str(spec["name"]),
            gen=str(spec.get("gen", "faker")),
            raw=dict(spec),
            transform=spec.get("transform"),
            nullable=bool(spec.get("nullable", False)),
        )


@dataclass
class Schema:
    entity: str
    fields: list[Field]
    count: int = 1
    unique: list[str] = dc_field(default_factory=list)
    source: Path | None = None
    description: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source: Path | None = None) -> "Schema":
        if "entity" not in data:
            raise DataError(f"schema 缺少 entity 字段: {source or '(内存)'}")
        raw_fields = data.get("fields") or []
        if not raw_fields:
            raise DataError(f"schema {data['entity']!r} 没有任何字段定义")
        return cls(
            entity=str(data["entity"]),
            fields=[Field.of(f) for f in raw_fields],
            count=int(data.get("count", 1)),
            unique=[str(u) for u in (data.get("unique") or [])],
            source=source,
            description=str(data.get("description", "")),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Schema":
        p = Path(path)
        if not p.exists():
            raise DataError(f"schema 文件不存在: {p}")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls.from_dict(data, source=p)


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
class DataFactory:
    """按 Schema 生成记录 / 变异数据集。"""

    def __init__(self, ctx: Context, *, schema_dir: str | Path | None = None):
        self.ctx = ctx
        self.schema_dir = Path(schema_dir) if schema_dir else None
        self.transforms = Transforms()
        self._seqs: dict[str, _Seq] = {}
        self._cache: dict[str, list[dict[str, Any]]] = {}   # entity -> rows
        self._rng = ctx._rng

    # ---------------- 内部生成器 ----------------
    def _seq_for(self, f: Field) -> _Seq:
        if f.name not in self._seqs:
            self._seqs[f.name] = _Seq(
                start=int(f.raw.get("start", 1)),
                step=int(f.raw.get("step", 1)),
                width=int(f.raw.get("width", 0)),
                prefix=str(f.raw.get("prefix", "")),
            )
        return self._seqs[f.name]

    def _gen_value(self, f: Field, row: Mapping[str, Any]) -> Any:
        g = f.gen
        # 字段定义里的字符串值先做 ${} 插值，这样才能写出
        # 「每次运行都不撞车」的字段，例如 suffix:${uniq}@example.com
        raw = {k: (self.ctx.resolve(v) if isinstance(v, str) and "${" in v else v)
               for k, v in f.raw.items()} if g != "expr" else f.raw

        if g in ("skip", "none"):
            return MISSING

        if g == "const":
            return copy.deepcopy(raw.get("value"))
        if g == "seq":
            return self._seq_for(f).next()
        if g == "int":
            return self._rng.randint(int(raw.get("min", 0)), int(raw.get("max", 100)))
        if g == "float":
            lo, hi = float(raw.get("min", 0.0)), float(raw.get("max", 1.0))
            return round(self._rng.uniform(lo, hi), int(raw.get("precision", 2)))
        if g == "bool":
            return self._rng.random() < float(raw.get("p", 0.5))
        if g == "choice":
            values = raw.get("values") or []
            if not values:
                raise DataError(f"字段 {f.name!r} gen=choice 但未提供 values")
            weights = raw.get("weights")
            if weights:
                return copy.deepcopy(self._rng.choices(values, weights=weights, k=1)[0])
            return copy.deepcopy(self._rng.choice(values))
        if g == "enum":
            return copy.deepcopy(self._gen_value(Field(f.name, "choice", raw), row))
        if g == "pattern":
            pat = str(raw.get("pattern", "??####"))
            return "".join(
                self._rng.choice(string.ascii_uppercase) if c == "?"
                else self._rng.choice(string.digits) if c == "#"
                else self._rng.choice(string.ascii_lowercase) if c == "@"
                else c
                for c in pat
            )
        if g == "datetime":
            lo = parse_time_bound(raw.get("start", "-30d"))
            hi = parse_time_bound(raw.get("end", "now"))
            if lo > hi:
                lo, hi = hi, lo
            span = max((hi - lo).total_seconds(), 0)
            val = lo + timedelta(seconds=self._rng.uniform(0, span))
            return val.strftime(str(raw.get("fmt", "%Y-%m-%d %H:%M:%S")))
        if g == "regex":
            return self._expand_simple_regex(str(raw.get("pattern", "[a-z]{3}")))
        if g == "uuid":
            import uuid as _u

            return str(_u.UUID(int=self._rng.getrandbits(128), version=4))
        if g == "expr":
            return self._eval_expr(str(raw.get("value", "")), row)
        if g == "ref":
            return self._gen_ref(raw)
        if g in ("list", "array"):
            lo = int(raw.get("count", 1)) if not isinstance(raw.get("count"), (list, tuple)) else int(raw["count"][0])
            hi = int(raw.get("count", lo)) if not isinstance(raw.get("count"), (list, tuple)) else int(raw["count"][-1])
            n = self._rng.randint(lo, hi)
            item_spec = raw.get("item") or {"gen": "int", "min": 1, "max": 9}
            return [self._gen_value(Field.of({**item_spec, "name": f"{f.name}[i]"}), row) for i in range(n)]
        if g == "fake_phone":
            return self.transforms.apply("fake_phone", None)
        if g == "fake_id_card":
            return self.transforms.apply("fake_id_card", None)
        if g in ("faker", "fake"):
            method = str(raw.get("method", "name"))
            fn = getattr(self.ctx.faker, method, None)
            if fn is None:
                raise DataError(
                    f"字段 {f.name!r} 引用了不存在的 Faker provider: {method!r}",
                    hint="常见可用: name / email / phone_number / company / address / "
                         "user_name / postcode / job / ssn / date_time / random_int",
                )
            return fn(*[coerce_scalar(a) for a in (raw.get("args") or [])])

        raise DataError(
            f"未知 gen 类型: {g!r}（字段 {f.name!r}）",
            hint="可用 gen: const seq int float bool choice pattern datetime regex uuid "
                 "expr ref list faker fake_phone fake_id_card",
        )

    def _expand_simple_regex(self, pattern: str) -> str:
        """展开一个正则的**简单**子集：字面量、字符类、``\\d`` ``\\w``、``{n,m}`` ``+`` ``*`` ``?``。"""
        out: list[str] = []
        i = 0
        while i < len(pattern):
            ch = pattern[i]
            if ch == "[":
                end = pattern.find("]", i)
                if end == -1:
                    raise DataError(f"正则未闭合: {pattern!r}")
                pool = self._char_class(pattern[i + 1:end])
                i = end + 1
            elif ch == "\\" and i + 1 < len(pattern):
                nxt = pattern[i + 1]
                pool = {"d": string.digits, "w": string.ascii_letters + string.digits,
                        "s": " "}.get(nxt, nxt)
                i += 2
            elif ch in "^$":
                i += 1
                continue
            else:
                pool = ch
                i += 1

            count_min = count_max = 1
            if i < len(pattern):
                if pattern[i] == "{":
                    end = pattern.find("}", i)
                    if end != -1:
                        spec = pattern[i + 1:end]
                        if "," in spec:
                            a, _, b = spec.partition(",")
                            count_min, count_max = int(a), int(b or a)
                        else:
                            count_min = count_max = int(spec)
                        i = end + 1
                elif pattern[i] == "+":
                    count_min, count_max = 1, 6
                    i += 1
                elif pattern[i] == "*":
                    count_min, count_max = 0, 5
                    i += 1
                elif pattern[i] == "?":
                    count_min, count_max = 0, 1
                    i += 1
            out.append("".join(self._rng.choice(pool) for _ in range(self._rng.randint(count_min, count_max))))
        return "".join(out)

    @staticmethod
    def _char_class(spec: str) -> str:
        if spec.startswith("^"):
            base = string.ascii_letters + string.digits + "_-.@"
            return "".join(c for c in base if c not in spec[1:])
        pool = ""
        i = 0
        while i < len(spec):
            if i + 2 < len(spec) and spec[i + 1] == "-":
                pool += "".join(chr(c) for c in range(ord(spec[i]), ord(spec[i + 2]) + 1))
                i += 3
            else:
                pool += spec[i]
                i += 1
        return pool or string.ascii_letters

    def _eval_expr(self, expr: str, row: Mapping[str, Any]) -> Any:
        """字段派生表达式求值。配置由项目所有人维护，非外部不可信输入。"""
        if not expr:
            return None
        text = re.sub(r"\$\{([^}]+)\}", lambda m: repr(row.get(m.group(1).strip())), expr)
        try:
            return eval(text, {"__builtins__": {}, "len": len, "abs": abs, "str": str,
                               "int": int, "float": float, "round": round, "min": min, "max": max},
                        dict(row))
        except Exception as exc:
            raise DataError(f"派生字段表达式求值失败: {expr!r} → {exc}",
                            hint="表达式里引用其他字段用 ${字段名}，例如 ${age} >= 30") from exc

    def _gen_ref(self, raw: Mapping[str, Any]) -> Any:
        entity = str(raw.get("entity", ""))
        field = str(raw.get("field", "id"))
        rows = self._cache.get(entity)
        if not rows:
            raise DataError(
                f"字段引用了实体 {entity!r}，但它还没生成",
                hint="在 schema 里把被引用实体写在前面，或用 `forgeqa gen` 一次性生成全部实体",
            )
        return copy.deepcopy(self._rng.choice(rows).get(field))

    # ---------------- 公开 API ----------------
    def generate(
        self,
        schema: Schema | str | Path | Mapping[str, Any],
        *,
        count: int | None = None,
        overrides: Mapping[str, Any] | None = None,
        remember: bool = True,
    ) -> list[dict[str, Any]]:
        """生成记录。``overrides`` 会覆盖每条记录的对应字段（用于定点构造场景）。"""
        sch = self._as_schema(schema)
        total = int(count if count is not None else sch.count)
        rows: list[dict[str, Any]] = []
        seen: dict[str, set] = {u: set() for u in sch.unique}
        max_rounds = 200

        for idx in range(total):
            for _attempt in range(max_rounds):
                row: dict[str, Any] = {}
                for f in sch.fields:
                    try:
                        val = self._gen_value(f, row)
                    except DataError:
                        raise
                    if val is MISSING:
                        continue
                    if f.transform:
                        val = self.transforms.apply(self.ctx.resolve(f.transform), val)
                    row[f.name] = val
                if self._check_unique(row, seen):
                    break
            else:
                raise DataError(
                    f"实体 {sch.entity!r} 唯一字段 {sch.unique} 在 {max_rounds} 次尝试内无法去重",
                    hint="扩大字段取值空间（如 Faker 换成带随机后缀的 pattern），或减少 unique 字段",
                )
            for u, bucket in seen.items():
                bucket.add(_hashable(row.get(u)))
            if overrides:
                row.update(copy.deepcopy(dict(overrides)))
            if self.ctx.seed is not None:
                row.setdefault("_seed", self.ctx.seed)
            row["_index"] = idx
            rows.append(row)

        if remember and self.ctx.seed is not None:
            rows = [r for r in rows]
        if remember:
            self._cache.setdefault(sch.entity, []).extend(
                [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
            )
            self.ctx.set(f"{sch.entity}", self._cache[sch.entity], layer="data")
        return rows

    @staticmethod
    def _check_unique(row: Mapping[str, Any], seen: Mapping[str, set]) -> bool:
        for key, bucket in seen.items():
            if _hashable(row.get(key)) in bucket:
                return False
        return True

    def generate_many(self, schemas: Sequence[Schema | str | Path]) -> dict[str, list[dict[str, Any]]]:
        """按顺序生成多个实体（后面的实体可以用 ref 引用前面的）。"""
        out: dict[str, list[dict[str, Any]]] = {}
        for s in schemas:
            sch = self._as_schema(s)
            out[sch.entity] = self.generate(sch)
        return out

    def _as_schema(self, schema: Schema | str | Path | Mapping[str, Any]) -> Schema:
        if isinstance(schema, Schema):
            return schema
        if isinstance(schema, Mapping):
            return Schema.from_dict(schema)
        p = Path(schema)
        if not p.is_absolute() and self.schema_dir:
            cand = self.schema_dir / p
            if cand.exists():
                p = cand
        if not p.exists() and self.schema_dir:
            cand = self.schema_dir / f"{p.name}.yaml"
            if cand.exists():
                p = cand
        return Schema.load(p)

    # ---------------- 变异造数 ----------------
    def mutate(
        self,
        schema: Schema | str | Path | Mapping[str, Any],
        *,
        base: Mapping[str, Any] | None = None,
        categories: Sequence[str] = ("boundary", "abnormal", "extreme"),
    ) -> list[dict[str, Any]]:
        """围绕一个正常样本，批量产出变异数据集。

        返回结构与技能 ``gen_test_data.py`` 对齐：
        ``{"case_id", "entity", "target", "category", "description", "data"}``
        """
        sch = self._as_schema(schema)
        base_row = dict(base) if base else self.generate(sch, count=1)[0]
        base_row = {k: v for k, v in base_row.items() if not k.startswith("_")}
        cases: list[dict[str, Any]] = []
        counter = 0

        for f in sch.fields:
            variants: list[tuple[str, str, Any]] = []
            if "boundary" in categories:
                variants += self._boundary_variants(f, base_row.get(f.name))
            if "abnormal" in categories:
                variants += self._abnormal_variants(f)
            if "extreme" in categories:
                variants += self._extreme_variants(f)

            for cat, desc, val in variants:
                counter += 1
                payload = copy.deepcopy(base_row)
                if val is MISSING:
                    payload.pop(f.name, None)
                else:
                    payload[f.name] = val
                cases.append({
                    "case_id": f"{sch.entity.upper()}-MUT-{counter:03d}",
                    "entity": sch.entity,
                    "target": f.name,
                    "category": cat,
                    "description": desc,
                    "data": payload,
                })
        return cases

    def _boundary_variants(self, f: Field, cur: Any) -> list[tuple[str, str, Any]]:
        out: list[tuple[str, str, Any]] = []
        raw = f.raw
        if f.gen in ("int", "float"):
            lo, hi = raw.get("min"), raw.get("max")
            if lo is not None:
                out += [("boundary", f"{f.name}=最小值({lo})", lo),
                        ("boundary", f"{f.name}=最小值-1({int(lo) - 1})", int(lo) - 1)]
            if hi is not None:
                out += [("boundary", f"{f.name}=最大值({hi})", hi),
                        ("boundary", f"{f.name}=最大值+1({int(hi) + 1})", int(hi) + 1)]
            if lo is not None and hi is not None:
                out.append(("boundary", f"{f.name}=0（区间外最低）", 0))
        if isinstance(cur, str) and raw.get("min_len") is not None:
            ml, xl = int(raw["min_len"]), int(raw.get("max_len", raw["min_len"]))
            out += [("boundary", f"{f.name}=最小长度({ml})", "a" * ml),
                    ("boundary", f"{f.name}=最小长度-1({ml - 1})", "a" * max(ml - 1, 0)),
                    ("boundary", f"{f.name}=最大长度({xl})", "a" * xl),
                    ("boundary", f"{f.name}=最大长度+1({xl + 1})", "a" * (xl + 1))]
        if f.raw.get("required") is False and cur is not None:
            out.append(("boundary", f"{f.name}=缺省（允许为空）", None))
        return out

    def _abnormal_variants(self, f: Field) -> list[tuple[str, str, Any]]:
        out: list[tuple[str, str, Any]] = []
        if f.nullable:
            return out
        out.append(("abnormal", f"{f.name}=缺省（必填未传）", MISSING))
        if f.gen in ("int", "float"):
            out += [("abnormal", f"{f.name}=非数字字符串", "abc"),
                    ("abnormal", f"{f.name}=小数（整数型传小数）", 1.5),
                    ("abnormal", f"{f.name}=布尔值", True),
                    ("abnormal", f"{f.name}=溢出大数", 2 ** 63)]
        elif f.gen in ("faker", "pattern", "const", "regex") or isinstance(f.raw.get("value"), str):
            for tag, text in NASTY_STRINGS[:8]:
                out.append(("abnormal", f"{f.name}={tag}", text))
        elif f.gen in ("bool",):
            out.append(("abnormal", f"{f.name}=字符串 'yes'（类型混淆）", "yes"))
        elif f.gen in ("datetime",):
            out += [("abnormal", f"{f.name}=非法日期 2026-13-45", "2026-13-45"),
                    ("abnormal", f"{f.name}=时间戳数字", 1893456000)]
        elif f.gen in ("choice", "enum"):
            out.append(("abnormal", f"{f.name}=枚举外的值 __not_in_enum__", "__not_in_enum__"))
        return out

    def _extreme_variants(self, f: Field) -> list[tuple[str, str, Any]]:
        out: list[tuple[str, str, Any]] = []
        if f.gen in ("int", "float"):
            out += [("extreme", f"{f.name}=极大值 10^18", 10 ** 18),
                    ("extreme", f"{f.name}=极小值 -10^18", -(10 ** 18))]
        elif f.gen in ("faker", "pattern", "const") or isinstance(f.raw.get("value"), str):
            out += [("extreme", f"{f.name}=超长字符串 64KB", "A" * 65535),
                    ("extreme", f"{f.name}=纯空白", " " * 100),
                    ("extreme", f"{f.name}=emoji 轰炸", "🎉" * 200),
                    ("extreme", f"{f.name}=深层嵌套 JSON", json.dumps(_nested(20), ensure_ascii=False))]
        elif f.gen in ("list", "array"):
            out.append(("extreme", f"{f.name}=空数组", []))
        return out

    # ---------------- 落盘 ----------------
    def dump(self, data: Any, out_dir: str | Path, name: str, fmt: str = "json") -> Path:
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{name}.{fmt}"
        if fmt == "json":
            target.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        elif fmt in ("yaml", "yml"):
            target.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        else:
            raise DataError(f"不支持的导出格式: {fmt}",
                            hint="支持 json / yaml；落库请用 `forgeqa seed`")
        return target


def _hashable(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
    return v


def _nested(depth: int) -> dict:
    node: dict[str, Any] = {"leaf": 1}
    for _ in range(depth):
        node = {"n": node}
    return node


# --------------------------------------------------------------------------- #
# 从真实响应反推 schema —— 适配任意网站的第一步
# --------------------------------------------------------------------------- #
def infer_schema(sample: Any, entity: str = "entity", *, count: int = 1) -> dict[str, Any]:
    """从接口响应样本推导造数 schema。

    这一层的作用：拿到任意站点的真实响应，一条命令就能得到可编辑的造数定义，
    不用从零手写字段。
    """
    fields: list[dict[str, Any]] = []
    if isinstance(sample, list):
        sample = sample[0] if sample else {}
    if not isinstance(sample, Mapping):
        raise DataError("样本不是对象或数组，无法推导 schema",
                        hint="传入接口返回的 JSON 对象 / 对象数组")

    for name, value in sample.items():
        fields.append(_infer_field(str(name), value))
    return {"entity": entity, "count": count, "fields": fields,
            "description": f"由样本自动推导（原始字段数 {len(fields)}）"}


def _infer_field(name: str, value: Any) -> dict[str, Any]:
    lname = name.lower()
    base: dict[str, Any] = {"name": name}
    if value is None:
        return {**base, "gen": "const", "value": None, "nullable": True}
    if isinstance(value, bool):
        return {**base, "gen": "bool", "p": 0.5}
    if isinstance(value, int):
        # 主键/外键类字段用自增序列更贴近真实，也天然满足唯一性
        if lname == "id" or lname.endswith("_id") or lname in ("uid", "pk"):
            return {**base, "gen": "seq", "start": 1, "step": 1}
        size = max(abs(value).bit_length(), 8)
        return {**base, "gen": "int", "min": 1, "max": min(10 ** 6, 2 ** size)}
    if isinstance(value, float):
        return {**base, "gen": "float", "min": 0.0, "max": max(1.0, abs(value) * 10), "precision": 2}
    if isinstance(value, list):
        item = _infer_field(f"{name}[]", value[0]) if value else {"name": f"{name}[]", "gen": "int", "min": 1, "max": 9}
        item.pop("name", None)
        return {**base, "gen": "list", "count": [1, 3], "item": item}
    if isinstance(value, Mapping):
        return {**base, "gen": "const", "value": value}
    text = str(value)
    if re.fullmatch(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
        # 邮箱：local part 用 pattern 生成，域名用 suffix 转换补上，
        # 避免 pattern 占位符把字面量 @ 吃掉
        return {**base, "gen": "pattern", "pattern": "@@@@.####",
                "transform": "suffix:@example.com", "min_len": 8, "max_len": 64}
    if re.fullmatch(r"1\d{10}", text):
        return {**base, "gen": "fake_phone"}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2})?", text):
        return {**base, "gen": "datetime", "fmt": "%Y-%m-%d %H:%M:%S" if " " in text or "T" in text else "%Y-%m-%d"}
    if re.fullmatch(r"[0-9a-fA-F-]{32,36}", text):
        return {**base, "gen": "uuid"}
    if lname in ("id", "uid", "user_id", "pk") and re.fullmatch(r"\d+", text):
        return {**base, "gen": "seq", "start": 1, "step": 1}
    if lname.endswith(("name", "title", "昵称")):
        return {**base, "gen": "faker", "method": "name"}
    if lname.endswith(("status", "state", "type")):
        return {**base, "gen": "const", "value": value}
    return {**base, "gen": "pattern", "pattern": "?" * max(min(len(text), 12), 4)}
