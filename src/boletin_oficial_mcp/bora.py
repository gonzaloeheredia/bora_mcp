"""Búsqueda de avisos en boletinoficial.gob.ar.

El sitio no publica una API documentada, pero la búsqueda avanzada sí pega un
POST JSON/AJAX a `/busquedaAvanzada/realizarBusqueda` (confirmado en
`js/busqueda.js` y en el onclick de `#btnBusquedaAvanzada`). Ese endpoint
devuelve `{error, mensajes, content: {html, cantidad_result_seccion, ...}}`.

Un POST crudo con urllib falla (WAF F5). Por eso se llama al mismo endpoint
desde el Chromium persistente, con la sesión ya establecida.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Page

from boletin_oficial_mcp.browser import debug_dir, get_context

BUSQUEDA_URL = "https://www.boletinoficial.gob.ar/busquedaAvanzada/all"
API_BUSQUEDA = "/busquedaAvanzada/realizarBusqueda"
API_BUSQUEDA_SEGUNDA = "/busquedaAvanzada/realizarBusqueda/segunda"
BASE_URL = "https://www.boletinoficial.gob.ar"

# Selectores confirmados contra el DOM real de /busquedaAvanzada/all (2026-09-17).
# El primero de cada lista es el id/class que vimos en el HTML servido; el resto
# queda como fallback por si el sitio cambia el markup.
_CANDIDATOS_PALABRA_CLAVE = [
    "#palabraClave",
    "input[placeholder*='palabra']",
    "input[placeholder*='Buscar por palabra']",
]
_CANDIDATOS_FECHA_DESDE = [
    "#fechaDesdeInput",
    "#fechaDesde input",
    "input[id*='fechaDesde']",
]
_CANDIDATOS_FECHA_HASTA = [
    "#fechaHastaInput",
    "#fechaHasta input",
    "input[id*='fechaHasta']",
]
_CANDIDATOS_BOTON_BUSCAR = [
    "#btnBusquedaAvanzada",
    "button:has-text('Buscar')",
    "button.btn-success",
]
_CANDIDATOS_SECCION = [
    "#magicsuggest",
    ".ms-ctn",
]
_CANDIDATOS_RESULTADOS = [
    "a[href*='/detalleAviso/']",
    "a[href*='/detalleAviso/']:has(.linea-aviso)",
    "div.linea-aviso",
]
_CANDIDATOS_VACIO = [
    "#sinResultadosAvanzada",
    "#sinResultados",
]
_CANDIDATOS_CONTENEDOR_RESULTADOS = [
    "#avisosSeccionDiv",
    "#resultadosBusqueda",
    "#resultadosBusquedaRapida",
]

_SECCION_IDS = {
    "1": 1,
    "primera": 1,
    "primera y suplementos": 1,
    "legislacion": 1,
    "legislación": 1,
    "legislacion y avisos oficiales": 1,
    "2": 2,
    "segunda": 2,
    "sociedades": 2,
    "sociedades y avisos judiciales": 2,
    "3": 3,
    "tercera": 3,
    "contrataciones": 3,
    "4": 4,
    "cuarta": 4,
    "dominios": 4,
    "dominios de internet": 4,
}

_FECHA_RE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b")
_DETALLE_RE = re.compile(
    r"/detalleAviso/(primera|segunda|tercera|cuarta)/([^/]+)(?:/(\d{8}))?",
    re.I,
)


def buscar(
    texto: str,
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    seccion: str | None = None,
    max_resultados: int = 20,
) -> dict[str, Any]:
    """Busca avisos. Formato estable: {ok, resultados, error?}."""
    query = (texto or "").strip()
    if not query:
        return {"ok": False, "resultados": [], "error": "El campo texto es obligatorio."}

    try:
        secciones = _parse_seccion(seccion)
        desde = _normalizar_fecha(fecha_desde)
        hasta = _normalizar_fecha(fecha_hasta)
    except ValueError as exc:
        return {"ok": False, "resultados": [], "error": str(exc)}

    page: Page | None = None
    try:
        context = get_context()
        page = context.new_page()
        page.set_default_timeout(60_000)
        _abrir_busqueda(page)
        payload = _payload_busqueda(query, desde, hasta, secciones, pagina=1)
        acumulado: list[dict[str, str]] = []
        html_total = ""
        cantidades: Any = None
        via_formulario = False
        while len(acumulado) < max(1, max_resultados):
            respuesta = _llamar_api(page, payload)
            if respuesta is None and not acumulado:
                respuesta = _buscar_por_formulario(
                    page, query, desde, hasta, secciones
                )
                via_formulario = True
            lote, cantidades, html_frag, error = _extraer_lote(page, respuesta)
            if error:
                if acumulado:
                    break
                return error
            html_total += html_frag
            nuevos = 0
            vistos = {item["url"] for item in acumulado}
            for item in lote:
                if item["url"] in vistos:
                    continue
                vistos.add(item["url"])
                acumulado.append(item)
                nuevos += 1
                if len(acumulado) >= max_resultados:
                    break
            if via_formulario or nuevos == 0 or not html_frag.strip():
                break
            siguiente = (respuesta.get("content") or {}).get("sig_pag")
            if not siguiente or siguiente == payload.get("numeroPagina"):
                break
            payload = _payload_busqueda(
                query,
                desde,
                hasta,
                secciones,
                pagina=int(siguiente),
                ultima_seccion=str(
                    (respuesta.get("content") or {}).get("ult_seccion") or ""
                ),
                ultimo_rubro=str(
                    (respuesta.get("content") or {}).get("ult_rubro") or ""
                ),
                busqueda_original=False,
            )
        return _cerrar_busqueda(page, acumulado[:max_resultados], html_total, cantidades)
    except Exception as exc:
        _guardar_debug(page, motivo="excepcion")
        return {
            "ok": False,
            "resultados": [],
            "error": f"Fallo al buscar en el Boletín Oficial: {exc}",
        }
    finally:
        if page is not None:
            page.close()


def _abrir_busqueda(page: Page) -> None:
    page.goto(BUSQUEDA_URL, wait_until="domcontentloaded")
    page.wait_for_selector(_CANDIDATOS_PALABRA_CLAVE[0], timeout=45_000)
    page.wait_for_function(
        "() => typeof window.jQuery === 'function' && typeof window.jQuery.ajax === 'function'"
    )
    page.wait_for_timeout(800)


def _payload_busqueda(
    texto: str,
    fecha_desde: str,
    fecha_hasta: str,
    secciones: list[int],
    pagina: int,
    *,
    ultima_seccion: str = "",
    ultimo_rubro: str = "",
    busqueda_original: bool = True,
) -> dict[str, Any]:
    return {
        "busquedaRubro": False,
        "hayMasResultadosBusqueda": True,
        "ejecutandoLlamadaAsincronicaBusqueda": False,
        "ultimaSeccion": ultima_seccion,
        "filtroPorRubrosSeccion": False,
        "filtroPorRubroBusqueda": False,
        "filtroPorSeccionBusqueda": False,
        "busquedaOriginal": busqueda_original,
        "ordenamientoSegunda": False,
        "seccionesOriginales": secciones,
        "ultimoItemExterno": None,
        "ultimoItemInterno": None,
        "texto": texto,
        "rubros": [],
        "nroNorma": "",
        "anioNorma": "",
        "denominacion": "",
        "tipoContratacion": "",
        "anioContratacion": "",
        "nroContratacion": "",
        "fechaDesde": fecha_desde,
        "fechaHasta": fecha_hasta,
        "todasLasPalabras": True,
        "comienzaDenominacion": True,
        "seccion": secciones,
        "tipoBusqueda": "Avanzada",
        "numeroPagina": pagina,
        "ultimoRubro": ultimo_rubro,
    }


def _api_url(secciones: list[int]) -> str:
    if secciones == [2]:
        return API_BUSQUEDA_SEGUNDA
    return API_BUSQUEDA


def _llamar_api(page: Page, params: dict[str, Any]) -> dict[str, Any] | None:
    url = _api_url(list(params.get("seccion") or []))
    try:
        return page.evaluate(
            """async ({ url, params }) => {
                if (typeof jQuery === 'undefined') {
                    throw new Error('jQuery no está disponible');
                }
                return await new Promise((resolve, reject) => {
                    jQuery.ajax({
                        url,
                        dataType: 'json',
                        type: 'POST',
                        data: {
                            params: JSON.stringify(params),
                            array_volver: JSON.stringify([]),
                        },
                        success: (response) => resolve(response),
                        error: (xhr, status, err) => reject(
                            new Error(
                                'HTTP ' + (xhr && xhr.status) + ' ' + status + ' ' + (err || '')
                            )
                        ),
                    });
                });
            }""",
            {"url": url, "params": params},
        )
    except Exception:
        return None


def _buscar_por_formulario(
    page: Page,
    texto: str,
    fecha_desde: str,
    fecha_hasta: str,
    secciones: list[int],
) -> dict[str, Any]:
    _llenar(_CANDIDATOS_PALABRA_CLAVE, page, texto)
    if fecha_desde:
        _llenar(_CANDIDATOS_FECHA_DESDE, page, fecha_desde)
    if fecha_hasta:
        _llenar(_CANDIDATOS_FECHA_HASTA, page, fecha_hasta)
    page.evaluate(
        """(ids) => {
            const ms = window.jQuery('#magicsuggest').magicSuggest();
            ms.clear();
            ms.setValue(ids);
        }""",
        secciones,
    )
    _click(_CANDIDATOS_BOTON_BUSCAR, page)
    page.wait_for_function(
        """() => {
            const loading = document.querySelector('#cargandoListadoBusquedaAvanzada');
            const loadingVisible = loading && window.getComputedStyle(loading).display !== 'none';
            if (loadingVisible) return false;
            const html = (document.querySelector('#resultadosBusqueda') || {}).innerHTML || '';
            const empty = (document.querySelector('#sinResultadosAvanzada') || {}).textContent || '';
            return html.trim().length > 0 || empty.trim().length > 0;
        }""",
        timeout=90_000,
    )
    html = page.eval_on_selector(
        _CANDIDATOS_CONTENEDOR_RESULTADOS[0],
        "el => el.innerHTML",
    ) or ""
    vacio = ""
    for sel in _CANDIDATOS_VACIO:
        loc = page.locator(sel)
        if loc.count():
            vacio = (loc.first.inner_text() or "").strip()
            if vacio:
                break
    return {
        "error": 0,
        "mensajes": [],
        "content": {
            "html": html,
            "cantidad_result_seccion": {},
            "mensaje_vacio": vacio,
        },
        "_via": "formulario",
    }


def _extraer_lote(
    page: Page,
    respuesta: dict[str, Any] | None,
) -> tuple[list[dict[str, str]], Any, str, dict[str, Any] | None]:
    if not isinstance(respuesta, dict):
        _guardar_debug(page, motivo="respuesta-invalida")
        return [], None, "", {
            "ok": False,
            "resultados": [],
            "error": "El sitio no devolvió una respuesta JSON reconocible.",
        }

    error = respuesta.get("error")
    if error not in (0, None, "0"):
        mensajes = respuesta.get("mensajes") or []
        detalle = "; ".join(str(m) for m in mensajes) if mensajes else f"error={error}"
        _guardar_debug(page, motivo="api-error", extra=respuesta)
        return [], None, "", {
            "ok": False,
            "resultados": [],
            "error": f"El Boletín Oficial rechazó la búsqueda: {detalle}",
        }

    content = respuesta.get("content") or {}
    html = content.get("html") or ""
    cantidades = content.get("cantidad_result_seccion")
    return _parsear_resultados(html), cantidades, html, None


def _cerrar_busqueda(
    page: Page,
    resultados: list[dict[str, str]],
    html: str,
    cantidades: Any,
) -> dict[str, Any]:
    if resultados:
        return {"ok": True, "resultados": resultados}

    total_sitio = _total_cantidades(cantidades)
    hay_links = bool(re.search(r"/detalleAviso/", html or ""))
    vacio_en_pagina = _texto_vacio(page)
    mensaje_vacio = vacio_en_pagina.lower()

    if not hay_links and (
        total_sitio == 0
        or "no se pudo encontrar" in mensaje_vacio
        or not (html or "").strip()
    ):
        return {"ok": True, "resultados": []}

    _guardar_debug(page, motivo="selectores-resultados", extra={"html": (html or "")[:50_000]})
    return {
        "ok": False,
        "resultados": [],
        "error": (
            "La búsqueda respondió HTML pero no se pudieron extraer avisos. "
            "Revisá ~/.boletin-oficial-mcp/debug/ (fallo de selector, no 0 resultados)."
        ),
    }


def _parsear_resultados(html: str) -> list[dict[str, str]]:
    if not html or not html.strip():
        return []
    soup = BeautifulSoup(html, "html.parser")
    vistos: set[str] = set()
    resultados: list[dict[str, str]] = []
    for enlace in soup.select(_CANDIDATOS_RESULTADOS[0]):
        href = (enlace.get("href") or "").strip()
        if "/detalleAviso/" not in href or href.startswith("#"):
            continue
        clases = " ".join(enlace.get("class") or [])
        if "btn" in clases.split():
            continue
        url = urljoin(BASE_URL, href.split("?")[0])
        if url in vistos:
            continue
        vistos.add(url)
        seccion = ""
        match = _DETALLE_RE.search(href)
        if match:
            seccion = match.group(1).lower()
        if seccion == "segunda" or enlace.select_one(".linea-aviso") is None:
            parsed = _parsear_aviso_segunda(enlace, href)
        else:
            parsed = _parsear_aviso_primera(enlace, href)
        if match and not parsed["fecha"]:
            parsed["fecha"] = _fecha_aviso("", href)
        parsed["url"] = url
        parsed["seccion"] = seccion
        resultados.append(parsed)
    return resultados


def _parsear_aviso_primera(enlace: Tag, href: str) -> dict[str, str]:
    linea = enlace.select_one(".linea-aviso") or enlace
    titulo_el = linea.select_one("p.item")
    titulo = _texto_visible(titulo_el) if titulo_el else _texto_visible(enlace)
    extras: list[str] = []
    fecha = ""
    for detalle in linea.select("p.item-detalle"):
        texto_det = _texto_visible(detalle)
        if re.search(r"fecha de publicaci[oó]n", texto_det, re.I):
            fecha = _fecha_aviso(texto_det, href)
        elif texto_det:
            extras.append(texto_det)
    if extras and extras[0] not in titulo:
        titulo = f"{titulo} — {extras[0]}".strip(" —")
    resumen = extras[-1] if extras else _resumen_aviso(_texto_visible(linea), titulo)
    return {"titulo": titulo[:300], "fecha": fecha, "resumen": resumen[:500]}


def _parsear_aviso_segunda(enlace: Tag, href: str) -> dict[str, str]:
    linea = enlace.find_parent(class_="linea-aviso")
    rubro_el = linea.select_one("p.item") if isinstance(linea, Tag) else None
    rubro = _texto_visible(rubro_el) if rubro_el else ""
    heading = None
    cursor: Tag | None = linea if isinstance(linea, Tag) else enlace
    if cursor is not None:
        heading = cursor.find_previous("h5", class_="seccion-rubro")
    entidad = _texto_visible(heading) if heading else ""
    titulo = entidad or rubro or _texto_visible(enlace)
    if rubro and rubro not in titulo:
        titulo = f"{titulo} — {rubro}"
    fecha = _fecha_aviso(_texto_visible(enlace), href)
    resumen = rubro
    return {"titulo": titulo[:300], "fecha": fecha, "resumen": resumen[:500]}


def _fecha_aviso(texto: str, href: str) -> str:
    match = _FECHA_RE.search(texto)
    if match:
        return match.group(1).replace("-", "/")
    detalle = _DETALLE_RE.search(href)
    if detalle and detalle.group(3):
        raw = detalle.group(3)
        try:
            return datetime.strptime(raw, "%Y%m%d").strftime("%d/%m/%Y")
        except ValueError:
            return raw
    return ""


def _resumen_aviso(texto: str, titulo: str) -> str:
    limpio = texto
    if titulo and titulo in limpio:
        limpio = limpio.replace(titulo, " ", 1)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    return limpio[:500]


def _texto_visible(nodo: Tag) -> str:
    return re.sub(r"\s+", " ", nodo.get_text(" ", strip=True)).strip()


def _texto_vacio(page: Page) -> str:
    for sel in _CANDIDATOS_VACIO:
        loc = page.locator(sel)
        if loc.count():
            texto = (loc.first.inner_text() or "").strip()
            if texto:
                return texto
    return ""


def _total_cantidades(cantidades: Any) -> int | None:
    if isinstance(cantidades, (int, float)):
        return int(cantidades)
    if isinstance(cantidades, str) and cantidades.isdigit():
        return int(cantidades)
    if not isinstance(cantidades, dict) or not cantidades:
        return None
    total = 0
    hay = False
    for valor in cantidades.values():
        if isinstance(valor, (int, float)):
            total += int(valor)
            hay = True
        elif isinstance(valor, str) and valor.isdigit():
            total += int(valor)
            hay = True
    return total if hay else None


def _parse_seccion(seccion: str | None) -> list[int]:
    if seccion is None or not str(seccion).strip():
        return [1, 2, 3]
    clave = str(seccion).strip().lower()
    clave = re.sub(r"\s+", " ", clave)
    if clave in {"all", "todas", "todas las secciones"}:
        return [1, 2, 3]
    if clave not in _SECCION_IDS:
        raise ValueError(
            "Sección no reconocida. Usá primera, segunda, tercera o cuarta."
        )
    valor = _SECCION_IDS[clave]
    if valor == 4:
        # El magicSuggest de búsqueda avanzada solo lista 1/2/3. Cuarta existe
        # como sección del sitio (dominios) y se intenta igual por id.
        return [4]
    return [valor]


def _normalizar_fecha(valor: str | None) -> str:
    if valor is None or not str(valor).strip():
        return ""
    bruto = str(valor).strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(bruto, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    raise ValueError(f"Fecha inválida: {valor}. Usá DD/MM/YYYY o YYYY-MM-DD.")


def _llenar(candidatos: list[str], page: Page, valor: str) -> None:
    for sel in candidatos:
        loc = page.locator(sel)
        if loc.count():
            loc.first.fill(valor)
            return
    raise RuntimeError(f"No se encontró el campo {candidatos[0]}")


def _click(candidatos: list[str], page: Page) -> None:
    for sel in candidatos:
        loc = page.locator(sel)
        if loc.count():
            loc.first.click()
            return
    raise RuntimeError(f"No se encontró el botón {candidatos[0]}")


def _guardar_debug(page: Page | None, motivo: str, extra: dict[str, Any] | None = None) -> Path | None:
    carpeta = debug_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = carpeta / f"{stamp}-{motivo}"
    destino.mkdir(parents=True, exist_ok=True)
    if extra is not None:
        serializable = dict(extra)
        html = serializable.get("html")
        if isinstance(html, str):
            (destino / "response.html").write_text(html, encoding="utf-8")
            serializable["html"] = f"<{len(html)} chars, ver response.html>"
        (destino / "extra.json").write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    if page is None:
        return destino
    try:
        (destino / "page.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        page.screenshot(path=str(destino / "screenshot.png"), full_page=True)
    except Exception:
        pass
    return destino
