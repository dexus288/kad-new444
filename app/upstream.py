"""Асинхронний клієнт до ЄДЕССБ з cookies сесії: пошук, ідентифікація, тайли шару ДЗК."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

from . import db
from .config import Upstream, load_upstream, settings
from .cookiestore import CookieStore
from .edessb import base_headers, looks_logged_out, read_status, write_status
from .geo import tile_bbox_3857, to_3857
from .normalize import parse_parcels

log = logging.getLogger("upstream")

TRANSPARENT_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


class UpstreamError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind      # session | network | notfound | config | bad
        self.message = message


class Edessb:
    def __init__(self) -> None:
        self.store = CookieStore(settings.cookies_path)
        self.up: Upstream = load_upstream(settings.upstream_file)
        self._cfg_mtime = self._mtime(settings.upstream_file)
        self.client = self._make_client()
        self.tile_client = self._make_tile_client()
        self.sem = asyncio.Semaphore(self.up.max_parallel)
        self.tile_sem = asyncio.Semaphore(self.up.tile_parallel)
        self.store.load_into(self.client.cookies.jar)
        self._last_session_mark = 0.0

    @staticmethod
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=base_headers(self.up), follow_redirects=False,
                                 timeout=self.up.timeout, proxy=self.up.proxy or None,
                                 limits=httpx.Limits(max_connections=self.up.max_parallel * 2))

    def _make_tile_client(self) -> httpx.AsyncClient:
        """Клієнт БЕЗ cookies сесії: публічний растровий шар (data.gki.com.ua) їх не потребує."""
        h = {"User-Agent": self.up.user_agent, "Accept-Language": "uk-UA,uk;q=0.9"}
        if self.up.referer:
            h["Referer"] = self.up.referer
        h.update(self.up.tile_headers or {})
        return httpx.AsyncClient(headers=h, follow_redirects=False, timeout=self.up.timeout,
                                 proxy=self.up.proxy or None,
                                 limits=httpx.Limits(max_connections=self.up.tile_parallel * 2))

    async def refresh(self) -> None:
        """Підхопити змінений конфіг і нові cookies без перезапуску."""
        m = self._mtime(settings.upstream_file)
        if m != self._cfg_mtime:
            self._cfg_mtime = m
            self.up = load_upstream(settings.upstream_file)
            old, old_t = self.client, self.tile_client
            self.client = self._make_client()
            self.tile_client = self._make_tile_client()
            self.sem = asyncio.Semaphore(self.up.max_parallel)
            self.tile_sem = asyncio.Semaphore(self.up.tile_parallel)
            self.store.load_into(self.client.cookies.jar)
            await old.aclose()
            await old_t.aclose()
            log.info("upstream.toml перечитано")
        elif self.store.changed_on_disk():
            self.store.load_into(self.client.cookies.jar)

    async def close(self) -> None:
        await self.client.aclose()
        await self.tile_client.aclose()

    # ------------------------------------------------------------ низький рівень
    async def _get(self, url: str, accept: str) -> httpx.Response:
        await self.refresh()
        async with self.sem:
            try:
                resp = await self.client.get(url, headers={"Accept": accept})
            except httpx.HTTPError as e:
                raise UpstreamError("network", f"ЄДЕССБ недоступна: {e.__class__.__name__}")
        self.store.save_from(self.client.cookies.jar, "app")
        if resp.status_code == 404 and not resp.history:
            raise UpstreamError("notfound", "Не знайдено")
        # перевіряємо сесію без alive_json: success=false у пошуку = «не знайдено»
        reason = looks_logged_out(resp, dataclasses.replace(self.up, alive_json="", alive_marker=""))
        if reason:
            if resp.status_code >= 500:
                raise UpstreamError("network", reason)
            self._mark_session_bad(reason)
            raise UpstreamError("session", "Сесія ЄДЕССБ неактивна. Зверніться до адміністратора.")
        return resp

    def _mark_session_ok(self) -> None:
        if time.time() - self._last_session_mark > 60 and read_status(settings).get("ok") is False:
            self._last_session_mark = time.time()
            write_status(settings, True, "застосунок: запит до ЄДЕССБ успішний", "app")

    def _mark_session_bad(self, reason: str) -> None:
        # не частіше раз на хвилину, щоб не смикати статус/Telegram
        if time.time() - self._last_session_mark > 60:
            self._last_session_mark = time.time()
            if read_status(settings).get("ok") is not False:
                write_status(settings, False, f"застосунок: {reason}", "app")

    @staticmethod
    def _json(resp: httpx.Response):
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            ct = resp.headers.get("content-type", "")
            if "html" in ct:
                raise UpstreamError("session", "ЄДЕССБ повернула HTML замість даних — схоже, сесія закінчилась.")
            raise UpstreamError("bad", "Неочікувана відповідь ЄДЕССБ")

    # ------------------------------------------------------------ пошук
    async def search(self, cadnum: str, max_age: int = 6 * 3600) -> dict:
        if not self.up.search_url:
            raise UpstreamError("config", "Не налаштовано search_url")
        row = db.conn().execute("SELECT data, fetched_at FROM parcel_cache WHERE cadnum=?",
                                (cadnum,)).fetchone()
        if row and time.time() - row["fetched_at"] < max_age:
            return {**json.loads(row["data"]), "cached_at": row["fetched_at"]}

        url = self.up.search_url.format(cadnum=quote(cadnum, safe=":"))
        try:
            resp = await self._get(url, "application/json, text/plain, */*")
            data = self._json(resp)
        except UpstreamError as e:
            if row and e.kind in ("session", "network"):
                # краще показати збережене, ніж нічого
                return {**json.loads(row["data"]), "cached_at": row["fetched_at"], "stale": True,
                        "warning": e.message}
            raise
        if isinstance(data, dict) and data.get("success") is not False:
            self._mark_session_ok()
        parcels = parse_parcels(data, self.up, cadnum)
        if not parcels:
            msg = data.get("message") or data.get("error") if isinstance(data, dict) else None
            raise UpstreamError("notfound", msg or "Ділянку не знайдено")
        parcel = parcels[0]
        parcel["cadnum"] = parcel.get("cadnum") or cadnum
        db.conn().execute("INSERT OR REPLACE INTO parcel_cache(cadnum, data, fetched_at) VALUES (?,?,?)",
                          (cadnum, json.dumps(parcel, ensure_ascii=False), int(time.time())))
        return parcel

    # ------------------------------------------------------------ точка на карті
    @property
    def can_identify(self) -> bool:
        return bool(self.up.identify_url or (self.up.wms_url and self.up.identify_mode in ("auto", "wms")))

    async def identify(self, lat: float, lng: float, zoom: int) -> list[dict]:
        x, y = to_3857(lng, lat)
        if self.up.identify_url and self.up.identify_mode in ("auto", "url"):
            d = 2 * 20037508.34 / (256 * 2 ** zoom) * 3   # ~3 пікселі
            url = self.up.identify_url.format(
                lat=lat, lng=lng, lon=lng, x=x, y=y, zoom=zoom,
                bbox=f"{x - d},{y - d},{x + d},{y + d}")
        elif self.up.wms_url:
            d = 2 * 20037508.34 / (256 * 2 ** zoom) * 5
            params = {
                "SERVICE": "WMS", "REQUEST": "GetFeatureInfo", "VERSION": self.up.wms_version,
                "LAYERS": self.up.wms_layers, "QUERY_LAYERS": self.up.wms_layers,
                "STYLES": self.up.wms_styles, "INFO_FORMAT": self.up.wms_info_format,
                "FEATURE_COUNT": 5, "WIDTH": 101, "HEIGHT": 101,
                "BBOX": f"{x - d},{y - d},{x + d},{y + d}",
                ("CRS" if self.up.wms_version.startswith("1.3") else "SRS"): "EPSG:3857",
                ("I" if self.up.wms_version.startswith("1.3") else "X"): 50,
                ("J" if self.up.wms_version.startswith("1.3") else "Y"): 50,
                **self.up.wms_extra,
            }
            url = self.up.wms_url + ("&" if "?" in self.up.wms_url else "?") + urlencode(params)
        else:
            raise UpstreamError("config", "Пошук за точкою не налаштовано")
        resp = await self._get(url, "application/json, */*")
        up = dataclasses.replace(self.up, result_path=self.up.identify_result_path)
        return parse_parcels(self._json(resp), up)

    # ------------------------------------------------------------ тайли шару ДЗК
    @property
    def has_layer(self) -> bool:
        return bool(self.up.tile_url or self.up.wms_url)

    async def _fetch_tile(self, url: str) -> bytes | None:
        """Байти тайла; None — тайл порожній (нема ділянок). Помилки — UpstreamError."""
        if self.up.tile_session:
            resp = await self._get(url, "image/webp,image/png,image/*;q=0.8")
        else:
            await self.refresh()
            async with self.tile_sem:
                try:
                    resp = await self.tile_client.get(url)
                except httpx.HTTPError as e:
                    raise UpstreamError("network", f"Тайл: {e.__class__.__name__}")
            if resp.status_code in (204, 404):
                return None
            if resp.status_code != 200:
                # публічний шар — це не сесія ЄДЕССБ, статус сесії не чіпаємо
                log.warning("Тайл %s → HTTP %s", url, resp.status_code)
                raise UpstreamError("bad", f"Тайл: HTTP {resp.status_code}")
        if resp.status_code == 204 or not resp.content:
            return None
        if not resp.headers.get("content-type", "").startswith("image/") or not sniff(resp.content):
            raise UpstreamError("bad", "Тайл: відповідь не є зображенням")
        return resp.content

    async def tile(self, z: int, x: int, y: int) -> tuple[bytes, str, bool]:
        """Повертає (байти, content-type, з_кешу)."""
        if not self.has_layer:
            raise UpstreamError("config", "Шар не налаштовано")
        path = settings.tile_cache_dir / str(z) / str(x) / f"{y}.img"
        ttl = self.up.tile_cache_hours * 3600
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            data = path.read_bytes()
            return data, sniff(data) or "image/png", True

        if self.up.tile_url:
            url = self.up.tile_url.format(z=z, x=x, y=y)
        else:
            minx, miny, maxx, maxy = tile_bbox_3857(z, x, y)
            v13 = self.up.wms_version.startswith("1.3")
            params = {
                "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": self.up.wms_version,
                "LAYERS": self.up.wms_layers, "STYLES": self.up.wms_styles,
                "FORMAT": self.up.wms_format, "TRANSPARENT": "true",
                "WIDTH": 256, "HEIGHT": 256, ("CRS" if v13 else "SRS"): "EPSG:3857",
                "BBOX": f"{minx},{miny},{maxx},{maxy}", **self.up.wms_extra,
            }
            url = self.up.wms_url + ("&" if "?" in self.up.wms_url else "?") + urlencode(params)
        try:
            data = await self._fetch_tile(url)
        except UpstreamError:
            if path.exists():   # протермінований кеш краще за порожнечу
                data = path.read_bytes()
                return data, sniff(data) or "image/png", True
            raise
        if data is None:        # порожній тайл теж кешуємо — не смикаємо джерело повторно
            data = TRANSPARENT_PNG
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return data, sniff(data) or "image/png", False


def sniff(data: bytes) -> str | None:
    """Тип зображення за сигнатурою (джерело може віддати PNG, WebP або JPEG)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return None
