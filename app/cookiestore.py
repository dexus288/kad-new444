"""Спільне файлове сховище cookies ЄДЕССБ.

Файл читають/пишуть два процеси: веб-застосунок і keepalive.py.
Щоб не затерти одне одного, запис відбувається під flock і як *злиття*:
процес записує лише ті cookies, які змінилися відносно того, що він сам
востаннє прочитав з файлу.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

Key = tuple[str, str, str]  # (domain, path, name)


def _key(c: dict) -> Key:
    return (c.get("domain", ""), c.get("path", "/"), c["name"])


def _cookie_to_dict(c: Cookie) -> dict:
    return {
        "name": c.name,
        "value": c.value or "",
        "domain": c.domain,
        "path": c.path or "/",
        "secure": bool(c.secure),
        "expires": c.expires,
    }


def _dict_to_cookie(d: dict) -> Cookie:
    domain = d.get("domain", "")
    return Cookie(
        version=0, name=d["name"], value=d.get("value", ""),
        port=None, port_specified=False,
        domain=domain, domain_specified=bool(domain), domain_initial_dot=domain.startswith("."),
        path=d.get("path", "/"), path_specified=True,
        secure=bool(d.get("secure", True)),
        expires=int(d["expires"]) if d.get("expires") else None,
        discard=not d.get("expires"), comment=None, comment_url=None,
        rest={"HttpOnly": None}, rfc2109=False,
    )


class CookieStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_suffix(".lock")
        self._baseline: dict[Key, str] = {}
        self._mtime = 0.0

    # ------------------------------------------------------------ низький рівень
    @contextmanager
    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data.get("cookies", []) if isinstance(data, dict) else data
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _write(self, cookies: list[dict], source: str) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"updated_at": int(time.time()), "source": source, "cookies": cookies},
            ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        # self._mtime навмисно НЕ оновлюємо: після будь-якого запису (зокрема нових
        # cookies з адмінки) всі jar-и, що читають цей файл, мають перечитати його.

    # ------------------------------------------------------------ API
    def changed_on_disk(self) -> bool:
        try:
            return self.path.stat().st_mtime != self._mtime
        except FileNotFoundError:
            return False

    def load_into(self, jar: CookieJar) -> int:
        """Повністю замінює вміст jar cookies з файлу."""
        with self._locked():
            cookies = self._read()
            try:
                self._mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                self._mtime = 0.0
        jar.clear()
        now = time.time()
        for d in cookies:
            if d.get("expires") and d["expires"] < now:
                continue
            jar.set_cookie(_dict_to_cookie(d))
        self._baseline = {_key(d): d.get("value", "") for d in cookies}
        return len(cookies)

    def save_from(self, jar: CookieJar, source: str) -> bool:
        """Зберігає змінені cookies (злиття з файлом). Повертає True, якщо щось записано."""
        current = {_key(d): d for d in (_cookie_to_dict(c) for c in jar)}
        changed = {k: d for k, d in current.items() if self._baseline.get(k) != d["value"]}
        if not changed:
            return False
        with self._locked():
            on_disk = {_key(d): d for d in self._read()}
            on_disk.update(changed)
            merged = list(on_disk.values())
            self._write(merged, source)
        self._baseline = {k: d.get("value", "") for k, d in on_disk.items()}
        return True

    def merge_domains(self, cookies: list[dict], source: str) -> None:
        """Замінює cookies лише тих доменів, що є у вводі; інші домени не чіпає.
        (Вставка cookies для data.gki.com.ua не стирає session_edessb і навпаки.)"""
        domains = {c.get("domain", "").lstrip(".") for c in cookies}
        with self._locked():
            keep = [d for d in self._read() if d.get("domain", "").lstrip(".") not in domains]
            merged = keep + cookies
            self._write(merged, source)
        self._baseline = {_key(d): d.get("value", "") for d in merged}

    def replace_all(self, cookies: list[dict], source: str) -> None:
        with self._locked():
            self._write(cookies, source)
        self._baseline = {_key(d): d.get("value", "") for d in cookies}

    def summary(self) -> dict:
        cookies = self._read()
        try:
            meta = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        return {
            "count": len(cookies),
            "updated_at": meta.get("updated_at") if isinstance(meta, dict) else None,
            "source": meta.get("source") if isinstance(meta, dict) else None,
            "names": sorted({f'{c["name"]} ({c.get("domain", "")})' for c in cookies}),
        }


# ---------------------------------------------------------------- імпорт
def parse_cookie_input(text: str, default_domain: str, session_name: str = "session_edessb") -> list[dict]:
    """Приймає: JSON-експорт (Cookie-Editor / EditThisCookie), формат Netscape
    cookies.txt, рядок заголовка `Cookie: a=1; b=2`, або лише ЗНАЧЕННЯ
    сесійної cookie (тоді назва — session_name)."""
    text = text.strip()
    if not text:
        raise ValueError("Порожній ввід")

    # «Copy as cURL (bash)» з DevTools: домен береться з URL запиту
    if text.startswith("curl "):
        return _parse_curl(text)

    # Лише значення, напр. «s%3ABp-…» — так його показує DevTools → Application → Cookies
    if "=" not in text.rstrip("=") and ";" not in text and not text.startswith(("[", "{")) \
            and not any(ch.isspace() for ch in text):
        return [{"name": session_name, "value": text, "domain": default_domain,
                 "path": "/", "secure": False, "expires": None}]

    # JSON
    if text[0] in "[{":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("cookies", [data])
        out = []
        for c in data:
            if "name" not in c:
                continue
            exp = c.get("expirationDate") or c.get("expires")
            out.append({
                "name": c["name"], "value": str(c.get("value", "")),
                "domain": c.get("domain") or default_domain,
                "path": c.get("path") or "/",
                "secure": bool(c.get("secure", True)),
                "expires": int(float(exp)) if exp and not c.get("session") else None,
            })
        if not out:
            raise ValueError("У JSON не знайдено cookies")
        return out

    # Netscape cookies.txt
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("# ")]
    if lines and all(len(l.split("\t")) >= 7 for l in lines if not l.startswith("#")):
        out = []
        for l in lines:
            if l.startswith("#HttpOnly_"):
                l = l[len("#HttpOnly_"):]
            elif l.startswith("#"):
                continue
            domain, _flag, path, secure, exp, name, value = l.split("\t")[:7]
            out.append({"name": name, "value": value, "domain": domain, "path": path,
                        "secure": secure.upper() == "TRUE", "expires": int(exp) or None})
        return out

    # Заголовок Cookie
    if text.lower().startswith("cookie:"):
        text = text[7:]
    out = []
    for part in text.replace("\n", ";").split(";"):
        if "=" in part:
            name, value = part.strip().split("=", 1)
            if name:
                out.append({"name": name.strip(), "value": value.strip(),
                            "domain": default_domain, "path": "/", "secure": False, "expires": None})
    if not out:
        raise ValueError("Не вдалося розпізнати cookies")
    return out


def _parse_curl(text: str) -> list[dict]:
    import shlex
    from urllib.parse import urlsplit
    try:
        args = shlex.split(text.replace("\\\n", " "))
    except ValueError as e:
        raise ValueError(f"Не вдалося розібрати cURL ({e}). Копіюйте «Copy as cURL (bash)»")
    url, raw = "", []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-H", "--header") and i + 1 < len(args):
            h = args[i + 1]
            if h.lower().startswith("cookie:"):
                raw.append(h.split(":", 1)[1])
            i += 2
            continue
        if a in ("-b", "--cookie") and i + 1 < len(args):
            raw.append(args[i + 1])
            i += 2
            continue
        if a.startswith("http") and not url:
            url = a
        i += 1
    host = urlsplit(url).hostname if url else None
    if not host:
        raise ValueError("У cURL не знайдено URL")
    out = []
    for chunk in raw:
        for part in chunk.split(";"):
            if "=" in part:
                n, v = part.strip().split("=", 1)
                if n:
                    out.append({"name": n.strip(), "value": v.strip(), "domain": host,
                                "path": "/", "secure": False, "expires": None})
    if not out:
        raise ValueError(f"У цьому запиті до {host} немає cookies — значить, вони там не потрібні")
    return out
