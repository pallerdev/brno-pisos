"""Una URL pegada por el usuario en #revisar.

Es una petición iniciada por el usuario, como abrir el enlace en el navegador; no es un rastreo.
Por eso aquí sí se admite Sreality (su robots.txt prohíbe rastrear), pero solo por su API oficial
y con una única petición: nunca se pide el HTML de sreality.cz.
"""
from __future__ import annotations

import math
import re
import sys
from datetime import date
from urllib.parse import urlparse

import httpx

from pisobot.collectors import bazos, bezrealitky, espolubydleni, idnes
from pisobot.collectors.base import HTMLParser, block_text, price_czk
from pisobot.models import Kind, Listing
from pisobot.parse_text import norm, normalize_disposition

# Se aplica a la ruta de la URL: un texto parecido en la consulta no debe disparar una petición al API
SREALITY_ID = re.compile(r"^/detail/(?:[^/]+/){4}(\d+)/?$")
SREALITY_API = "https://www.sreality.cz/api/v1/estates/{id}"
# Hasta dónde llega la precisión del punto: solo la dirección completa es exacta y lo desconocido no se da por bueno
_PRECISION = {"address": "exact", "street": "street"}
# Con solo la ciudad el punto es su centro: geocodificarlo pondría el piso donde no está
_CITY_LEVEL = {"city", "municipality", "region", "country"}
# El encabezado de las listas estructuradas no puede disparar ninguna regla (con "Vybavení" daría furnished=True)
_PARAMS_LABEL = "Parametry inzerátu:"
_ESPOLUBYDLENI_AD = re.compile(r"/\d+-[^/]*\.htm")
_PAGE_TEXT_LIMIT = 8000
# Fallos de estructura de un parser (JSON roto, clave ausente, nodo nulo); un fallo de red no entra aquí
_PARSE_ERRORS = (ValueError, KeyError, IndexError, TypeError, AttributeError)
_REVIEW_HOSTS = frozenset({
    "www.bezrealitky.cz", "bezrealitky.cz",
    "www.ulovdomov.cz", "ulovdomov.cz",
    "www.espolubydleni.cz", "espolubydleni.cz",
    "reality.idnes.cz", "reality.bazos.cz",
    "www.sreality.cz", "sreality.cz",
})


class UnsupportedReviewURL(ValueError):
    """El bot solo abre URLs HTTPS de los portales de alquiler admitidos."""


def _review_url(url: str | httpx.URL) -> httpx.URL:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise UnsupportedReviewURL from None
    if parsed.scheme != "https" or parsed.host not in _REVIEW_HOSTS or parsed.port or parsed.userinfo:
        raise UnsupportedReviewURL
    return parsed


def _get_review_page(http: httpx.Client, url: str) -> httpx.Response:
    target = _review_url(url)
    for _ in range(6):
        response = http.get(target, follow_redirects=False)
        if response.next_request is None:
            return response
        target = _review_url(response.next_request.url)
        if _on(target.host, "sreality.cz"):
            # Sreality se consulta solo a través de su API y a partir de un enlace directo.
            raise UnsupportedReviewURL
    raise httpx.TooManyRedirects("Demasiadas redirecciones")


def fetch_review_url(http: httpx.Client, url: str) -> Listing | str:
    # Los mensajes de Discord no pueden elegir destinos fuera de los portales, ni mediante redirecciones.
    return fetch(http, str(_review_url(url)), review_only=True)


def _name(value) -> str:
    """Los campos *_cb del API son {"name": "Ano", "value": 1}: solo interesa el texto."""
    name = value.get("name") if isinstance(value, dict) else None
    return name if isinstance(name, str) else ""


def _whole(value) -> int | None:
    """Entero >= 0 tal cual (el API manda 13000.0); texto, bool, decimales o no finitos dan None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _money(value) -> int | None:
    """Un texto libre ("3 000 Kč + energie") da su primer importe; juntar todas sus cifras daría uno falso."""
    return price_czk(value) if isinstance(value, str) else _whole(value)


def _area(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        return None
    try:
        return round(value) or None
    except OverflowError:
        return None


def _coordinate(value, limit: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
        return None
    return float(value)


def _iso_date(raw) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _furnished(result: dict) -> bool | None:
    return {"ano": True, "castecne": True, "ne": False}.get(norm(_name(result.get("furnished"))))


def _names(values) -> list[str]:
    return [name for name in (_name(item) for item in values) if name] if isinstance(values, list) else []


def _heating(result: dict) -> str:
    """Las listas de calefacción solo salen en campos estructurados y parse_text las necesita para detectar la
    eléctrica. El tipo ("Lokální elektrické") solo casa con su regla pegado a "topení"; el resto, con su etiqueta."""
    items = [f"{name} topení" for name in _names(result.get("heating_set"))]
    for field, label in (("heating_source_set", "Zdroj topení"), ("heating_element_set", "Topná tělesa")):
        names = _names(result.get(field))
        if names:
            items.append(f"{label}: {', '.join(names)}")
    return f"{_PARAMS_LABEL} {' | '.join(items)}" if items else ""


def _description(result: dict) -> str:
    main = result.get("advert_description")
    parts = [main if isinstance(main, str) else ""]
    # La nota del precio y los gastos suelen decir "+ energie": parse_text los necesita
    parts += [result.get("price_note"), result.get("cost_of_living")]
    parts.append(_heating(result))
    return "\n".join(part for part in parts if isinstance(part, str) and part)


def parse_sreality(payload: dict, url: str) -> Listing:
    result = payload.get("result", payload) if isinstance(payload, dict) else None
    if not isinstance(result, dict) or not result.get("hash_id"):
        raise ValueError("Sreality: la respuesta no trae el anuncio (¿cambió la API?)")
    locality = result.get("locality")
    locality = locality if isinstance(locality, dict) else {}
    category = _name(result.get("category_sub_cb"))
    # price_czk es el precio ya en coronas, pero con otra moneda o periodo no es el alquiler mensual
    currency, unit = _name(result.get("price_currency_cb")), _name(result.get("price_unit_cb"))
    czk = not currency or norm(currency) in ("kc", "czk")
    monthly = not unit or norm(unit) == "za mesic"
    rent = _whole(result.get("price_czk"))
    # Sreality usa 1 Kč como "cena na vyžádání"
    rent = rent if czk and monthly and rent and rent > 1 else None
    accuracy = locality.get("inaccuracy_type")
    accuracy = accuracy if isinstance(accuracy, str) else None
    city_level = accuracy in _CITY_LEVEL
    precision = "none" if city_level else _PRECISION.get(accuracy, "postcode")
    lat, lon = _coordinate(locality.get("gps_lat"), 90), _coordinate(locality.get("gps_lon"), 180)
    if city_level or lat is None or lon is None:
        lat = lon = None
    address = ", ".join(str(x) for x in (locality.get("street"), locality.get("citypart"), locality.get("city")) if x)
    return Listing(
        source="sreality",
        source_id=str(result["hash_id"]),
        url=url,
        title=" ".join(str(result.get("advert_name") or "").split()),
        kind=Kind.ROOM if norm(category) == "pokoj" else Kind.FLAT,
        rent=rent,
        fees=_money(result.get("cost_of_living")) if czk else None,
        deposit=_money(result.get("refundable_deposit")) if czk else None,
        disposition=normalize_disposition(category),
        # En la categoría "Pokoj" la superficie es la de la propia habitación
        area_m2=_area(result.get("usable_area")),
        lat=lat,
        lon=lon,
        location_precision=precision,
        address_text="" if city_level else address,
        description=_description(result),
        available_from=_iso_date(result.get("ready_date")),
        furnished=_furnished(result),
    )


def page_text(html: str) -> str:
    tree = HTMLParser(html)
    # Sin quitarlos, el JavaScript de la página se come el límite y despista a las reglas de precio
    tree.strip_tags(["script", "style", "noscript", "template"])
    return block_text(tree.css_first("body"))[:_PAGE_TEXT_LIMIT]


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _fetch_sreality(http: httpx.Client, url: str) -> Listing | str:
    match = SREALITY_ID.match(urlparse(url).path)
    if match is None:
        # Sin id de anuncio no hay API que consultar, y pedir su HTML sería rastrear la web
        return ""
    response = http.get(SREALITY_API.format(id=match.group(1)), follow_redirects=False)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("Sreality: la respuesta no es JSON (¿captcha o cambió la API?)") from exc
    return parse_sreality(payload, url)


def _is_ad_url(host: str, path: str) -> bool:
    """Si host y ruta son los de un anuncio de un portal con parser (UlovDomov no tiene: solo API)."""
    return (
        (_on(host, "bezrealitky.cz") and path.startswith("/nemovitosti-byty-domy/"))
        or (_on(host, "reality.idnes.cz") and "/detail/" in path)
        or (_on(host, "reality.bazos.cz") and "/inzerat/" in path)
        or (_on(host, "espolubydleni.cz") and _ESPOLUBYDLENI_AD.search(path) is not None)
    )


def _parse_ad(html: str, host: str, url: str) -> Listing | None:
    """El parser del portal de una URL que ya es de anuncio; eSpolubydleni da None si es de alguien que busca piso."""
    if _on(host, "bezrealitky.cz"):
        return bezrealitky.parse_detail(html)
    if _on(host, "reality.idnes.cz"):
        return idnes.parse_detail(html, url=url)
    if _on(host, "reality.bazos.cz"):
        return bazos.parse_detail(html, url=url)
    return espolubydleni.parse_detail(html, url)


def fetch(http: httpx.Client, url: str, *, review_only: bool = False) -> Listing | str:
    """Un Listing si el portal tiene parser y la página lo cumple; si no, el texto de la página.

    Devuelve "" cuando no hay nada que evaluar: Sreality sin id de anuncio, o un anuncio retirado que el portal
    redirige a un listado. Los errores HTTP se propagan (httpx.HTTPError). Un parser que falla con la página de un
    anuncio no tumba el mensaje: se cae al texto, que el flujo de texto libre sabe evaluar.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if _on(host, "sreality.cz"):
        return _fetch_sreality(http, url)
    response = _get_review_page(http, url) if review_only else http.get(url)
    # eSpolubydleni sirve el anuncio con HTTP 404 (como su listado); cualquier otro error no trae anuncio
    if response.status_code != 404 or not _on(host, "espolubydleni.cz"):
        response.raise_for_status()
    html = response.text
    if _is_ad_url(host, parsed.path):
        final = urlparse(str(response.url))
        # Un anuncio retirado redirige al listado: su texto es el de otros anuncios, no el de este
        final_host = (final.hostname or "").lower()
        if not _is_ad_url(final_host, final.path):
            return ""
        try:
            listing = _parse_ad(html, final_host, str(response.url))
        except _PARSE_ERRORS as exc:
            # Sin esto un cambio de formato pasaría por una página cualquiera; solo tipo y host, sin la URL ni el texto
            print(f"single_url: {type(exc).__name__} {host}", file=sys.stderr)
            listing = None
        if listing is not None:
            return listing
    return page_text(html)
