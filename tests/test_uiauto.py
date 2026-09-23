"""uiauto.locate 等待状态的回归测试（本机需装有 playwright chromium）。

背景：locate() 原先用 state="attached" 等待元素，会与页面的 autofocus 类脚本竞态——
WordPress 登录页的 wp_attempt_focus() 会在页面加载约 200ms 后聚焦并选中账号框，
此时对密码框 fill 的值会被写进刚被聚焦的账号框（值串位）。
locate() 现默认等 visible（可交互前提），需要读隐藏控件时可显式写 state: attached。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from forgeqa.config import Context
from forgeqa.uiauto import UiDriver

pytest.importorskip("playwright.sync_api")

# 模拟 WordPress 登录页的 autofocus 行为：页面加载后聚焦并选中第一个输入框
_AUTOFOCUS_PAGE = """
<input id="a"><input id="b">
<script>
  setTimeout(function () {
    var el = document.getElementById('a');
    el.focus(); el.select();
  }, 120);
</script>
"""

_CHROMIUM: bool | None = None


def _chromium_available() -> bool:
    global _CHROMIUM
    if _CHROMIUM is None:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                p.chromium.launch(headless=True).close()
            _CHROMIUM = True
        except Exception:
            _CHROMIUM = False
    return _CHROMIUM


def _driver(tmp_path: Path) -> UiDriver:
    ctx = Context(layers={})
    return UiDriver(
        ctx,
        {"browser": "chromium", "headless": True, "timeout": 10000,
         "viewport": {"width": 800, "height": 600}, "base_url": "https://demo.invalid"},
        artifacts_dir=tmp_path,
    ).start()


@pytest.mark.skipif(not _chromium_available(), reason="本机未安装 playwright chromium")
class TestLocateWaitState:
    def test_fill_wins_over_autofocus_script(self, tmp_path):
        """对第二个输入框 fill 的值不允许被 autofocus 脚本抢进第一个输入框。"""
        d = _driver(tmp_path)
        try:
            d.page.set_content(_AUTOFOCUS_PAGE, wait_until="domcontentloaded")
            d.act("fill", {"target": "#a", "value": "first"})
            d.act("fill", {"target": "#b", "value": "second"})
            assert d.locate("#a").input_value() == "first"
            assert d.locate("#b").input_value() == "second"
        finally:
            d.stop()

    def test_state_attached_can_be_overridden(self, tmp_path):
        """读隐藏控件等场景，允许显式退回 attached。"""
        d = _driver(tmp_path)
        try:
            d.page.set_content(
                '<input id="hidden" style="display:none" value="x">',
                wait_until="domcontentloaded")
            assert d.locate({"css": "#hidden", "state": "attached"}).input_value() == "x"
        finally:
            d.stop()
