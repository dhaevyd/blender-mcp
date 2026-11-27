"""Opt-in dashboard Basic auth must not change API bearer or public viewer behavior."""

import base64
import json
import types

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

from blender_mcp import server


def _basic(username="admin", credential=None):
    token = base64.b64encode(f"{username}:{credential or 'password'}".encode()).decode()
    return {"authorization": f"Basic {token}"}


@pytest.fixture(autouse=True)
def _reset_login_state(tmp_path, monkeypatch):
    # Isolate the shared limiter store so counts never leak between tests.
    monkeypatch.setenv("MCP_LOGIN_LIMIT_FILE", str(tmp_path / "login-limits.json"))
    monkeypatch.delenv("MCP_TRUSTED_PROXY", raising=False)
    server._reset_login_limiter()
    yield
    server._reset_login_limiter()


@pytest.fixture
def app(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text(
        '<html><head><link rel="icon" href="/favicon.svg"><link rel="stylesheet" href="/assets/index-B2a3d4e5.css"></head><body><div id="root"></div><script type="module" src="/assets/index-B2a3d4e5.js"></script></body></html>'
    )
    (tmp_path / "favicon.svg").write_text("<svg>icon</svg>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("dashboard bundle")
    (assets / "index-B2a3d4e5.js").write_text("public login bundle")
    (assets / "index-B2a3d4e5.css").write_text("public login styles")
    (assets / "private-B2a3d4e5.map").write_text("private source map")
    monkeypatch.setattr(server, "_dashboard_dist", tmp_path)
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "api-secret")
    monkeypatch.setenv("MCP_ROLE", "stand-alone")
    monkeypatch.delenv("MCP_DASH_AUTH", raising=False)
    monkeypatch.delenv("MCP_USERNAME", raising=False)
    monkeypatch.delenv("MCP_PASSWORD", raising=False)
    return Starlette(
        routes=[
            Route("/", server._root_page),
            Route("/login", server._dashboard_login_page),
            Route("/login/", server._dashboard_login_page),
            Route("/api/v1/dashboard/login", server._dashboard_login, methods=["POST"]),
            Route("/api/v1/dashboard/session", server._dashboard_session),
            Route("/api/v1/dashboard/logout", server._dashboard_logout, methods=["POST"]),
            Mount("/assets", StaticFiles(directory=assets)),
            Route("/health", lambda request: PlainTextResponse("ok")),
            Route("/api/v1/health", lambda request: PlainTextResponse("ok")),
            Route("/api/v1/diagnostics", lambda request: PlainTextResponse("diagnostics")),
            Route("/viewer", lambda request: PlainTextResponse("viewer")),
            Route("/viewer/", lambda request: PlainTextResponse("viewer")),
            Route("/api/v1/viewer/state", lambda request: PlainTextResponse("state")),
            Route("/api/private", lambda request: PlainTextResponse("private")),
            Route("/tool", lambda request: PlainTextResponse("tool")),
            Route("/mcp", lambda request: PlainTextResponse("mcp")),
            Route("/{full_path:path}", server._root_page),
        ],
        middleware=[Middleware(server._ApiKeyMiddleware)],
    )


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def test_disabled_and_unset_auth_keep_dashboard_public(client, monkeypatch):
    for setting in (None, "false", "TRUEish"):
        if setting is None:
            monkeypatch.delenv("MCP_DASH_AUTH", raising=False)
        else:
            monkeypatch.setenv("MCP_DASH_AUTH", setting)
        for path in ("/", "/settings", "/assets/app.js"):
            assert client.get(path, headers={"accept": "text/html"}).status_code == 200
        assert client.get("/api/private").status_code == 401


@pytest.mark.parametrize(
    "username,password", [(None, None), ("admin", None), (None, "password"), (" ", "password"), ("admin", "  ")]
)
def test_enabled_without_valid_config_fails_closed(client, monkeypatch, username, password):
    monkeypatch.setenv("MCP_DASH_AUTH", "true")
    if username is not None:
        monkeypatch.setenv("MCP_USERNAME", username)
    if password is not None:
        monkeypatch.setenv("MCP_PASSWORD", password)
    for path in ("/", "/settings", "/assets/app.js"):
        response = client.get(path, headers=_basic())
        assert response.status_code == 503
        assert "api-secret" not in response.text
    assert client.get("/health").status_code == 200


def test_enabled_challenges_dashboard_and_does_not_leak_secrets(client, monkeypatch):
    monkeypatch.setenv("MCP_DASH_AUTH", "true")
    monkeypatch.setenv("MCP_USERNAME", "admin")
    monkeypatch.setenv("MCP_PASSWORD", "password")
    wrong_headers = [
        {},
        {"authorization": "Basic ???"},
        {"authorization": "Basic YTpi===="},
        {"authorization": "Basic /w=="},
        {"authorization": "Basic YWJj"},
        _basic("other", "password"),
        _basic("admin", "wrong"),
        {"authorization": "Bearer api-secret"},
    ]
    for path in ("/", "/settings", "/new-spa-route", "/assets/app.js"):
        for headers in wrong_headers:
            # Each malformed credential is judged on its own, below the throttle.
            server._reset_login_limiter()
            response = client.get(path, headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == 'Basic realm="Blender MCP Dashboard", charset="UTF-8"'
            assert "api-secret" not in response.text
            assert "password" not in response.text
        assert client.get(path, params={"token": "api-secret"}).status_code == 401
        assert client.get(path, headers=_basic()).status_code == 200


def test_authenticated_html_is_private_no_store(client, monkeypatch):
    monkeypatch.setenv("MCP_DASH_AUTH", "true")
    monkeypatch.setenv("MCP_USERNAME", "admin")
    monkeypatch.setenv("MCP_PASSWORD", "password")
    for path in ("/", "/settings"):
        response = client.get(path, headers=_basic())
        assert 'window.__BLENDER_MCP_API_KEY__="api-secret"' in response.text
        assert response.headers["cache-control"] == "private, no-store"


def test_basic_does_not_authorize_api_or_mcp_and_public_contract_survives(client, monkeypatch):
    monkeypatch.setenv("MCP_DASH_AUTH", "true")
    monkeypatch.setenv("MCP_USERNAME", "admin")
    monkeypatch.setenv("MCP_PASSWORD", "password")
    for path in ("/api/private", "/tool", "/mcp"):
        response = client.get(path, headers={**_basic(), "accept": "text/html"})
        assert response.status_code == 401
        assert "www-authenticate" not in response.headers
        assert (
            client.get(path, headers={"authorization": "Bearer api-secret", "accept": "text/html"}).status_code == 200
        )
    for path in ("/health", "/api/v1/diagnostics", "/viewer", "/api/v1/viewer/state"):
        assert client.get(path).status_code == 200


def test_key_bearing_html_no_store_even_without_basic_auth(client):
    response = client.get("/")
    assert 'window.__BLENDER_MCP_API_KEY__="api-secret"' in response.text
    assert response.headers["cache-control"] == "private, no-store"


def test_api_key_script_injection_is_escaped(client, monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "</script><script>alert(1)</script>")
    response = client.get("/")
    assert response.status_code == 200
    assert "</script><script>alert(1)" not in response.text
    assert "\\u003c/script\\u003e" in response.text


def _enable(monkeypatch):
    monkeypatch.setenv("MCP_DASH_AUTH", "true")
    monkeypatch.setenv("MCP_USERNAME", "admin")
    monkeypatch.setenv("MCP_PASSWORD", "password")


def test_react_login_shell_and_resources_public_without_api_key(client, monkeypatch):
    _enable(monkeypatch)
    for path in ("/login", "/login/"):
        page = client.get(path)
        assert page.status_code == 200
        assert '<div id="root"></div>' in page.text
        assert 'src="/assets/index-B2a3d4e5.js"' in page.text
        assert "window.__BLENDER_MCP_API_KEY__" not in page.text
        assert "api-secret" not in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "script-src 'self'" in page.headers["content-security-policy"]
        # Vite inlines the display faces as data: URLs; blocking them silently
        # drops the login typography to fallback fonts.
        assert "font-src 'self' data:" in page.headers["content-security-policy"]
    for path in ("/assets/index-B2a3d4e5.js", "/assets/index-B2a3d4e5.css"):
        asset = client.get(path)
        assert asset.status_code == 200
        assert "api-secret" not in asset.text
    favicon = client.get("/favicon.svg")
    assert favicon.status_code == 200
    assert "api-secret" not in favicon.text
    assert client.head("/favicon.svg").status_code == 200
    assert client.head("/assets/index-B2a3d4e5.js").status_code == 200
    missing_icon = client.get("/favicon-32x32.png")
    assert missing_icon.status_code == 404
    assert "www-authenticate" not in missing_icon.headers
    assert "api-secret" not in missing_icon.text
    missing_bundle = client.get("/assets/missing-B2a3d4e5.js")
    assert missing_bundle.status_code == 404
    assert "www-authenticate" not in missing_bundle.headers
    assert "api-secret" not in missing_bundle.text
    for path in ("/assets/app.js", "/assets/private-B2a3d4e5.map"):
        assert client.get(path).status_code == 401
    assert client.get("/dashboard-login.js").status_code == 401
    browser = client.get("/settings", headers={"accept": "text/html"}, follow_redirects=False)
    assert browser.status_code == 303
    assert browser.headers["location"] == "/login"


def test_login_session_logout_and_bearer_separation(client, monkeypatch):
    _enable(monkeypatch)
    origin = {"origin": "http://testserver"}
    assert client.get("/api/v1/dashboard/session").status_code == 401
    bad = client.post("/api/v1/dashboard/login", json={"username": "admin", "password": "wrong"}, headers=origin)
    assert bad.status_code == 401
    assert "password" not in bad.text
    assert "set-cookie" not in bad.headers
    good = client.post("/api/v1/dashboard/login", json={"username": "admin", "password": "password"}, headers=origin)
    assert good.status_code == 200
    cookie = good.headers["set-cookie"]
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert "api-secret" not in cookie
    assert "password" not in cookie
    session = client.get("/api/v1/dashboard/session")
    assert session.json() == {"authenticated": True}
    assert "api-secret" not in session.text
    assert client.get("/settings").status_code == 200
    assert client.get("/api/private").status_code == 401
    assert 'window.__BLENDER_MCP_API_KEY__="api-secret"' in client.get("/").text
    assert client.get("/mcp").status_code == 401
    assert client.get("/tool").status_code == 401
    logout = client.post("/api/v1/dashboard/logout", headers=origin)
    assert logout.status_code == 200
    assert "max-age=0" in logout.headers["set-cookie"].lower()
    assert client.get("/settings").status_code == 401


def test_session_cookie_secure_on_https(client, monkeypatch):
    _enable(monkeypatch)
    response = client.post(
        "https://testserver/api/v1/dashboard/login",
        json={"username": "admin", "password": "password"},
        headers={"origin": "https://testserver"},
    )
    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_csrf_and_malformed_login_rejected(client, monkeypatch):
    _enable(monkeypatch)
    endpoint = "/api/v1/dashboard/login"
    payload = {"username": "admin", "password": "password"}
    assert client.post(endpoint, json=payload).status_code == 403
    assert client.post(endpoint, json=payload, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post(endpoint, data=payload, headers={"origin": "http://testserver"}).status_code == 415
    assert (
        client.post(
            endpoint, content=b"{", headers={"origin": "http://testserver", "content-type": "application/json"}
        ).status_code
        == 400
    )
    assert client.get("/api/v1/dashboard/session", headers=_basic()).status_code == 401
    assert client.post("/api/v1/dashboard/logout", headers={"origin": "https://evil.example"}).status_code == 403


def test_cookie_tampering_and_rotation_invalidate_session(client, monkeypatch):
    _enable(monkeypatch)
    origin = {"origin": "http://testserver"}
    response = client.post(
        "/api/v1/dashboard/login", json={"username": "admin", "password": "password"}, headers=origin
    )
    name, value = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)
    client.cookies.set(name, value[:-1] + ("A" if value[-1] != "A" else "B"))
    assert client.get("/settings").status_code == 401
    client.post("/api/v1/dashboard/login", json={"username": "admin", "password": "password"}, headers=origin)
    monkeypatch.setenv("MCP_PASSWORD", "changed")
    assert client.get("/settings").status_code == 401


def test_missing_config_login_fails_closed(client, monkeypatch):
    monkeypatch.setenv("MCP_DASH_AUTH", "true")
    response = client.post(
        "/api/v1/dashboard/login",
        json={"username": "admin", "password": "password"},
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 503
    assert client.get("/login").status_code == 503
    assert client.get("/assets/index-B2a3d4e5.js").status_code == 503


def test_disabled_login_redirects_to_root(client):
    for path in ("/login", "/login/"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_login_with_no_dist_fails_closed(client, monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(server, "_dashboard_dist", tmp_path / "missing")
    response = client.get("/login")
    assert response.status_code == 503
    assert "api-secret" not in response.text


def test_fingerprint_assets_never_grant_api_access(client, monkeypatch):
    _enable(monkeypatch)
    client.post(
        "/api/v1/dashboard/login",
        json={"username": "admin", "password": "password"},
        headers={"origin": "http://testserver"},
    )
    for path in ("/api", "/api/private", "/tool", "/mcp", "/api/v1/other-B2a3d4e5.js"):
        assert client.get(path, headers={"accept": "text/html"}).status_code == 401
    assert client.post("/assets/index-B2a3d4e5.js").status_code == 401


def test_session_expires_after_fifteen_minutes(client, monkeypatch):
    _enable(monkeypatch)
    start = 1_800_000_000
    monkeypatch.setattr(server.time, "time", lambda: start)
    response = client.post(
        "/api/v1/dashboard/login",
        json={"username": "admin", "password": "password"},
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 200
    assert client.get("/settings").status_code == 200
    monkeypatch.setattr(server.time, "time", lambda: start + 901)
    assert client.get("/api/v1/dashboard/session").status_code == 401
    assert client.get("/settings").status_code == 401


def test_public_allowlist_cannot_fall_back_to_key_bearing_dashboard(client, monkeypatch):
    _enable(monkeypatch)
    for path in ("/viewer/", "/api/v1/health"):
        response = client.get(path, follow_redirects=False)
        assert "api-secret" not in response.text
        assert "window.__BLENDER_MCP_API_KEY__" not in response.text


def test_real_asgi_public_routes_do_not_serve_dashboard(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "test-api-key")
    with TestClient(server.asgi_app) as actual:
        assert "window.__BLENDER_MCP_API_KEY__" not in actual.get("/viewer/").text
        health = actual.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"


def test_real_asgi_login_routes_and_authenticated_root(tmp_path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("MCP_ROLE", "stand-alone")
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "fixture-key")
    (tmp_path / "index.html").write_text('<html><head></head><body><div id="root"></div></body></html>')
    monkeypatch.setattr(server, "_dashboard_dist", tmp_path)
    with TestClient(server.asgi_app) as actual:
        for path in ("/login", "/login/"):
            login = actual.get(path)
            assert login.status_code == 200
            assert '<div id="root"></div>' in login.text
            assert "fixture-key" not in login.text
        root = actual.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert root.status_code == 303
        assert root.headers["location"] == "/login"
        assert (
            actual.post(
                "/api/v1/dashboard/login",
                json={"username": "admin", "password": "password"},
                headers={"origin": "http://testserver"},
            ).status_code
            == 200
        )
        dashboard = actual.get("/")
        assert dashboard.status_code == 200
        assert 'window.__BLENDER_MCP_API_KEY__="fixture-key"' in dashboard.text
        assert actual.get("/tool").status_code == 401


def test_auth_routes_registered_before_spa_fallback():
    routes = server.asgi_app.router.routes
    paths = [getattr(route, "path", "") for route in routes]
    fallback_index = paths.index("/{full_path:path}") if "/{full_path:path}" in paths else len(paths)
    for path in (
        "/login",
        "/login/",
        "/api/v1/dashboard/login",
        "/api/v1/dashboard/session",
        "/api/v1/dashboard/logout",
    ):
        assert path in paths
        assert paths.index(path) < fallback_index


EVIL_ORIGIN = "http://localhost:9999"
LOGIN_PATH = "/api/v1/dashboard/login"


def _write_shell(tmp_path):
    (tmp_path / "index.html").write_text('<html><head></head><body><div id="root"></div></body></html>')


@pytest.fixture
def real_app(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ROLE", "stand-alone")
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "fixture-key")
    _write_shell(tmp_path)
    monkeypatch.setattr(server, "_dashboard_dist", tmp_path)
    return TestClient(server.asgi_app)


def test_cross_origin_with_session_cookie_cannot_read_key_bearing_html(real_app, monkeypatch):
    """A page on another localhost port shares the cookie but must not read the key."""
    _enable(monkeypatch)
    with real_app as actual:
        login = actual.post(
            LOGIN_PATH, json={"username": "admin", "password": "password"}, headers={"origin": "http://testserver"}
        )
        assert login.status_code == 200
        response = actual.get("/", headers={"origin": EVIL_ORIGIN, "accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 200
        # Same-origin navigation still gets the key-bearing shell locally...
        assert 'window.__BLENDER_MCP_API_KEY__="fixture-key"' in response.text
        # ...but no CORS grant makes that body readable from another origin.
        assert "access-control-allow-origin" not in response.headers
        assert "access-control-allow-credentials" not in response.headers
        assert "access-control-expose-headers" not in response.headers


def test_legacy_root_key_injection_is_not_cross_origin_readable(real_app, monkeypatch):
    monkeypatch.delenv("MCP_DASH_AUTH", raising=False)
    with real_app as actual:
        response = actual.get("/", headers={"origin": EVIL_ORIGIN})
        assert 'window.__BLENDER_MCP_API_KEY__="fixture-key"' in response.text
        assert "access-control-allow-origin" not in response.headers
        assert "access-control-allow-credentials" not in response.headers


def test_cross_origin_bearer_api_keeps_cors_without_credentials(real_app, monkeypatch):
    monkeypatch.delenv("MCP_DASH_AUTH", raising=False)
    with real_app as actual:
        preflight = actual.options(
            "/api/v1/logs",
            headers={
                "origin": EVIL_ORIGIN,
                "access-control-request-method": "GET",
                "access-control-request-headers": "authorization",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == EVIL_ORIGIN
        assert "access-control-allow-credentials" not in preflight.headers
        response = actual.get("/api/v1/logs", headers={"origin": EVIL_ORIGIN, "authorization": "Bearer fixture-key"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == EVIL_ORIGIN
        assert "access-control-allow-credentials" not in response.headers


@pytest.mark.parametrize("path", ["/api/private", "/tool", "/mcp"])
def test_query_token_never_authorizes_protected_routes(client, monkeypatch, path):
    for dash_auth in (None, "true"):
        if dash_auth is None:
            monkeypatch.delenv("MCP_DASH_AUTH", raising=False)
        else:
            _enable(monkeypatch)
        anonymous = client.get(path, params={"token": "api-secret"}, headers={"accept": "text/html"})
        assert anonymous.status_code == 401
        assert "www-authenticate" not in anonymous.headers
        assert "api-secret" not in anonymous.text
        assert client.get(path, headers={"authorization": "Bearer api-secret"}).status_code == 200


def test_query_token_is_rejected_on_real_asgi_protected_routes(monkeypatch):
    monkeypatch.delenv("MCP_DASH_AUTH", raising=False)
    monkeypatch.setenv("BLENDER_MCP_API_KEY", "fixture-key")
    with TestClient(server.asgi_app) as actual:
        for path in ("/api/v1/logs", "/api/private", "/tool", "/mcp"):
            assert actual.get(path, params={"token": "fixture-key"}).status_code == 401
        assert actual.get("/api/v1/logs", headers={"authorization": "Bearer fixture-key"}).status_code == 200


def _login(client, password=None, origin="http://testserver"):
    supplied = password or "wrong-password"
    return client.post(LOGIN_PATH, json={"username": "admin", "password": supplied}, headers={"origin": origin})


def test_login_failures_are_rate_limited_per_client_and_recover(client, monkeypatch):
    _enable(monkeypatch)
    now = 1_800_000_000
    monkeypatch.setattr(server.time, "time", lambda: now)
    for _ in range(server._LOGIN_MAX_ATTEMPTS):
        assert _login(client).status_code == 401
    blocked = _login(client)
    assert blocked.status_code == 429
    assert blocked.headers["cache-control"] == "no-store"
    retry_after = int(blocked.headers["retry-after"])
    assert 0 < retry_after <= server._LOGIN_WINDOW_SECONDS
    assert "wrong-password" not in blocked.text
    assert "api-secret" not in blocked.text
    # Correct credentials cannot bypass an active limit window.
    assert _login(client, password="password").status_code == 429  # noqa: S106
    monkeypatch.setattr(server.time, "time", lambda: now + server._LOGIN_WINDOW_SECONDS + 1)
    assert _login(client, password="password").status_code == 200  # noqa: S106
    assert client.get("/api/v1/dashboard/session").json() == {"authenticated": True}


def test_login_limiter_is_per_client(app, monkeypatch):
    _enable(monkeypatch)
    now = 1_800_000_000
    monkeypatch.setattr(server.time, "time", lambda: now)
    with TestClient(app, client=("10.1.0.1", 50000)) as attacker:
        for _ in range(server._LOGIN_MAX_ATTEMPTS):
            assert _login(attacker).status_code == 401
        assert _login(attacker).status_code == 429
    with TestClient(app, client=("10.1.0.2", 50000)) as other:
        assert _login(other).status_code == 401


def test_login_limiter_global_cap_blocks_distinct_clients(app, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(server, "_LOGIN_GLOBAL_MAX_ATTEMPTS", 4)
    now = 1_800_000_000
    monkeypatch.setattr(server.time, "time", lambda: now)
    for index in range(4):
        with TestClient(app, client=(f"10.2.0.{index}", 50000)) as attacker:
            assert _login(attacker).status_code == 401
    with TestClient(app, client=("10.2.1.1", 50000)) as fresh:
        blocked = _login(fresh)
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) > 0
        monkeypatch.setattr(server.time, "time", lambda: now + server._LOGIN_WINDOW_SECONDS + 1)
        assert _login(fresh, password="password").status_code == 200  # noqa: S106


def test_login_limiter_state_stays_bounded(monkeypatch):
    monkeypatch.setattr(server, "_LOGIN_MAX_TRACKED_CLIENTS", 4)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    for index in range(20):
        clock[0] += 0.1
        server._record_login_failure(types.SimpleNamespace(client=types.SimpleNamespace(host=f"10.3.0.{index}")))
    assert len(server._login_failures) <= 4
    assert len(server._login_global_failures) <= server._LOGIN_GLOBAL_MAX_ATTEMPTS
    # Stale buckets are pruned once the window passes.
    clock[0] += server._LOGIN_WINDOW_SECONDS + 1
    assert server._login_retry_after(types.SimpleNamespace(client=types.SimpleNamespace(host="10.3.0.19"))) == 0
    assert server._login_failures == {}


# --- Basic fallback shares the login throttle -------------------------------


def test_basic_fallback_is_rate_limited_and_recovers(client, monkeypatch):
    _enable(monkeypatch)
    now = 1_800_000_000
    monkeypatch.setattr(server.time, "time", lambda: now)
    for _ in range(server._LOGIN_MAX_ATTEMPTS):
        assert client.get("/", headers=_basic("admin", "wrong-secret")).status_code == 401
    blocked = client.get("/", headers=_basic("admin", "wrong-secret"))
    assert blocked.status_code == 429
    assert blocked.headers["cache-control"] == "no-store"
    # A 429 must not invite another attempt with a fresh challenge.
    assert "www-authenticate" not in blocked.headers
    retry_after = int(blocked.headers["retry-after"])
    assert 0 < retry_after <= server._LOGIN_WINDOW_SECONDS
    assert "wrong-secret" not in blocked.text
    assert "api-secret" not in blocked.text
    # Correct credentials cannot bypass an active limit window.
    assert client.get("/", headers=_basic()).status_code == 429
    monkeypatch.setattr(server.time, "time", lambda: now + server._LOGIN_WINDOW_SECONDS + 1)
    assert client.get("/", headers=_basic()).status_code == 200


def test_basic_failures_share_one_bucket_with_password_login(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(server.time, "time", lambda: 1_800_000_000)
    for _ in range(server._LOGIN_MAX_ATTEMPTS - 1):
        assert client.get("/", headers=_basic("admin", "wrong-secret")).status_code == 401
    assert (
        client.post(
            LOGIN_PATH, json={"username": "admin", "password": "wrong-secret"}, headers={"origin": "http://testserver"}
        ).status_code
        == 401
    )
    assert client.get("/", headers=_basic()).status_code == 429
    # A successful Basic attempt clears the client bucket again.
    monkeypatch.setattr(server.time, "time", lambda: 1_800_000_000 + server._LOGIN_WINDOW_SECONDS + 1)
    assert client.get("/", headers=_basic()).status_code == 200
    assert server._login_failures == {}


# --- Shared (cross-process) limiter state -----------------------------------


def test_shared_limit_file_enforces_one_cap_for_independent_limiters(tmp_path):
    store = tmp_path / "shared-limits.json"
    first = server._LoginLimiter(store=store)
    second = server._LoginLimiter(store=store)
    now = 1_800_000_000.0
    assert second.retry_after("10.9.0.1", now) == 0
    for _ in range(server._LOGIN_MAX_ATTEMPTS):
        first.record_failure("10.9.0.1", now)
    assert second.retry_after("10.9.0.1", now) > 0
    assert first.retry_after("10.9.0.1", now) > 0
    assert second.retry_after("10.9.0.2", now) == 0
    assert second.retry_after("10.9.0.1", now + server._LOGIN_WINDOW_SECONDS + 1) == 0


def test_shared_limit_file_records_are_bounded_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_LOGIN_MAX_TRACKED_CLIENTS", 3)
    store = tmp_path / "bounded-limits.json"
    limiter = server._LoginLimiter(store=store)
    now = 1_800_000_000.0
    for index in range(10):
        limiter.record_failure(f"10.9.2.{index}", now + index / 10)
    payload = json.loads(store.read_text(encoding="utf-8"))
    assert len(payload["failures"]) == 3
    assert payload["failures"].keys() == {"10.9.2.7", "10.9.2.8", "10.9.2.9"}
    # A single flooding client cannot grow the file without limit either.
    flood = server._LoginLimiter(store=tmp_path / "flood-limits.json")
    for index in range(60):
        flood.record_failure("10.9.3.1", now + index / 100)
    stamps = json.loads((tmp_path / "flood-limits.json").read_text(encoding="utf-8"))["failures"]["10.9.3.1"]
    assert len(stamps) <= max(server._LOGIN_MAX_ATTEMPTS, server._LOGIN_GLOBAL_MAX_ATTEMPTS)
    assert flood.retry_after("10.9.3.1", now + 0.6) > 0


def test_unwritable_limit_file_degrades_to_memory(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("keeps the counter path unusable", encoding="utf-8")
    limiter = server._LoginLimiter(store=blocker / "limits.json")
    now = 1_800_000_000.0
    for _ in range(server._LOGIN_MAX_ATTEMPTS):
        limiter.record_failure("10.9.1.1", now)
    assert limiter.retry_after("10.9.1.1", now) > 0
    assert limiter.retry_after("10.9.1.2", now) == 0


def test_corrupt_limit_file_is_recovered_without_raising(tmp_path):
    store = tmp_path / "corrupt-limits.json"
    store.write_text("{not json", encoding="utf-8")
    limiter = server._LoginLimiter(store=store)
    now = 1_800_000_000.0
    limiter.record_failure("10.9.4.1", now)
    assert limiter.retry_after("10.9.4.1", now) == 0
    assert json.loads(store.read_text(encoding="utf-8"))["failures"] == {"10.9.4.1": [now]}


def test_limit_file_never_stores_credentials(client, monkeypatch, tmp_path):
    _enable(monkeypatch)
    store = tmp_path / "credential-free-limits.json"
    monkeypatch.setenv("MCP_LOGIN_LIMIT_FILE", str(store))
    monkeypatch.setattr(server.time, "time", lambda: 1_800_000_000)
    token = base64.b64encode(b"admin:wrong-secret").decode()
    for _ in range(2):
        assert client.get("/", headers={"authorization": f"Basic {token}"}).status_code == 401
        assert (
            client.post(
                LOGIN_PATH,
                json={"username": "admin", "password": "wrong-secret"},
                headers={"origin": "http://testserver"},
            ).status_code
            == 401
        )
    text = store.read_text(encoding="utf-8")
    assert len(json.loads(text)["failures"]["testclient"]) == 4
    for secret in ("admin", "wrong-secret", token, "password", "api-secret"):
        assert secret not in text
    assert set(json.loads(text)["failures"]) == {"testclient"}


# --- Trusted reverse proxy ---------------------------------------------------


def test_forwarded_headers_ignored_unless_explicitly_trusted(client, monkeypatch):
    _enable(monkeypatch)
    payload = {"username": "admin", "password": "password"}
    # HTTPS browser origin behind a proxy is rejected while the proxy is untrusted.
    proxied = client.post(
        LOGIN_PATH, json=payload, headers={"origin": "https://testserver", "x-forwarded-proto": "https"}
    )
    assert proxied.status_code == 403
    # No Secure cookie on a plain connection even if the header claims HTTPS.
    plain = client.post(LOGIN_PATH, json=payload, headers={"origin": "http://testserver", "x-forwarded-proto": "https"})
    assert plain.status_code == 200
    assert "secure" not in plain.headers["set-cookie"].lower()
    client.cookies.clear()
    # A spoofed X-Forwarded-For cannot mint fresh limiter buckets.
    now = 1_800_000_000
    monkeypatch.setattr(server.time, "time", lambda: now)
    for index in range(server._LOGIN_MAX_ATTEMPTS):
        response = client.get("/", headers={**_basic("admin", "wrong-secret"), "x-forwarded-for": f"10.7.0.{index}"})
        assert response.status_code == 401
    assert (
        client.get("/", headers={**_basic("admin", "wrong-secret"), "x-forwarded-for": "10.7.9.9"}).status_code == 429
    )


def test_trusted_proxy_uses_forwarded_scheme_and_client(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("MCP_TRUSTED_PROXY", "true")
    payload = {"username": "admin", "password": "password"}
    proxied = client.post(
        LOGIN_PATH,
        json=payload,
        headers={"origin": "https://testserver", "x-forwarded-proto": "https"},
    )
    assert proxied.status_code == 200
    cookie = proxied.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    client.cookies.clear()
    # Multiple or malformed proto values are ignored, not guessed at.
    for proto in ("https,http", "https" + " " * 12, "ftp", ""):
        rejected = client.post(
            LOGIN_PATH, json=payload, headers={"origin": "https://testserver", "x-forwarded-proto": proto}
        )
        assert rejected.status_code == 403
    now = 1_800_000_000
    monkeypatch.setattr(server.time, "time", lambda: now)
    for _ in range(server._LOGIN_MAX_ATTEMPTS):
        response = client.get("/", headers={**_basic("admin", "wrong-secret"), "x-forwarded-for": "10.8.0.1"})
        assert response.status_code == 401
    assert (
        client.get("/", headers={**_basic("admin", "wrong-secret"), "x-forwarded-for": "10.8.0.1"}).status_code == 429
    )
    # The last hop is the client identity, so a different proxy peer is a new bucket.
    assert client.get("/", headers={**_basic(), "x-forwarded-for": "10.8.0.2"}).status_code == 200
    assert (
        client.get("/", headers={**_basic("admin", "wrong-secret"), "x-forwarded-for": "10.8.0.2"}).status_code == 401
    )


def test_trusted_proxy_ignores_implausible_forwarded_for(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("MCP_TRUSTED_PROXY", "true")
    monkeypatch.setattr(server.time, "time", lambda: 1_800_000_000)
    for index in range(server._LOGIN_MAX_ATTEMPTS):
        response = client.get("/", headers={**_basic("admin", "wrong-secret"), "x-forwarded-for": f"not-an-ip-{index}"})
        assert response.status_code == 401
    assert (
        client.get("/", headers={**_basic("admin", "wrong-secret"), "x-forwarded-for": "not-an-ip"}).status_code == 429
    )
    # Oversized values are discarded rather than parsed.
    huge = ",".join(["10.6.0.1"] * 200)
    assert client.get("/", headers={**_basic("admin", "wrong-secret"), "x-forwarded-for": huge}).status_code == 429


# --- TLS-terminating reverse proxy: explicit origin allowlist ---------------

PROXY_ORIGIN = "https://blender.d4rkcyber.com"
PROXY_HOST = {"host": "blender.d4rkcyber.com"}


def _proxied_login(client, origin=PROXY_ORIGIN, password=None, host="blender.d4rkcyber.com"):
    """Login as a browser behind a TLS proxy whose ASGI hop is plain HTTP."""
    supplied = password or "password"
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    return client.post(LOGIN_PATH, json={"username": "admin", "password": supplied}, headers=headers)


def _proxied_logout(client, origin=PROXY_ORIGIN):
    return client.post("/api/v1/dashboard/logout", headers={**PROXY_HOST, "origin": origin})


def test_allowlisted_origin_authorizes_login_over_plain_http_hop(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("MCP_DASH_ALLOWED_ORIGINS", PROXY_ORIGIN)
    response = _proxied_login(client)
    assert response.status_code == 200
    assert response.json() == {"authenticated": True}
    cookie = response.headers["set-cookie"].lower()
    assert "blender_dash_session=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    # The session actually works; the proxy origin is the only thing allowlisted.
    assert client.get("/api/v1/dashboard/session", headers=PROXY_HOST).json() == {"authenticated": True}
    assert client.get("/settings").status_code == 200


def test_proxy_origin_still_rejected_without_allowlist(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.delenv("MCP_DASH_ALLOWED_ORIGINS", raising=False)
    response = _proxied_login(client)
    assert response.status_code == 403
    assert response.json() == {"error": "origin not allowed"}
    assert "set-cookie" not in response.headers


def test_unlisted_https_origin_rejected_even_with_allowlist(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("MCP_DASH_ALLOWED_ORIGINS", PROXY_ORIGIN)
    response = _proxied_login(client, origin="https://evil.example")
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    # A sibling subdomain is not covered by an exact-origin allowlist.
    assert _proxied_login(client, origin="https://admin.blender.d4rkcyber.com").status_code == 403


def test_allowlist_requires_an_exact_scheme(client, monkeypatch):
    _enable(monkeypatch)
    # An http:// entry must not authorise an https:// browser origin.
    monkeypatch.setenv("MCP_DASH_ALLOWED_ORIGINS", "http://blender.d4rkcyber.com")
    assert _proxied_login(client).status_code == 403
    # ...and the reverse mismatch is rejected too (the http host is not the
    # request's own origin, so only the allowlist could admit it).
    monkeypatch.setenv("MCP_DASH_ALLOWED_ORIGINS", "https://other.example")
    assert _proxied_login(client, origin="http://other.example").status_code == 403


def test_allowlist_entries_are_normalised(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("MCP_DASH_ALLOWED_ORIGINS", "  HTTPS://Blender.D4rkCyber.com/  ")
    assert _proxied_login(client).status_code == 200
    # An explicit non-default port stays part of the origin.
    monkeypatch.setenv("MCP_DASH_ALLOWED_ORIGINS", "https://Blender.D4rkCyber.com:8443/")
    port_origin = "https://blender.d4rkcyber.com:8443"
    assert _proxied_login(client, origin=port_origin, host="blender.d4rkcyber.com:8443").status_code == 200
    assert _proxied_login(client, origin="https://blender.d4rkcyber.com").status_code == 403
    # A default port is dropped on both sides, so these two agree.
    monkeypatch.setenv("MCP_DASH_ALLOWED_ORIGINS", "https://blender.d4rkcyber.com:443")
    assert _proxied_login(client).status_code == 200


def test_invalid_allowlist_entries_are_ignored(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv(
        "MCP_DASH_ALLOWED_ORIGINS",
        ",".join(
            [
                "https://blender.d4rkcyber.com/dashboard",
                "https://blender.d4rkcyber.com?next=/settings",
                "https://blender.d4rkcyber.com#settings",
                "https://user:pass@blender.d4rkcyber.com",
                "https://*.d4rkcyber.com",
                "ftp://blender.d4rkcyber.com",
                "https://blender d4rkcyber.com",
                "blender.d4rkcyber.com",
                "https://blender.d4rkcyber.com:",
                "https://blender.d4rkcyber.com:not-a-port",
                "",
            ]
        ),
    )
    assert server._dashboard_allowed_origins() == set()
    assert _proxied_login(client).status_code == 403
    # One valid entry among the junk is still honoured.
    monkeypatch.setenv("MCP_DASH_ALLOWED_ORIGINS", "https://*.d4rkcyber.com,https://blender.d4rkcyber.com")
    assert server._dashboard_allowed_origins() == {PROXY_ORIGIN}
    assert _proxied_login(client).status_code == 200


def test_logout_honours_the_allowlist_and_is_read_per_request(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("MCP_DASH_ALLOWED_ORIGINS", PROXY_ORIGIN)
    assert _proxied_login(client).status_code == 200
    # Dropping the allowlist immediately rejects the same proxied login and logout...
    monkeypatch.delenv("MCP_DASH_ALLOWED_ORIGINS")
    assert _proxied_login(client).status_code == 403
    assert _proxied_logout(client).status_code == 403
    # ...and restoring it lets logout clear the cookie.
    monkeypatch.setenv("MCP_DASH_ALLOWED_ORIGINS", PROXY_ORIGIN)
    logout = _proxied_logout(client)
    assert logout.status_code == 200
    assert "max-age=0" in logout.headers["set-cookie"].lower()
    assert client.get("/settings").status_code == 401


def test_allowlist_does_not_excuse_a_missing_origin(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("MCP_DASH_ALLOWED_ORIGINS", PROXY_ORIGIN)
    assert _proxied_login(client, origin=None).status_code == 403
    assert client.post("/api/v1/dashboard/logout", headers=PROXY_HOST).status_code == 403
