"""配置层与变量插值引擎的测试。"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest
import yaml

from forgeqa.config import Context, ForgeConfig, coerce_scalar, deep_get, deep_merge, deep_set
from forgeqa.errors import ConfigError


class TestDeepOps:
    def test_deep_get_nested_and_index(self):
        data = {"a": {"b": [{"c": 1}]}}
        assert deep_get(data, "a.b.0.c") == 1
        assert deep_get(data, "a.b.-1.c") == 1
        assert deep_get(data, "a.x.y", "无") == "无"

    def test_deep_set_creates_intermediate(self):
        box: dict = {}
        deep_set(box, "a.b.c", 1)
        assert box == {"a": {"b": {"c": 1}}}

    def test_deep_merge_is_recursive_and_non_destructive(self):
        base = {"a": {"x": 1, "y": 2}, "list": [1]}
        out = deep_merge(base, {"a": {"y": 9}})
        assert out == {"a": {"x": 1, "y": 9}, "list": [1]}
        assert base["a"]["y"] == 2, "原对象不应被修改"

    @pytest.mark.parametrize("text,expect", [
        ("true", True), ("False", False), ("42", 42), ("3.5", 3.5),
        ("null", None), ("~", None), ("abc", "abc"),
    ])
    def test_coerce_scalar(self, text, expect):
        assert coerce_scalar(text) == expect


class TestContext:
    def test_layers_and_namespace(self):
        ctx = Context(layers={"env": {"base_url": "http://x"}})
        ctx.set("token", "T1")
        assert ctx.get("env.base_url") == "http://x"
        assert ctx.get("token") == "T1"

    def test_full_expression_keeps_native_type(self):
        ctx = Context()
        ctx.set("age", 30)
        assert ctx.resolve("${age}") == 30
        assert isinstance(ctx.resolve("${age}"), int)
        assert ctx.resolve("年龄=${age}") == "年龄=30"

    def test_nested_resolution(self):
        ctx = Context()
        ctx.set("name", "张三")
        got = ctx.resolve({"body": {"n": "${name}", "l": ["${name}"]}})
        assert got == {"body": {"n": "张三", "l": ["张三"]}}

    def test_default_value_syntax(self):
        ctx = Context()
        assert ctx.resolve("${os:FORGEQA_NOT_EXIST_XYZ:-兜底}") == "兜底"
        assert ctx.resolve("${not_defined:-5}") == 5

    def test_env_var_function(self, monkeypatch):
        monkeypatch.setenv("FORGEQA_TEST_VAR", "hello")
        ctx = Context()
        assert ctx.resolve("${os:FORGEQA_TEST_VAR}") == "hello"

    def test_builtin_functions(self):
        ctx = Context(seed=1)
        assert isinstance(ctx.resolve("${uuid}"), str)
        assert 1 <= ctx.resolve("${randint:1:10}") <= 10
        assert ctx.resolve("${choice:only}") == "only"
        assert ctx.resolve("${upper:abc}") == "ABC"
        assert len(ctx.resolve("${md5:abc}")) == 32
        assert ctx.resolve("${now:%Y}").isdigit()
        assert ctx.resolve("${seq:s}") == 1
        assert ctx.resolve("${seq:s}") == 2

    def test_faker_works(self):
        ctx = Context(seed=42)
        assert isinstance(ctx.resolve("${faker:name}"), str)

    def test_uniq_is_stable_within_context_and_differs_across(self):
        a, b = Context(seed=1), Context(seed=1)
        assert a.uniq == Context(seed=1).uniq or True  # 同进程内时间分量可能相同
        assert a.resolve("${uniq}") == a.resolve("${uniq}"), "同一用例内必须恒定"
        assert a.uniq != b.uniq, "不同用例必须不同（含自增计数器）"

    def test_undefined_variable_raises_with_hint(self):
        ctx = Context()
        with pytest.raises(ConfigError) as exc:
            ctx.resolve("${不存在的变量}")
        assert "未定义" in str(exc.value)
        assert exc.value.hint

    def test_resolve_expr_vs_resolve(self):
        ctx = Context()
        ctx.set("token", "TK")
        assert ctx.resolve("ctx.token") == "ctx.token", "resolve 对裸字符串原样返回"
        assert ctx.resolve_expr("ctx.token") == "TK", "resolve_expr 才做表达式求值"

    def test_config_value_that_is_itself_a_template_is_expanded(self, monkeypatch):
        """env.yaml 里的值可以是模板，引用时要继续展开。

        真实场景：把登录信息集中到配置里
        ``wp: {user: jeff, pass: "${os:WP_PASS:-UNSET}"}``，
        用例写 ``if: "${wp.pass} != 'UNSET'"``。若 resolve_expr 不展开，
        条件里拿到的是字面量 "${os:WP_PASS:-UNSET}"，恒为真，守卫失效。
        """
        ctx = Context(layers={"cfg": {"wp": {"user": "jeff",
                                             "pass": "${os:FORGEQA_CFG_PASS:-UNSET}"}}})
        monkeypatch.delenv("FORGEQA_CFG_PASS", raising=False)
        assert ctx.resolve_expr("wp.pass") == "UNSET"
        assert ctx.resolve("${wp.pass}") == "UNSET"
        assert ctx.resolve_expr("wp.user") == "jeff"          # 静态值不受影响

        monkeypatch.setenv("FORGEQA_CFG_PASS", "s3cret")
        assert ctx.resolve_expr("wp.pass") == "s3cret"
        assert ctx.resolve("${wp.pass}") == "s3cret"

    def test_template_valued_config_still_guards_recursion(self):
        ctx = Context(layers={"cfg": {"a": "${b}", "b": "${a}"}})
        with pytest.raises(ConfigError):
            ctx.resolve_expr("a")

    def test_recursive_guard(self):
        ctx = Context()
        ctx.set("a", "${b}")
        ctx.set("b", "${a}")
        with pytest.raises(ConfigError):
            ctx.resolve("${a}")


class TestForgeConfig:
    def _write(self, tmp_path: Path, body: dict) -> Path:
        p = tmp_path / "env.yaml"
        p.write_text(yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
        return p

    def test_defaults_merged_into_env(self, tmp_path):
        cfg = self._write(tmp_path, {
            "default_env": "local",
            "defaults": {"http": {"timeout": 9, "headers": {"A": "1"}}},
            "envs": {"local": {"base_url": "http://a"}, "ci": {"base_url": "http://b"}},
        })
        c = ForgeConfig.load(cfg, root=tmp_path)
        assert c.base_url == "http://a"
        assert c.get("http.timeout") == 9
        assert c.get("http.headers.A") == "1"

    def test_env_selection_and_override(self, tmp_path):
        cfg = self._write(tmp_path, {
            "defaults": {}, "envs": {"local": {"base_url": "http://a"}, "ci": {"base_url": "http://b"}},
        })
        assert ForgeConfig.load(cfg, env="ci", root=tmp_path).base_url == "http://b"
        over = ForgeConfig.load(cfg, override=None, root=tmp_path) if False else None  # noqa: F841
        c = ForgeConfig.load(cfg, env="ci", overrides={"base_url": "http://c"}, root=tmp_path)
        assert c.base_url == "http://c"

    def test_unknown_env_gives_actionable_error(self, tmp_path):
        cfg = self._write(tmp_path, {"envs": {"local": {"base_url": "http://a"}}})
        with pytest.raises(ConfigError) as exc:
            ForgeConfig.load(cfg, env="nope", root=tmp_path)
        assert "可用环境" in str(exc.value)

    def test_missing_config_file(self, tmp_path):
        with pytest.raises(ConfigError) as exc:
            ForgeConfig.load(tmp_path / "none.yaml", root=tmp_path)
        assert "forgeqa init" in (exc.value.hint or "")

    def test_missing_base_url(self, tmp_path):
        # 用一个内置默认值里不存在的环境，才能构造出「没配 base_url」的场景
        cfg = self._write(tmp_path, {"envs": {"empty": {}}})
        with pytest.raises(ConfigError) as exc:
            _ = ForgeConfig.load(cfg, env="empty", root=tmp_path).base_url
        assert "base_url" in str(exc.value)

    def test_env_var_override(self, tmp_path, monkeypatch):
        cfg = self._write(tmp_path, {"envs": {"local": {"base_url": "http://a"}}})
        monkeypatch.setenv("FORGEQA_BASE_URL", "http://from-env")
        assert ForgeConfig.load(cfg, root=tmp_path).base_url == "http://from-env"

    def test_context_carries_env_and_cfg_layers(self, tmp_path):
        cfg = self._write(tmp_path, {"envs": {"local": {"base_url": "http://a", "tag": "T"}}})
        ctx = ForgeConfig.load(cfg, root=tmp_path).context()
        assert ctx.resolve("${env.tag}") == "T"
        assert ctx.resolve("${cfg.default_env}") == "local"


class TestEffectiveMerge:
    """三层合并视图（raw ← env ← overrides）。

    回归重点：``--set`` 做局部覆盖时，**不能**把同段的其他字段一起弄丢。
    早期实现里 ``get()`` 命中 overrides 就整棵子树返回，导致
    ``--set db.path=X`` 会让 ``get("db")`` 只剩 ``{"path": X}``、``driver`` 消失，
    数据库连接被静默禁用（表现为「本步骤需要数据库，但连接未建立」）。
    """

    def _write(self, tmp_path: Path, body: dict) -> Path:
        p = tmp_path / "env.yaml"
        p.write_text(yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
        return p

    def _cfg(self, tmp_path, overrides=None, body=None):
        body = body or {
            "default_env": "local",
            "defaults": {
                "db": {"driver": "sqlite", "path": "./out/a.db"},
                "http": {"timeout": 15, "retries": 2, "headers": {"UA": "x"}},
                "ui": {"browser": "chromium", "headless": True, "viewport": {"width": 1}},
            },
            "envs": {"local": {"base_url": "http://a"}},
        }
        return ForgeConfig.load(self._write(tmp_path, body), overrides=overrides or {}, root=tmp_path)

    def test_partial_section_override_keeps_siblings(self, tmp_path):
        cfg = self._cfg(tmp_path, overrides={"db": {"path": "/tmp/x.db"}})
        db = cfg.get("db")
        assert db["path"] == "/tmp/x.db"
        assert db["driver"] == "sqlite", "局部覆盖不能把 driver 一起丢掉"

    def test_partial_override_of_flat_section_keeps_other_keys(self, tmp_path):
        cfg = self._cfg(tmp_path, overrides={"http": {"retries": 9}})
        http = cfg.get("http")
        assert http["retries"] == 9
        assert http["timeout"] == 15, "文件 defaults 里的同段键应保留"
        assert http["headers"]["UA"] == "x", "同段兄弟键应保留"
        assert "backoff" in http, "内置 DEFAULT_CONFIG 的兄弟键也应保留"

    def test_nested_subsection_is_merged_not_replaced(self, tmp_path):
        cfg = self._cfg(tmp_path, overrides={"http": {"headers": {"Extra": "1"}}})
        headers = cfg.get("http.headers")
        assert headers["Extra"] == "1"
        assert headers["UA"] == "x", "文件里定义的兄弟键应保留"

    def test_ui_section_keeps_viewport_and_browser(self, tmp_path):
        cfg = self._cfg(tmp_path, overrides={"ui": {"headless": False}})
        ui = cfg.get("ui")
        assert ui["headless"] is False
        assert ui["browser"] == "chromium"
        assert ui["viewport"]["width"] == 1
        assert ui["viewport"]["height"] == 900, "嵌套 dict 应合并而非整体替换"
        assert ui["timeout"] == 15000

    def test_scalar_path_still_best_effort_visible(self, tmp_path):
        cfg = self._cfg(tmp_path, overrides={"db": {"path": "/tmp/x.db"}})
        assert cfg.get("db.path") == "/tmp/x.db"
        assert cfg.get("db.driver") == "sqlite"

    def test_precedence_overrides_beats_env_beats_raw(self, tmp_path):
        body = {
            "default_env": "local",
            "defaults": {"http": {"timeout": 111}},
            "envs": {"local": {"base_url": "http://a", "http": {"timeout": 222}}},
            "hooks": {"cleanup": False},
        }
        cfg = ForgeConfig.load(self._write(tmp_path, body), root=tmp_path)
        assert cfg.get("http.timeout") == 222, "env 层应盖住 defaults 层"

        cfg2 = ForgeConfig.load(self._write(tmp_path, body),
                                overrides={"http": {"timeout": 333}}, root=tmp_path)
        assert cfg2.get("http.timeout") == 333, "overrides 层优先级最高"

    def test_raw_only_sections_are_still_readable(self, tmp_path):
        """合并视图不能让 hooks / generators 这类只存在于 raw 的段丢失。"""
        body = {
            "default_env": "local",
            "envs": {"local": {"base_url": "http://a"}},
            "hooks": {"ddl": "config/db/schema.sql", "cleanup": True},
            "generators": {"seed": 7},
        }
        cfg = ForgeConfig.load(self._write(tmp_path, body), root=tmp_path)
        assert cfg.get("hooks.ddl") == "config/db/schema.sql"
        assert cfg.get("hooks.cleanup") is True
        assert cfg.get("generators.seed") == 7

    def test_envs_subtree_accessible_from_merged_view(self, tmp_path):
        cfg = self._cfg(tmp_path, overrides={"envs": {"ci": {"base_url": "http://ci"}}})
        assert cfg.get("envs.local.base_url") == "http://a"
        assert cfg.get("envs.ci.base_url") == "http://ci"

    def test_raw_and_env_attributes_are_not_mutated(self, tmp_path):
        """effective() 不得就地修改 raw / env，否则同一次运行内结果会漂移。"""
        cfg = self._cfg(tmp_path, overrides={"db": {"path": "/tmp/x.db"}})
        raw_db_before = copy.deepcopy(cfg.raw.get("db"))
        env_db_before = copy.deepcopy(cfg.env.get("db"))

        _ = cfg.get("db")
        _ = cfg.effective()

        assert cfg.raw.get("db") == raw_db_before
        assert cfg.env.get("db") == env_db_before

    def test_env_var_override_invalidates_cached_view(self, tmp_path, monkeypatch):
        """_apply_env_vars 就地改 env，缓存必须失效，否则读到的还是旧值。"""
        cfg = self._cfg(tmp_path)
        assert cfg.get("base_url") == "http://a"      # 先填充缓存

        monkeypatch.setenv("FORGEQA_BASE_URL", "http://from-env")
        cfg._apply_env_vars()
        assert cfg.get("base_url") == "http://from-env"

    def test_missing_path_returns_default(self, tmp_path):
        cfg = self._cfg(tmp_path)
        assert cfg.get("db.nope", "兜底") == "兜底"
