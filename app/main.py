"""Веб-застосунок кадастрової карти (FastAPI)."""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db
from .auth import current_user, public_user, require_admin
from .config import BASE_DIR, settings
from .cookiestore import parse_cookie_input
from .edessb import check_once, read_status
from .normalize import normalize_cadnum
from .upstream import TRANSPARENT_PNG, Edessb, UpstreamError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    app.state.edessb = Edessb()
    yield
    await app.state.edessb.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def E(req: Request) -> Edessb:
    return req.app.state.edessb


# ---------------------------------------------------------------- middleware
CSP = ("default-src 'self'; img-src 'self' data: blob: https://tile.openstreetmap.org "
       "https://server.arcgisonline.com; "
       "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.middleware("http")
async def security(request: Request, call_next):
    # CSRF: змінювати стан можна лише fetch-запитом з нашого фронтенду
    if (request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/")
            and request.headers.get("x-requested-with") != "kd"):
        return JSONResponse({"detail": "CSRF"}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def upstream_http_error(e: UpstreamError) -> HTTPException:
    code = {"session": 503, "network": 502, "notfound": 404, "config": 501, "bad": 502, "busy": 503}[e.kind]
    return HTTPException(code, {"kind": e.kind, "message": e.message})


# ---------------------------------------------------------------- сторінки
@app.get("/", include_in_schema=False)
def index(request: Request):
    if not request.cookies.get(auth.COOKIE):
        nxt = "/" + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?next=" + quote(nxt, safe=""), 302)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- авторизація
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


@app.get("/api/config")
def public_config():
    return {"registration": settings.registration}


@app.post("/api/auth/register")
def register(request: Request, body: dict = Body(...)):
    if settings.registration == "closed":
        raise HTTPException(403, "Реєстрацію закрито. Зверніться до адміністратора.")
    username = str(body.get("username", "")).strip()
    name = str(body.get("name", "")).strip()[:60] or username
    password = str(body.get("password", ""))
    ip = auth.client_ip(request)
    auth.check_rate(f"reg:{ip}")
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "Логін: 3–32 символи, латиниця, цифри, . _ -")
    if len(password) < 8:
        raise HTTPException(400, "Пароль має містити щонайменше 8 символів")

    with db.tx() as c:
        first = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        if first:
            role, status = "admin", "active"
        elif settings.registration == "invite":
            if not settings.invite_code or str(body.get("invite", "")).strip() != settings.invite_code:
                auth.note_failure(f"reg:{ip}")
                raise HTTPException(400, "Невірний код запрошення")
            role, status = "user", "active"
        else:
            role, status = "user", "pending"
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(409, "Такий логін уже зайнятий")
        cur = c.execute("INSERT INTO users(username, name, pw_hash, role, status) VALUES (?,?,?,?,?)",
                        (username, name, auth.hash_password(password), role, status))
        uid = cur.lastrowid
    db.audit(uid, "register", f"{username} ({status})")
    auth.note_failure(f"reg:{ip}")   # рахуємо й успішні реєстрації — захист від спаму

    if status == "active":
        resp = JSONResponse({"status": status, "role": role})
        auth.set_cookie(resp, auth.create_session(uid, request.headers.get("user-agent", "")))
        return resp
    return {"status": status}


@app.post("/api/auth/login")
def login(request: Request, body: dict = Body(...)):
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    ip = auth.client_ip(request)
    auth.check_rate(f"ip:{ip}")
    auth.check_rate(f"user:{username.lower()}")
    row = db.conn().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        auth.verify_password(password, auth._DUMMY)
    if not row or not auth.verify_password(password, row["pw_hash"]):
        auth.note_failure(f"ip:{ip}")
        auth.note_failure(f"user:{username.lower()}")
        raise HTTPException(401, "Невірний логін або пароль")
    if row["status"] == "pending":
        raise HTTPException(403, "Обліковий запис очікує підтвердження адміністратором")
    if row["status"] != "active":
        raise HTTPException(403, "Обліковий запис заблоковано")
    resp = JSONResponse({"ok": True})
    auth.set_cookie(resp, auth.create_session(row["id"], request.headers.get("user-agent", "")))
    db.audit(row["id"], "login", ip)
    return resp


@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(auth.COOKIE)
    if token:
        auth.drop_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/me")
async def me(request: Request, user: dict = Depends(current_user)):
    st = read_status(settings)
    e = E(request)
    await e.refresh()
    return {
        "user": public_user(user),
        "edessb_ok": st.get("ok"),
        "features": {"layer": e.has_layer, "identify": e.can_identify, "search": bool(e.up.search_url),
                     "layer_min_zoom": e.up.min_zoom,
                     "layer_max_native_zoom": e.up.max_native_zoom,
                     "layer2": e.has_vector, "layer2_min_zoom": e.up.vector_min_zoom,
                     "layer2_max_native_zoom": e.up.vector_max_native_zoom},
    }


@app.post("/api/me/password")
def change_password(request: Request, body: dict = Body(...), user: dict = Depends(current_user)):
    if not auth.verify_password(str(body.get("old", "")), user["pw_hash"]):
        raise HTTPException(400, "Старий пароль невірний")
    new = str(body.get("new", ""))
    if len(new) < 8:
        raise HTTPException(400, "Новий пароль: щонайменше 8 символів")
    db.conn().execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(new), user["id"]))
    auth.drop_user_sessions(user["id"])
    resp = JSONResponse({"ok": True})
    auth.set_cookie(resp, auth.create_session(user["id"], request.headers.get("user-agent", "")))
    return resp


# ---------------------------------------------------------------- ділянки
@app.get("/api/parcel/search")
async def parcel_search(request: Request, q: str = Query(..., max_length=40),
                        fresh: bool = False, user: dict = Depends(current_user)):
    cad = normalize_cadnum(q)
    if not cad:
        raise HTTPException(400, {"kind": "input", "message": "Кадастровий номер має 19 цифр: XXXXXXXXXX:XX:XXX:XXXX"})
    try:
        return await E(request).search(cad, max_age=0 if fresh else 6 * 3600)
    except UpstreamError as e:
        raise upstream_http_error(e)


@app.get("/api/parcel/at")
async def parcel_at(request: Request, lat: float = Query(..., ge=44, le=53),
                    lng: float = Query(..., ge=22, le=41), z: int = Query(16, ge=10, le=20),
                    user: dict = Depends(current_user)):
    try:
        return {"items": await E(request).identify(lat, lng, z)}
    except UpstreamError as e:
        raise upstream_http_error(e)


@app.get("/tiles/dzk2/{z}/{x}/{y}.pbf", include_in_schema=False)
async def dzk2_tile(request: Request, z: int, x: int, y: int, user: dict = Depends(current_user)):
    """Векторні тайли «сервера ДЗК (2)» — віддаємо як є (gzip), браузер розпаковує сам."""
    e = E(request)
    if z < e.up.vector_min_zoom or z > e.up.vector_max_native_zoom:
        return Response(status_code=204)
    try:
        data, cached = await e.vtile(z, x, y)
    except UpstreamError as ex:
        return Response(status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "5",
                                                  "X-Tile-Error": ex.message[:80]})
    if not data:
        return Response(status_code=204, headers={"Cache-Control": "private, max-age=3600"})
    return Response(data, media_type="application/x-protobuf",
                    headers={"Content-Encoding": "gzip", "Cache-Control": "private, max-age=3600",
                             "X-Cache": "HIT" if cached else "MISS"})


@app.get("/tiles/dzk/{z}/{x}/{y}.img", include_in_schema=False)
async def dzk_tile(request: Request, z: int, x: int, y: int, user: dict = Depends(current_user)):
    e = E(request)
    if z < e.up.min_zoom or z > 20:
        return Response(TRANSPARENT_PNG, media_type="image/png")
    try:
        data, ct, cached = await e.tile(z, x, y)
    except UpstreamError as ex:
        # 503 + прозоре тіло: Leaflet отримає tileerror і перезапитає тайл пізніше
        # (див. static/app.js), а не лишить діру назавжди
        return Response(TRANSPARENT_PNG, status_code=503, media_type="image/png",
                        headers={"Cache-Control": "no-store", "Retry-After": "5", "X-Tile-Error": ex.message[:80]})
    return Response(data, media_type=ct, headers={"Cache-Control": "private, max-age=3600",
                                                   "X-Cache": "HIT" if cached else "MISS"})


# ---------------------------------------------------------------- закладки
@app.get("/api/bookmarks")
def bookmarks(user: dict = Depends(current_user)):
    rows = db.conn().execute("SELECT id, cadnum, note, lat, lng, created_at FROM bookmarks "
                             "WHERE user_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/bookmarks")
def bookmark_save(body: dict = Body(...), user: dict = Depends(current_user)):
    cad = normalize_cadnum(str(body.get("cadnum", "")))
    if not cad:
        raise HTTPException(400, "Невірний кадастровий номер")
    note = str(body.get("note", ""))[:300]
    lat, lng = body.get("lat"), body.get("lng")
    db.conn().execute(
        """INSERT INTO bookmarks(user_id, cadnum, note, lat, lng) VALUES (?,?,?,?,?)
           ON CONFLICT(user_id, cadnum) DO UPDATE SET note=excluded.note,
           lat=COALESCE(excluded.lat, lat), lng=COALESCE(excluded.lng, lng)""",
        (user["id"], cad, note, lat if isinstance(lat, (int, float)) else None,
         lng if isinstance(lng, (int, float)) else None))
    return {"ok": True}


@app.delete("/api/bookmarks/{bid}")
def bookmark_delete(bid: int, user: dict = Depends(current_user)):
    db.conn().execute("DELETE FROM bookmarks WHERE id=? AND user_id=?", (bid, user["id"]))
    return {"ok": True}


# ---------------------------------------------------------------- адміністрування
@app.get("/api/admin/users")
def admin_users(admin: dict = Depends(require_admin)):
    rows = db.conn().execute(
        "SELECT * FROM users ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC").fetchall()
    return [public_user(dict(r)) for r in rows]


@app.patch("/api/admin/users/{uid}")
def admin_user_update(uid: int, body: dict = Body(...), admin: dict = Depends(require_admin)):
    if uid == admin["id"]:
        raise HTTPException(400, "Не можна змінювати власний обліковий запис тут")
    fields, args = [], []
    if body.get("status") in ("active", "blocked", "pending"):
        fields.append("status=?"); args.append(body["status"])
    if body.get("role") in ("user", "admin"):
        fields.append("role=?"); args.append(body["role"])
    if body.get("password"):
        if len(str(body["password"])) < 8:
            raise HTTPException(400, "Пароль: щонайменше 8 символів")
        fields.append("pw_hash=?"); args.append(auth.hash_password(str(body["password"])))
    if not fields:
        raise HTTPException(400, "Нічого змінювати")
    db.conn().execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", (*args, uid))
    if body.get("status") in ("blocked", "pending") or body.get("password"):
        auth.drop_user_sessions(uid)
    db.audit(admin["id"], "user_update", f"{uid}: {sorted(k for k in body if k != 'password')}")
    return {"ok": True}


@app.delete("/api/admin/users/{uid}")
def admin_user_delete(uid: int, admin: dict = Depends(require_admin)):
    if uid == admin["id"]:
        raise HTTPException(400, "Не можна видалити себе")
    db.conn().execute("DELETE FROM users WHERE id=?", (uid,))
    db.audit(admin["id"], "user_delete", str(uid))
    return {"ok": True}


@app.get("/api/admin/edessb")
async def admin_edessb(request: Request, admin: dict = Depends(require_admin)):
    e = E(request)
    await e.refresh()
    return {"status": read_status(settings), "cookies": e.store.summary(),
            "config": {"keepalive_urls": e.up.keepalive_urls, "interval": e.up.keepalive_interval,
                       "search_url": e.up.search_url, "layer": e.has_layer, "layer2": e.has_vector,
                       "identify": e.can_identify, "proxy": bool(e.up.proxy),
                       "telegram": bool(settings.telegram_token and settings.telegram_chat)}}


@app.post("/api/admin/edessb/cookies")
async def admin_set_cookies(request: Request, body: dict = Body(...), admin: dict = Depends(require_admin)):
    e = E(request)
    await e.refresh()   # свіжий upstream.toml (назва/домен cookie)
    try:
        cookies = parse_cookie_input(str(body.get("text", "")), e.up.cookie_domain, e.up.session_cookie)
    except (ValueError, KeyError) as ex:
        raise HTTPException(400, f"Не вдалося розібрати cookies: {ex}")
    e.store.merge_domains(cookies, f"admin:{admin['username']}")
    db.audit(admin["id"], "cookies_upload", f"{len(cookies)} шт.")
    st = await asyncio.to_thread(check_once, settings, e.up, f"admin:{admin['username']}")
    return {"count": len(cookies), "status": st}


@app.post("/api/admin/edessb/check")
async def admin_check(request: Request, admin: dict = Depends(require_admin)):
    await E(request).refresh()
    st = await asyncio.to_thread(check_once, settings, E(request).up, f"admin:{admin['username']}")
    return {"status": st}


@app.get("/api/admin/audit")
def admin_audit(admin: dict = Depends(require_admin)):
    rows = db.conn().execute(
        """SELECT a.ts, a.action, a.detail, u.username FROM audit a
           LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 100""").fetchall()
    return [dict(r) for r in rows]


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

