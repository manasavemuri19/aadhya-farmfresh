"""AAD-SEC-017 (TrustedHostMiddleware, no allowlist existed at all),
AAD-SEC-018 (Strict-Transport-Security header, missing entirely), and
AAD-SEC-019 (health/root routes leaking `env` and the service version to
anyone, unauthenticated).
"""

from __future__ import annotations

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import deps
from app.core.config import Settings


@pytest.fixture
async def client(engine):
    """Same real-ASGI-app pattern test_http_layer.py's own `client` fixture
    uses — see that file's docstring for why this, rather than calling
    services directly, is what proves middleware behaviour."""
    from app.main import app as real_app

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def override_db_session(request: Request):
        session = factory()
        request.state.db_session = session
        yield session

    real_app.dependency_overrides[deps.db_session] = override_db_session
    transport = ASGITransport(app=real_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    real_app.dependency_overrides.pop(deps.db_session, None)


class TestSecurityHeaders:
    async def test_hsts_header_is_present_on_a_real_response(self, client):
        resp = await client.get("/v1/health/live")
        assert resp.headers.get("Strict-Transport-Security") == (
            "max-age=31536000; includeSubDomains"
        )

    async def test_hsts_is_present_on_an_error_response_too(self, client):
        resp = await client.get("/v1/orders")  # unauthenticated -> 401
        assert resp.status_code == 401
        assert "Strict-Transport-Security" in resp.headers

    async def test_the_existing_baseline_headers_are_all_still_present(self, client):
        """Regression guard: adding HSTS must not have displaced anything
        SecurityHeadersMiddleware already set."""
        resp = await client.get("/v1/health/live")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "no-referrer"
        assert resp.headers.get("Cache-Control") == "no-store"
        assert "Content-Security-Policy" in resp.headers


class TestTrustedHostMiddleware:
    async def test_the_test_suites_own_host_header_is_accepted(self, client):
        """If this fails, every other test in the suite would too — proves
        the default allowlist genuinely includes what ASGITransport sends,
        not just that the middleware exists."""
        resp = await client.get("/v1/health/live")
        assert resp.status_code == 200

    async def test_a_forged_host_header_is_rejected(self, client):
        resp = await client.get(
            "/v1/health/live", headers={"Host": "evil-forged-host.example.com"}
        )
        assert resp.status_code == 400

    async def test_a_forged_host_header_with_a_port_is_also_rejected(self, client):
        resp = await client.get("/v1/health/live", headers={"Host": "evil.example.com:8000"})
        assert resp.status_code == 400


class TestAllowedHostsConfig:
    def test_the_default_list_has_no_wildcard(self):
        assert "*" not in Settings().allowed_host_list

    def test_allowed_host_list_parses_the_comma_separated_string(self):
        s = Settings(allowed_hosts="a.example.com, b.example.com ,c.example.com")
        assert s.allowed_host_list == ["a.example.com", "b.example.com", "c.example.com"]

    def test_a_wildcard_is_rejected_by_deploy_safe(self):
        kwargs = {
            "jwt_secret": "a" * 32,
            "database_url": "postgresql://user:pass@prod-db.internal:5432/aadhya",
            "google_web_client_id": "x",
            "google_android_client_id": "y",
            "cors_origins": "https://app.example.com",
            "allowed_hosts": "*",
        }
        with pytest.raises(RuntimeError, match="ALLOWED_HOSTS must not contain a wildcard"):
            Settings(env="production", **kwargs).assert_deploy_safe()

    def test_a_leftover_local_host_is_rejected_by_deploy_safe(self):
        kwargs = {
            "jwt_secret": "a" * 32,
            "database_url": "postgresql://user:pass@prod-db.internal:5432/aadhya",
            "google_web_client_id": "x",
            "google_android_client_id": "y",
            "cors_origins": "https://app.example.com",
            "allowed_hosts": "localhost,app.example.com",
        }
        with pytest.raises(RuntimeError, match="ALLOWED_HOSTS must not contain a local/test host"):
            Settings(env="production", **kwargs).assert_deploy_safe()

    def test_a_real_host_alone_passes(self):
        """env=staging, not production, so the default mock payment
        provider doesn't also trip assert_deploy_safe's unrelated
        AAD-OPS-007 check — this test is only about ALLOWED_HOSTS."""
        kwargs = {
            "jwt_secret": "a" * 32,
            "database_url": "postgresql://user:pass@prod-db.internal:5432/aadhya",
            "google_web_client_id": "x",
            "google_android_client_id": "y",
            "cors_origins": "https://app.example.com",
            "allowed_hosts": "app.example.com",
        }
        Settings(env="staging", **kwargs).assert_deploy_safe()


class TestHealthAndRootDoNotLeakInfo:
    async def test_health_live_no_longer_reports_env(self, client):
        resp = await client.get("/v1/health/live")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"status": "ok"}
        assert "env" not in body

    async def test_root_no_longer_reports_service_name_or_version(self, client):
        resp = await client.get("/")
        assert resp.status_code == 204
        assert resp.text == ""
