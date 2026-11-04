"""Мінімальний декодер Mapbox Vector Tile (.pbf) без залежностей.

Потрібен для визначення ділянки тапом: беремо векторний тайл, знаходимо
полігон під точкою, повертаємо його атрибути та геометрію в WGS84.
Специфікація: https://github.com/mapbox/vector-tile-spec/tree/master/2.1
"""
from __future__ import annotations

import math
import struct


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, i
        s += 7


def _fields(b: bytes):
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 1:
            v, i = b[i:i + 8], i + 8
        elif w == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif w == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"MVT: невідомий wire type {w}")
        yield f, w, v


def _packed(b: bytes) -> list[int]:
    out, i = [], 0
    while i < len(b):
        v, i = _varint(b, i)
        out.append(v)
    return out


def _zz(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _value(b: bytes):
    for f, _w, v in _fields(b):
        if f == 1:
            return v.decode("utf-8", "replace")
        if f == 2:
            return round(struct.unpack("<f", v)[0], 6)
        if f == 3:
            return struct.unpack("<d", v)[0]
        if f == 4:
            return v - (1 << 64) if v >= 1 << 63 else v
        if f == 5:
            return v
        if f == 6:
            return _zz(v)
        if f == 7:
            return bool(v)
    return None


def _rings(cmds: list[int]) -> list[list[tuple[int, int]]]:
    x = y = 0
    rings: list[list[tuple[int, int]]] = []
    ring: list[tuple[int, int]] | None = None
    i = 0
    while i < len(cmds):
        c = cmds[i]
        i += 1
        cmd, cnt = c & 7, c >> 3
        if cmd in (1, 2):
            for _ in range(cnt):
                x += _zz(cmds[i])
                y += _zz(cmds[i + 1])
                i += 2
                if cmd == 1:
                    ring = [(x, y)]
                    rings.append(ring)
                elif ring is not None:
                    ring.append((x, y))
        elif cmd == 7 and ring:
            ring.append(ring[0])
    return rings


def decode(data: bytes) -> dict[str, dict]:
    """{назва_шару: {"extent": int, "features": [{"type", "props", "rings"}]}}"""
    layers: dict[str, dict] = {}
    for f, _w, lb in _fields(data):
        if f != 3:
            continue
        name, extent, keys, values, raw = "", 4096, [], [], []
        for lf, _lw, lv in _fields(lb):
            if lf == 1:
                name = lv.decode("utf-8", "replace")
            elif lf == 2:
                raw.append(lv)
            elif lf == 3:
                keys.append(lv.decode("utf-8", "replace"))
            elif lf == 4:
                values.append(_value(lv))
            elif lf == 5:
                extent = lv
        feats = []
        for fb in raw:
            tags, gtype, geom = [], 0, []
            for ff, _fw, fv in _fields(fb):
                if ff == 2:
                    tags = _packed(fv)
                elif ff == 3:
                    gtype = fv
                elif ff == 4:
                    geom = _packed(fv)
            props = {keys[tags[k]]: values[tags[k + 1]] for k in range(0, len(tags) - 1, 2)
                     if tags[k] < len(keys) and tags[k + 1] < len(values)}
            feats.append({"type": gtype, "props": props, "rings": _rings(geom)})
        layers[name] = {"extent": extent, "features": feats}
    return layers


# ---------------------------------------------------------------- геометрія
def _signed_area(r) -> float:
    return sum(r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1] for i in range(len(r) - 1)) / 2


def _inside(rings, x: float, y: float) -> bool:
    """Even-odd по всіх кільцях — враховує й отвори."""
    c = False
    for r in rings:
        for i in range(len(r) - 1):
            (x1, y1), (x2, y2) = r[i], r[i + 1]
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                c = not c
    return c


def features_at(data: bytes, layer: str | None, z: int, tx: int, ty: int,
                lng: float, lat: float) -> list[dict]:
    """Полігони тайла (z/tx/ty), що містять точку. Повертає [{props, geometry(WGS84)}]."""
    n = 2 ** z
    out = []
    for lname, lay in decode(data).items():
        if layer and lname != layer:
            continue
        ext = lay["extent"]
        px = ((lng + 180) / 360 * n - tx) * ext
        sy = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
        py = ((1 - sy / math.pi) / 2 * n - ty) * ext
        for feat in lay["features"]:
            if feat["type"] != 3 or not _inside(feat["rings"], px, py):
                continue
            out.append({"props": feat["props"], "geometry": _to_geojson(feat["rings"], ext, z, tx, ty)})
    return out


def _to_geojson(rings, ext: int, z: int, tx: int, ty: int) -> dict:
    n = 2 ** z

    def ll(p):
        lon = (tx + p[0] / ext) / n * 360 - 180
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + p[1] / ext) / n))))
        return [round(lon, 7), round(lat, 7)]

    polys: list[list] = []
    for r in rings:
        if len(r) < 4:
            continue
        # MVT 2.x: зовнішнє кільце має додатну площу (y донизу), отвір — від'ємну
        if _signed_area(r) > 0 or not polys:
            polys.append([[ll(p) for p in r]])
        else:
            polys[-1].append([ll(p) for p in r])
    return {"type": "MultiPolygon", "coordinates": polys}
