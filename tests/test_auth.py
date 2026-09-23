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


# --------------------------------------------------------------------------- #
# auth 段自身的模板插值
# --------------------------------------------------------------------------- #
class TestAuthSpecInterpolation:
    """auth 段里的凭证常写成 ${os:XXX}（不进仓库），必须真解析。

    回归锁：配置层只做三层合并、不做插值，若这里直接把 spec 交给 requests，
    发出去的是字面量 "${os:XXX}"，服务端一律 401，且报错完全看不出原因。
    """

    def test_bearer_token_from_env(self, monkeypatch):
        monkeypatch.setenv("QA_TOKEN", "tok-abc")
        ctx = Context()
        c = HttpClient(ctx, {"auth": {"type": "bearer", "token": "${os:QA_TOKEN:-UNSET}"}},
                       base_url="http://127.0.0.1:8080")
        assert c.session.headers.get("Authorization") == "Bearer tok-abc"

    def test_unset_env_does_not_leak_template(self, monkeypatch):
        monkeypatch.delenv("QA_TOKEN_NOPE", raising=False)
        ctx = Context()
        c = HttpClient(ctx, {"auth": {"type": "bearer", "token": "${os:QA_TOKEN_NOPE:-UNSET}"}},
                       base_url="http://127.0.0.1:8080")
        sent = str(c.session.headers.get("Authorization"))
        assert "${" not in sent            # 字面量模板绝不能被当成凭证发出去
        assert sent == "Bearer UNSET"      # 哨兵值：配合 skip_if 守卫生效

    def test_basic_credentials_resolved(self, monkeypatch):
        monkeypatch.setenv("QA_USER", "alice")
        monkeypatch.setenv("QA_PASS", "s3cr3t")
        ctx = Context()
        c = HttpClient(ctx, {"auth": {"type": "basic", "username": "${os:QA_USER}",
                                      "password": "${os:QA_PASS}"}},
                       base_url="http://127.0.0.1:8080")
        assert c.session.auth == ("alice", "s3cr3t")

    def test_api_key_header_resolved(self, monkeypatch):
        monkeypatch.setenv("QA_KEY", "k-9")
        ctx = Context()
        c = HttpClient(ctx, {"auth": {"type": "api_key", "name": "X-API-Key",
                                      "value": "${os:QA_KEY}"}},
                       base_url="http://127.0.0.1:8080")
        assert c.session.headers.get("X-API-Key") == "k-9"

    def test_custom_scheme_respected(self, monkeypatch):
        """scheme 非 Bearer（如 Token）时，解析后的 token 也要按 scheme 拼。"""
        monkeypatch.setenv("QA_TOKEN", "t-1")
        ctx = Context()
        c = HttpClient(ctx, {"auth": {"type": "bearer", "token": "${os:QA_TOKEN}",
                                      "scheme": "Token"}},
                       base_url="http://127.0.0.1:8080")
        assert c.session.headers.get("Authorization") == "Token t-1"
