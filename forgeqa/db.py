"""forgeqa.db — SQL 层：造数入库 / 断言取数 / 快照回归 / 数据清理。

设计要点
--------
1. **零依赖默认路径**：``driver: sqlite`` 走 Python 内置 ``sqlite3``，
   任何机器上开箱即用（本地回归、CI 都不需要额外服务）。
2. **生产同构路径**：``driver: sqlalchemy`` + ``dsn`` 可接 MySQL / PostgreSQL /
   SQL Server，SQL 与参数风格保持一致（统一用 ``:name`` 具名参数）。
3. **造数入库要能回滚**：所有插入的 PK 全部登记，``cleanup`` 时精确删除，
   不在被测环境留垃圾数据。
4. **快照回归**：把关键表的数据拍成基线，后续运行做 diff，
   能发现「接口返回对了但数据写歪了」这类问题。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import DbError

Row = dict[str, Any]


# --------------------------------------------------------------------------- #
# 驱动
# --------------------------------------------------------------------------- #
class _Driver:
    name = "base"

    def connect(self) -> Any:  # pragma: no cover - 抽象
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 抽象
        pass

    def query(self, sql: str, params: Any = None) -> list[Row]:  # pragma: no cover
        raise NotImplementedError

    def execute(self, sql: str, params: Any = None) -> int:  # pragma: no cover
        raise NotImplementedError

    def executemany(self, sql: str, seq: Sequence[Any]) -> int:  # pragma: no cover
        raise NotImplementedError

    def list_tables(self) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def list_columns(self, table: str) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def primary_key(self, table: str) -> list[str]:  # pragma: no cover
        raise NotImplementedError


class SqliteDriver(_Driver):
    name = "sqlite"

    def __init__(self, path: str | Path):
        self.lastrowid: int | None = None
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def query(self, sql: str, params: Any = None) -> list[Row]:
        cur = self.connect().execute(sql, params or {})
        return [dict(r) for r in cur.fetchall()]

    def execute(self, sql: str, params: Any = None) -> int:
        cur = self.connect().execute(sql, params or {})
        self.lastrowid = cur.lastrowid
        return cur.rowcount

    def executemany(self, sql: str, seq: Sequence[Any]) -> int:
        cur = self.connect().executemany(sql, list(seq))
        return cur.rowcount

    def script(self, ddl: str) -> None:
        self.connect().executescript(ddl)

    def list_tables(self) -> list[str]:
        rows = self.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        return sorted(r["name"] for r in rows)

    def list_columns(self, table: str) -> list[str]:
        return [r["name"] for r in self.query(f"PRAGMA table_info({_quote_ident(table)})")]

    def primary_key(self, table: str) -> list[str]:
        info = self.query(f"PRAGMA table_info({_quote_ident(table)})")
        pk = [r["name"] for r in sorted(info, key=lambda r: r["pk"]) if r["pk"]]
        return pk


class SqlAlchemyDriver(_Driver):
    name = "sqlalchemy"

    def __init__(self, dsn: str):
        try:
            import sqlalchemy as sa
        except ImportError as exc:  # pragma: no cover
            raise DbError(
                "使用 driver=sqlalchemy 需要安装 SQLAlchemy",
                hint="pip install sqlalchemy，并按数据库安装驱动："
                     "MySQL→pymysql、PostgreSQL→psycopg2-binary",
            ) from exc
        self._sa = sa
        self.dsn = dsn
        self._engine = None

    def connect(self):
        if self._engine is None:
            self._engine = self._sa.create_engine(self.dsn, pool_pre_ping=True, future=True)
        return self._engine

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    @contextmanager
    def _tx(self):
        with self.connect().begin() as conn:
            yield conn

    def query(self, sql: str, params: Any = None) -> list[Row]:
        sa = self._sa
        with self.connect().connect() as conn:
            res = conn.execute(sa.text(sql), dict(params or {}))
            return [dict(r._mapping) for r in res.fetchall()]

    def execute(self, sql: str, params: Any = None) -> int:
        sa = self._sa
        with self.connect().begin() as conn:
            res = conn.execute(sa.text(sql), dict(params or {}))
            self.lastrowid = _scalar_pk(res)
            return res.rowcount or 0

    def executemany(self, sql: str, seq: Sequence[Any]) -> int:
        sa = self._sa
        total = 0
        with self.connect().begin() as conn:
            for item in seq:
                res = conn.execute(sa.text(sql), dict(item or {}))
                total += res.rowcount or 0
        return total

    def list_tables(self) -> list[str]:
        return sorted(self._sa.inspect(self.connect()).get_table_names())

    def list_columns(self, table: str) -> list[str]:
        return [c["name"] for c in self._sa.inspect(self.connect()).get_columns(table)]

    def primary_key(self, table: str) -> list[str]:
        insp = self._sa.inspect(self.connect())
        try:
            return list(insp.get_pk_constraint(table).get("constrained_columns") or [])
        except Exception:  # pragma: no cover
            return []


def _scalar_pk(res: Any) -> Any:
    """尽力从 SQLAlchemy 结果里拿到自增主键（不同方言字段名不同）。"""
    for attr in ("lastrowid", "last_inserted_id"):
        val = getattr(res, attr, None)
        if val:
            return val
    try:
        pk = res.inserted_primary_key
        return pk[0] if pk else None
    except Exception:
        return None


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# --------------------------------------------------------------------------- #
# Database 门面
# --------------------------------------------------------------------------- #
class Database:
    """统一 SQL 门面。用法::

        with Database.from_opts({"driver": "sqlite", "path": "./out/demo.db"}, root) as db:
            db.query("SELECT 1 AS ok")
    """

    def __init__(self, driver: _Driver, *, label: str = ""):
        self.driver = driver
        self.label = label
        self.log: list[dict[str, Any]] = []
        # 最近一次 insert() 拿到的自增主键，供 Seeder 登记回收用
        self.last_inserted_pks: list[Any] = []

    @classmethod
    def from_opts(cls, opts: Mapping[str, Any], root: Path) -> "Database":
        driver_name = str(opts.get("driver", "sqlite")).lower()
        if driver_name in ("sqlite", "sqlite3"):
            raw_path = opts.get("path") or (opts.get("dsn") or "").replace("sqlite:///", "") or "./out/forgeqa.db"
            p = Path(str(raw_path)).expanduser()
            if not p.is_absolute():
                p = root / p
            # 内存库在多连接下会丢数据，强制走文件，保证跨进程 / 跨线程可见
            return cls(SqliteDriver(p), label=f"sqlite:{p}")
        dsn = opts.get("dsn")
        if not dsn:
            raise DbError(
                f"driver={driver_name} 必须提供 db.dsn",
                hint="例如 mysql+pymysql://user:pass@host:3306/dbname",
            )
        return cls(SqlAlchemyDriver(str(dsn)), label=f"{driver_name}:{_mask_dsn(str(dsn))}")

    # ---------------- 生命周期 ----------------
    def __enter__(self) -> "Database":
        self.driver.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.driver.close()

    def close(self) -> None:
        self.driver.close()

    # ---------------- 执行 ----------------
    def query(self, sql: str, params: Mapping[str, Any] | None = None) -> list[Row]:
        started = time.perf_counter()
        try:
            rows = self.driver.query(sql, params)
        except Exception as exc:
            self._record(sql, params, started, error=str(exc))
            raise DbError(f"SQL 查询失败: {exc}", hint=_sql_hint(sql, exc)) from exc
        self._record(sql, params, started, rows=len(rows))
        return rows

    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> int:
        started = time.perf_counter()
        try:
            n = self.driver.execute(sql, params)
        except Exception as exc:
            self._record(sql, params, started, error=str(exc))
            raise DbError(f"SQL 执行失败: {exc}", hint=_sql_hint(sql, exc)) from exc
        self._record(sql, params, started, rows=n)
        return n

    def executemany(self, sql: str, rows: Sequence[Mapping[str, Any]]) -> int:
        return self.driver.executemany(sql, rows)

    def scalar(self, sql: str, params: Mapping[str, Any] | None = None, default: Any = None) -> Any:
        rows = self.query(sql, params)
        if not rows:
            return default
        return next(iter(rows[0].values()))

    def one(self, sql: str, params: Mapping[str, Any] | None = None) -> Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def script(self, ddl: str) -> None:
        if isinstance(self.driver, SqliteDriver):
            self.driver.script(ddl)
        else:  # pragma: no cover - 非 sqlite 逐句执行
            for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
                self.execute(stmt)

    def script_file(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            raise DbError(f"SQL 脚本不存在: {p}")
        self.script(p.read_text(encoding="utf-8"))

    def _record(self, sql: str, params: Any, started: float, **extra) -> None:
        self.log.append({
            "sql": " ".join(str(sql).split())[:2000],
            "params": dict(params) if isinstance(params, Mapping) else params,
            "ms": round((time.perf_counter() - started) * 1000, 1),
            **extra,
        })

    # ---------------- 元数据 ----------------
    def tables(self) -> list[str]:
        return self.driver.list_tables()

    def columns(self, table: str) -> list[str]:
        return self.driver.list_columns(table)

    def primary_key(self, table: str) -> list[str]:
        return self.driver.primary_key(table)

    def ensure_table(self, table: str) -> None:
        if table not in self.tables():
            raise DbError(
                f"表 {table!r} 不存在，当前库中的表: {self.tables()}",
                hint="先用 `forgeqa db init --ddl <建表脚本>` 或 `forgeqa db script <file.sql>` 建表",
            )

    # ---------------- 写入 ----------------
    def insert(self, table: str, rows: Sequence[Mapping[str, Any]], *,
               mapping: Mapping[str, str] | None = None,
               defaults: Mapping[str, Any] | None = None) -> int:
        """按表结构裁剪字段后批量插入。``mapping`` 把 schema 字段名映射为表列名。"""
        if not rows:
            return 0
        self.ensure_table(table)
        cols = self.columns(table)
        map_ = dict(mapping or {})
        merged: list[Row] = []
        for raw in rows:
            row: Row = {}
            for k, v in raw.items():
                if k.startswith("_"):
                    continue
                row[map_.get(k, k)] = v
            if defaults:
                for k, v in defaults.items():
                    row.setdefault(k, v)
            row = {k: v for k, v in row.items() if k in cols}
            merged.append(row)
        keys = sorted({k for r in merged for k in r})
        if not keys:
            raise DbError(
                f"实体字段与表 {table!r} 的列没有任何交集（表列: {cols}）",
                hint=f"在 seeding 配置里用 mapping 做字段映射，例如 mapping: {{{rows[0].get('_index', '') or '字段名'}: {cols[0]}}}",
            )
        sql = (f"INSERT INTO {_quote_ident(table)} ({', '.join(_quote_ident(k) for k in keys)}) "
               f"VALUES ({', '.join(':' + k for k in keys)})")
        total = 0
        self.last_inserted_pks = []
        for r in merged:
            params = {k: r.get(k) for k in keys}
            total += self.execute(sql, params)
            pk = getattr(self.driver, "lastrowid", None)
            if pk is not None:
                self.last_inserted_pks.append(pk)
        return total

    def delete(self, table: str, where: Mapping[str, Any]) -> int:
        if not where:
            raise DbError(
                f"拒绝执行无条件的 DELETE FROM {table}",
                hint="清理数据必须带条件；整表清空请显式使用 truncate()",
            )
        clause = " AND ".join(f"{_quote_ident(k)} = :{k}" for k in where)
        return self.execute(f"DELETE FROM {_quote_ident(table)} WHERE {clause}", dict(where))

    def truncate(self, table: str) -> None:
        if isinstance(self.driver, SqliteDriver):
            self.execute(f"DELETE FROM {_quote_ident(table)}")
            try:
                self.execute("DELETE FROM sqlite_sequence WHERE name = :t", {"t": table})
            except Exception:
                pass  # 表没有 AUTOINCREMENT 时不存在 sqlite_sequence 记录
        else:  # pragma: no cover
            self.execute(f"DELETE FROM {_quote_ident(table)}")

    # ---------------- 快照回归 ----------------
    def snapshot(self, table: str, *, key: str, fields: Sequence[str] | None = None,
                 where: str | None = None, order_by: str | None = None) -> dict[str, Any]:
        """把表数据拍成 ``{key值: {列: 值}}``，用于跨运行 diff。"""
        self.ensure_table(table)
        cols = list(fields) if fields else [c for c in self.columns(table) if c != key]
        select = ", ".join(_quote_ident(c) for c in dict.fromkeys([key, *cols]))
        sql = f"SELECT {select} FROM {_quote_ident(table)}"
        if where:
            sql += f" WHERE {where}"
        sql += f" ORDER BY {order_by or _quote_ident(key)}"
        rows = self.query(sql)
        return {str(r.get(key)): {c: _jsonable(r.get(c)) for c in cols} for r in rows}


def _jsonable(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v


def _mask_dsn(dsn: str) -> str:
    import re

    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", dsn)


def _sql_hint(sql: str, exc: Exception) -> str | None:
    text = str(exc).lower()
    s = sql.lower()
    if "no such table" in text:
        return "表不存在：先建表或检查表名拼写"
    if "no such column" in text:
        return "列不存在：检查字段名，或确认造数字段与表列的 mapping 配置"
    if "unique" in text and "constraint" in text:
        return "唯一约束冲突：造数时给该字段加 unique，或调整测试数据"
    if "foreign key" in text:
        return "外键约束失败：先造父表数据，或调整 seeding 的顺序"
    if "not null" in text:
        return "非空约束失败：该列缺值，检查 schema 是否有对应字段或 defaults 补默认值"
    if "locked" in text:
        return "数据库被占用：SQLite 并发写入受限，把 runner.jobs 调成 1，或换用服务型数据库"
    if "syntax error" in text or "syntax" in text:
        return f"SQL 语法错误，请检查：{' '.join(s.split())[:120]}"
    return None


# --------------------------------------------------------------------------- #
# 造数入库
# --------------------------------------------------------------------------- #
@dataclass
class SeedStep:
    entity: str
    table: str
    schema: Any = None                     # schema 路径 / dict / 省略则复用已生成数据
    count: int | None = None
    mapping: dict[str, str] = dc_field(default_factory=dict)
    defaults: dict[str, Any] = dc_field(default_factory=dict)
    where: str | None = None               # 清理条件模板，缺省按 PK 精确删


class Seeder:
    """把数据工厂的产出写进数据库，并全程登记以便精确回收。"""

    def __init__(self, db: Database, factory, ctx, *, ledger_path: Path | None = None):
        self.db = db
        self.factory = factory
        self.ctx = ctx
        self.ledger_path = ledger_path
        self.ledger: dict[str, list[Row]] = {}   # table -> [{pk: 值}]

    # ---------------- 造 + 入 ----------------
    def run(self, plan: Sequence[Mapping[str, Any] | SeedStep]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in plan:
            step = item if isinstance(item, SeedStep) else SeedStep(
                entity=str(item["entity"]),
                table=str(item.get("table") or item["entity"]),
                schema=item.get("schema"),
                count=item.get("count"),
                mapping=dict(item.get("mapping") or {}),
                defaults=dict(item.get("defaults") or {}),
                where=item.get("where"),
            )
            rows = self._rows_for(step)
            self.db.insert(step.table, rows, mapping=step.mapping, defaults=step.defaults)
            counts[step.table] = counts.get(step.table, 0) + len(rows)
            self._track(step, rows)
            self.ctx.set(step.entity, [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
                         layer="data")
        self.save_ledger()
        return counts

    def _rows_for(self, step: SeedStep) -> list[Row]:
        if step.schema is not None:
            return self.factory.generate(step.schema, count=step.count)
        cached = self.ctx.get(f"data.{step.entity}", None)
        if cached:
            return list(cached)
        raise DbError(
            f"seeding 步骤 {step.entity!r} 没有数据来源",
            hint="给该步骤指定 schema，或先用 `forgeqa gen` 生成同名实体",
        )

    # ---------------- 回收登记 ----------------
    def _track(self, step: SeedStep, rows: Sequence[Row]) -> None:
        """登记本次插入的行，供后续精确回收。

        三级兜底，从最精确到最宽松：
        1. 造数数据里自带主键值 → 按主键登记
        2. 数据库回传的自增主键（AUTOINCREMENT 表的常态）→ 按主键登记
        3. 都没有 → 按整行内容登记（DELETE 时做全列匹配）
        """
        try:
            pk = self.db.primary_key(step.table) or []
        except Exception:
            pk = []
        bucket = self.ledger.setdefault(step.table, [])

        if pk and rows:
            cols = [step.mapping.get(c, c) for c in pk]
            if all(c in rows[0] for c in cols):
                for r in rows:
                    bucket.append({c: _jsonable(r[c]) for c in cols})
                return
            inserted = getattr(self.db, "last_inserted_pks", []) or []
            if inserted and len(inserted) == len(rows):
                col = pk[0]
                for v in inserted:
                    bucket.append({col: _jsonable(v)})
                return

        for r in rows:
            bucket.append({k: _jsonable(v) for k, v in r.items() if not k.startswith("_")})

    def save_ledger(self) -> None:
        if not self.ledger_path:
            return
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, list[Row]] = {}
        if self.ledger_path.exists():
            try:
                existing = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        for table, keys in self.ledger.items():
            existing.setdefault(table, []).extend(keys)
        self.ledger_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    # 兼容旧命名
    def register_cleanup(self, step: SeedStep, rows: Sequence[Row]) -> None:
        self._track(step, rows)


def cleanup_from_ledger(db: Database, ledger_path: str | Path) -> dict[str, int]:
    """按登记文件精确回收造数。删除顺序按表的依赖倒序（有外键时更安全）。"""
    p = Path(ledger_path)
    if not p.exists():
        return {}
    ledger: dict[str, list[Row]] = json.loads(p.read_text(encoding="utf-8"))
    tables = db.tables()
    deleted: dict[str, int] = {}
    for table in sorted(ledger, key=lambda t: (t not in tables, t), reverse=True):
        if table not in tables:
            continue
        n = 0
        for key in ledger[table]:
            n += db.delete(table, key)
        deleted[table] = n
    p.unlink(missing_ok=True)
    return deleted


# --------------------------------------------------------------------------- #
# 基线快照
# --------------------------------------------------------------------------- #
def diff_snapshots(baseline: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """对比两份快照，输出 added / removed / changed。"""
    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    changed: dict[str, dict[str, Any]] = {}
    for key in sorted(set(baseline) & set(current)):
        b, c = baseline[key], current[key]
        if b != c:
            fields = {}
            for f in sorted(set(b) | set(c)):
                if b.get(f) != c.get(f):
                    fields[f] = {"baseline": b.get(f), "current": c.get(f)}
            changed[key] = fields or {"_note": "整行指纹变化"}
    return {"added": added, "removed": removed, "changed": changed,
            "has_diff": bool(added or removed or changed)}
