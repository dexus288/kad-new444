"""Вхід лише для читання для CRM (dexus288/edessb-crm).

CRM не отримує cookies ЄДЕССБ: вона шле запит сюди з ключем CRM_API_KEY, а сервіс
сам іде в ЄДЕССБ з тією сесією, яку вже тримає keepalive.

    GET /api/crm/edessb/<шлях у кабінеті>?<параметри>
    заголовок X-API-Key: <CRM_API_KEY>

Без CRM_API_KEY у .env вхід вимкнено (404). Дозволено лише GET і лише шляхи, що
починаються з одного з префіксів CRM_ALLOW_PREFIXES (через кому). За замовчуванням —
API кабінету та розділи зі списками документів (схеми намірів, проєктна документація /
повідомлення й декларації, технічна інвентаризація).
"""
from __future__ import annotations

import hmac
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .config import _env
from .upstream import UpstreamError

router = APIRouter()
SAFE_PATH = re.compile(r"^[A-Za-z0-9._\-/]{1,200}$")
DEFAULT_PREFIXES = (
    "api-user/",
    "intention_schema",
    "project_documentation_filter",
    "technical_inventory_employee_ARM",
    "ti_dov",
)


def allowed_prefixes() -> tuple[str, ...]:
    raw = _env("CRM_ALLOW_PREFIXES")
    if not raw:
        return DEFAULT_PREFIXES
    return tuple(p.strip().lstrip("/") for p in raw.split(",") if p.strip())


def _check_key(request: Request) -> None:
    expected = _env("CRM_API_KEY")
    if not expected:
        raise HTTPException(404)
    given = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "Невірний ключ")


def _path_ok(path: str) -> bool:
    if not SAFE_PATH.match(path) or ".." in path or path.startswith("/"):
        return False
    for p in allowed_prefixes():
        # префікс без «/» в кінці — це розділ: дозволяємо сам розділ і все під ним
        if path == p.rstrip("/") or path.startswith(p if p.endswith("/") else p + "/") or path.startswith(p + "?"):
            return True
    return False


@router.get("/api/crm/edessb/{path:path}", include_in_schema=False)
async def crm_edessb(path: str, request: Request):
    _check_key(request)
    if not _path_ok(path):
        raise HTTPException(400, "Недопустимий шлях")
    edessb = request.app.state.edessb
    url = f"https://{edessb.up.cookie_domain}/{path}"
    if request.url.query:
        url += "?" + request.url.query
    accept = request.headers.get("accept") or "application/json"
    try:
        resp = await edessb._get(url, accept)
    except UpstreamError as e:
        code = {"session": 503, "network": 502, "notfound": 404, "config": 501, "bad": 502, "busy": 503}[e.kind]
        raise HTTPException(code, {"kind": e.kind, "message": e.message})
    return Response(resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))
