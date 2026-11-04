#!/usr/bin/env python3
"""Службові команди.

    python manage.py create-admin <логін> [ім'я]   # створити/підвищити адміна (пароль запитає)
    python manage.py set-password <логін>
    python manage.py users
    python manage.py check                          # одноразова перевірка сесії ЄДЕССБ
    python manage.py import-cookies <файл>          # завантажити cookies з файлу
    python manage.py clear-cache                    # очистити кеш тайлів і пошуку
"""
from __future__ import annotations

import getpass
import shutil
import sys

from app import auth, db
from app.config import load_upstream, settings
from app.cookiestore import CookieStore, parse_cookie_input
from app.edessb import check_once


def ask_pw() -> str:
    while True:
        a = getpass.getpass("Пароль (мін. 8): ")
        if len(a) < 8:
            print("Закороткий"); continue
        if a != getpass.getpass("Ще раз: "):
            print("Не збігається"); continue
        return a


def main(argv: list[str]) -> int:
    db.init()
    if not argv:
        print(__doc__); return 1
    cmd, args = argv[0], argv[1:]
    c = db.conn()
    if cmd == "create-admin" and args:
        username = args[0]
        name = " ".join(args[1:]) or username
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row:
            c.execute("UPDATE users SET role='admin', status='active' WHERE id=?", (row["id"],))
            print(f"@{username} тепер адміністратор")
        else:
            c.execute("INSERT INTO users(username,name,pw_hash,role,status) VALUES (?,?,?,'admin','active')",
                      (username, name, auth.hash_password(ask_pw())))
            print(f"Створено адміністратора @{username}")
    elif cmd == "set-password" and args:
        n = c.execute("UPDATE users SET pw_hash=? WHERE username=?", (auth.hash_password(ask_pw()), args[0])).rowcount
        c.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (args[0],))
        print("Готово" if n else "Користувача не знайдено")
    elif cmd == "users":
        for r in c.execute("SELECT username, name, role, status FROM users ORDER BY id"):
            print(f"{r['username']:<20} {r['role']:<6} {r['status']:<8} {r['name']}")
    elif cmd == "check":
        st = check_once(settings, load_upstream(settings.upstream_file), "cli")
        print(("OK: " if st["ok"] else "FAIL: ") + st["message"])
        return 0 if st["ok"] else 1
    elif cmd == "import-cookies" and args:
        up = load_upstream(settings.upstream_file)
        cookies = parse_cookie_input(open(args[0], encoding="utf-8").read(), up.cookie_domain, up.session_cookie)
        CookieStore(settings.cookies_path).merge_domains(cookies, "cli")
        print(f"Імпортовано {len(cookies)} cookies")
    elif cmd == "clear-cache":
        shutil.rmtree(settings.tile_cache_dir, ignore_errors=True)
        c.execute("DELETE FROM parcel_cache")
        print("Кеш очищено")
    else:
        print(__doc__); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
