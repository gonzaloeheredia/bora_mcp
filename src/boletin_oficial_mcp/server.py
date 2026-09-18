"""Servidor MCP del Boletín Oficial (stdio local o HTTP en Railway)."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from boletin_oficial_mcp.bora import buscar


def _use_http() -> bool:
    transport = os.environ.get("MCP_TRANSPORT", "").strip().lower()
    if transport in {"http", "streamable-http", "sse"}:
        return True
    if transport == "stdio":
        return False
    return bool(os.environ.get("PORT") or os.environ.get("RAILWAY_ENVIRONMENT"))


def _http_port() -> int:
    return int(os.environ.get("PORT", "8080"))


mcp = FastMCP(
    "boletin-oficial",
    host="0.0.0.0" if _use_http() else "127.0.0.1",
    port=_http_port(),
    stateless_http=True,
)
_playwright_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bora-pw")


@mcp.tool()
async def buscar_boletin_oficial(
    texto: str,
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    seccion: str | None = None,
    max_resultados: int = 20,
) -> dict[str, Any]:
    """Busca avisos en el Boletín Oficial de la República Argentina.

    Args:
        texto: Palabra o frase a buscar (usar comillas para frase exacta).
        fecha_desde: Fecha inicial DD/MM/YYYY o YYYY-MM-DD.
        fecha_hasta: Fecha final DD/MM/YYYY o YYYY-MM-DD.
        seccion: primera, segunda, tercera o cuarta. Vacío busca las tres primeras.
        max_resultados: Tope de avisos a devolver (default 20).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _playwright_thread,
        lambda: buscar(
            texto=texto,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
            seccion=seccion,
            max_resultados=max_resultados,
        ),
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> Response:
    return JSONResponse({"ok": True})


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path in {"/health", "/"}:
            return await call_next(request)
        expected = os.environ.get("MCP_API_KEY", "").strip()
        if not expected:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {expected}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def _run_http() -> None:
    import uvicorn

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)
    uvicorn.run(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )


def main() -> None:
    if _use_http():
        _run_http()
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
