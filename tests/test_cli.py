"""命令行层的参数映射测试。

重点是 ``--set``：它把命令行字符串翻译成配置树的路径，翻译错了不会报错，
只会静默失效——用户以为改了配置，实际还在跑旧值。这类 bug 极难发现，
所以逐个写法钉死。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forgeqa.cli import _normalize_env_key, _overrides
from forgeqa.config import ForgeConfig
from forgeqa.errors import ForgeQAError


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """一份最小可加载的配置：local 与 staging 两个环境。"""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "env.yaml").write_text(
        yaml.safe_dump(
            {
                "default_env": "local",
                "defaults": {"http": {"retries": 2, "timeout": 15}},
                "envs": {
                    "local": {"base_url": "http://127.0.0.1:8000"},
                    "staging": {"base_url": "https://staging.example.com"},
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return tmp_path


class TestOverridesMapping:
    """``_overrides`` 产出的字典结构必须能被 ForgeConfig 命中。"""

    def test_bare_env_key_goes_to_top_level(self):
        """裸键（base_url）不能塞进 envs. 底下——那是没人读的死键。"""
        assert _overrides(["base_url=http://x:1"]) == {"base_url": "http://x:1"}

    def test_section_key_keeps_dotted_path(self):
        assert _overrides(["db.path=./a.db"]) == {"db": {"path": "./a.db"}}
        assert _overrides(["http.retries=9"]) == {"http": {"retries": 9}}

    def test_env_prefix_is_normalized_to_envs(self):
        """env. 与 envs. 同义；早期版本 env. 会写进死键 env。"""
        assert _overrides(["env.local.base_url=http://x:1"]) == {
            "envs": {"local": {"base_url": "http://x:1"}}
        }

    def test_values_are_coerced(self):
        assert _overrides(["ui.headless=false", "runner.repeat=5"]) == {
            "ui": {"headless": False},
            "runner": {"repeat": 5},
        }

    def test_missing_equals_raises(self):
        with pytest.raises(ForgeQAError, match="key=value"):
            _overrides(["base_url"])

    def test_normalize_env_key_helper(self):
        assert _normalize_env_key("env.local.base_url") == "envs.local.base_url"
        assert _normalize_env_key("envs.local.base_url") == "envs.local.base_url"
        assert _normalize_env_key("base_url") == "base_url"

    def test_empty_and_none(self):
        assert _overrides(None) == {}
        assert _overrides([]) == {}


class TestOverridesActuallyApply:
    """回归：命令行覆盖必须真的改变解析结果（而不只是结构对）。"""

    @pytest.mark.parametrize(
        "raw,probe,expect",
        [
            ("base_url=http://127.0.0.1:9001", "base_url", "http://127.0.0.1:9001"),
            ("envs.local.base_url=http://127.0.0.1:9002", "base_url", "http://127.0.0.1:9002"),
            ("env.local.base_url=http://127.0.0.1:9003", "base_url", "http://127.0.0.1:9003"),
            ("db.path=./zz.db", "db.path", "./zz.db"),
            ("http.retries=9", "http.retries", 9),
            ("http.timeout=99", "http.timeout", 99),
        ],
    )
    def test_override_takes_effect(self, project, raw, probe, expect):
        cfg = ForgeConfig.load(None, env=None, overrides=_overrides([raw]), root=project)
        assert cfg.get(probe) == expect

    def test_override_beats_defaults_layer(self, project):
        """http.* 写在 defaults 段里，--set 必须能盖住它。"""
        base = ForgeConfig.load(None, env=None, overrides={}, root=project)
        assert base.get("http.retries") == 2

        cfg = ForgeConfig.load(None, env=None, overrides=_overrides(["http.retries=9"]), root=project)
        assert cfg.get("http.retries") == 9

    def test_targeting_other_env_does_not_touch_active_env(self, project):
        """改 staging 时 local 不受影响；切到 staging 才生效。"""
        ov = _overrides(["envs.staging.base_url=https://stg.new"])

        local_cfg = ForgeConfig.load(None, env=None, overrides=ov, root=project)
        assert local_cfg.get("base_url") == "http://127.0.0.1:8000"

        stg_cfg = ForgeConfig.load(None, env="staging", overrides=ov, root=project)
        assert stg_cfg.get("base_url") == "https://stg.new"

    def test_empty_pairs_leave_config_intact(self, project):
        cfg = ForgeConfig.load(None, env=None, overrides=_overrides([]), root=project)
        assert cfg.get("base_url") == "http://127.0.0.1:8000"
        assert cfg.get("http.retries") == 2


class TestInitGuard:
    def test_init_refuses_inside_package_dir(self, capsys):
        """在 ForgeQA 源码包目录里跑 init 必须拒绝，防止脚手架污染源码树。"""
        from forgeqa.cli import cmd_init

        class Args:
            root = str(Path(__file__).resolve().parent.parent / "forgeqa")
            set = []
            base_url = None

        assert cmd_init(Args()) == 2          # EXIT_USAGE
        assert "拒绝" in capsys.readouterr().out
