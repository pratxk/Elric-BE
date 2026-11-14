"""Auth router (v1.1) — email + password login, access + refresh tokens.

Pinned token contract (see backend/README.md):
- Access token: JWT HS256, 15-minute expiry, returned in JSON as
  `access_token`, sent as `Authorization: Bearer <token>`.
- Refresh token: opaque random token, 7-day expiry, stored server-side
  only as a SHA-256 hash, delivered in the HttpOnly `elric_refresh`
  cookie (SameSite=Lax, Path=/, Secure only when the request is HTTPS —
  localhost dev is HTTP, so Secure is conditional).
- POST /api/auth/refresh rotates: the old token is revoked, a new one
  is issued in the SAME family_id. Presenting an already-rotated or
  expired token revokes the whole family (reuse detection) and forces
  re-login. A completely unknown token hash revokes nothing — 401 only.
- POST /api/auth/logout revokes the presented refresh token.
- POST /api/auth/change-password revokes ALL of the user's refresh
  tokens; the client must redirect to /login (access tokens are
  stateless JWTs, so the client discards its copy).

Login errors are deliberately generic: a wrong password and an unknown
email both return 401 "Invalid email or password" — never reveal whether
an email is registered. Register is the exception: a duplicate email is
a normal 409 there.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.schemas import (
    AuthOut,
    ChangePasswordIn,
    LoginIn,
    RefreshOut,
    RegisterIn,
    UserOut,
)
from app.services import auth_service

router = APIRouter(prefix="/auth")
_bearer = HTTPBearer(auto_error=False)

REFRESH_COOKIE = "elric_refresh"
REFRESH_COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days, matching the token TTL

GENERIC_LOGIN_ERROR = "Invalid email or password"


def _set_refresh_cookie(
    response: Response, request: Request, raw_token: str
) -> None:
    """HttpOnly refresh cookie. Secure only over HTTPS — detected from
    the request scheme, so localhost dev (HTTP) still works while any
    HTTPS deployment (Render) gets the Secure flag."""
    secure = request.url.scheme == "https"
    response.set_cookie(
        REFRESH_COOKIE,
        raw_token,
        max_age=REFRESH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        secure=secure,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/")


def _issue_session(
    response: Response, request: Request, user
) -> tuple[str, str]:
    """Create a fresh refresh token (new family) + access token for a
    login/register. Returns (access_token, raw_refresh_token); the
    caller sets the cookie. The raw refresh token is never logged."""
    access_token = auth_service.issue_access_token(user)
    raw_refresh, _row = auth_service.create_refresh_token(user.id)
    _set_refresh_cookie(response, request, raw_refresh)
    return access_token, raw_refresh


def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Auth guard: a valid access token (Bearer) is required. Anything
    else → 401. Returns a plain dict {id, email, onboarding_completed}
    (never a detached ORM row)."""
    token = credentials.credentials if credentials else None
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    user = auth_service.user_from_access_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return auth_service.public_user_dict(user)


@router.post("/register", response_model=AuthOut, status_code=201)
def register(body: RegisterIn, request: Request, response: Response):
    try:
        user = auth_service.create_user(body.email, body.password)
    except auth_service.DuplicateEmailError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    access_token, _raw = _issue_session(response, request, user)
    return AuthOut(
        access_token=access_token, user=auth_service.public_user_dict(user)
    )


@router.post("/login", response_model=AuthOut)
def login(body: LoginIn, request: Request, response: Response):
    email = auth_service.normalize_email(body.email)
    if auth_service.is_locked_out(email):
        raise HTTPException(
            status_code=429,
            detail="too many failed login attempts; try again in 15 minutes",
        )
    user = auth_service.get_user_by_email(email)
    if user is None or not auth_service.verify_user_password(
        user, body.password
    ):
        # Generic error either way — never reveal whether the email
        # is registered. The failure is still recorded for lockout.
        auth_service.record_failed_attempt(email)
        raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)
    auth_service.clear_failed_attempts(email)
    access_token, _raw = _issue_session(response, request, user)
    return AuthOut(
        access_token=access_token, user=auth_service.public_user_dict(user)
    )


@router.post("/refresh", response_model=RefreshOut)
def refresh(request: Request, response: Response):
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="no refresh token")
    status, new_raw, new_row = auth_service.rotate_refresh_token(raw)
    if status != "ok" or new_raw is None or new_row is None:
        # Reuse (revoked/expired → whole family already revoked) or
        # unknown hash: clear the cookie and force re-login.
        _clear_refresh_cookie(response)
        raise HTTPException(
            status_code=401, detail="invalid or expired refresh token"
        )
    user = auth_service.get_user(new_row.user_id)
    if user is None:  # pragma: no cover — user deleted mid-session
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="invalid or expired session")
    _set_refresh_cookie(response, request, new_raw)
    return RefreshOut(access_token=auth_service.issue_access_token(user))


@router.post("/logout")
def logout(request: Request, response: Response):
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        auth_service.revoke_refresh_token(raw)
    _clear_refresh_cookie(response)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: dict = Depends(require_user)):
    return user


@router.post("/complete-onboarding", response_model=UserOut)
def complete_onboarding(user: dict = Depends(require_user)):
    updated = auth_service.complete_onboarding(user["id"])
    if updated is None:  # pragma: no cover — user deleted mid-session
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return auth_service.public_user_dict(updated)


@router.post("/change-password")
def change_password(
    body: ChangePasswordIn, request: Request, response: Response,
    user: dict = Depends(require_user),
):
    try:
        auth_service.change_user_password(
            user["id"], body.current_password, body.new_password
        )
    except auth_service.InvalidPasswordError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    # Every session, every device: all refresh tokens die. The access
    # token is a stateless JWT and cannot be revoked server-side, so the
    # client must discard it and redirect to /login (15 min max anyway).
    auth_service.revoke_all_refresh_tokens(user["id"])
    _clear_refresh_cookie(response)
    return {
        "ok": True,
        "reauthenticate": True,
        "detail": "Password changed. Please log in again.",
    }
