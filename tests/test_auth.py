"""配置级登录引导（bootstrap_auth）的单元测试。

覆盖三件事：类型门槛（只有 login/form/bearer + login 段才引导）、
预备请求的执行顺序（CSRF / test cookie 必须先拿到）、
以及登录失败必须显式报错——静默带着匿名身份继续，会把后面的 401 误判成接口坏了。
"""
from __future__ import annotations

import pytest

from forgeqa.config import Context
from forgeqa.errors import HttpError
from forgeqa.httpclient import HttpClient, Response, supports_bootstrap


def _resp(status: int = 200, text: str = "{}") -> Response:
    return Response(status=status, headers={}, text=text, url="http://127.0.0.1:8080/",
                    method="POST", elapsed_ms=1)


class _StubRequest:
    """替换 HttpClient.request：记录调用，按序吐预置响应（不碰网络）。"""

    def __init__(self, responses: list[Response]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, **_kwargs) -> Response:
        self.calls.append((method, path))
        if self.responses:
            return self.responses.pop(0)
        return _resp()


def _client(spec, responses):
    ctx = Context()
    client = HttpClient(ctx, {"auth": spec}, base_url="http://127.0.0.1:8080")
    stub = _StubRequest(responses)
    client.request = stub            # type: ignore[method-assign]
    return ctx, client, stub


# --------------------------------------------------------------------------- #
# 类型门槛
# --------------------------------------------------------------------------- #
class TestSupportsBootstrap:
    @pytest.mark.parametrize("spec, expect", [
        ({"type": "login", "login": {"path": "/wp-login.php"}}, True),
        ({"type": "form", "login": {"path": "/wp-login.php"}}, True),
        ({"type": "bearer", "login": {"path": "/api/token"}}, True),
        ({"type": "LOGIN", "login": {"path": "/x"}}, True),      # 大小写不敏感
        ({"type": "login"}, False),                              # 有类型没 login 段
        ({"type": "none", "login": {"path": "/x"}}, False),      # 显式不登录
        ({}, False),
        (None, False),
    ])
    def test_matrix(self, spec, expect):
        assert supports_bootstrap(spec) is expect


class TestBootstrapAuth:
    def test_prepare_runs_before_login(self):
        """WordPress 要求先拿 wordpress_test_cookie，缺了会直接拒绝登录。"""
        spec = {"type": "login", "login": {
            "prepare": [{"method": "GET", "path": "/wp-login.php"}],
            "method": "POST", "path": "/wp-login.php",
            "data": {"log": "admin", "pwd": "x"},
        }}
        _ctx, client, stub = _client(spec, [_resp(), _resp(302)])
        client.bootstrap_auth()
        assert stub.calls == [("GET", "/wp-login.php"), ("POST", "/wp-login.php")]

    def test_expect_status_raises_on_bad_credentials(self):
        spec = {"type": "login", "login": {
            "path": "/wp-login.php", "data": {"log": "a", "pwd": "wrong"},
            "expect": {"status": 302}}}
        _ctx, client, _stub = _client(spec, [_resp(200, "错误：密码不正确")])
        with pytest.raises(HttpError) as excinfo:
            client.bootstrap_auth()
        assert "登录失败" in str(excinfo.value)

    def test_expect_status_accepts_list(self):
        spec = {"type": "login", "login": {
            "path": "/login", "expect": {"status": [200, 302]}}}
        _ctx, client, _stub = _client(spec, [_resp(302)])
        client.bootstrap_auth()                       # 不抛异常即通过

    def test_no_login_configured_is_noop(self):
        _ctx, client, stub = _client({"type": "none"}, [])
        assert client.bootstrap_auth() == {}
        assert stub.calls == []

    def test_extract_lands_in_context(self):
        spec = {"type": "login", "login": {
            "path": "/api/token", "json": {"u": "a", "p": "b"},
            "extract": {"token": "$.data.token"}}}
        ctx, client, _stub = _client(spec, [_resp(200, '{"data": {"token": "T-123"}}')])
        found = client.bootstrap_auth()
        assert found["token"] == "T-123"
        assert ctx.get("login.token") == "T-123"
