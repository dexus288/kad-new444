"""Вхід лише для читання для CRM (dexus288/edessb-crm).

CRM не отримує cookies ЄДЕССБ: вона шле запит сюди з ключем CRM_API_KEY, а сервіс
сам іде в ЄДЕССБ з тією сесією, яку вже тримає keepalive.

    GET /api/crm/edessb/<шлях після /api-user/>?<параметри>
    заголовок X-API-Key: <CRM_API_KEY>

Без CRM_API_KEY у .env вхід вимкнено (404). Дозволено лише GET і лише шляхи /api-user/.
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


def _check_key(request: Request) -> None:
    expected = _env("CRM_API_KEY")
    if not expected:
        raise HTTPException(404)
    given = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "Невірний ключ")


@router.get("/api/crm/edessb/{path:path}", include_in_schema=False)
async def crm_edessb(path: str, request: Request):
    _check_key(request)
    if not SAFE_PATH.match(path) or ".." in path or path.startswith("/"):
        raise HTTPException(400, "Недопустимий шлях")
    edessb = request.app.state.edessb
    url = f"https://{edessb.up.cookie_domain}/api-user/{path}"
    if request.url.query:
        url += "?" + request.url.query
    try:
        resp = await edessb._get(url, "application/json")
    except UpstreamError as e:
        code = {"session": 503, "network": 502, "notfound": 404, "config": 501, "bad": 502, "busy": 503}[e.kind]
        raise HTTPException(code, {"kind": e.kind, "message": e.message})
    return Response(resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))
