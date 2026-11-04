"""Приведення довільної відповіді ЄДЕССБ/ДЗК до єдиного формату ділянки.

Формат відповіді upstream заздалегідь невідомий (JSON, GeoJSON, WMS
GetFeatureInfo), тому:
  * шукаємо ознаки (features / data / result / items …) або шлях з result_path;
  * основні поля шукаємо за списком синонімів з [fields];
  * усе інше показуємо як «Усі поля» з підписами з [labels].
"""
from __future__ import annotations

import re
from typing import Any

from .config import Upstream
from .geo import area_m2, bbox_of, geometry_to_4326

CADNUM_RE = re.compile(r"\b(\d{10}):(\d{2}):(\d{3}):(\d{4})\b")


def normalize_cadnum(s: str) -> str | None:
    digits = re.sub(r"\D", "", s or "")
    if len(digits) != 19:
        return None
    return f"{digits[:10]}:{digits[10:12]}:{digits[12:15]}:{digits[15:]}"


def _dig(obj: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
            for n, item in enumerate(v[:5]):
                out.update(_flatten(item, f"{key}.{n}."))
        else:
            out[key] = v
    return out


def extract_items(data: Any, up: Upstream) -> list[dict]:
    """Повертає список «сирих» об'єктів {properties, geometry}."""
    if up.result_path:
        data = _dig(data, up.result_path)
    if data is None:
        return []
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        return [{"properties": f.get("properties") or {}, "geometry": f.get("geometry")}
                for f in data.get("features") or []]
    if isinstance(data, dict) and data.get("type") == "Feature":
        return [{"properties": data.get("properties") or {}, "geometry": data.get("geometry")}]
    if isinstance(data, dict):
        for k in ("features", "data", "result", "results", "items", "parcels", "objects"):
            if k in data and isinstance(data[k], (list, dict)):
                inner = extract_items(data[k], Upstream(fields=up.fields))
                if inner:
                    return inner
        geom = data.get("geometry") or data.get("geom") or data.get("geojson")
        centroid = data.get("centroid") or data.get("center")
        props = {k: v for k, v in data.items()
                 if k not in ("geometry", "geom", "geojson", "centroid", "center", "success", "type")}
        return [{"properties": props, "geometry": geom if isinstance(geom, dict) else None,
                 "centroid": centroid if isinstance(centroid, dict) else None}]
    if isinstance(data, list):
        items: list[dict] = []
        for el in data:
            if isinstance(el, dict):
                items.extend(extract_items(el, Upstream(fields=up.fields)))
        return items
    return []


def _pick(flat: dict, names: list[str]) -> tuple[str | None, Any]:
    lower = {k.lower(): k for k in flat}
    for n in names:
        # точний збіг ключа, або збіг останнього сегмента вкладеного ключа
        if n.lower() in lower:
            k = lower[n.lower()]
            if flat[k] not in (None, ""):
                return k, flat[k]
        for k in flat:
            if k.lower().rsplit(".", 1)[-1] == n.lower() and flat[k] not in (None, ""):
                return k, flat[k]
    return None, None


def to_parcel(item: dict, up: Upstream, cadnum: str | None = None) -> dict:
    flat = _flatten(item.get("properties") or {})
    main: dict[str, Any] = {}
    used: set[str] = set()
    for field, names in up.fields.items():
        k, v = _pick(flat, names)
        if k:
            main[field] = v
            used.add(k)

    # кадастровий номер можна знайти й у будь-якому текстовому полі
    if not main.get("cadnum"):
        for v in flat.values():
            m = CADNUM_RE.search(str(v))
            if m:
                main["cadnum"] = m.group(0)
                break

    if not main.get("cadnum") and cadnum:
        main["cadnum"] = cadnum

    hidden = {h.lower() for h in up.hidden_fields}
    extra = []
    for k, v in flat.items():
        if k in used or v in (None, "", [], {}) or k.lower() in hidden:
            continue
        label = up.labels.get(k) or up.labels.get(k.rsplit(".", 1)[-1]) or k
        extra.append({"key": k, "label": label, "value": v if isinstance(v, (int, float, bool)) else str(v)})

    geom = geometry_to_4326(item.get("geometry"), up.geometry_crs) if item.get("geometry") else None
    centroid = None
    if item.get("centroid"):
        c = geometry_to_4326(item["centroid"], up.geometry_crs)
        if c and c.get("type") == "Point":
            centroid = c["coordinates"][:2]
    bbox = bbox_of(geom) if geom else None
    if not centroid and bbox:
        centroid = [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2]
    a = area_m2(geom)
    return {
        **{k: (v if isinstance(v, (int, float)) else str(v)) for k, v in main.items()},
        "area_calc_ha": round(a / 10000, 4) if a else None,
        "fields": extra,
        "geometry": geom,
        "bbox": bbox,
        "centroid": centroid,
    }


def parse_parcels(data: Any, up: Upstream, cadnum: str | None = None) -> list[dict]:
    if isinstance(data, dict) and data.get("success") is False:
        return []
    return [p for p in (to_parcel(i, up, cadnum) for i in extract_items(data, up))
            if p.get("geometry") or p.get("fields") or len(p) > 6]
