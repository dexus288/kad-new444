#!/usr/bin/env python3
"""Імітація ЄДЕССБ для локальної перевірки (НЕ для продакшену).

* сесія — cookie `sid`; без активності `IDLE` секунд «вмирає» (як справжня);
* кожна відповідь продовжує сесію і ротує cookie `XSRF-TOKEN`;
* /api-user/dzk.api.coords?cad_num=… — формат як у справжнього API;
* /tiles/{z}/{x}/{y}.png — публічний растровий шар (без cookies, потрібні Referer+Origin), /api-user/at — тап по карті.

    python dev/mock_edessb.py      # слухає :9001, логін: http://localhost:9001/login
"""
import secrets
import struct
import time
import zlib

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

IDLE = 120
app = FastAPI()
SESSIONS: dict[str, float] = {}


def alive(req: Request) -> bool:
    sid = req.cookies.get("sid")
    t = SESSIONS.get(sid or "")
    if not t or time.time() - t > IDLE:
        SESSIONS.pop(sid or "", None)
        return False
    SESSIONS[sid] = time.time()
    return True


def touch(resp: Response) -> Response:
    resp.set_cookie("XSRF-TOKEN", secrets.token_hex(8), path="/")
    return resp


@app.get("/login")
def login():
    sid = secrets.token_hex(12)
    SESSIONS[sid] = time.time()
    r = JSONResponse({"logged_in": True, "cookie": f"sid={sid}"})
    r.set_cookie("sid", sid, path="/")
    return r


def parcel(lng0=30.4067, lat0=50.3435):
    d = 0.0012
    outer = [[lng0 - d, lat0 - d * .7], [lng0 + d, lat0 - d], [lng0 + d * 1.2, lat0 + d * .8],
             [lng0 - d * .8, lat0 + d], [lng0 - d, lat0 - d * .7]]
    hole = [[lng0, lat0], [lng0 + .0002, lat0], [lng0 + .0002, lat0 + .0002], [lng0, lat0 + .0002], [lng0, lat0]]
    return {"geom": {"type": "MultiPolygon", "coordinates": [[outer, hole]]},
            "centroid": {"type": "Point", "coordinates": [lng0, lat0]}}


@app.get("/api-user/dzk.api.coords")
def coords(req: Request, cad_num: str = ""):
    if not alive(req):
        return RedirectResponse("https://id.gov.ua/?redirect=mock", 302)
    if cad_num.startswith("9"):
        return touch(JSONResponse({"success": False, "type": "dzk", "message": "Ділянку не знайдено"}))
    return touch(JSONResponse({"success": True, "type": "dzk", "result": parcel()}))


@app.get("/api-user/dzk.api")
def dzk_api(req: Request, cad_num: str = ""):
    """Як справжній dzk.api: геометрія + атрибути."""
    if not alive(req):
        return touch(Response("<html><title>ЄДЕССБ - час відповіді на запит закінчився!</title></html>",
                              status_code=403, media_type="text/html"))
    if cad_num.startswith("9"):
        return touch(JSONResponse({"success": False, "type": "dzk"}))
    p = parcel()
    return touch(JSONResponse({"success": True, "type": "dzk", "result": {
        "cadnum": cad_num, "category": "Землі сільськогосподарського призначення", "category_code": "100",
        "purpose_code": "01.09", "purpose": "01.09 Для дослідних і навчальних цілей", "use": "",
        "area": "4.0110", "unit_area": "га", "ownershipcode": "", "ownership": "Державна власність",
        "valuation_value": "1234567.89", "valuation_date": "2024-01-01",
        "ppoint": "POINT(30.4067 50.3435)", "zoom_to": "POLYGON((…))", "geom": p["geom"], "centroid": p["centroid"]}}))


DZK_HITS = {"n": 0}


@app.get("/dzk-hits")
def dzk_hits():
    return DZK_HITS


@app.get("/api-user/dzk")
def dzk_point(req: Request, lng: float, lat: float):
    """Як справжній /api-user/dzk?lng=…&lat=…: ділянка в точці або 404."""
    DZK_HITS["n"] += 1
    if not alive(req):
        return touch(Response("<html><title>ЄДЕССБ - час відповіді на запит закінчився!</title></html>",
                              status_code=403, media_type="text/html"))
    if not (30.4055 < lng < 30.4082 and 50.3423 < lat < 50.3447):
        return touch(JSONResponse({"error": "not_found", "error_description": "Ділянку не знайдено"}, status_code=404))
    return dzk_api(req, "3222481600:02:001:5160")


@app.get("/api-user/at")
def at(req: Request, lat: float, lng: float):
    if not alive(req):
        return RedirectResponse("https://id.gov.ua/", 302)
    p = parcel(lng, lat)
    return touch(JSONResponse({"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": p["geom"],
        "properties": {"cadnum": "3222486200:03:002:5017", "area": "1.2345", "purpose": "01.03 Для ведення особистого селянського господарства",
                       "ownership": "Приватна", "category": "Землі сільськогосподарського призначення", "koatuu": "3222486200"}}]}))


def _png(w, h, pixel):
    raw = b"".join(b"\x00" + b"".join(pixel(x, y) for x in range(w)) for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


_TILE = _png(256, 256, lambda x, y: b"\xff\xa0\x00\xd0" if (x % 64 < 2 or y % 85 < 2 or (x + y) % 170 < 2) else b"\x00\x00\x00\x00")


TILE_HITS = {"n": 0}
RATE = 4
_RATE_WIN: list[float] = []


@app.get("/tiles/{z}/{x}/{y}.png")
def tile(req: Request, z: int, x: int, y: int):
    """Як data.gki.com.ua: cookies не потрібні, але без Referer/Origin — відмова."""
    TILE_HITS["n"] += 1
    if not req.headers.get("referer") or not req.headers.get("origin"):
        return Response("forbidden", status_code=403)
    # обмеження частоти, як nginx limit_req: понад RATE запитів за секунду → 503
    now = time.time()
    while _RATE_WIN and _RATE_WIN[0] < now - 1:
        _RATE_WIN.pop(0)
    if len(_RATE_WIN) >= RATE:
        TILE_HITS["503"] = TILE_HITS.get("503", 0) + 1
        return Response("<html>503 Service Temporarily Unavailable</html>", status_code=503, media_type="text/html")
    _RATE_WIN.append(now)
    if y % 5 == 0:
        return Response(status_code=404)   # порожня територія
    return Response(_TILE, media_type="image/png")


@app.get("/tile-hits")
def tile_hits():
    return TILE_HITS


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9001, log_level="warning")
