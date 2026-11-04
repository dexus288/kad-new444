"""Перевірка/підтримка сесії ЄДЕССБ. Спільний код для keepalive.py і адмін-панелі."""
from __future__ import annotations

import json
import logging
import os
import time
from urllib.parse import urlsplit

import httpx

from .config import Settings, Upstream
from .cookiestore import CookieStore

log = logging.getLogger("edessb")


def base_headers(up: Upstream) -> dict:
    h = {
        "User-Agent": up.user_agent,
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.6",
    }
    if up.referer:
        h["Referer"] = up.referer
        u = urlsplit(up.referer)
        h["Origin"] = f"{u.scheme}://{u.netloc}"
    h.update(up.headers or {})
    return h


def looks_logged_out(resp: httpx.Response, up: Upstream) -> str | None:
    """Повертає причину, якщо відповідь схожа на «сесія закінчилась», інакше None."""
    if resp.status_code in (401, 403, 419, 440):
        return f"HTTP {resp.status_code}"
    urls = [str(r.url) for r in resp.history] + [str(resp.url)]
    for r in [*resp.history, resp]:
        urls.append(r.headers.get("location", ""))
    for u in urls:
        low = u.lower()
        for m in up.login_markers:
            if m and m.lower() in low:
                return f"редирект на сторінку входу ({u[:120]})"
    if 300 <= resp.status_code < 400:
        # API ЄДЕССБ не редиректить, коли все гаразд — редирект = вихід із сесії
        return f"редирект ({resp.status_code}) на {resp.headers.get('location', '?')[:120]}"
    if resp.status_code >= 500:
        return f"сервер ЄДЕССБ відповів {resp.status_code}"
    if resp.status_code >= 400:
        return f"HTTP {resp.status_code}"
    if up.alive_json:
        try:
            data = resp.json()
        except Exception:
            return "відповідь не JSON (схоже на сторінку входу)"
        if not (isinstance(data, dict) and data.get(up.alive_json)):
            return f"у JSON {up.alive_json}={data.get(up.alive_json) if isinstance(data, dict) else '?'}"
    if up.alive_marker:
        try:
            body = resp.text
        except Exception:
            body = ""
        if up.alive_marker not in body:
            return f"у відповіді немає маркера «{up.alive_marker}»"
    return None


# ---------------------------------------------------------------- статус
def read_status(settings: Settings) -> dict:
    try:
        return json.loads(settings.status_path.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": None, "message": "Ще не перевірялось"}


def write_status(settings: Settings, ok: bool, message: str, source: str) -> dict:
    prev = read_status(settings)
    now = int(time.time())
    st = {
        "ok": ok,
        "message": message,
        "source": source,
        "checked_at": now,
        "last_ok_at": now if ok else prev.get("last_ok_at"),
        "fail_count": 0 if ok else int(prev.get("fail_count") or 0) + 1,
        "down_since": None if ok else (prev.get("down_since") if prev.get("ok") is False else now),
    }
    tmp = settings.status_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, settings.status_path)

    # Сповіщення лише при зміні стану (і не при першому запуску з «ok»)
    if prev.get("ok") is not None and prev.get("ok") != ok:
        if ok:
            notify(settings, "✅ Сесію ЄДЕССБ відновлено.")
        else:
            notify(settings, f"⚠️ Сесія ЄДЕССБ впала: {message}\n"
                             f"Оновіть cookies: {settings.public_url}/#admin")
    elif prev.get("ok") is None and not ok:
        notify(settings, f"⚠️ Сесія ЄДЕССБ неактивна: {message}")
    return st


def notify(settings: Settings, text: str) -> None:
    if not (settings.telegram_token and settings.telegram_chat):
        return
    try:
        httpx.post(f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
                   json={"chat_id": settings.telegram_chat, "text": text}, timeout=10)
    except Exception as e:  # сповіщення не повинні ламати основний цикл
        log.warning("Telegram: %s", e)


# ---------------------------------------------------------------- перевірка
def ping(up: Upstream, store: CookieStore, client: httpx.Client) -> tuple[bool, str]:
    """Один «пінг»: проходить по всіх keepalive_urls. Сесія жива, якщо жоден не впав."""
    if not up.keepalive_urls:
        return False, "Не задано keepalive_urls у config/upstream.toml"
    if store.changed_on_disk():
        n = store.load_into(client.cookies.jar)
        log.info("Перечитано cookies з файлу (%d шт.)", n)
    if not list(client.cookies.jar):
        return False, "Немає cookies — завантажте їх в адмін-панелі"

    for url in up.keepalive_urls:
        try:
            resp = client.get(url)
        except httpx.HTTPError as e:
            store.save_from(client.cookies.jar, "keepalive")
            return False, f"мережева помилка: {e.__class__.__name__}: {e}"[:300]
        store.save_from(client.cookies.jar, "keepalive")  # ротація cookies сервером
        reason = looks_logged_out(resp, up)
        if reason:
            return False, f"{url}: {reason}"
    return True, f"OK ({len(up.keepalive_urls)} URL)"


def make_sync_client(up: Upstream) -> httpx.Client:
    return httpx.Client(
        headers=base_headers(up),
        follow_redirects=False,   # не йдемо на id.gov.ua: редирект = сесія впала
        timeout=up.timeout,
        proxy=up.proxy or None,
    )


def check_once(settings: Settings, up: Upstream, source: str = "manual") -> dict:
    store = CookieStore(settings.cookies_path)
    with make_sync_client(up) as client:
        store.load_into(client.cookies.jar)
        ok, msg = ping(up, store, client)
    return write_status(settings, ok, msg, source)
