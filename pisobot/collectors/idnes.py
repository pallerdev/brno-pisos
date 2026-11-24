"""reality.idnes.cz: HTML del servidor. Mucho stock de agencias que también está en Sreality."""
from __future__ import annotations

import dataclasses
import json
import math
import re
from datetime import date
from urllib.parse import urljoin

import httpx

from pisobot.collectors.base import HTMLParser, block_text, inline_text, price_czk
from pisobot.models import Kind, Listing
from pisobot.parse_text import norm, normalize_disposition, parse_area

BASE = "https://reality.idnes.cz"
FLATS_URL = f"{BASE}/s/pronajem/byty/brno/"
ROOMS_URL = f"{BASE}/s/pronajem/byty/pokoj/brno/"
LIST_URLS = (FLATS_URL, ROOMS_URL)
_ID = re.compile(r"/([0-9a-f]{24})/?(?:[?#]|$)")
# "19 900 Kč/měsíc": con otra unidad ("Kč/den", "Kč/m²/měsíc") o sin ella no se sabe cuánto es al mes
_MONTHLY = re.compile(r"Kč\s*/\s*měs", re.IGNORECASE)
# Espacios de ancho cero que iDNES cuela entre las cifras del precio y en los valores de la ficha
_INVISIBLE = re.compile(r"[\u200b-\u200d\u2060\ufeff]")
# "Životského, Brno - Židenice", "Žilkova, Brno", "Brno - Obřany", "Brno": calle opcional, ciudad y barrio opcional
_PLACE = re.compile(r"(?:(?P<street>.+?),\s*)?Brno(?:\s*[-–]\s*(?P<quarter>.+))?", re.IGNORECASE)
# "Brno - město" o "Brno-město" no es un barrio: no sitúa el anuncio
_CITY = re.compile(r"(?:brno\s*[-–]\s*)?město", re.IGNORECASE)
_NO_LISTINGS = "iDNES: el listado no trae anuncios (¿cambió el formato?)"
# Encabezado del bloque de atributos que se añade a la descripción: ninguna palabra suya dispara una regla de parse_text
_PARAMS_LABEL = "Parametry inzerátu:"
# Ya se leen aparte o no describen el piso
_SKIPPED_PARAMS = {"Číslo zakázky", "Cena", "Užitná plocha", "Datum nastěhování"}


def _kind(title: str) -> Kind:
    return Kind.ROOM if "pokoj" in norm(title) else Kind.FLAT


def _rent(raw: str) -> int | None:
    text = _INVISIBLE.sub("", raw)
    return price_czk(text) if _MONTHLY.search(text) else None


def _area(kind: Kind, text: str | None) -> int | None:
    # En una habitación no se sabe si los m² son los del cuarto o los del piso: mejor sin dato que uno que despiste
    return None if kind is Kind.ROOM else (parse_area(text or "") or None)


def _place(info: str) -> tuple[str, str]:
    """(address_text, location_precision). Con calle se geocodifica la calle; solo con barrio, su centroide queda a
    kilómetros de la casa y se marca aproximado; sin ninguno de los dos no hay ubicación (no se geocodifica Brno)."""
    if not info:
        return "", "none"
    match = _PLACE.fullmatch(info)
    if match is None:
        return info, "postcode"
    if match["street"]:
        return info, "street"
    quarter = (match["quarter"] or "").strip()
    if not quarter or _CITY.fullmatch(quarter):
        return "", "none"
    return info, "postcode"


def _listing(*, source_id: str, url: str, title: str, price: str, info: str, needs_detail: bool) -> Listing:
    kind = _kind(title)
    address, precision = _place(info)
    return Listing(
        source="idnes",
        source_id=source_id,
        url=url,
        title=title,
        kind=kind,
        rent=_rent(price),
        disposition=normalize_disposition(title),
        area_m2=_area(kind, title),
        address_text=address,
        location_precision=precision,
        needs_detail=needs_detail,
    )


def _no_results(tree) -> bool:
    """Página de resultados que existe pero no tiene ningún anuncio (los huecos de publicidad no cuentan)."""
    return (
        tree.css_first("#snippet-s-result") is not None
        and tree.css_first("div.c-products__item:not(.c-products__item-advertisment)") is None
    )


def parse_list(html: str, *, allow_empty: bool = False) -> list[Listing]:
    """allow_empty: una categoría con pocos anuncios (habitaciones) puede quedarse en cero sin que sea un fallo."""
    tree = HTMLParser(html)
    listings = []
    for card in tree.css("div.c-products__item"):
        link = card.css_first('a.c-products__link[href*="/detail/"]')
        href = link.attributes.get("href") if link is not None else None
        match = _ID.search(href or "")
        # Los huecos de publicidad entre anuncios no llevan enlace al detalle
        if not match:
            continue
        listings.append(_listing(
            source_id=match.group(1),
            url=urljoin(BASE, href),
            title=inline_text(card.css_first("h2.c-products__title")),
            price=inline_text(card.css_first("p.c-products__price")),
            info=inline_text(card.css_first("p.c-products__info")),
            needs_detail=True,
        ))
    # Los pisos en Brno son cientos: un [] silencioso taparía un cambio de formato.
    # Una página sin su estructura falla siempre, también la de habitaciones.
    if not listings and not (allow_empty and _no_results(tree)):
        raise ValueError(_NO_LISTINGS)
    return listings


def _params(tree) -> dict[str, str]:
    """Pares dt/dd de la ficha. Los dd de publicidad ("Spočítej stěhování") no son datos del anuncio. Un dd sin texto
    es un icono: con el de "sí" el atributo existe (valor vacío); con cualquier otro ("no") se descarta."""
    params, key = {}, None
    for definition in tree.css(".b-definition-columns dl"):
        for node in definition.iter():
            if node.tag == "dt":
                key = inline_text(node)
            elif node.tag == "dd":
                value = " ".join(_INVISIBLE.sub("", node.text()).split())
                present = value or node.css_first(".icon--check") is not None
                if key and present and "advertisement" not in (node.attributes.get("class") or ""):
                    params[key] = value
                key = None
    return params


def _extras(params: dict[str, str]) -> str:
    """Los atributos de la ficha ("Vybavení: zařízený", "Topení"...) solo salen ahí: sin añadirlos al texto las reglas
    de palabras clave no los ven. El encabezado no puede disparar ninguna ("Vybavení" daría furnished=True)."""
    items = (f"{key}: {value}" if value else key for key, value in params.items() if key not in _SKIPPED_PARAMS)
    text = " | ".join(items)
    return f"{_PARAMS_LABEL} {text}" if text else ""


def _coordinate(value, limit: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
        return None
    return float(value)


def _gps(tree, source_id: str) -> tuple[float | None, float | None]:
    """El mapa dibuja este piso y otros "similares": solo vale la feature con el id del anuncio. iDNES solo pinta en
    el mapa inmuebles con dirección exacta, así que unas coordenadas aquí son las del propio inmueble."""
    script = tree.css_first("script[data-maptiler-json]")
    if script is None:
        return None, None
    try:
        features = json.loads(script.text())["geojson"]["features"]
    except (ValueError, KeyError, TypeError):
        return None, None
    if not isinstance(features, list):
        return None, None
    for feature in features:
        # Una feature rota (sin properties, que no sea un objeto) es de otro piso: no impide leer la propia
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict) or properties.get("id") != source_id:
            continue
        try:
            lon, lat = feature["geometry"]["coordinates"]
        except (KeyError, TypeError, ValueError):
            return None, None
        lat, lon = _coordinate(lat, 90), _coordinate(lon, 180)
        return (lat, lon) if lat is not None and lon is not None else (None, None)
    return None, None


def _date(raw: str | None) -> date | None:
    match = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", raw or "")
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_detail(html: str, listing: Listing | None = None, url: str = "") -> Listing:
    tree = HTMLParser(html)
    heading = tree.css_first("h1.b-detail__title")
    # Un anuncio retirado redirige al listado: sin su ficha no es un detalle
    if heading is None:
        raise ValueError("iDNES: el detalle no trae la ficha del anuncio (¿cambió el formato?)")
    if listing is None:
        match = _ID.search(url)
        if not match:
            raise ValueError(f"iDNES: la URL no lleva el id del anuncio ({url})")
        listing = _listing(
            source_id=match.group(1),
            url=url,
            title=inline_text(heading),
            price=inline_text(tree.css_first("p.b-detail__price")),
            info=inline_text(tree.css_first("p.b-detail__info")),
            needs_detail=False,
        )
    params = _params(tree)
    parts = (
        block_text(tree.css_first("div.b-desc")),
        inline_text(tree.css_first(".wrapper-price-notes")),
        _extras(params),
    )
    lat, lon = _gps(tree, listing.source_id)
    return dataclasses.replace(
        listing,
        description="\n".join(part for part in parts if part) or listing.description,
        # Lo que el detalle no trae no borra lo que ya había
        lat=lat if lat is not None else listing.lat,
        lon=lon if lon is not None else listing.lon,
        location_precision="exact" if lat is not None else listing.location_precision,
        available_from=_date(params.get("Datum nastěhování")) or listing.available_from,
        area_m2=listing.area_m2 or _area(listing.kind, params.get("Užitná plocha")),
        needs_detail=False,
    )


class Idnes:
    name = "idnes"

    def __init__(self, http: httpx.Client):
        self.http = http

    def fetch_list(self) -> list[Listing]:
        listings: dict[str, Listing] = {}
        for url in LIST_URLS:
            response = self.http.get(url)
            response.raise_for_status()
            # Habitaciones puede estar vacío sin que pase nada; pisos nunca
            for listing in parse_list(response.text, allow_empty=url == ROOMS_URL):
                listings.setdefault(listing.source_id, listing)
        return list(listings.values())

    def fetch_detail(self, listing: Listing) -> Listing:
        response = self.http.get(listing.url)
        response.raise_for_status()
        return parse_detail(response.text, listing)
