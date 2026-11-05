#!/usr/bin/env python3
"""Підтримка сесії ЄДЕССБ «живою».

Кожні keepalive_interval ± keepalive_jitter секунд робить GET-запити на
keepalive_urls з cookies сесії, зберігає оновлені сервером cookies
(Set-Cookie) у спільний файл і пише статус у data/edessb_status.json.
Якщо сесія впала — шле сповіщення в Telegram (якщо налаштовано) і продовжує
перевіряти: щойно адміністратор завантажить нові cookies, скрипт підхопить
їх автоматично (без перезапуску).

Запуск:
    python keepalive.py            # нескінченний цикл
    python keepalive.py --once     # одна перевірка, код виходу 0 = сесія жива
"""
from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import time

from app.config import load_upstream, settings
from app.cookiestore import CookieStore
from app.edessb import make_sync_client, ping, write_status
from app import db, timemachine

log = logging.getLogger("keepalive")
_stop = False


def _handle_stop(*_):
    global _stop
    _stop = True


def sleep_interruptible(seconds: float, store: CookieStore) -> None:
    """Спимо, але прокидаємось раніше, якщо адмін завантажив нові cookies."""
    start = time.monotonic()
    end = start + seconds
    while not _stop and time.monotonic() < end:
        time.sleep(1)
        if (getattr(sleep_interruptible, "wake_on_change", False)
                and time.monotonic() - start > 20 and store.changed_on_disk()):
            log.info("Виявлено нові cookies — перевіряю одразу")
            return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="одна перевірка і вихід")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.INFO if args.verbose else logging.WARNING)

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    store = CookieStore(settings.cookies_path)
    up = load_upstream(settings.upstream_file)
    client = make_sync_client(up)
    store.load_into(client.cookies.jar)

    if args.once:
        ok, msg = ping(up, store, client)
        write_status(settings, ok, msg, "keepalive --once")
        print(("OK: " if ok else "FAIL: ") + msg)
        return 0 if ok else 1

    log.info("Старт: %d URL, інтервал %ss ± %ss", len(up.keepalive_urls),
             up.keepalive_interval, up.keepalive_jitter)
    cfg_mtime = settings.upstream_file.stat().st_mtime if settings.upstream_file.exists() else 0

    db.init()
    tm_last = 0.0
    while not _stop:
        # машина часу: раз на годину перевіряємо, чи настав час знімка якоїсь області
        if time.time() - tm_last > 3600:
            tm_last = time.time()
            try:
                n = timemachine.run_due()
                if n:
                    log.info("Машина часу: зроблено знімків — %d", n)
            except Exception as e:
                log.warning("Машина часу: %s", e)
        # гаряче перечитування config/upstream.toml
        try:
            m = settings.upstream_file.stat().st_mtime
            if m != cfg_mtime:
                cfg_mtime = m
                up = load_upstream(settings.upstream_file)
                client.close()
                client = make_sync_client(up)
                store.load_into(client.cookies.jar)
                log.info("Конфіг перечитано")
        except FileNotFoundError:
            pass

        ok, msg = ping(up, store, client)
        write_status(settings, ok, msg, "keepalive")
        (log.info if ok else log.warning)("%s %s", "ЖИВА" if ok else "ВПАЛА", msg)

        # Коли сесія мертва — чекаємо нові cookies і перевіряємо одразу, як з'являться
        sleep_interruptible.wake_on_change = not ok
        delay = up.keepalive_interval + random.uniform(-up.keepalive_jitter, up.keepalive_jitter)
        sleep_interruptible(max(30, delay), store)

    client.close()
    log.info("Зупинено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
