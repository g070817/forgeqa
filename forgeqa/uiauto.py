"""forgeqa.uiauto — Playwright 声明式驱动，用于任意网站的 UI 回归。

适配任意网站的三个关键设计
--------------------------
1. **多策略选择器**：一个目标可以给多个候选定位方式，按顺序尝试，
   谁先命中用谁。写用例的人不需要知道页面的具体实现::

       target: {label: 邮箱, placeholder: 请输入邮箱, css: "#email"}

   也支持简写 ``"text=登录"`` / ``"#submit"`` / ``"//button"``。
2. **动作即数据**：所有交互都是 YAML 里的一行，加站点不改代码。
3. **失败自动取证**：断言失败自动截图 + 记录 DOM 摘要，
   回归报告里直接能看到「挂在哪个界面」。

可选能力：``UIAssert`` 的 ``screenshot_diff`` 做像素级视觉回归（依赖 Pillow）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .assertions import CheckResult, check
from .config import Context
from .errors import DependencyError, UiError

# CSS 元字符：出现这些说明是选择器而不是可读文本
_CSS_META = set("#.[]>=:~+*(),\"")


def _is_plain_text(s: str) -> bool:
    return bool(s) and not (set(s) & _CSS_META)


def normalize_selectors(spec: Any) -> list[str]:
    """把各种简写规范成 Playwright 选择器候选列表，按优先级排列。"""
    if spec is None:
        raise UiError("元素定位为 None", hint="target 必须提供 css/text/label/placeholder 之一")
    if isinstance(spec, Mapping):
        out: list[str] = []
        for key in ("css", "xpath", "text", "label", "placeholder", "role",
                    "testid", "alt", "title", "id", "name"):
            if key not in spec:
                continue
            val = spec[key]
            s = str(val)
            if key == "css":
                out.append(s if s.startswith(("css=", "xpath=", "text=", "role=", "#", ".", "[", "//")) else f"css={s}")
            elif key == "xpath":
                out.append(s if s.startswith("xpath=") else f"xpath={s}")
            elif key == "testid":
                out.append(f"data-testid={s}")
            elif key == "name":
                out.append(f'css=[name="{s}"]')
            else:  # text / label / placeholder / role / alt / title / id：Playwright 原生引擎
                out.append(f"{key}={s}")
        for extra in spec.get("fallback") or spec.get("any") or []:
            out.extend(normalize_selectors(extra))
        if not out:
            raise UiError(f"元素定位对象里没有可识别的策略: {spec!r}",
                          hint="可用键：css / text / label / placeholder / role / testid / xpath")
        return list(dict.fromkeys(out))

    s = str(spec).strip()
    if not s:
        raise UiError("元素定位为空字符串")
    if "=" in s and s.split("=", 1)[0] in (
            "css", "xpath", "text", "role", "id", "label", "placeholder", "alt", "title", "data-testid"):
        return [s]
    if s.startswith("//") or s.startswith("(//") or s.startswith(".."):
        return [f"xpath={s}"]
    if s.startswith("#") or s.startswith(".") or s.startswith("["):
        return [f"css={s}"]
    # 含空格（后代选择器）或 CSS 元字符 → 当成 CSS
    if " " in s or (set(s) & _CSS_META):
        return [f"css={s}"]
    # 纯文本：先按可见文本找，再退回 CSS（同时兼容中文标签和英文 id）
    if _is_plain_text(s):
        return [f"text={s}", f"css={s}"]
    return [f"css={s}"]


def _kw(spec: Mapping[str, Any], key: str) -> Any:
    return spec.get(key)


class UiDriver:
    """Playwright 同步 API 薄封装。一个实例对应一次浏览器会话。"""

    def __init__(self, ctx: Context, opts: Mapping[str, Any], *, artifacts_dir: Path,
                 baseline_dir: Path | None = None, logger=None, baseline_mode: str = "off"):
        self.ctx = ctx
        self.opts = dict(opts or {})
        self.artifacts_dir = Path(artifacts_dir)
        self.baseline_dir = Path(baseline_dir) if baseline_dir else self.artifacts_dir / "ui_baseline"
        self.baseline_mode = baseline_mode
        self.logger = logger
        self.timeout = int(self.opts.get("timeout", 15000))
        self.headless = bool(self.opts.get("headless", True))
        # slow_mo：Playwright 原生参数，每个动作（点击/填值/导航）之间强制间隔 N 毫秒。
        # 有头模式（headless: false）想「看着执行」时必须配它，否则浏览器一闪就跑完。
        self.slow_mo = max(0, int(self.opts.get("slow_mo", 0)))
        self.browser_name = str(self.opts.get("browser", "chromium"))
        self.viewport = dict(self.opts.get("viewport") or {"width": 1440, "height": 900})
        self.screenshot_on_fail = bool(self.opts.get("screenshot_on_fail", True))
        self.locale = str(self.opts.get("locale", "zh-CN"))
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self._frames: list[str] = []
        self.steps: list[dict[str, Any]] = []
        self._started_at = 0.0
        self.visual_diffs: list[dict[str, Any]] = []

    # ---------------- 生命周期 ----------------
    def start(self) -> "UiDriver":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise DependencyError(
                "UI 用例需要 Playwright",
                hint="pip install playwright && playwright install chromium",
            ) from exc
        self._pw = sync_playwright().start()
        return self._launch()

    def _launch(self) -> "UiDriver":
        """start() 的后半段：启动浏览器并建会话。独立成方法，便于测试
        捕获 launch_kwargs（配置了 slow_mo 却没生效是最坑的假象）。"""
        launcher = getattr(self._pw, self.browser_name, None)
        if launcher is None:
            raise UiError(f"不支持的浏览器 {self.browser_name!r}",
                          hint="可用: chromium / firefox / webkit")
        launch_kwargs: dict[str, Any] = {"headless": self.headless}
        if self.slow_mo:
            launch_kwargs["slow_mo"] = self.slow_mo
        if self.browser_name == "chromium":
            launch_kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
        try:
            self._browser = launcher.launch(**launch_kwargs)
        except Exception as exc:
            raise DependencyError(
                f"{self.browser_name} 浏览器未安装或启动失败: {exc}",
                hint=f"playwright install {self.browser_name}",
            ) from exc

        ctx_kwargs: dict[str, Any] = {"viewport": self.viewport, "locale": self.locale}
        if self.opts.get("storage_state") and Path(str(self.opts["storage_state"])).exists():
            ctx_kwargs["storage_state"] = str(self.opts["storage_state"])
        if self.opts.get("extra_http_headers"):
            ctx_kwargs["extra_http_headers"] = dict(self.opts["extra_http_headers"])
        self._context = self._browser.new_context(**ctx_kwargs)
        self._context.set_default_timeout(self.timeout)
        self.page = self._context.new_page()
        self.page.on("dialog", lambda d: d.dismiss())
        self._started_at = time.time()
        return self

    def stop(self, *, keep_state: bool = False) -> None:
        try:
            if keep_state and self._context is not None and self.opts.get("storage_state"):
                Path(str(self.opts["storage_state"])).parent.mkdir(parents=True, exist_ok=True)
                self._context.storage_state(path=str(self.opts["storage_state"]))
        except Exception:
            pass
        for obj, closer in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, closer)()
            except Exception:
                pass
        self._pw = self._browser = self._context = self.page = None

    def __enter__(self) -> "UiDriver":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------------- 定位 ----------------
    def _root(self):
        """当前操作上下文（页面或 iframe）。"""
        if self._frames:
            frame = self.page
            for f in self._frames:
                frame = frame.frame_locator(f)
            return frame
        return self.page

    def locate(self, spec: Any, *, strict: bool = True, timeout: int | None = None,
               state: str | None = None):
        """按候选策略依次尝试，返回第一个在超时内可达的 locator。

        等待状态默认 ``visible``：只等 ``attached`` 会与页面的 autofocus 类脚本竞态
        （典型：WordPress 登录页 wp_attempt_focus() 在 200ms 后聚焦并选中账号框，
        此时对密码框 fill 的值会被写进刚被聚焦的账号框）。需要读隐藏控件时，
        可在 target 对象里显式写 ``state: attached`` 覆盖。
        """
        candidates = normalize_selectors(spec)
        if state is None and isinstance(spec, Mapping):
            state = str(spec.get("state") or "") or None
        state = state or "visible"
        last_err: Exception | None = None
        root = self._root()
        total = timeout or self.timeout
        fast = int(self.opts.get("fallback_probe_timeout", 1200))
        for i, sel in enumerate(candidates):
            # 首选策略等满超时（应对慢渲染），备选策略快速试探（避免多个策略叠加等待）
            per = total if i == 0 else min(fast, total)
            try:
                loc = root.locator(sel).first
                loc.wait_for(state=state, timeout=per)   # type: ignore[arg-type]
                return loc
            except Exception as exc:  # 换下一个策略
                last_err = exc
        if strict:
            raise UiError(
                f"找不到元素，已尝试定位策略: {candidates}",
                hint="页面结构可能已变更：用 `forgeqa run --ui-record` 重新录制，"
                     "或在 target 里补充 label/placeholder/css 等候选策略",
            ) from last_err
        raise UiError(f"元素不可达: {candidates}")

    def count_matching(self, spec: Any, timeout: int | None = None) -> int:
        """按候选策略依次计数，返回第一个非零结果。

        只取首选策略会误伤：例如 target="form input" 既可能是 CSS 也可能被当作文本。
        """
        best = 0
        for sel in normalize_selectors(spec):
            try:
                n = self._root().locator(sel).count()
            except Exception:
                n = 0
            if n:
                return n
            best = max(best, n)
        return best

    def exists(self, spec: Any, timeout: int = 1500) -> bool:
        try:
            self.locate(spec, strict=False, timeout=timeout)
            return True
        except UiError:
            return False

    # ---------------- 动作 ----------------
    def act(self, action: str, spec: Mapping[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"do_{action}", None)
        if handler is None:
            raise UiError(
                f"未知 UI 动作: {action!r}",
                hint="可用动作: " + ", ".join(sorted(
                    k[3:] for k in dir(self) if k.startswith("do_"))),
            )
        started = time.perf_counter()
        detail = ""
        try:
            detail = handler(spec) or ""
        except (UiError, DependencyError):
            raise
        except Exception as exc:
            raise UiError(f"UI 动作 {action} 执行失败: {exc}",
                          hint=self._failure_hint(action, spec)) from exc
        finally:
            self.steps.append({"action": action, "target": _brief_spec(spec),
                               "ms": round((time.perf_counter() - started) * 1000, 1)})
        return {"action": action, "detail": detail}

    def _failure_hint(self, action: str, spec: Mapping[str, Any]) -> str:
        if action in ("fill", "type", "click", "check", "select_option"):
            return ("元素可能存在但不可交互（被遮挡/禁用/在 iframe 内）。"
                    "可加 `force: true`、先 `wait_for`，或用 `frame:` 指定 iframe。")
        if action == "goto":
            return "检查 base_url 与 path 拼接；页面可能需要登录态，先跑登录步骤。"
        return "检查用例 YAML 与页面实际结构是否一致"

    # --- 导航 ---
    def do_goto(self, spec: Mapping[str, Any]) -> str:
        url = self.ctx.resolve(_kw(spec, "url") or _kw(spec, "path") or "/")
        if not str(url).startswith(("http://", "https://")):
            base = str(self.opts.get("base_url", "")).rstrip("/")
            url = base + ("" if str(url).startswith("/") else "/") + str(url)
        self.page.goto(str(url), wait_until=str(spec.get("wait_until", "domcontentloaded")),
                       timeout=int(spec.get("timeout", self.timeout)))
        return str(url)

    def do_reload(self, spec: Mapping[str, Any]) -> str:
        self.page.reload(wait_until=str(spec.get("wait_until", "domcontentloaded")))
        return "reloaded"

    def do_go_back(self, _spec) -> str:
        self.page.go_back()
        return "back"

    def do_go_forward(self, _spec) -> str:
        self.page.go_forward()
        return "forward"

    # --- 交互 ---
    def do_click(self, spec: Mapping[str, Any]) -> str:
        loc = self.locate(_kw(spec, "target") or _kw(spec, "selector") or _kw(spec, "text"))
        loc.click(force=bool(spec.get("force", False)),
                  click_count=int(spec.get("click_count", 1)),
                  timeout=int(spec.get("timeout", self.timeout)))
        return "clicked"

    def do_dblclick(self, spec: Mapping[str, Any]) -> str:
        self.locate(_kw(spec, "target")).dblclick()
        return "dblclicked"

    def do_fill(self, spec: Mapping[str, Any]) -> str:
        """填入值并**读回校验**。

        页面脚本可能在 fill 的「聚焦」与「赋值」之间抢走焦点——WordPress 登录页的
        ``wp_attempt_focus()`` 就是典型：它在 200ms 后清空并聚焦账号框，于是紧接着
        给密码框填的值会落进账号框。只把定位等待改成 visible 挡不住这种竞态（它发生在
        fill 内部），所以填完必须读回；不一致就重填——这类脚本只跑一次，第二次必中。
        """
        loc = self.locate(_kw(spec, "target") or _kw(spec, "selector"))
        value = "" if spec.get("value") is None else str(self.ctx.resolve(spec["value"]))
        timeout = int(spec.get("timeout", self.timeout))
        actual: str | None = None
        for attempt in range(max(1, int(spec.get("verify_attempts", 2)))):
            if attempt:
                # 重填不再走 fill()：它内部靠 insertText，还是依赖焦点，会被同一个
                # 脚本再次干扰。改用 DOM 直写，把值写进目标元素本身，竞态免疫。
                self._set_value_direct(loc, value)
            else:
                loc.fill(value, timeout=timeout)
            actual = self._read_back(loc)
            if actual is None or actual == value:
                break
        if actual is not None and actual != value:
            raise UiError(
                f"填入后值对不上：期望 {value!r}，实际 {actual!r}",
                hint="页面脚本可能在填值中途抢走了焦点；可给该步骤加 verify_attempts: 3，"
                     "或在 fill 前补一个 wait_for 让页面先稳定下来",
            )
        return f"filled({value[:40]})"

    def _read_back(self, loc) -> str | None:
        """读回输入框的值；控件不支持读取（非 input/textarea）时返回 None。"""
        try:
            return loc.input_value(timeout=1200)
        except Exception:
            return None

    def _set_value_direct(self, loc, value: str) -> None:
        """绕开焦点机制直接给元素设值（autofocus 抢焦点对它免疫）。

        只用于 fill 校验失败后的重填：原生 fill 依赖焦点（insertText），
        而 DOM 直写把值写到目标元素上，与「当前焦点在哪」无关。
        """
        loc.evaluate(
            "(el, v) => { el.value = v;"
            " el.dispatchEvent(new Event('input', {bubbles: true})); }",
            value,
        )

    def do_type(self, spec: Mapping[str, Any]) -> str:
        loc = self.locate(_kw(spec, "target"))
        loc.type(str(self.ctx.resolve(spec.get("value", ""))),
                 delay=float(spec.get("delay", 30)))
        return "typed"

    def do_press(self, spec: Mapping[str, Any]) -> str:
        key = str(spec.get("key", "Enter"))
        if spec.get("target"):
            self.locate(spec["target"]).press(key)
        else:
            self.page.keyboard.press(key)
        return f"pressed({key})"

    def do_hover(self, spec: Mapping[str, Any]) -> str:
        self.locate(_kw(spec, "target")).hover()
        return "hovered"

    def do_check(self, spec: Mapping[str, Any]) -> str:
        self.locate(_kw(spec, "target")).check(force=bool(spec.get("force", False)))
        return "checked"

    def do_uncheck(self, spec: Mapping[str, Any]) -> str:
        self.locate(_kw(spec, "target")).uncheck(force=bool(spec.get("force", False)))
        return "unchecked"

    def do_select(self, spec: Mapping[str, Any]) -> str:
        loc = self.locate(_kw(spec, "target"))
        val = self.ctx.resolve(spec.get("value") or spec.get("label") or spec.get("index"))
        if isinstance(val, (int, float)) and "index" in spec:
            loc.select_option(index=int(val))
        else:
            loc.select_option(str(val))
        return f"selected({val})"

    def do_upload(self, spec: Mapping[str, Any]) -> str:
        loc = self.locate(_kw(spec, "target"))
        files = self.ctx.resolve(spec.get("files") or spec.get("value"))
        paths = [str(f) for f in (files if isinstance(files, (list, tuple)) else [files])]
        loc.set_input_files(paths)
        return f"uploaded({paths})"

    def do_drag(self, spec: Mapping[str, Any]) -> str:
        src = self.locate(_kw(spec, "source") or _kw(spec, "from"))
        dst = self.locate(_kw(spec, "target_drop") or _kw(spec, "to"))
        src.drag_to(dst)
        return "dragged"

    def do_scroll(self, spec: Mapping[str, Any]) -> str:
        if spec.get("target"):
            self.locate(spec["target"]).scroll_into_view_if_needed()
            return "scrolled_into_view"
        self.page.mouse.wheel(0, int(spec.get("y", 600)))
        return "wheel"

    def do_focus(self, spec: Mapping[str, Any]) -> str:
        self.locate(_kw(spec, "target")).focus()
        return "focused"

    def do_clear(self, spec: Mapping[str, Any]) -> str:
        self.locate(_kw(spec, "target")).clear()
        return "cleared"

    # --- 等待 ---
    def do_wait_for(self, spec: Mapping[str, Any]) -> str:
        timeout = int(spec.get("timeout", self.timeout))
        if spec.get("target"):
            self.locate(spec["target"], timeout=timeout)
            return "element_ready"
        if spec.get("text"):
            self.page.get_by_text(str(self.ctx.resolve(spec["text"]))).first.wait_for(timeout=timeout)
            return "text_ready"
        if spec.get("url"):
            self.page.wait_for_url(str(self.ctx.resolve(spec["url"])), timeout=timeout)
            return "url_ready"
        if spec.get("load_state"):
            self.page.wait_for_load_state(str(spec["load_state"]), timeout=timeout)
            return "load_state"
        if spec.get("response"):
            self.page.wait_for_response(str(spec["response"]), timeout=timeout)
            return "response"
        self.page.wait_for_timeout(int(spec.get("ms", 500)))
        return "waited"

    def do_sleep(self, spec: Mapping[str, Any]) -> str:
        ms = int(self.ctx.resolve(spec.get("ms", spec.get("seconds", 1) * 1000 if "seconds" in spec else 1000)))
        self.page.wait_for_timeout(ms)
        return f"slept({ms}ms)"

    # --- 上下文 ---
    def do_frame(self, spec: Mapping[str, Any]) -> str:
        """进入 iframe；``frame: null`` 回到主文档。"""
        sel = spec.get("target") or spec.get("frame")
        if sel in (None, "main", "top"):
            self._frames = []
            return "main"
        self._frames = [normalize_selectors(sel)[0]]
        return f"frame({self._frames[0]})"

    def do_exec_js(self, spec: Mapping[str, Any]) -> str:
        script = str(spec.get("script") or spec.get("value") or "return null")
        result = self.page.evaluate(script)
        if spec.get("var"):
            self.ctx.set(str(spec["var"]), result)
        return f"js→{str(result)[:60]}"

    def do_screenshot(self, spec: Mapping[str, Any]) -> str:
        name = str(self.ctx.resolve(spec.get("name", f"shot_{int(time.time())}")))
        path = self.screenshot(name, full_page=bool(spec.get("full_page", True)))
        if spec.get("var"):
            self.ctx.set(str(spec["var"]), str(path))
        return str(path)

    def do_cookie(self, spec: Mapping[str, Any]) -> str:
        """从浏览器取 cookie 注入到后续接口请求（UI 登录 → 接口复用登录态）。"""
        if spec.get("action") == "clear":
            self._context.clear_cookies()
            return "cleared"
        cookies = self._context.cookies()
        for c in cookies:
            self.ctx.set(f"ui_cookie.{c['name']}", c["value"])
        if spec.get("var"):
            self.ctx.set(str(spec["var"]), "; ".join(f"{c['name']}={c['value']}" for c in cookies))
        return f"{len(cookies)} cookies"

    def do_block(self, spec: Mapping[str, Any]) -> str:
        """路由拦截：屏蔽广告/埋点，或 mock 接口返回，降低 UI 回归的外部噪声。"""
        pattern = str(spec.get("url") or "**/*")
        if spec.get("mock") is not None:
            body = self.ctx.resolve(spec["mock"])
            import json as _json

            payload = body if isinstance(body, str) else _json.dumps(body, ensure_ascii=False)
            self.page.route(pattern, lambda route: route.fulfill(
                status=int(spec.get("status", 200)),
                content_type=str(spec.get("content_type", "application/json")),
                body=payload,
            ))
            return f"mock({pattern})"
        self.page.route(pattern, lambda route: route.abort() if spec.get("abort", True) else route.continue_())
        return f"block({pattern})"

    def do_route_free(self, spec: Mapping[str, Any]) -> str:
        self.page.unroute(str(spec.get("url") or "**/*"))
        return "unrouted"

    # ---------------- 取证 ----------------
    def screenshot(self, name: str, *, full_page: bool = True) -> Path:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        path = self.artifacts_dir / f"{safe}.png"
        try:
            self.page.screenshot(path=str(path), full_page=full_page)
        except Exception as exc:  # 页面已崩溃也要给出可用信息
            path.write_text(f"screenshot failed: {exc}", encoding="utf-8")
        return path

    def dom_digest(self, limit: int = 4000) -> str:
        try:
            html = self.page.content()
        except Exception as exc:
            return f"<无法获取 DOM: {exc}>"
        return html[:limit]

    def console_errors(self) -> list[str]:
        return list(getattr(self, "_console_errors", []))

    # ---------------- 视觉回归 ----------------
    def visual_diff(self, name: str, *, threshold: float = 0.02,
                    full_page: bool = True) -> dict[str, Any]:
        """与基线截图做像素比对。``baseline update`` 时写入基线。"""
        self.baseline_dir.mkdir(parents=True, exist_ok=True)
        base_path = self.baseline_dir / f"{name}.png"
        shot = self.screenshot(f"ui_{name}", full_page=full_page)

        if self.baseline_mode == "update" or not base_path.exists():
            import shutil

            shutil.copyfile(shot, base_path)
            result = {"name": name, "status": "baseline_created" if not base_path.exists() else "baseline_updated",
                      "ratio": 0.0, "baseline": str(base_path), "current": str(shot)}
            self.visual_diffs.append(result)
            return result

        try:
            from PIL import Image, ImageChops
        except ImportError:
            result = {"name": name, "status": "skipped", "ratio": 0.0,
                      "reason": "未安装 Pillow，无法做像素比对", "current": str(shot)}
            self.visual_diffs.append(result)
            return result

        a = Image.open(base_path).convert("RGB")
        b = Image.open(shot).convert("RGB")
        if a.size != b.size:
            result = {"name": name, "status": "size_changed", "ratio": 1.0,
                      "baseline_size": a.size, "current_size": b.size,
                      "baseline": str(base_path), "current": str(shot)}
            self.visual_diffs.append(result)
            return result
        diff = ImageChops.difference(a, b)
        bbox = diff.getbbox()
        total = a.size[0] * a.size[1]
        changed = 0
        if bbox:
            gray = diff.convert("L")
            hist = gray.histogram()
            changed = sum(hist[16:])           # 忽略轻微抗锯齿噪声
        ratio = changed / total if total else 0.0
        result = {
            "name": name,
            "status": "changed" if ratio > threshold else "same",
            "ratio": round(ratio, 5),
            "threshold": threshold,
            "diff_bbox": bbox,
            "baseline": str(base_path),
            "current": str(shot),
        }
        if bbox:
            try:
                diff.save(str(self.artifacts_dir / f"ui_{name}_diff.png"))
                result["diff_image"] = str(self.artifacts_dir / f"ui_{name}_diff.png")
            except Exception:
                pass
        self.visual_diffs.append(result)
        return result


def _brief_spec(spec: Mapping[str, Any]) -> str:
    keys = ("target", "selector", "url", "text", "value", "key", "name", "script")
    parts = [f"{k}={str(spec[k])[:40]}" for k in keys if k in spec]
    return ", ".join(parts) or "-"


# --------------------------------------------------------------------------- #
# UI 断言
# --------------------------------------------------------------------------- #
def eval_ui_assertions(driver: UiDriver, specs: Sequence[Any]) -> list[CheckResult]:
    out: list[CheckResult] = []
    for spec in specs or []:
        if isinstance(spec, str):
            spec = {"visible": spec}
        if not isinstance(spec, Mapping):
            raise UiError(f"UI 断言格式错误: {spec!r}",
                          hint="写法：- visible: text=提交成功   或 - {text_contains: {target: '#msg', value: ok}}")
        label = spec.get("label") or ""

        if "visible" in spec:
            ok = driver.exists(spec["visible"])
            out.append(CheckResult(ok, label or "visible", "present", spec["visible"],
                                   "found" if ok else "not found",
                                   "" if ok else f"元素未出现: {spec['visible']}"))
        if "hidden" in spec:
            ok = not driver.exists(spec["hidden"], timeout=1200)
            out.append(CheckResult(ok, label or "hidden", "absent", spec["hidden"],
                                   "absent" if ok else "still visible",
                                   "" if ok else f"元素仍然可见: {spec['hidden']}"))
        if "url_contains" in spec:
            want = str(driver.ctx.resolve(spec["url_contains"]))
            cur = driver.page.url
            out.append(check(cur, "contains", want, target=label or "当前 URL"))
        if "title_contains" in spec:
            out.append(check(driver.page.title(), "contains",
                             str(driver.ctx.resolve(spec["title_contains"])), target=label or "页面标题"))
        if "count" in spec:
            c = spec["count"] if isinstance(spec["count"], Mapping) else {"target": spec["count"]}
            n = driver.count_matching(c["target"])
            out.append(check(n, str(c.get("op", "eq")), int(c.get("value", 1)),
                             target=label or f"{c['target']} 元素数量"))
        if "text_contains" in spec or "text_equals" in spec or "text_regex" in spec:
            t = spec.get("text_contains") or spec.get("text_equals") or spec.get("text_regex")
            op = ("contains" if "text_contains" in spec else
                  "eq" if "text_equals" in spec else "regex")
            target_sel, want = (t.get("target"), t.get("value")) if isinstance(t, Mapping) else (spec.get("target"), t)
            loc = driver.locate(target_sel) if target_sel else driver._root()
            actual = loc.inner_text() if hasattr(loc, "inner_text") else ""
            out.append(check(actual, op, driver.ctx.resolve(want), target=label or f"文本({target_sel})"))
        if "value" in spec:
            v = spec["value"] if isinstance(spec["value"], Mapping) else {"target": spec.get("target"), "value": spec["value"]}
            actual = driver.locate(v["target"]).input_value()
            out.append(check(actual, "eq", driver.ctx.resolve(v["value"]), target=label or "输入框值"))
        if "enabled" in spec:
            loc = driver.locate(spec["enabled"])
            out.append(CheckResult(loc.is_enabled(), label or "enabled", "is_true", True,
                                   loc.is_enabled()))
        if "disabled" in spec:
            loc = driver.locate(spec["disabled"])
            out.append(CheckResult(not loc.is_enabled(), label or "disabled", "is_false", False,
                                   loc.is_enabled()))
        if "checked" in spec:
            loc = driver.locate(spec["checked"])
            out.append(CheckResult(loc.is_checked(), label or "checked", "is_true", True,
                                   loc.is_checked()))
        if "attribute" in spec:
            a = spec["attribute"]
            actual = driver.locate(a["target"]).get_attribute(str(a["name"]))
            out.append(check(actual, str(a.get("op", "eq")), driver.ctx.resolve(a.get("value")),
                             target=label or f"属性 {a['name']}"))
        if "screenshot_diff" in spec:
            s = spec["screenshot_diff"] if isinstance(spec["screenshot_diff"], Mapping) else {"name": spec["screenshot_diff"]}
            res = driver.visual_diff(str(s["name"]), threshold=float(s.get("threshold", 0.02)))
            ok = res["status"] in ("same", "baseline_created", "baseline_updated", "skipped")
            out.append(CheckResult(ok, label or f"视觉({s['name']})", "eq", "same",
                                   res["status"],
                                   "" if ok else f"页面视觉变化 {res['ratio']:.2%} 超过阈值（{res.get('diff_image')}）",
                                   ))
        if not out:
            raise UiError(
                f"UI 断言里没有可识别字段: {spec!r}",
                hint="可用键：visible / hidden / text_contains / text_equals / value / count / "
                     "url_contains / title_contains / enabled / disabled / checked / attribute / screenshot_diff",
            )
    return out
