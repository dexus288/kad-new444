"""Асинхронний клієнт до ЄДЕССБ з cookies сесії: пошук, ідентифікація, тайли шару ДЗК."""
from __future__ import annotations

import asyncio
import dataclasses
import gzip
import math
import random
import json
import logging
import time
import zlib
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

from . import db
from .config import Upstream, load_upstream, settings
from .cookiestore import CookieStore
from .edessb import base_headers, looks_logged_out, read_status, write_status
from .geo import tile_bbox_3857, to_3857
from .mvt import features_at
from .normalize import parse_parcels, to_parcel

log = logging.getLogger("upstream")

TRANSPARENT_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


OLD_LAYER_WARNING = ("Дані зі старого шару ДЗК (2) — інформація може бути застарілою; "
                     "контур може бути неповним.")


def _why(e: "UpstreamError") -> str:
    return {"session": "сесія неактивна", "network": "немає з'єднання",
            "busy": "перевантажена"}.get(e.kind, e.message.rstrip("."))


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
        self.vec_client = self._make_vec_client()
        self.sem = asyncio.Semaphore(self.up.max_parallel)
        self.tile_sem = asyncio.Semaphore(self.up.tile_parallel)
        self.vec_sem = asyncio.Semaphore(self.up.vector_parallel)
        self.store.load_into(self.client.cookies.jar)
        # окремий облік змін для jar тайлів (той самий файл), щоб він не затер
        # свіжіший session_edessb старим значенням
        self.tile_store = CookieStore(settings.cookies_path)
        self.tile_store.load_into(self.tile_client.cookies.jar)
        self._last_session_mark = 0.0
        self._tile_fails = 0           # поспіль відмов джерела тайлів
        self._tile_pause_until = 0.0   # «запобіжник»: не смикаємо джерело, поки воно відмовляє

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
        """Клієнт тайлів. Cookies йдуть лише ті, чий домен збігається з хостом тайлів
        (напр. вставлені з «Copy as cURL» для data.gki.com.ua) — session_edessb туди не потрапляє."""
        h = {"User-Agent": self.up.user_agent, "Accept-Language": "uk-UA,uk;q=0.9"}
        if self.up.referer:
            h["Referer"] = self.up.referer
        h.update(self.up.tile_headers or {})
        return httpx.AsyncClient(headers=h, follow_redirects=False, timeout=self.up.timeout,
                                 proxy=self.up.proxy or None,
                                 limits=httpx.Limits(max_connections=self.up.tile_parallel * 2))

    def _make_vec_client(self) -> httpx.AsyncClient:
        """Клієнт «сервера ДЗК (2)» (векторні тайли): без cookies, свої заголовки."""
        h = {"User-Agent": self.up.user_agent, "Accept": "application/x-protobuf,*/*",
             "Accept-Encoding": "gzip"}
        h.update(self.up.vector_headers or {})
        return httpx.AsyncClient(headers=h, follow_redirects=True, timeout=self.up.timeout,
                                 proxy=self.up.proxy or None,
                                 limits=httpx.Limits(max_connections=self.up.vector_parallel * 2))

    async def refresh(self) -> None:
        """Підхопити змінений конфіг і нові cookies без перезапуску."""
        m = self._mtime(settings.upstream_file)
        if m != self._cfg_mtime:
            self._cfg_mtime = m
            self.up = load_upstream(settings.upstream_file)
            old, old_t, old_v = self.client, self.tile_client, self.vec_client
            self.client = self._make_client()
            self.tile_client = self._make_tile_client()
            self.vec_client = self._make_vec_client()
            self.sem = asyncio.Semaphore(self.up.max_parallel)
            self.tile_sem = asyncio.Semaphore(self.up.tile_parallel)
            self.vec_sem = asyncio.Semaphore(self.up.vector_parallel)
            self.store.load_into(self.client.cookies.jar)
            self.tile_store.load_into(self.tile_client.cookies.jar)
            await old.aclose()
            await old_t.aclose()
            await old_v.aclose()
            log.info("upstream.toml перечитано")
        else:
            if self.store.changed_on_disk():
                self.store.load_into(self.client.cookies.jar)
            if self.tile_store.changed_on_disk():
                self.tile_store.load_into(self.tile_client.cookies.jar)

    async def close(self) -> None:
        await self.client.aclose()
        await self.tile_client.aclose()
        await self.vec_client.aclose()

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
        return bool(self.up.vector_tile_url or self.up.identify_url
                    or (self.up.wms_url and self.up.identify_mode in ("auto", "wms")))

    def _identify_request_url(self, lat: float, lng: float, zoom: int) -> str | None:
        x, y = to_3857(lng, lat)
        if self.up.identify_url and self.up.identify_mode in ("auto", "url"):
            d = 2 * 20037508.34 / (256 * 2 ** zoom) * 3   # ~3 пікселі
            return self.up.identify_url.format(
                lat=lat, lng=lng, lon=lng, x=x, y=y, zoom=zoom,
                bbox=f"{x - d},{y - d},{x + d},{y + d}")
        if self.up.wms_url and self.up.identify_mode in ("auto", "wms"):
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
            return self.up.wms_url + ("&" if "?" in self.up.wms_url else "?") + urlencode(params)
        return None

    def _cache_parcel(self, p: dict) -> None:
        if p.get("cadnum") and p.get("geometry") and not p.get("partial_geometry"):
            db.conn().execute("INSERT OR REPLACE INTO parcel_cache(cadnum, data, fetched_at) VALUES (?,?,?)",
                              (p["cadnum"], json.dumps(p, ensure_ascii=False), int(time.time())))

    async def _identify_edessb_point(self, lat: float, lng: float, zoom: int) -> list[dict]:
        """Один запит до ЄДЕССБ у точці. [] — нічого не знайдено; помилки — UpstreamError."""
        url = self._identify_request_url(lat, lng, zoom)
        try:
            resp = await self._get(url, "application/json, */*")
        except UpstreamError as e:
            if e.kind == "notfound":
                return []
            raise
        data = self._json(resp)
        if isinstance(data, dict) and data.get("success") is False:
            return []
        if isinstance(data, dict):
            self._mark_session_ok()
        up = dataclasses.replace(self.up, result_path=self.up.identify_result_path)
        out = []
        for p in parse_parcels(data, up):
            if not p.get("geometry") and p.get("cadnum") and self.up.search_url:
                try:
                    p = await self.search(p["cadnum"])
                except UpstreamError:
                    pass
            self._cache_parcel(p)
            out.append(p)
        return out

    async def identify(self, lat: float, lng: float, zoom: int) -> list[dict]:
        """Тап по карті.

        1) ЄДЕССБ (identify_url) у самій точці — повні дані і контур;
        2) якщо в точці порожньо — ще кілька точок навколо (кільцями, ближче → далі):
           межі ДЗК не завжди збігаються з тим, що видно, а палець на телефоні не точний;
        3) якщо ЄДЕССБ недоступна або й навколо нічого немає — векторні тайли
           «сервера ДЗК (2)»."""
        url = self._identify_request_url(lat, lng, zoom)
        edessb_err: UpstreamError | None = None
        if url:
            try:
                out = await self._identify_edessb_point(lat, lng, zoom)
                if not out:
                    out = await self._identify_around(lat, lng, zoom)
                if out or not self.has_vector:
                    return out
            except UpstreamError as e:
                if e.kind != "notfound":
                    if not self.has_vector:
                        raise
                    edessb_err = e
                elif not self.has_vector:
                    return []
        if self.has_vector:
            # сюди потрапляємо, коли ЄДЕССБ ні в точці, ні навколо нічого не знайшла
            # (url є, помилки немає) або недоступна (edessb_err)
            return await self._identify_vector(lat, lng, edessb_err,
                                               point_checked=bool(url) and edessb_err is None)
        raise UpstreamError("config", "Пошук за точкою не налаштовано")

    async def _identify_around(self, lat: float, lng: float, zoom: int) -> list[dict]:
        """Опитує точки навколо тапу. Два кільця: ~6 і ~14 пікселів поточного масштабу
        (на z17 це ≈5 і ≈12 м). Зупиняється, щойно щось знайдено на кільці."""
        px = 156543.03 * math.cos(math.radians(lat)) / (2 ** zoom)   # метрів у пікселі
        m_lat = 1 / 111320.0
        m_lng = 1 / (111320.0 * math.cos(math.radians(lat)))
        found: dict[str, dict] = {}
        for ring_px, n in ((6, 4), (14, 8)):
            r = max(2.0, min(40.0, ring_px * px))
            pts = []
            for i in range(n):
                a = 2 * math.pi * i / n + (math.pi / 4 if n == 4 else 0)
                pts.append((lat + r * math.cos(a) * m_lat, lng + r * math.sin(a) * m_lng))
            results = await asyncio.gather(*(self._identify_edessb_point(la, ln, zoom) for la, ln in pts),
                                           return_exceptions=True)
            err = None
            for res in results:
                if isinstance(res, UpstreamError):
                    err = res
                    continue
                if isinstance(res, Exception):
                    continue
                for p in res:
                    cad = p.get("cadnum") or f"?{len(found)}"
                    if cad not in found:
                        p = dict(p)
                        p["near_m"] = round(r)
                        found[cad] = p
            if found:
                break
            if err is not None and not found:
                raise err
        return list(found.values())

    async def _identify_vector(self, lat: float, lng: float,
                               edessb_err: UpstreamError | None = None,
                               point_checked: bool = False) -> list[dict]:
        """Тап по карті через векторні тайли «сервера ДЗК (2)».

        У тайлі є номер, призначення, площа, адреса — але контур обрізаний межами
        тайла. Тому для знайденої ділянки беремо повні дані з ЄДЕССБ (dzk.api,
        з кешем), а якщо ЄДЕССБ недоступна — показуємо дані з тайла."""
        z = self.up.vector_identify_zoom
        n = 2 ** z
        tx = int((lng + 180) / 360 * n)
        ty = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
        gz, _ = await self.vtile(z, tx, ty)
        if not gz:
            return []
        hits = features_at(gzip.decompress(gz), self.up.vector_layer, z, tx, ty, lng, lat)
        # дрібніші ділянки першими: тап «всередині» вкладеної ділянки має знайти саме її
        hits.sort(key=lambda h: float(h["props"].get("area") or 0))
        out = []
        for i, h in enumerate(hits[:5]):
            p = to_parcel({"properties": h["props"], "geometry": h["geometry"]}, self.up)
            p["partial_geometry"] = True
            p["source"] = "dzk2"
            p.pop("area_calc_ha", None)          # площа клаптя тайла нічого не означає
            p["warning"] = OLD_LAYER_WARNING
            if edessb_err is not None:
                # ЄДЕССБ щойно відмовила — не смикаємо її вдруге, показуємо дані з тайла
                p["warning"] = f"ЄДЕССБ недоступна ({_why(edessb_err)}). " + OLD_LAYER_WARNING
            elif i == 0 and p.get("cadnum") and self.up.search_url:
                try:
                    full = await self.search(p["cadnum"])
                    for k in ("address", "purpose", "area", "unit"):
                        if p.get(k) and not full.get(k):
                            full[k] = p[k]
                    full = dict(full)
                    full["source"] = "dzk2"
                    full["warning"] = (
                        "Ділянку визначено за старим шаром ДЗК (2): у цій точці ЄДЕССБ зараз "
                        "ділянки не знаходить — її могли змінити, поділити або скасувати. "
                        "Дані нижче — з ЄДЕССБ за кадастровим номером."
                        if point_checked else
                        "Ділянку визначено за старим шаром ДЗК (2) — перевірте, чи це актуальна ділянка. "
                        "Дані нижче — з ЄДЕССБ за кадастровим номером.")
                    out.append(full)
                    continue
                except UpstreamError as e:
                    if e.kind == "notfound":
                        p["warning"] = ("Цього номера немає в ЄДЕССБ — ймовірно, ділянку скасовано або змінено. "
                                        + OLD_LAYER_WARNING)
                    else:
                        p["warning"] = f"ЄДЕССБ недоступна ({_why(e)}). " + OLD_LAYER_WARNING
            out.append(p)
        return out

    # ------------------------------------------------------------ сервер ДЗК (2): векторні тайли
    @property
    def has_vector(self) -> bool:
        return bool(self.up.vector_tile_url)

    async def vtile(self, z: int, x: int, y: int) -> tuple[bytes, bool]:
        """(gzip-байти MVT або b"" для порожнього тайла, з_кешу)."""
        if not self.has_vector:
            raise UpstreamError("config", "Сервер ДЗК (2) не налаштовано")
        path = settings.data_dir / "vtilecache" / str(z) / str(x) / f"{y}.pbf.gz"
        ttl = self.up.tile_cache_hours * 3600
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            return path.read_bytes(), True
        await self.refresh()
        url = self.up.vector_tile_url.format(z=z, x=x, y=y)
        try:
            async with self.vec_sem:
                # сирі байти: сервер позначає gzip навіть текстові 404 — авторозпакування падає
                async with self.vec_client.stream("GET", url) as r:
                    raw = b"".join([chunk async for chunk in r.aiter_raw()])
                    status = r.status_code
        except httpx.HTTPError as e:
            if path.exists():
                return path.read_bytes(), True
            raise UpstreamError("network", f"Сервер ДЗК (2): {e.__class__.__name__}")
        if status in (204, 404):
            data = b""
        elif status == 200:
            data = raw if raw[:2] == b"\x1f\x8b" else gzip.compress(raw, 6)
            try:
                gzip.decompress(data)
            except (OSError, EOFError, zlib.error):
                raise UpstreamError("bad", "Сервер ДЗК (2): пошкоджений тайл")
        else:
            log.warning("Векторний тайл %s → HTTP %s", url, status)
            if path.exists():
                return path.read_bytes(), True
            raise UpstreamError("busy" if status in (429, 503) else "bad", f"Сервер ДЗК (2): HTTP {status}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return data, False

    # ------------------------------------------------------------ тайли шару ДЗК
    @property
    def has_layer(self) -> bool:
        return bool(self.up.tile_url or self.up.wms_url)

    async def _fetch_tile(self, url: str) -> bytes | None:
        """Байти тайла; None — тайл порожній (нема ділянок). Помилки — UpstreamError."""
        if self.up.tile_session:
            resp = await self._get(url, "image/webp,image/png,image/*;q=0.8")
        else:
            resp = await self._fetch_public_tile(url)
            if resp.status_code in (204, 404):
                return None
        if resp.status_code == 204 or not resp.content:
            return None
        if not resp.headers.get("content-type", "").startswith("image/") or not sniff(resp.content):
            raise UpstreamError("bad", "Тайл: відповідь не є зображенням")
        return resp.content

    async def _fetch_public_tile(self, url: str) -> httpx.Response:
        """Запит до публічного джерела тайлів з повторами і «запобіжником».

        data.gki.com.ua (nginx за Cloudflare) обмежує частоту: на залп запитів відповідає
        503/429/403 і якийсь час «карає». Тому: мало паралельних запитів, повтор з
        паузою, а після серії відмов — пауза для всіх тайлів (віддаємо кеш)."""
        if time.time() < self._tile_pause_until:
            raise UpstreamError("busy", "Джерело тайлів перевантажене, пауза")
        await self.refresh()
        last = ""
        for attempt in range(self.up.tile_retries + 1):
            if attempt:
                await asyncio.sleep(self.up.tile_retry_delay * attempt + random.uniform(0, 0.5))
                if time.time() < self._tile_pause_until:
                    break
            async with self.tile_sem:
                try:
                    resp = await self.tile_client.get(url)
                except httpx.HTTPError as e:
                    last = e.__class__.__name__
                    continue
                if self.tile_store.save_from(self.tile_client.cookies.jar, "tiles"):
                    log.info("Джерело тайлів оновило свої cookies")
            if resp.status_code in (200, 204, 404):
                self._tile_fails = 0
                return resp
            last = f"HTTP {resp.status_code}"
            if resp.status_code not in (403, 429, 500, 502, 503, 504):
                break
        # статус сесії ЄДЕССБ не чіпаємо: це окреме публічне джерело
        self._tile_fails += 1
        if self._tile_fails >= self.up.tile_breaker_after:
            self._tile_pause_until = time.time() + self.up.tile_breaker_pause
            self._tile_fails = 0
            log.warning("Джерело тайлів відмовляє (%s) — пауза %ss", last, self.up.tile_breaker_pause)
        else:
            log.warning("Тайл %s → %s", url, last)
        raise UpstreamError("busy" if last.startswith(("HTTP 503", "HTTP 429", "HTTP 403")) else "bad",
                            f"Тайл: {last}")

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
