"""Contexto persistente de Playwright (Chromium) para el Boletín Oficial."""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import BrowserContext, Playwright, sync_playwright

DATA_DIR = Path(os.environ.get("BOLETIN_MCP_HOME") or (Path.home() / ".boletin-oficial-mcp"))
BROWSER_DIR = DATA_DIR / "browser"
DEBUG_DIR = DATA_DIR / "debug"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_playwright: Playwright | None = None
_context: BrowserContext | None = None


def data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def debug_dir() -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    return DEBUG_DIR


def _chromium_args() -> list[str]:
    args = ["--disable-blink-features=AutomationControlled"]
    if os.environ.get("RAILWAY_ENVIRONMENT") or Path("/.dockerenv").exists():
        args.extend(
            [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
            ]
        )
    return args


def get_context() -> BrowserContext:
    """Reusa un perfil persistente para pasar el challenge F5 del sitio."""
    global _playwright, _context
    if _context is not None:
        return _context
    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
    _playwright = sync_playwright().start()
    _context = _playwright.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_DIR),
        headless=True,
        locale="es-AR",
        timezone_id="America/Argentina/Buenos_Aires",
        user_agent=USER_AGENT,
        viewport={"width": 1400, "height": 900},
        args=_chromium_args(),
    )
    return _context


def close_context() -> None:
    global _playwright, _context
    if _context is not None:
        _context.close()
        _context = None
    if _playwright is not None:
        _playwright.stop()
        _playwright = None
