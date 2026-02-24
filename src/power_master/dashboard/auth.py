"""Dashboard authentication — cookie-based session auth.

Single-user authentication using SHA-256 password hashing with random salt
and HMAC-signed session cookies. No external dependencies.

Auth is disabled when password_hash is empty (the default).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

SALT_LENGTH = 32  # bytes

# Paths that bypass authentication
PUBLIC_PATH_PREFIXES = (
    "/static/",
    "/login",
)

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with a random salt. Returns ``salt_hex:hash_hex``."""
    salt = secrets.token_hex(SALT_LENGTH)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored ``salt_hex:hash_hex`` string."""
    if ":" not in stored_hash:
        return False
    salt, expected = stored_hash.split(":", 1)
    actual = hashlib.sha256((salt + password).encode()).hexdigest()
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# Session cookie signing
# ---------------------------------------------------------------------------


def sign_session(data: dict, secret: str, max_age: int) -> str:
    """Create a signed, timestamped session cookie value.

    Format: ``base64(json).timestamp.hmac_signature``
    """
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    timestamp = str(int(time.time()))
    message = f"{payload}.{timestamp}"
    sig = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{message}.{sig}"


def verify_session(cookie_value: str, secret: str, max_age: int) -> dict | None:
    """Verify and decode a signed session cookie. Returns *None* if invalid."""
    parts = cookie_value.split(".")
    if len(parts) != 3:
        return None

    payload_b64, ts_str, signature = parts

    # Verify signature
    message = f"{payload_b64}.{ts_str}"
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None

    # Check expiry
    try:
        issued_at = int(ts_str)
    except ValueError:
        return None
    if time.time() - issued_at > max_age:
        return None

    # Decode payload
    try:
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------


class AuthMiddleware:
    """ASGI middleware enforcing cookie-based authentication.

    Unauthenticated browser requests → redirect to ``/login``.
    Unauthenticated API requests → 401 JSON.
    """

    def __init__(self, app: ASGIApp, auth_config) -> None:
        self.app = app
        self.auth_config = auth_config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        path = request.url.path

        # Public paths pass through
        if any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Auth disabled (no password set)
        if not self.auth_config.password_hash:
            await self.app(scope, receive, send)
            return

        # Check session cookie
        cookie = request.cookies.get("pm_session")
        if cookie:
            session = verify_session(
                cookie,
                self.auth_config.session_secret,
                self.auth_config.session_max_age_seconds,
            )
            if session and session.get("authenticated"):
                await self.app(scope, receive, send)
                return

        # Not authenticated
        if path.startswith("/api/"):
            response = JSONResponse({"error": "Authentication required"}, status_code=401)
        else:
            response = RedirectResponse(f"/login?next={quote(path)}", status_code=302)
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# Login / logout routes
# ---------------------------------------------------------------------------

auth_router = APIRouter()


@auth_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": request.query_params.get("error", ""),
            "next": request.query_params.get("next", "/"),
        },
    )


@auth_router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> RedirectResponse:
    auth_config = request.app.state.config.dashboard.auth

    if username == auth_config.username and verify_password(password, auth_config.password_hash):
        session_data = {"authenticated": True, "username": username}
        cookie_value = sign_session(
            session_data,
            auth_config.session_secret,
            auth_config.session_max_age_seconds,
        )
        # Only redirect to local paths (prevent open redirect)
        target = next if next.startswith("/") else "/"
        response = RedirectResponse(target, status_code=302)
        response.set_cookie(
            key="pm_session",
            value=cookie_value,
            max_age=auth_config.session_max_age_seconds,
            httponly=True,
            samesite="lax",
            path="/",
        )
        logger.info("Login successful: %s", username)
        return response

    logger.warning("Failed login attempt: %s", username)
    return RedirectResponse(
        f"/login?error=Invalid+credentials&next={quote(next)}",
        status_code=302,
    )


@auth_router.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("pm_session", path="/")
    return response


@auth_router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "change_password.html",
        {
            "request": request,
            "success": request.query_params.get("success", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@auth_router.post("/change-password")
async def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> RedirectResponse:
    auth_config = request.app.state.config.dashboard.auth
    config_manager = request.app.state.config_manager

    if not verify_password(current_password, auth_config.password_hash):
        return RedirectResponse(
            "/change-password?error=Current+password+is+incorrect", status_code=302
        )

    if new_password != confirm_password:
        return RedirectResponse(
            "/change-password?error=New+passwords+do+not+match", status_code=302
        )

    if len(new_password) < 8:
        return RedirectResponse(
            "/change-password?error=Password+must+be+at+least+8+characters", status_code=302
        )

    new_hash = hash_password(new_password)
    if config_manager:
        new_config = config_manager.save_user_config(
            {"dashboard": {"auth": {"password_hash": new_hash}}}
        )
        request.app.state.config = new_config

    logger.info("Password changed for user: %s", auth_config.username)
    return RedirectResponse("/change-password?success=Password+changed+successfully", status_code=302)


# ---------------------------------------------------------------------------
# CLI helper: generate password hash
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import getpass
    import sys

    if "--set-password" in sys.argv:
        username = input("Username [admin]: ").strip() or "admin"
        pw = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")

        if pw != confirm:
            print("Passwords do not match.")
            sys.exit(1)

        if len(pw) < 12:
            print("WARNING: Password should be at least 12 characters for external access.")

        hashed = hash_password(pw)
        print("\nAdd the following to your config.yaml:\n")
        print("dashboard:")
        print("  auth:")
        print(f'    username: "{username}"')
        print(f'    password_hash: "{hashed}"')
    else:
        print("Usage: python -m power_master.dashboard.auth --set-password")
