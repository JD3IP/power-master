"""Dashboard authentication — multi-user cookie-based session auth.

Supports multiple users with role-based permissions (admin / viewer).
Auth is disabled when the users list is empty (the default).

Password hashing: SHA-256 with random salt. No external dependencies.
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
    """Create a signed, timestamped session cookie value."""
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

    message = f"{payload_b64}.{ts_str}"
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None

    try:
        issued_at = int(ts_str)
    except ValueError:
        return None
    if time.time() - issued_at > max_age:
        return None

    try:
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_user(auth_config, username: str):
    """Find a user by username. Returns UserConfig or None."""
    for user in auth_config.users:
        if user.username == username:
            return user
    return None


def get_session(request: Request) -> dict | None:
    """Get the verified session from request cookie, or None."""
    auth_config = request.app.state.config.dashboard.auth
    cookie = request.cookies.get("pm_session")
    if not cookie:
        return None
    return verify_session(cookie, auth_config.session_secret, auth_config.session_max_age_seconds)


def require_admin(request: Request) -> JSONResponse | None:
    """Return a 403 response if the current user is not an admin. None if OK.

    When auth is disabled (no users configured), always allows access.
    """
    auth_config = request.app.state.config.dashboard.auth
    if not auth_config.users:
        return None  # Auth disabled — allow all
    session = get_session(request)
    if session and session.get("role") == "admin":
        return None
    return JSONResponse({"error": "Admin access required"}, status_code=403)


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------


class AuthMiddleware:
    """ASGI middleware enforcing cookie-based authentication.

    Unauthenticated browser requests → redirect to ``/login``.
    Unauthenticated API requests → 401 JSON.
    Stores user info in scope for downstream access.
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

        # Auth disabled (no users configured)
        if not self.auth_config.users:
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
                # Store user info in scope for route access
                scope.setdefault("state", {})
                scope["state"]["username"] = session.get("username", "")
                scope["state"]["user_role"] = session.get("role", "viewer")
                await self.app(scope, receive, send)
                return

        # Not authenticated
        if path.startswith("/api/"):
            response = JSONResponse({"error": "Authentication required"}, status_code=401)
        else:
            response = RedirectResponse(f"/login?next={quote(path)}", status_code=302)
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# Login / logout / change-password routes
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

    # Find matching enabled user
    user = _find_user(auth_config, username)
    if user and user.enabled and verify_password(password, user.password_hash):
        session_data = {
            "authenticated": True,
            "username": user.username,
            "role": user.role,
        }
        cookie_value = sign_session(
            session_data,
            auth_config.session_secret,
            auth_config.session_max_age_seconds,
        )
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
        logger.info("Login successful: %s (role=%s)", user.username, user.role)
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

    # Get current user from session
    session = get_session(request)
    if not session:
        return RedirectResponse("/login", status_code=302)

    username = session.get("username", "")
    user = _find_user(auth_config, username)
    if not user or not verify_password(current_password, user.password_hash):
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

    # Update user's password in config
    new_hash = hash_password(new_password)
    if config_manager:
        users_data = [u.model_dump() for u in auth_config.users]
        for u in users_data:
            if u["username"] == username:
                u["password_hash"] = new_hash
                break
        new_config = config_manager.save_user_config(
            {"dashboard": {"auth": {"users": users_data}}}
        )
        request.app.state.config = new_config

    logger.info("Password changed for user: %s", username)
    return RedirectResponse(
        "/change-password?success=Password+changed+successfully", status_code=302
    )


# ---------------------------------------------------------------------------
# User management API (admin only)
# ---------------------------------------------------------------------------


@auth_router.get("/api/users")
async def list_users(request: Request) -> JSONResponse:
    denied = require_admin(request)
    if denied:
        return denied
    auth_config = request.app.state.config.dashboard.auth
    users = [
        {"username": u.username, "role": u.role, "enabled": u.enabled}
        for u in auth_config.users
    ]
    return JSONResponse({"users": users})


@auth_router.post("/api/users")
async def create_user(request: Request) -> JSONResponse:
    denied = require_admin(request)
    if denied:
        return denied

    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    role = body.get("role", "viewer")

    if not username:
        return JSONResponse({"error": "Username is required"}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters"}, status_code=400)
    if role not in ("admin", "viewer"):
        return JSONResponse({"error": "Role must be 'admin' or 'viewer'"}, status_code=400)

    auth_config = request.app.state.config.dashboard.auth
    if _find_user(auth_config, username):
        return JSONResponse({"error": "Username already exists"}, status_code=409)

    users_data = [u.model_dump() for u in auth_config.users]
    users_data.append({
        "username": username,
        "password_hash": hash_password(password),
        "role": role,
        "enabled": True,
    })

    config_manager = request.app.state.config_manager
    if config_manager:
        new_config = config_manager.save_user_config(
            {"dashboard": {"auth": {"users": users_data}}}
        )
        request.app.state.config = new_config

    logger.info("User created: %s (role=%s)", username, role)
    return JSONResponse({"ok": True, "username": username, "role": role}, status_code=201)


@auth_router.put("/api/users/{username}")
async def update_user(request: Request, username: str) -> JSONResponse:
    denied = require_admin(request)
    if denied:
        return denied

    auth_config = request.app.state.config.dashboard.auth
    user = _find_user(auth_config, username)
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)

    body = await request.json()
    users_data = [u.model_dump() for u in auth_config.users]
    for u in users_data:
        if u["username"] == username:
            if "role" in body and body["role"] in ("admin", "viewer"):
                u["role"] = body["role"]
            if "enabled" in body:
                u["enabled"] = bool(body["enabled"])
            break

    config_manager = request.app.state.config_manager
    if config_manager:
        new_config = config_manager.save_user_config(
            {"dashboard": {"auth": {"users": users_data}}}
        )
        request.app.state.config = new_config

    logger.info("User updated: %s", username)
    return JSONResponse({"ok": True})


@auth_router.delete("/api/users/{username}")
async def delete_user(request: Request, username: str) -> JSONResponse:
    denied = require_admin(request)
    if denied:
        return denied

    # Can't delete yourself
    session = get_session(request)
    if session and session.get("username") == username:
        return JSONResponse({"error": "Cannot delete your own account"}, status_code=400)

    auth_config = request.app.state.config.dashboard.auth
    if not _find_user(auth_config, username):
        return JSONResponse({"error": "User not found"}, status_code=404)

    users_data = [u.model_dump() for u in auth_config.users if u.username != username]

    config_manager = request.app.state.config_manager
    if config_manager:
        new_config = config_manager.save_user_config(
            {"dashboard": {"auth": {"users": users_data}}}
        )
        request.app.state.config = new_config

    logger.info("User deleted: %s", username)
    return JSONResponse({"ok": True})


@auth_router.post("/api/users/{username}/reset-password")
async def reset_user_password(request: Request, username: str) -> JSONResponse:
    denied = require_admin(request)
    if denied:
        return denied

    body = await request.json()
    new_password = body.get("password", "")
    if len(new_password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters"}, status_code=400)

    auth_config = request.app.state.config.dashboard.auth
    if not _find_user(auth_config, username):
        return JSONResponse({"error": "User not found"}, status_code=404)

    users_data = [u.model_dump() for u in auth_config.users]
    for u in users_data:
        if u["username"] == username:
            u["password_hash"] = hash_password(new_password)
            break

    config_manager = request.app.state.config_manager
    if config_manager:
        new_config = config_manager.save_user_config(
            {"dashboard": {"auth": {"users": users_data}}}
        )
        request.app.state.config = new_config

    logger.info("Password reset for user: %s", username)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# CLI helper: generate password hash
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import getpass
    import sys

    if "--set-password" in sys.argv:
        username = input("Username [admin]: ").strip() or "admin"
        role = input("Role [admin]: ").strip() or "admin"
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
        print("    users:")
        print(f'      - username: "{username}"')
        print(f'        password_hash: "{hashed}"')
        print(f'        role: "{role}"')
    else:
        print("Usage: python -m power_master.dashboard.auth --set-password")
