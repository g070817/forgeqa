"""SQL 层测试：写入、回收、快照 diff、错误提示。全部走 SQLite，无需外部服务。"""
from __future__ import annotations

from pathlib import Path

import pytest

from forgeqa.config import Context
from forgeqa.db import Database, Seeder, cleanup_from_ledger, diff_snapshots
from forgeqa.errors import DbError
from forgeqa.factory import DataFactory

DDL = """
CREATE TABLE dept (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dept_no TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  status INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  dept_id INTEGER
);
"""

USER_SCHEMA = {
    "entity": "user", "count": 2,
    "fields": [
        {"name": "name", "gen": "faker", "method": "name"},
        {"name": "email", "gen": "pattern", "pattern": "@@@@",
         "transform": "suffix:${uniq}@example.com"},
    ],
}
DEPT_SCHEMA = {
    "entity": "dept", "count": 3,
    "fields": [
        {"name": "dept_no", "gen": "pattern", "pattern": "D###",
         "transform": "suffix:${uniq}"},
        {"name": "name", "gen": "choice", "values": ["研发部"]},
        {"name": "status", "gen": "const", "value": 1},
    ],
}


@pytest.fixture()
def db(tmp_path: Path):
    d = Database.from_opts({"driver": "sqlite", "path": str(tmp_path / "t.db")}, tmp_path)
    with d:
        d.script(DDL)
        yield d


class TestDatabase:
    def test_query_and_scalar(self, db):
        assert db.scalar("SELECT COUNT(*) FROM users") == 0
        assert db.one("SELECT * FROM users") is None

    def test_tables_and_columns_and_pk(self, db):
        assert set(db.tables()) == {"dept", "users"}
        assert db.columns("dept") == ["id", "dept_no", "name", "status"]
        assert db.primary_key("dept") == ["id"]

    def test_insert_filters_and_maps_columns(self, db):
        n = db.insert("users", [{"name": "A", "email": "a@b.c", "不存在的列": 1, "_meta": 2}])
        assert n == 1
        assert db.scalar("SELECT name FROM users") == "A"

    def test_insert_with_mapping(self, db):
        db.insert("dept", [{"code": "D1", "title": "研发部", "status": 1}],
                  mapping={"code": "dept_no", "title": "name"})
        assert db.scalar("SELECT dept_no FROM dept") == "D1"

    def test_insert_returns_autoincrement_pk(self, db):
        db.insert("dept", [{"dept_no": "D1", "name": "a"}, {"dept_no": "D2", "name": "b"}])
        assert db.last_inserted_pks == [1, 2]

    def test_insert_with_no_common_column_gives_hint(self, db):
        with pytest.raises(DbError) as exc:
            db.insert("users", [{"完全不相干": 1}])
        assert "mapping" in (exc.value.hint or "")

    def test_delete_requires_condition(self, db):
        with pytest.raises(DbError) as exc:
            db.delete("users", {})
        assert "条件" in str(exc.value)

    def test_unknown_table_gives_hint(self, db):
        with pytest.raises(DbError) as exc:
            db.query("SELECT * FROM nope")
        assert "ddl" in (exc.value.hint or "").lower() or "建表" in (exc.value.hint or "")

    def test_unique_violation_hint(self, db):
        db.insert("users", [{"name": "A", "email": "x@y.z"}])
        with pytest.raises(DbError) as exc:
            db.insert("users", [{"name": "B", "email": "x@y.z"}])
        assert "唯一约束" in (exc.value.hint or "")

    def test_script_and_truncate(self, db):
        db.insert("users", [{"name": "A", "email": "a@b.c"}])
        db.truncate("users")
        assert db.scalar("SELECT COUNT(*) FROM users") == 0

    def test_execute_returns_affected_rows(self, db):
        db.insert("users", [{"name": "A", "email": "a@b.c"}])
        assert db.execute("UPDATE users SET name = 'B' WHERE id = 1") == 1

    def test_log_records_sql(self, db):
        db.query("SELECT 1 AS v")
        assert db.log and "ms" in db.log[0]


class TestSnapshot:
    def test_snapshot_and_diff(self, db):
        db.insert("users", [{"name": "A", "email": "a@b.c"}, {"name": "B", "email": "b@b.c"}])
        base = db.snapshot("users", key="id", fields=["name", "email"])
        assert base == {"1": {"name": "A", "email": "a@b.c"}, "2": {"name": "B", "email": "b@b.c"}}

        db.execute("UPDATE users SET name = 'A2' WHERE id = 1")
        db.insert("users", [{"name": "C", "email": "c@b.c"}])
        db.delete("users", {"id": 2})
        diff = diff_snapshots(base, db.snapshot("users", key="id", fields=["name", "email"]))
        assert diff["has_diff"]
        assert diff["added"] == ["3"] and diff["removed"] == ["2"]
        assert diff["changed"]["1"]["name"] == {"baseline": "A", "current": "A2"}

    def test_no_diff(self, db):
        db.insert("users", [{"name": "A", "email": "a@b.c"}])
        snap = db.snapshot("users", key="id", fields=["name"])
        assert diff_snapshots(snap, snap)["has_diff"] is False


class TestSeeder:
    def test_seed_and_cleanup_leaves_nothing(self, tmp_path, db):
        ctx = Context(seed=1)
        factory = DataFactory(ctx)
        ledger = tmp_path / "ledger.json"
        seeder = Seeder(db, factory, ctx, ledger_path=ledger)
        counts = seeder.run([{"entity": "dept", "schema": DEPT_SCHEMA, "count": 3, "table": "dept"}])
        assert counts == {"dept": 3}
        assert db.scalar("SELECT COUNT(*) FROM dept") == 3

        deleted = cleanup_from_ledger(db, ledger)
        assert deleted == {"dept": 3}
        assert db.scalar("SELECT COUNT(*) FROM dept") == 0, "造数必须能精确回收，不留垃圾数据"
        assert not ledger.exists()

    def test_seed_reuses_generated_data(self, tmp_path, db):
        ctx = Context(seed=1)
        factory = DataFactory(ctx)
        factory.generate(USER_SCHEMA, count=2)
        seeder = Seeder(db, factory, ctx, ledger_path=tmp_path / "l.json")
        seeder.run([{"entity": "user", "count": 2, "table": "users"}])
        assert db.scalar("SELECT COUNT(*) FROM users") == 2

    def test_seed_without_source_gives_hint(self, tmp_path, db):
        seeder = Seeder(db, DataFactory(Context()), Context(), ledger_path=tmp_path / "l.json")
        with pytest.raises(DbError) as exc:
            seeder.run([{"entity": "ghost", "table": "users"}])
        assert "schema" in (exc.value.hint or "")

    def test_cleanup_without_ledger_is_noop(self, db, tmp_path):
        assert cleanup_from_ledger(db, tmp_path / "none.json") == {}

    def test_partial_ledger_cleanup(self, tmp_path, db):
        db.insert("users", [{"name": "keep", "email": "k@b.c"}])
        ledger = tmp_path / "l.json"
        ledger.write_text('{"users": [{"id": 999}]}', encoding="utf-8")
        assert cleanup_from_ledger(db, ledger) == {"users": 0}
        assert db.scalar("SELECT COUNT(*) FROM users") == 1, "不应误删不在登记里的数据"


class TestDriverSelection:
    def test_memory_path_is_promoted_to_file(self, tmp_path):
        d = Database.from_opts({"driver": "sqlite", "path": ":memory:"}, tmp_path)
        assert "sqlite" in d.label

    def test_non_sqlite_requires_dsn(self, tmp_path):
        with pytest.raises(DbError) as exc:
            Database.from_opts({"driver": "mysql"}, tmp_path)
        assert "dsn" in str(exc.value)

    def test_relative_path_resolved_against_root(self, tmp_path):
        d = Database.from_opts({"driver": "sqlite", "path": "./sub/a.db"}, tmp_path)
        assert str(tmp_path / "sub" / "a.db") in d.label
