"""Tests for dashboard authentication."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from power_master.config.manager import ConfigManager
from power_master.dashboard.auth import (
    hash_password,
    sign_session,
    verify_password,
    verify_session,
)


# ---------------------------------------------------------------------------
# Unit tests — password hashing
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    def test_hash_and_verify(self) -> None:
        h = hash_password("my-secure-password")
        assert verify_password("my-secure-password", h)

    def test_wrong_password_rejected(self) -> None:
        h = hash_password("correct-password")
        assert not verify_password("wrong-password", h)

    def test_different_salts(self) -> None:
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # Different salts each time

    def test_empty_hash_rejected(self) -> None:
        assert not verify_password("anything", "")

    def test_malformed_hash_rejected(self) -> None:
        assert not verify_password("anything", "no-colon-here")


# ---------------------------------------------------------------------------
# Unit tests — session signing
# ---------------------------------------------------------------------------


class TestSessionSigning:
    def test_sign_and_verify(self) -> None:
        data = {"authenticated": True, "username": "admin"}
        cookie = sign_session(data, "secret", 3600)
        result = verify_session(cookie, "secret", 3600)
        assert result == data

    def test_tampered_cookie_rejected(self) -> None:
        cookie = sign_session({"authenticated": True}, "secret", 3600)
        tampered = cookie[:-1] + ("a" if cookie[-1] != "a" else "b")
        assert verify_session(tampered, "secret", 3600) is None

    def test_wrong_secret_rejected(self) -> None:
        cookie = sign_session({"authenticated": True}, "secret1", 3600)
        assert verify_session(cookie, "secret2", 3600) is None

    def test_expired_session_rejected(self) -> None:
        cookie = sign_session({"authenticated": True}, "secret", 1)
        time.sleep(1.1)
        assert verify_session(cookie, "secret", 1) is None

    def test_malformed_cookie_rejected(self) -> None:
        assert verify_session("not.valid", "secret", 3600) is None
        assert verify_session("", "secret", 3600) is None


# ---------------------------------------------------------------------------
# Integration tests — middleware + routes
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_config_manager(tmp_path: Path) -> ConfigManager:
    defaults = tmp_path / "config.defaults.yaml"
    defaults.write_text("db:\n  path: ':memory:'\n")
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    return mgr


def _make_authed_app(config, repo, config_manager, password: str):
    """Create an app with auth enabled."""
    from power_master.control.manual_override import ManualOverride
    from power_master.dashboard.app import create_app

    config = config.model_copy(deep=True)
    config.dashboard.auth.password_hash = hash_password(password)
    config.dashboard.auth.session_secret = "test-secret-for-tests"
    app = create_app(config, repo, config_manager=config_manager)
    app.state.manual_override = ManualOverride()
    return app


@pytest.fixture
async def authed_client(repo, settings_config_manager):
    """Test client with auth enabled (password: test-password-123)."""
    app = _make_authed_app(
        settings_config_manager.config, repo, settings_config_manager, "test-password-123"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def unauthed_client(repo, settings_config_manager):
    """Test client with auth disabled (default config)."""
    from power_master.control.manual_override import ManualOverride
    from power_master.dashboard.app import create_app

    config = settings_config_manager.config
    app = create_app(config, repo, config_manager=settings_config_manager)
    app.state.manual_override = ManualOverride()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestAuthDisabled:
    """When no password is set, auth is completely disabled."""

    @pytest.mark.asyncio
    async def test_pages_accessible(self, unauthed_client) -> None:
        resp = await unauthed_client.get("/")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_accessible(self, unauthed_client) -> None:
        resp = await unauthed_client.get("/api/status")
        assert resp.status_code == 200


class TestAuthEnabled:
    """When a password is set, all routes require authentication."""

    @pytest.mark.asyncio
    async def test_login_page_accessible(self, authed_client) -> None:
        resp = await authed_client.get("/login")
        assert resp.status_code == 200
        assert "Sign In" in resp.text

    @pytest.mark.asyncio
    async def test_static_accessible(self, authed_client) -> None:
        resp = await authed_client.get("/static/app.css")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_page_redirects_to_login(self, authed_client) -> None:
        resp = await authed_client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]

    @pytest.mark.asyncio
    async def test_api_returns_401(self, authed_client) -> None:
        resp = await authed_client.get("/api/status")
        assert resp.status_code == 401
        assert resp.json()["error"] == "Authentication required"

    @pytest.mark.asyncio
    async def test_login_success_sets_cookie(self, authed_client) -> None:
        resp = await authed_client.post(
            "/login",
            data={"username": "admin", "password": "test-password-123", "next": "/"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "pm_session" in resp.headers.get("set-cookie", "")

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, authed_client) -> None:
        resp = await authed_client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "next": "/"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "error=" in resp.headers["location"]

    @pytest.mark.asyncio
    async def test_authenticated_access(self, authed_client) -> None:
        # Login first
        login_resp = await authed_client.post(
            "/login",
            data={"username": "admin", "password": "test-password-123", "next": "/"},
            follow_redirects=False,
        )
        # Extract cookie and use it
        cookies = login_resp.cookies
        resp = await authed_client.get("/", cookies=cookies)
        assert resp.status_code == 200
        assert "Power Master" in resp.text

    @pytest.mark.asyncio
    async def test_logout_clears_cookie(self, authed_client) -> None:
        resp = await authed_client.get("/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]
