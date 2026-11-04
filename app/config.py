"""Налаштування: змінні оточення (.env) + config/upstream.toml (джерело даних ДЗК)."""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _load_dotenv(path: Path) -> None:
    """Мінімальний парсер .env, щоб не тягнути залежність (у Docker env передає compose)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


@dataclass
class Settings:
    data_dir: Path
    public_url: str
    registration: str          # approval | invite | closed
    invite_code: str
    session_days: int
    cookie_secure: bool
    upstream_file: Path
    telegram_token: str
    telegram_chat: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def cookies_path(self) -> Path:
        return self.data_dir / "edessb_cookies.json"

    @property
    def status_path(self) -> Path:
        return self.data_dir / "edessb_status.json"

    @property
    def tile_cache_dir(self) -> Path:
        return self.data_dir / "tilecache"


def load_settings() -> Settings:
    data_dir = Path(_env("DATA_DIR", str(BASE_DIR / "data"))).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        public_url=_env("PUBLIC_URL", "http://localhost:8000"),
        registration=_env("REGISTRATION", "approval").lower(),
        invite_code=_env("INVITE_CODE"),
        session_days=int(_env("SESSION_DAYS", "30")),
        cookie_secure=_env("COOKIE_SECURE", "true").lower() in ("1", "true", "yes"),
        upstream_file=Path(_env("UPSTREAM_CONFIG", str(BASE_DIR / "config" / "upstream.toml"))),
        telegram_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat=_env("TELEGRAM_CHAT_ID"),
    )


# ---------------------------------------------------------------- upstream.toml

DEFAULT_FIELDS = {
    "cadnum": ["cadnum", "cad_num", "cadastralNumber", "cadastral_number", "kadnum", "kadastr", "cadastr_number"],
    "area": ["area", "square", "area_ha", "ploshcha", "plosha", "areaHa"],
    "unit": ["unit_area", "area_unit", "unit"],
    "purpose": ["purpose", "use", "purpose_name", "cilpr", "cel_pr", "category_purpose", "intended_use"],
    "purpose_code": ["purpose_code", "kvcpz", "kvzr", "use_code"],
    "ownership": ["ownership", "ownershipvalue", "form_of_ownership", "ownership_form", "vlasn"],
    "category": ["category", "land_category", "cat_name"],
    "address": ["address", "location", "adress", "settlement"],
}


# Підписи полів відповіді ЄДЕССБ dzk.api, які не входять в основні
DEFAULT_LABELS = {
    "category_code": "Код категорії",
    "use": "Використання",
    "ownershipcode": "Код форми власності",
    "valuation_value": "Нормативна грошова оцінка, грн",
    "valuation_date": "Дата оцінки",
    "koatuu": "КОАТУУ",
}
# Технічні поля, які дублюють геометрію/центроїд
DEFAULT_HIDDEN = ["ppoint", "zoom_to", "category_code"]


@dataclass
class Upstream:
    # загальне
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    headers: dict = field(default_factory=dict)
    proxy: str = ""
    timeout: float = 20.0
    max_parallel: int = 4
    cookie_domain: str = "admin.e-construction.gov.ua"
    session_cookie: str = "session_edessb"   # назва сесійної cookie ЄДЕССБ
    referer: str = "https://admin.e-construction.gov.ua/"
    # keepalive
    keepalive_urls: list = field(default_factory=list)
    keepalive_interval: int = 240
    keepalive_jitter: int = 60
    alive_marker: str = ""
    alive_json: str = ""                # ключ JSON, який має бути truthy (напр. "success")
    login_markers: list = field(default_factory=lambda: ["id.gov.ua", "/login", "/auth", "signin"])
    # шар ДЗК
    tile_url: str = ""
    wms_url: str = ""
    wms_layers: str = ""
    wms_styles: str = ""
    wms_version: str = "1.1.1"
    wms_format: str = "image/png"
    wms_extra: dict = field(default_factory=dict)
    tile_cache_hours: int = 72
    min_zoom: int = 12
    max_native_zoom: int = 19          # вище — тайли розтягуються в браузері
    tile_session: bool = False         # True — слати cookies ЄДЕССБ і вважати помилку тайла падінням сесії
    tile_parallel: int = 3             # одночасних запитів до джерела тайлів (воно обмежує частоту)
    tile_retries: int = 2              # повторів при 503/429/403/мережевій помилці
    tile_retry_delay: float = 1.5      # сек; затримка росте з кожною спробою
    tile_breaker_after: int = 6        # стільки тайлів поспіль не вдалося → пауза
    tile_breaker_pause: int = 30       # сек паузи, протягом якої віддаємо лише кеш
    tile_headers: dict = field(default_factory=dict)
    # «Сервер ДЗК (2)» — векторні тайли (MVT .pbf): межі + тап по карті без ЄДЕССБ
    vector_tile_url: str = ""
    vector_layer: str = "parcels_ua"
    vector_min_zoom: int = 14
    vector_max_native_zoom: int = 18
    vector_identify_zoom: int = 16     # з якого масштабу брати тайл для тапу
    vector_parallel: int = 6
    vector_headers: dict = field(default_factory=dict)
    # пошук / ідентифікація
    search_url: str = ""
    identify_url: str = ""
    identify_mode: str = "auto"         # auto | url | wms
    wms_info_format: str = "application/json"
    geometry_crs: str = "auto"          # auto | EPSG:4326 | EPSG:3857 | EPSG:6381 ...
    result_path: str = ""               # шлях до об'єкта у відповіді пошуку, напр. "result"
    identify_result_path: str = ""      # те саме для відповіді identify
    fields: dict = field(default_factory=lambda: dict(DEFAULT_FIELDS))
    labels: dict = field(default_factory=lambda: dict(DEFAULT_LABELS))
    hidden_fields: list = field(default_factory=lambda: list(DEFAULT_HIDDEN))


def load_upstream(path: Path) -> Upstream:
    up = Upstream()
    if not path.exists():
        return up
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    flat: dict = {}
    for section in ("general", "keepalive", "layer", "layer2", "parcel"):
        flat.update(raw.get(section, {}))
    for k, v in raw.items():
        if not isinstance(v, dict) or k in ("headers", "fields", "labels", "wms_extra", "tile_headers", "vector_headers"):
            flat[k] = v
    for hk in ("headers", "tile_headers", "vector_headers"):
        if isinstance(flat.get(hk), dict):
            bad = [k for k, v in flat[hk].items() if not isinstance(v, str)]
            for k in bad:
                # типова помилка TOML: параметр записано ПІСЛЯ [*.headers] і він став заголовком
                logging.getLogger("config").warning(
                    "upstream.toml: «%s» у [%s] не рядок — пропущено (параметри пишіть ДО таблиці заголовків)", k, hk)
                flat[hk].pop(k)
    for k, v in flat.items():
        if k == "fields":
            merged = dict(DEFAULT_FIELDS)
            merged.update(v)
            up.fields = merged
        elif k == "labels":
            up.labels = {**DEFAULT_LABELS, **v}
        elif k == "hidden_fields":
            up.hidden_fields = list(DEFAULT_HIDDEN) + list(v)
        elif hasattr(up, k):
            setattr(up, k, v)
    return up


settings = load_settings()
