"""造数工厂的测试：可复现、去重、转换、变异、反推 schema。"""
from __future__ import annotations

import pytest
import yaml

from forgeqa.config import Context
from forgeqa.errors import DataError
from forgeqa.factory import (
    FAKE_PHONE_PREFIX, DataFactory, Schema, Transforms, infer_schema, parse_time_bound,
)

USER_SCHEMA = {
    "entity": "user",
    "count": 2,
    "unique": ["email", "username"],
    "fields": [
        {"name": "name", "gen": "faker", "method": "name", "min_len": 2, "max_len": 20},
        {"name": "username", "gen": "pattern", "pattern": "qa_????", "transform": "suffix:${uniq}",
         "min_len": 3, "max_len": 20},
        {"name": "email", "gen": "pattern", "pattern": "@@@@.####",
         "transform": "suffix:${uniq}@example.com"},
        {"name": "phone", "gen": "fake_phone"},
        {"name": "age", "gen": "int", "min": 18, "max": 65},
        {"name": "role", "gen": "choice", "values": ["user", "admin"], "weights": [9, 1]},
        {"name": "vip", "gen": "expr", "value": "${age} >= 30"},
        {"name": "created_at", "gen": "datetime", "start": "-30d", "end": "now",
         "fmt": "%Y-%m-%d %H:%M:%S"},
    ],
}


@pytest.fixture()
def factory() -> DataFactory:
    return DataFactory(Context(seed=20260922, faker_locale="zh_CN"))


class TestGenerate:
    def test_basic_generation(self, factory):
        rows = factory.generate(USER_SCHEMA)
        assert len(rows) == 2
        row = rows[0]
        assert 18 <= row["age"] <= 65
        assert row["role"] in ("user", "admin")
        assert row["phone"].startswith(FAKE_PHONE_PREFIX), "手机号必须是明显虚构的号段"
        assert "@example.com" in row["email"]
        assert row["vip"] in (True, False)

    def test_derived_field_matches_rule(self, factory):
        for row in factory.generate(USER_SCHEMA, count=20):
            assert row["vip"] is (row["age"] >= 30)

    def test_seed_makes_it_reproducible(self):
        a = DataFactory(Context(seed=7)).generate(USER_SCHEMA)[0]
        b = DataFactory(Context(seed=7)).generate(USER_SCHEMA)[0]
        # 随机部分必须可复现
        for key in ("name", "age", "created_at", "role"):
            assert a[key] == b[key], f"固定 seed 下 {key} 应完全一致"
        # 而 ${uniq} 部分是刻意不可复现的：它的职责是让每次运行的数据互不撞车
        assert a["email"] != b["email"]

    def test_uniq_makes_data_unique_across_cases(self):
        a = DataFactory(Context(seed=1)).generate(USER_SCHEMA)[0]
        b = DataFactory(Context(seed=1)).generate(USER_SCHEMA)[0]
        assert a["email"] != b["email"], "不同用例的数据不能撞车"
        assert a["username"] != b["username"]

    def test_unique_enforced_within_batch(self, factory):
        rows = factory.generate({**USER_SCHEMA, "count": 8})
        emails = [r["email"] for r in rows]
        assert len(set(emails)) == len(emails)

    def test_overrides_applied(self, factory):
        row = factory.generate(USER_SCHEMA, count=1, overrides={"role": "admin", "name": "指定名"})[0]
        assert row["role"] == "admin" and row["name"] == "指定名"

    def test_loaded_into_context_for_reference(self, factory):
        factory.generate(USER_SCHEMA)
        assert factory.ctx.get("data.user.name") is not None

    @pytest.mark.parametrize("gen_kwargs,expect", [
        ({"gen": "int", "min": 5, "max": 5}, 5),
        ({"gen": "const", "value": "固定"}, "固定"),
        ({"gen": "seq", "start": 100, "step": 10}, 100),
        ({"gen": "choice", "values": ["only"]}, "only"),
        ({"gen": "enum", "values": ["x"]}, "x"),
    ])
    def test_scalar_generators(self, factory, gen_kwargs, expect):
        rows = factory.generate({"entity": "t", "count": 1,
                                 "fields": [{"name": "v", **gen_kwargs}]})
        assert rows[0]["v"] == expect

    def test_pattern_placeholders(self, factory):
        rows = factory.generate({"entity": "t", "count": 1, "fields": [
            {"name": "v", "gen": "pattern", "pattern": "A?#@-"}]})
        v = rows[0]["v"]
        assert v[0] == "A" and v[2].isdigit() and v[4] == "-"

    def test_list_generator(self, factory):
        rows = factory.generate({"entity": "t", "count": 1, "fields": [
            {"name": "tags", "gen": "list", "count": [2, 3],
             "item": {"gen": "choice", "values": ["a", "b"]}}]})
        assert 2 <= len(rows[0]["tags"]) <= 3

    def test_ref_to_other_entity(self, factory):
        factory.generate({"entity": "dept", "count": 3, "fields": [
            {"name": "id", "gen": "seq", "start": 900}]})
        rows = factory.generate({"entity": "user2", "count": 3, "fields": [
            {"name": "dept_id", "gen": "ref", "entity": "dept", "field": "id"}]})
        assert all(r["dept_id"] in (900, 901, 902) for r in rows)

    def test_ref_before_generation_gives_hint(self, factory):
        with pytest.raises(DataError) as exc:
            factory.generate({"entity": "x", "count": 1, "fields": [
                {"name": "d", "gen": "ref", "entity": "never", "field": "id"}]})
        assert "还没生成" in str(exc.value)

    def test_unknown_gen_gives_hint(self, factory):
        with pytest.raises(DataError) as exc:
            factory.generate({"entity": "x", "count": 1, "fields": [{"name": "v", "gen": "没这个"}]})
        assert "可用 gen" in (exc.value.hint or "")

    def test_unknown_faker_provider_gives_hint(self, factory):
        with pytest.raises(DataError) as exc:
            factory.generate({"entity": "x", "count": 1,
                              "fields": [{"name": "v", "gen": "faker", "method": "no_such"}]})
        assert "Faker" in str(exc.value)

    def test_path_loading_and_missing_file(self, factory, tmp_path):
        p = tmp_path / "x.yaml"
        p.write_text(yaml.safe_dump(USER_SCHEMA, allow_unicode=True), encoding="utf-8")
        assert len(factory.generate(p)) == 2
        with pytest.raises(DataError):
            factory.generate(tmp_path / "none.yaml")

    def test_missing_entity_or_fields(self):
        with pytest.raises(DataError):
            Schema.from_dict({"count": 1})
        with pytest.raises(DataError):
            Schema.from_dict({"entity": "a", "fields": []})


class TestTransforms:
    def test_chain_and_args(self):
        t = Transforms()
        assert t.apply("strip|upper", "  ab ") == "AB"
        assert t.apply("truncate:3", "abcdef") == "abc"
        assert t.apply("suffix:@example.com", "a") == "a@example.com"
        assert t.apply("mask:3:2", "1234567890") == "123*****90"
        assert t.apply("to_int", "3.9") == 3

    def test_fake_phone_is_sequential_and_fictional(self):
        t = Transforms()
        p1, p2 = t.apply("fake_phone", None), t.apply("fake_phone", None)
        assert p1.startswith(FAKE_PHONE_PREFIX) and p1 != p2
        assert len(p1) == 11

    def test_unknown_transform_gives_hint(self):
        with pytest.raises(DataError) as exc:
            Transforms().apply("nope", "x")
        assert "可用" in (exc.value.hint or "")


class TestMutate:
    def test_categories_and_shape(self, factory):
        cases = factory.mutate(USER_SCHEMA)
        cats = {c["category"] for c in cases}
        assert {"boundary", "abnormal", "extreme"} <= cats
        for c in cases:
            assert set(c) >= {"case_id", "entity", "target", "category", "description", "data"}
            assert c["data"] is not None

    def test_boundary_values_cover_min_and_max(self, factory):
        cases = factory.mutate(USER_SCHEMA, categories=["boundary"])
        ages = [c["data"].get("age") for c in cases if c["target"] == "age"]
        assert 18 in ages and 17 in ages and 65 in ages and 66 in ages

    def test_abnormal_includes_missing_required(self, factory):
        cases = factory.mutate(USER_SCHEMA, categories=["abnormal"])
        assert any("缺省" in c["description"] for c in cases)

    def test_string_length_boundaries_from_min_max_len(self, factory):
        cases = factory.mutate(USER_SCHEMA, categories=["boundary"])
        names = {c["description"]: c["data"].get("name") for c in cases if c["target"] == "name"}
        assert any(v and len(v) == 2 for v in names.values())
        assert any(v and len(v) == 1 for v in names.values())

    def test_mutate_reuses_given_base(self, factory):
        base = factory.generate(USER_SCHEMA, count=1)[0]
        base = {k: v for k, v in base.items() if not k.startswith("_")}
        cases = factory.mutate({"entity": "user", "count": 1, "fields": [
            {"name": "age", "gen": "int", "min": 18, "max": 65}]}, base=base,
            categories=["abnormal"])
        assert all(c["data"]["name"] == base["name"] for c in cases)

    def test_no_secrets_leak(self, factory):
        """造数不得出现真实个人信息：手机号/身份证必须是虚构号段。"""
        text = str(factory.generate(USER_SCHEMA))
        assert "1380000" in text


class TestInferSchema:
    def test_infer_from_object(self):
        schema = infer_schema({"id": 1, "name": "张三", "email": "a@b.com",
                               "created_at": "2026-01-01 10:00:00", "tags": ["x"],
                               "meta": {"k": 1}, "flag": True}, entity="u")
        by_name = {f["name"]: f for f in schema["fields"]}
        assert by_name["email"]["gen"] == "pattern"
        assert "@example.com" in by_name["email"]["transform"]
        assert by_name["id"]["gen"] == "seq"
        assert by_name["created_at"]["gen"] == "datetime"
        assert by_name["tags"]["gen"] == "list"
        assert by_name["flag"]["gen"] == "bool"

    def test_infer_from_list_uses_first_item(self):
        schema = infer_schema([{"a": 1}, {"a": 2}], entity="x")
        assert schema["entity"] == "x" and len(schema["fields"]) == 1

    def test_inferred_schema_is_usable(self, factory):
        schema = infer_schema({"id": 7, "name": "张三", "email": "a@b.com", "amount": 1.5})
        rows = factory.generate(schema, count=2)
        assert len(rows) == 2 and all("@" in r["email"] for r in rows)

    def test_infer_rejects_scalar(self):
        with pytest.raises(DataError):
            infer_schema("just a string")


class TestTimeBound:
    @pytest.mark.parametrize("spec,delta_days", [("now", 0), ("-30d", -30), ("-2h", 0)])
    def test_relative(self, spec, delta_days):
        from datetime import datetime
        got = parse_time_bound(spec)
        assert abs((got - datetime.now()).total_seconds() / 86400 - delta_days) < 1

    def test_absolute(self):
        assert parse_time_bound("2026-01-01").year == 2026

    def test_invalid_gives_hint(self):
        with pytest.raises(DataError) as exc:
            parse_time_bound("上次发版")
        assert "now" in (exc.value.hint or "")
