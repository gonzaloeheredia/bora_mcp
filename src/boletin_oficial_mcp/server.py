"""Servidor MCP (stdio) del Boletín Oficial."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mcp.server.fastmcp import FastMCP

from boletin_oficial_mcp.bora import buscar

mcp = FastMCP("boletin-oficial")
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


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
