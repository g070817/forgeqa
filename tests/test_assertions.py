"""断言算子、JSONPath 子集、结构校验的测试。"""
from __future__ import annotations

import pytest

from forgeqa.assertions import (
    OPS, check, jsonpath, jsonpath_first, loose_eq, validate_schema,
)
from forgeqa.errors import AssertFailed

SAMPLE = {
    "code": 0,
    "data": {
        "list": [
            {"id": 1, "name": "张三", "age": 30, "tags": ["a", "b"]},
            {"id": 2, "name": "李四", "age": 17, "tags": []},
        ],
        "total": 2,
        "meta": {"page": 1, "nested": {"id": 99}},
    },
}


class TestJsonPath:
    def test_root(self):
        assert jsonpath(SAMPLE, "$") == [SAMPLE]

    def test_dot_path(self):
        assert jsonpath_first(SAMPLE, "$.data.total") == 2

    def test_index_and_negative_index(self):
        assert jsonpath_first(SAMPLE, "$.data.list[0].name") == "张三"
        assert jsonpath_first(SAMPLE, "$.data.list[-1].name") == "李四"

    def test_wildcard_expands(self):
        assert jsonpath(SAMPLE, "$.data.list[*].id") == [1, 2]

    def test_object_wildcard(self):
        assert sorted(jsonpath(SAMPLE, "$.data.meta.*"), key=str) == sorted([1, {"id": 99}], key=str)

    def test_recursive_descent(self):
        assert jsonpath(SAMPLE, "$..id") == [1, 2, 99]

    def test_filter(self):
        assert [x["id"] for x in jsonpath(SAMPLE, "$.data.list[?(@.age>18)]")] == [1]
        assert [x["id"] for x in jsonpath(SAMPLE, "$.data.list[?(@.name=='李四')]")] == [2]

    def test_bracket_quoted_key(self):
        assert jsonpath_first(SAMPLE, "$['data']['total']") == 2

    def test_missing_path_returns_empty(self):
        assert jsonpath(SAMPLE, "$.data.nope.deep") == []
        assert jsonpath_first(SAMPLE, "$.nope", "默认") == "默认"

    def test_bad_path_gives_hint(self):
        with pytest.raises(AssertFailed) as exc:
            jsonpath(SAMPLE, "data.total")
        assert "$" in (exc.value.hint or "")


class TestOps:
    @pytest.mark.parametrize("actual,op,expected,ok", [
        (1, "eq", 1, True),
        ("1", "eq", 1, True),          # 宽松相等：字符串数字 vs 数字
        (1.0, "eq", 1, True),
        (2, "ne", 1, True),
        (5, "gt", 3, True), (5, "gte", 5, True),
        (3, "lt", 5, True), (5, "lte", 5, True),
        ("abc", "contains", "b", True),
        ([1, 2], "contains", 2, True),
        ({"a": 1}, "contains", "a", True),
        ("abc", "not_contains", "z", True),
        (2, "in", [1, 2], True),
        (3, "not_in", [1, 2], True),
        ("13800000000", "regex", r"^1\d{10}$", True),
        ("abc", "startswith", "ab", True),
        ("abc", "endswith", "bc", True),
        (None, "is_null", None, True),
        ("", "is_null", None, True),
        ("x", "not_null", None, True),
        (True, "is_true", None, True),
        (False, "is_false", None, True),
        ([1, 2, 3], "len_eq", 3, True),
        ([1, 2, 3], "len_gte", 3, True),
        ([1], "len_lte", 3, True),
        (5, "between", [1, 10], True),
        ([], "empty", None, True),
        ([1], "not_empty", None, True),
        ("s", "type_is", "str", True),
        (1, "type_is", "int", True),
        (True, "type_is", "bool", True),
        (None, "absent", None, True),
        ("x", "present", None, True),
        (1.001, "approx", 1.0, True),
        ({"a": [1]}, "deep_eq", {"a": [1]}, True),
    ])
    def test_passing_ops(self, actual, op, expected, ok):
        assert check(actual, op, expected).passed is ok

    @pytest.mark.parametrize("actual,op,expected", [
        (1, "eq", 2), ("abc", "contains", "z"), (1, "gt", 9), ("a", "regex", "^b"),
        (None, "not_null", None), (True, "type_is", "int"),
    ])
    def test_failing_ops(self, actual, op, expected):
        result = check(actual, op, expected)
        assert not result.passed and result.message

    def test_unknown_op_lists_available(self):
        with pytest.raises(AssertFailed) as exc:
            check(1, "没这个算子")
        assert "可用算子" in (exc.value.hint or "")

    def test_bad_regex_gives_hint(self):
        with pytest.raises(AssertFailed) as exc:
            check("a", "regex", "([")
        assert "正则" in str(exc.value)

    def test_numeric_comparison_on_non_numeric(self):
        r = check("abc", "gt", 1)
        assert not r.passed and "无法数值比较" in r.message

    def test_type_is_does_not_treat_bool_as_int(self):
        assert not check(True, "type_is", "int").passed

    def test_loose_eq(self):
        assert loose_eq(" 1 ", 1) and loose_eq("A", "A") and not loose_eq(1, 2)

    def test_ops_registry_has_chinese_labels(self):
        assert OPS["eq"] == "等于" and all(OPS.values())


class TestValidateSchema:
    def test_object_required_and_types(self):
        errs = validate_schema({"id": 1, "name": "x"},
                               {"type": "object", "required": ["id", "name", "email"],
                                "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}})
        assert any("email" in e and "缺失" in e for e in errs)

    def test_wrong_type(self):
        errs = validate_schema({"id": "abc"}, {"type": "object", "properties": {"id": {"type": "integer"}}})
        assert any("类型应为 integer" in e for e in errs)

    def test_array_items_and_bounds(self):
        spec = {"type": "array", "min_items": 1, "items": {"type": "object", "required": ["id"]}}
        assert validate_schema([{"id": 1}], spec) == []
        assert any("min_items" in e for e in validate_schema([], spec))
        assert any("id" in e for e in validate_schema([{}], spec))

    def test_string_constraints(self):
        spec = {"type": "string", "min_len": 3, "max_len": 5, "pattern": "^a"}
        assert validate_schema("abc", spec) == []
        assert validate_schema("ab", spec)
        assert validate_schema("abcdef", spec)
        assert validate_schema("xyz", spec)

    def test_number_range(self):
        spec = {"type": "integer", "min": 1, "max": 10}
        assert validate_schema(5, spec) == []
        assert validate_schema(0, spec) and validate_schema(11, spec)

    def test_additional_properties_forbidden(self):
        errs = validate_schema({"a": 1, "b": 2},
                               {"type": "object", "properties": {"a": {}},
                                "additional_properties": False})
        assert any("未声明字段" in e for e in errs)

    def test_empty_spec_passes(self):
        assert validate_schema({"any": 1}, {}) == []

    def test_check_matches_schema_op(self):
        r = check({"id": 1}, "matches_schema",
                  {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}})
        assert r.passed
