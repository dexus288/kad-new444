"""Геометрія: тайли, перерахунок координат, нормалізація GeoJSON."""
from __future__ import annotations

import math
from functools import lru_cache

R = 6378137.0
ORIGIN = math.pi * R


def tile_bbox_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = 2 * ORIGIN / (2 ** z)
    minx = -ORIGIN + x * size
    maxy = ORIGIN - y * size
    return minx, maxy - size, minx + size, maxy


def to_3857(lng: float, lat: float) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    return (math.radians(lng) * R, math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R)


def from_3857(x: float, y: float) -> tuple[float, float]:
    return (math.degrees(x / R), math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2))


@lru_cache(maxsize=16)
def _transformer(crs: str):
    from pyproj import Transformer  # імпорт лише за потреби (UCS-2000 тощо)
    return Transformer.from_crs(crs, "EPSG:4326", always_xy=True)


def _first_xy(coords):
    while isinstance(coords, (list, tuple)) and coords and isinstance(coords[0], (list, tuple)):
        coords = coords[0]
    return coords


def _map_coords(coords, fn):
    if coords and isinstance(coords[0], (int, float)):
        x, y = fn(coords[0], coords[1])
        return [round(x, 7), round(y, 7)]
    return [_map_coords(c, fn) for c in coords]


def geometry_to_4326(geom: dict | None, crs: str = "auto") -> dict | None:
    if not geom or "coordinates" not in geom:
        return geom
    crs = (crs or "auto").upper()
    if crs == "AUTO":
        # GeoJSON може містити старе поле crs
        name = (((geom.get("crs") or {}).get("properties") or {}).get("name") or "").upper()
        if "3857" in name or "900913" in name:
            crs = "EPSG:3857"
        else:
            xy = _first_xy(geom["coordinates"])
            if not xy:
                return geom
            if abs(xy[0]) <= 180 and abs(xy[1]) <= 90:
                crs = "EPSG:4326"
            elif 2_000_000 < abs(xy[0]) < 5_000_000 and 5_000_000 < abs(xy[1]) < 7_000_000:
                crs = "EPSG:3857"   # Україна у Web Mercator
            else:
                return None        # невідома СК — краще не малювати неправильно
    if crs in ("EPSG:4326", "CRS84", "OGC:CRS84"):
        fn = None
    elif crs in ("EPSG:3857", "EPSG:900913", "EPSG:102100"):
        fn = from_3857
    else:
        t = _transformer(crs)
        fn = lambda x, y: t.transform(x, y)  # noqa: E731
    out = {"type": geom["type"]}
    out["coordinates"] = geom["coordinates"] if fn is None else _map_coords(geom["coordinates"], fn)
    return out


def bbox_of(geom: dict) -> list[float] | None:
    pts = []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            pts.append(c)
        else:
            for i in c:
                walk(i)
    try:
        walk(geom["coordinates"])
    except Exception:
        return None
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def _ring_area(ring) -> float:
    """Площа кільця на сфері (м²), алгоритм Chamberlain & Duquette (як у Turf/OpenLayers)."""
    n = len(ring)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        lo, mi, hi = ring[i], ring[(i + 1) % n], ring[(i + 2) % n]
        total += (math.radians(hi[0]) - math.radians(lo[0])) * math.sin(math.radians(mi[1]))
    return abs(total * R * R / 2)


def area_m2(geom: dict | None) -> float | None:
    """Площа Polygon/MultiPolygon у WGS84 (м²) з відніманням внутрішніх кілець."""
    if not geom:
        return None
    t, c = geom.get("type"), geom.get("coordinates")
    polys = [c] if t == "Polygon" else c if t == "MultiPolygon" else None
    if not polys:
        return None
    s = 0.0
    for poly in polys:
        if not poly:
            continue
        s += _ring_area(poly[0]) - sum(_ring_area(h) for h in poly[1:])
    return s
