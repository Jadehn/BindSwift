"""Passwords, 2FA, sessions, CSRF, encryption, rate limits."""
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request

from . import db
from .config import get_settings

ph = PasswordHasher()  # argon2id with the library's current recommended parameters
SESSION_COOKIE = "__Host-cc_admin"  # __Host- prefix: Secure, Path=/, no Domain; can't be set by subdomains
DEV_COOKIE = "cc_admin_dev"         # used only when ENV=development over plain http://localhost

# A real hash to compare against when the email doesn't exist, so response time
# doesn't reveal which admin emails are valid.
_DUMMY_HASH = ph.hash(secrets.token_urlsafe(16))


# ---------------- encryption at rest ----------------
def fernet() -> Fernet:
    return Fernet(get_settings().data_key.encode())


def encrypt(data: bytes) -> bytes:
    return fernet().encrypt(data)


def decrypt(token: bytes) -> bytes:
    return fernet().decrypt(bytes(token))


# ---------------- request helpers ----------------
def client_ip(request: Request) -> str:
    """The visitor's real IP. On Vercel every request arrives from Vercel's proxy, so without this
    all visitors would share one IP: one attacker could trip the login lockout and the submission
    rate limit for everyone. Vercel overwrites x-vercel-forwarded-for itself, so it can't be spoofed.
    Elsewhere (Render, Fly, Railway), run uvicorn with --proxy-headers --forwarded-allow-ips=<proxy>."""
    if get_settings().on_vercel:
        fwd = request.headers.get("x-vercel-forwarded-for") or request.headers.get("x-real-ip") or ""
        ip = fwd.split(",")[0].strip()
        if ip:
            return ip
    return request.client.host if request.client else "0.0.0.0"


def ip_hash(request: Request) -> str:
    salt = get_settings().ip_hash_salt.encode()
    return hmac.new(salt, client_ip(request).encode(), hashlib.sha256).hexdigest()


# ---------------- passwords + TOTP ----------------
def hash_password(pw: str) -> str:
    if not pw:
        raise ValueError("Enter a password.")
    return ph.hash(pw)


def check_password(stored_hash: str | None, pw: str) -> bool:
    try:
        return ph.verify(stored_hash or _DUMMY_HASH, pw) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def check_totp(admin: dict, code: str) -> bool:
    """Accepts the current 30-second code or the one right before it, once each."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        return False
    try:
        secret = decrypt(admin["totp_secret_enc"]).decode()
    except InvalidToken:  # DATA_KEY changed since this admin was created: re-run create_admin
        return False
    totp = pyotp.TOTP(secret)
    now_step = int(time.time()) // 30
    for step in (now_step, now_step - 1):
        if step <= admin["totp_last_step"]:
            continue  # already used: replay
        if hmac.compare_digest(totp.at(step * 30), code):
            db.run("UPDATE admins SET totp_last_step = %s WHERE id = %s", (step, admin["id"]))
            return True
    return False


# ---------------- login throttling ----------------
def too_many_failures(email: str, iphash: str) -> bool:
    s = get_settings()
    row = db.one(
        """SELECT
             count(*) FILTER (WHERE email = %(email)s) AS by_email,
             count(*) FILTER (WHERE ip_hash = %(ip)s)  AS by_ip
           FROM login_attempts
           WHERE NOT succeeded AND reason <> 'locked' AND created_at > now() - make_interval(mins => %(mins)s)""",
        {"email": email, "ip": iphash, "mins": s.login_window_minutes},
    )
    return row["by_email"] >= s.login_max_failures or row["by_ip"] >= s.login_max_failures * 2


def record_attempt(email: str, iphash: str, ok: bool, reason: str) -> None:
    db.run("INSERT INTO login_attempts (email, ip_hash, succeeded, reason) VALUES (%s,%s,%s,%s)",
           (email, iphash, ok, reason))


# ---------------- sessions ----------------
def _h(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def create_session(admin_id: int, request: Request) -> tuple[str, str]:
    s = get_settings()
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=s.session_absolute_hours)
    with db.tx() as cur:
        # one active session per admin: logging in elsewhere ends older sessions
        cur.execute("DELETE FROM admin_sessions WHERE admin_id = %s OR expires_at < now()", (admin_id,))
        cur.execute(
            """INSERT INTO admin_sessions (token_hash, admin_id, csrf_token, expires_at, ip_hash, user_agent)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (_h(token), admin_id, csrf, expires, ip_hash(request), (request.headers.get("user-agent") or "")[:300]),
        )
        cur.execute("UPDATE admins SET last_login_at = now() WHERE id = %s", (admin_id,))
    return token, csrf


def cookie_name() -> str:
    return DEV_COOKIE if get_settings().is_dev else SESSION_COOKIE


def set_session_cookie(response, token: str) -> None:
    s = get_settings()
    response.set_cookie(cookie_name(), token, httponly=True, secure=not s.is_dev, samesite="strict",
                        path="/", max_age=s.session_absolute_hours * 3600)


def clear_session_cookie(response) -> None:
    response.delete_cookie(cookie_name(), path="/", secure=not get_settings().is_dev, samesite="strict")


def current_admin(request: Request) -> dict | None:
    token = request.cookies.get(cookie_name())
    if not token:
        return None
    s = get_settings()
    row = db.one(
        """SELECT s.token_hash, s.csrf_token, s.last_seen_at, a.id, a.email
             FROM admin_sessions s JOIN admins a ON a.id = s.admin_id
            WHERE s.token_hash = %s AND s.expires_at > now() AND a.is_active""",
        (_h(token),),
    )
    if not row:
        return None
    if row["last_seen_at"] < datetime.now(timezone.utc) - timedelta(minutes=s.session_idle_minutes):
        db.run("DELETE FROM admin_sessions WHERE token_hash = %s", (row["token_hash"],))
        return None
    db.run("UPDATE admin_sessions SET last_seen_at = now() WHERE token_hash = %s", (row["token_hash"],))
    return row


def destroy_session(request: Request) -> None:
    token = request.cookies.get(cookie_name())
    if token:
        db.run("DELETE FROM admin_sessions WHERE token_hash = %s", (_h(token),))


def require_csrf(admin: dict, submitted: str | None) -> None:
    if not submitted or not hmac.compare_digest(admin["csrf_token"], submitted):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


# ---------------- public submission throttle ----------------
def submission_rate_ok(iphash: str) -> bool:
    row = db.one("SELECT count(*) AS n FROM submissions WHERE ip_hash = %s AND created_at > now() - interval '1 hour'",
                 (iphash,))
    return row["n"] < get_settings().submissions_per_ip_per_hour


def mask_fein(fein: str | None) -> str:
    digits = "".join(c for c in (fein or "") if c.isdigit())
    return f"XX-XXX{digits[-4:]}" if len(digits) == 9 else ""
