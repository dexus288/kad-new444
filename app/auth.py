"""Паролі (scrypt), сесії (випадковий токен у HttpOnly cookie), обмеження спроб входу."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request

from . import db
from .config import settings

COOKIE = "kd_session"
N, R_, P = 2 ** 14, 8, 1


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=N, r=R_, p=P, dklen=32)
    return f"scrypt${N}${R_}${P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r),
                              p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


# фіктивний хеш, щоб час відповіді не видавав, чи існує логін
_DUMMY = hash_password(secrets.token_hex(8))


def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int, ua: str) -> str:
    token = secrets.token_urlsafe(32)
    db.conn().execute(
        "INSERT INTO sessions(token_hash, user_id, expires_at, ua) VALUES (?,?,?,?)",
        (_th(token), user_id, int(time.time()) + settings.session_days * 86400, ua[:200]))
    return token


def drop_session(token: str) -> None:
    db.conn().execute("DELETE FROM sessions WHERE token_hash=?", (_th(token),))


def drop_user_sessions(user_id: int) -> None:
    db.conn().execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def set_cookie(resp, token: str) -> None:
    resp.set_cookie(COOKIE, token, max_age=settings.session_days * 86400, httponly=True,
                    secure=settings.cookie_secure, samesite="lax", path="/")


# ---------------------------------------------------------------- rate limit
_attempts: dict[str, deque] = defaultdict(deque)
WINDOW = 15 * 60
# на логін — 10 спроб; на IP — 30 (офіс за одним NAT не повинен блокуватись через одного)
LIMITS = {"user": 10, "ip": 30, "reg": 10}


def check_rate(key: str) -> None:
    q = _attempts[key]
    now = time.time()
    while q and q[0] < now - WINDOW:
        q.popleft()
    if len(q) >= LIMITS.get(key.split(":", 1)[0], 10):
        raise HTTPException(429, "Забагато спроб. Спробуйте за 15 хвилин.")


def note_failure(key: str) -> None:
    _attempts[key].append(time.time())


def client_ip(req: Request) -> str:
    return req.client.host if req.client else "?"


# ---------------------------------------------------------------- залежності
def current_user(req: Request) -> dict:
    token = req.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Потрібен вхід")
    now = int(time.time())
    row = db.conn().execute(
        """SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id
           WHERE s.token_hash=? AND s.expires_at>?""", (_th(token), now)).fetchone()
    if not row or row["status"] != "active":
        raise HTTPException(401, "Сесія недійсна")
    # ковзне продовження сесії + last_seen (не частіше ніж раз на годину)
    if not row["last_seen"] or now - row["last_seen"] > 3600:
        db.conn().execute("UPDATE users SET last_seen=? WHERE id=?", (now, row["id"]))
        db.conn().execute("UPDATE sessions SET expires_at=? WHERE token_hash=?",
                          (now + settings.session_days * 86400, _th(token)))
    return dict(row)


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Лише для адміністратора")
    return user


def public_user(u: dict) -> dict:
    return {k: u[k] for k in ("id", "username", "name", "role", "status", "created_at", "last_seen")}
