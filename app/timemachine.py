"""Машина часу кадастру.

Для кожної області спостереження періодично робиться «знімок»: усі ділянки з
векторного шару (сервер ДЗК 2) у межах області — номер, призначення, площа,
адреса, центроїд і хеш контуру. Знімки лежать у data/timemachine/<region>/<дата>.json.gz.
Порівняння двох знімків показує нові, зниклі та змінені ділянки.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import os
import threading
import time

import httpx

from . import db
from .config import Settings, Upstream, load_upstream, settings
from .edessb import notify
from .mvt import decode

log = logging.getLogger("timemachine")

SNAP_ZOOM = 14           # тайл ≈ 2,4 км; село — 4–9 тайлів
MAX_TILES = 150          # запобіжник на розмір області (~870 км²)
DEFAULT_INTERVAL_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS tm_regions (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    minlng REAL NOT NULL, minlat REAL NOT NULL, maxlng REAL NOT NULL, maxlat REAL NOT NULL,
    interval_days INTEGER NOT NULL DEFAULT 30,
    created_by  INTEGER,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS tm_snapshots (
    id          INTEGER PRIMARY KEY,
    region_id   INTEGER NOT NULL REFERENCES tm_regions(id) ON DELETE CASCADE,
    taken_at    INTEGER NOT NULL,
    parcels     INTEGER NOT NULL,
    tiles       INTEGER NOT NULL,
    file        TEXT NOT NULL,
    summary     TEXT
);
CREATE INDEX IF NOT EXISTS tm_snap_region ON tm_snapshots(region_id, taken_at);
"""

_lock = threading.Lock()
_running: set[int] = set()


def init() -> None:
    db.conn().executescript(SCHEMA)


# ---------------------------------------------------------------- тайли
def _tile_range(minlng, minlat, maxlng, maxlat, z=SNAP_ZOOM):
    n = 2 ** z

    def t(lng, lat):
        x = int((lng + 180) / 360 * n)
        y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
        return x, y
    x0, y0 = t(minlng, maxlat)
    x1, y1 = t(maxlng, minlat)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def tiles_for(region: dict) -> int:
    return len(_tile_range(region["minlng"], region["minlat"], region["maxlng"], region["maxlat"]))


def _fetch_tile(client: httpx.Client, up: Upstream, z: int, x: int, y: int) -> bytes | None:
    url = up.vector_tile_url.format(z=z, x=x, y=y)
    for attempt in range(3):
        try:
            with client.stream("GET", url) as r:
                raw = b"".join(r.iter_raw())
                st = r.status_code
        except httpx.HTTPError:
            time.sleep(1.5 * (attempt + 1))
            continue
        if st in (204, 404):
            return None
        if st == 200:
            return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"тайл {z}/{x}/{y}: не вдалося завантажити")


def _ring_centroid_area(ring):
    a = cx = cy = 0.0
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i], ring[i + 1]
        f = x1 * y2 - x2 * y1
        a += f; cx += (x1 + x2) * f; cy += (y1 + y2) * f
    if abs(a) < 1e-12:
        return 0.0, ring[0][0], ring[0][1]
    return a / 2, cx / (3 * a), cy / (3 * a)


# ---------------------------------------------------------------- знімок
def take_snapshot(region: dict, up: Upstream | None = None, st: Settings = settings) -> dict:
    """Робить знімок області. Повертає рядок tm_snapshots як dict."""
    up = up or load_upstream(st.upstream_file)
    if not up.vector_tile_url:
        raise RuntimeError("Не налаштовано vector_tile_url ([layer2])")
    tiles = _tile_range(region["minlng"], region["minlat"], region["maxlng"], region["maxlat"])
    if len(tiles) > MAX_TILES:
        raise RuntimeError(f"Область завелика: {len(tiles)} тайлів (ліміт {MAX_TILES})")
    minlng, minlat, maxlng, maxlat = region["minlng"], region["minlat"], region["maxlng"], region["maxlat"]
    n = 2 ** SNAP_ZOOM
    parcels: dict[str, dict] = {}
    headers = {"User-Agent": up.user_agent, "Accept-Encoding": "gzip", **(up.vector_headers or {})}
    with httpx.Client(headers=headers, timeout=up.timeout, proxy=up.proxy or None, follow_redirects=True) as c:
        for i, (tx, ty) in enumerate(tiles):
            data = _fetch_tile(c, up, SNAP_ZOOM, tx, ty)
            if i % 3 == 2:
                time.sleep(0.3)   # не навантажуємо джерело
            if not data:
                continue
            lay = decode(data).get(up.vector_layer)
            if not lay:
                continue
            ext = lay["extent"]
            for f in lay["features"]:
                cad = str(f["props"].get("cadnum") or "")
                if f["type"] != 3 or not cad or not f["rings"]:
                    continue
                # геометрія у координатах тайла → WGS84 (лише для центроїда і хешу)
                rings_ll = []
                for r in f["rings"]:
                    rings_ll.append([(
                        (tx + px / ext) / n * 360 - 180,
                        math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + py / ext) / n)))),
                    ) for px, py in r])
                area = cx = cy = 0.0
                for r in rings_ll:
                    a, x, y = _ring_centroid_area(r)
                    area += a; cx += x * a; cy += y * a
                if abs(area) < 1e-14:
                    continue
                cx /= area; cy /= area
                h = hashlib.sha1()
                for r in rings_ll:
                    for x, y in r:
                        h.update(f"{x:.6f},{y:.6f};".encode())
                p = parcels.setdefault(cad, {
                    "purpose": f["props"].get("purpose") or "", "area": f["props"].get("area"),
                    "address": f["props"].get("address") or "", "parts": [], "c": [0.0, 0.0], "w": 0.0,
                })
                p["parts"].append(h.hexdigest()[:12])
                p["c"][0] += cx * abs(area); p["c"][1] += cy * abs(area); p["w"] += abs(area)
    out = {}
    for cad, p in parcels.items():
        c = [p["c"][0] / p["w"], p["c"][1] / p["w"]] if p["w"] else None
        if not c or not (minlng <= c[0] <= maxlng and minlat <= c[1] <= maxlat):
            continue   # ділянка з краю тайла, але поза областю
        out[cad] = {
            "purpose": p["purpose"], "area": p["area"], "address": p["address"],
            "c": [round(c[0], 6), round(c[1], 6)],
            "g": hashlib.sha1("".join(sorted(p["parts"])).encode()).hexdigest()[:12],
        }
    now = int(time.time())
    d = st.data_dir / "timemachine" / str(region["id"])
    d.mkdir(parents=True, exist_ok=True)
    fname = time.strftime("%Y-%m-%d_%H%M", time.localtime(now)) + ".json.gz"
    path = d / fname
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump({"region": region["id"], "taken_at": now, "zoom": SNAP_ZOOM, "parcels": out}, fh, ensure_ascii=False)
    os.replace(tmp, path)

    prev = latest_snapshot(region["id"])
    summary = None
    if prev:
        dv = diff(load_snapshot(prev), {"parcels": out})
        summary = {k: len(v) for k, v in dv.items()}
    cur = db.conn().execute(
        "INSERT INTO tm_snapshots(region_id, taken_at, parcels, tiles, file, summary) VALUES (?,?,?,?,?,?)",
        (region["id"], now, len(out), len(tiles), fname, json.dumps(summary) if summary else None))
    row = get_snapshot(cur.lastrowid)
    log.info("Знімок «%s»: %d ділянок, %d тайлів%s", region["name"], len(out), len(tiles),
             f", зміни {summary}" if summary else "")
    if summary and any(summary.values()):
        notify(st, f"🕰 Машина часу — «{region['name']}»:\n"
                   f"нових ділянок: {summary['added']}, зникло: {summary['removed']}, "
                   f"змінено призначення: {summary['purpose']}, змінено контур/площу: {summary['geometry']}\n"
                   f"{st.public_url}/#tm")
    return row


# ---------------------------------------------------------------- читання
def load_snapshot(snap: dict) -> dict:
    path = settings.data_dir / "timemachine" / str(snap["region_id"]) / snap["file"]
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def get_snapshot(sid: int) -> dict | None:
    r = db.conn().execute("SELECT * FROM tm_snapshots WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def latest_snapshot(region_id: int) -> dict | None:
    r = db.conn().execute("SELECT * FROM tm_snapshots WHERE region_id=? ORDER BY taken_at DESC LIMIT 1",
                          (region_id,)).fetchone()
    return dict(r) if r else None


def diff(a: dict, b: dict) -> dict:
    """Зміни від знімка a до знімка b."""
    pa, pb = a["parcels"], b["parcels"]
    out = {"added": [], "removed": [], "purpose": [], "geometry": []}
    for cad, p in pb.items():
        q = pa.get(cad)
        if not q:
            out["added"].append({"cadnum": cad, **p})
        else:
            if (q["purpose"] or "") != (p["purpose"] or ""):
                out["purpose"].append({"cadnum": cad, **p, "was": q["purpose"]})
            if q["g"] != p["g"] or str(q.get("area")) != str(p.get("area")):
                out["geometry"].append({"cadnum": cad, **p, "was_area": q.get("area")})
    for cad, q in pa.items():
        if cad not in pb:
            out["removed"].append({"cadnum": cad, **q})
    return out


# ---------------------------------------------------------------- планувальник
def due_regions() -> list[dict]:
    rows = db.conn().execute("""
        SELECT r.*, (SELECT MAX(taken_at) FROM tm_snapshots s WHERE s.region_id=r.id) AS last
        FROM tm_regions r""").fetchall()
    now = time.time()
    return [dict(r) for r in rows if not r["last"] or now - r["last"] >= r["interval_days"] * 86400]


def run_due(st: Settings = settings) -> int:
    """Робить знімки для областей, у яких настав час. Повертає кількість зроблених."""
    init()
    done = 0
    for region in due_regions():
        if not _lock.acquire(blocking=False):
            return done
        try:
            if region["id"] in _running:
                continue
            _running.add(region["id"])
        finally:
            _lock.release()
        try:
            take_snapshot(region, st=st)
            done += 1
        except Exception as e:
            log.warning("Знімок «%s» не вдався: %s", region["name"], e)
        finally:
            _running.discard(region["id"])
    return done


def snapshot_in_background(region: dict) -> bool:
    """Запуск знімка з адмінки в окремому потоці. False — уже виконується."""
    with _lock:
        if region["id"] in _running:
            return False
        _running.add(region["id"])

    def job():
        try:
            take_snapshot(region)
        except Exception as e:
            log.warning("Знімок «%s» не вдався: %s", region["name"], e)
        finally:
            _running.discard(region["id"])
    threading.Thread(target=job, daemon=True).start()
    return True


def is_running(region_id: int) -> bool:
    return region_id in _running
