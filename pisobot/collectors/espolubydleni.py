"""eSpolubydleni: habitaciones en pisos compartidos. HTML del servidor, sin GPS (se geocodifica la calle)."""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin

import httpx

from pisobot.collectors.base import HTMLParser, block_text, inline_text, price_czk
from pisobot.models import Kind, Listing
from pisobot.parse_text import normalize_disposition

BASE = "https://www.espolubydleni.cz"
LIST_URL = f"{BASE}/podnajem-spolubydlici/brno"
_ID = re.compile(r"/(\d+)-[^/]*\.htm")
# "6.000Kč/měsíc/os.", "7.000Kč/měsíc/(osobu)" (por persona) o "17.000Kč/měsíc/byt" (piso entero)
_UNIT = re.compile(r"Kč\s*/\s*m[ěe]s\w*\s*/\s*\(?\s*(os|byt)", re.IGNORECASE)
_AVAILABLE = re.compile(r"Volné od:\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
# "Brno" o "Brno - město" no es un barrio: no sitúa el anuncio
_CITY = re.compile(r"brno(?:\s*[-–]\s*m[ěe]sto)?", re.IGNORECASE)
_NO_LISTINGS = "eSpolubydleni: el listado no trae anuncios (¿cambió el formato?)"


def _price(text: str) -> tuple[Kind, int | None]:
    """El importe lleva su unidad: por persona es el de una habitación, por byt el del piso entero.

    Con otra unidad (día, pokoj...) no se sabe qué cuesta: mejor sin precio que uno falso.
    """
    unit = _UNIT.search(text)
    if unit is None:
        return Kind.ROOM, None
    return (Kind.FLAT if unit.group(1).lower() == "byt" else Kind.ROOM), price_czk(text)


def _area(info: str, kind: Kind) -> int | None:
    # En una habitación solo vale la superficie del propio cuarto, no la del piso
    match = re.search(rf"plocha {'bytu' if kind is Kind.FLAT else 'pokoje'}:\s*(\d+(?:[.,]\d+)?)", info)
    return (round(float(match.group(1).replace(",", "."))) or None) if match else None


def _available_from(info: str) -> date | None:
    match = _AVAILABLE.search(info)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _place(quarter: str, street: str) -> tuple[str, str]:
    """(address_text, location_precision). Con calle se geocodifica la calle; solo con barrio, su centroide queda
    a kilómetros de la casa y se marca aproximado; sin ninguno de los dos no hay ubicación (no se geocodifica Brno)."""
    quarter, street = quarter.strip(" ,"), street.strip(" ,")
    if street:
        return ", ".join(part for part in (quarter, street) if part), "street"
    if not quarter or _CITY.fullmatch(quarter):
        return "", "none"
    return quarter, "postcode"


def _amenities(side) -> str:
    """Los equipamientos ("pračka", "postel"...) solo salen en listas de la columna derecha: sin añadirlos al texto
    las reglas de palabras clave no los ven."""
    items = (inline_text(node) for node in side.css('div[style*="text-align:justify"]')) if side is not None else ()
    return " | ".join(item for item in items if item)


def _listing(*, source_id: str, url: str, title: str, price: str, flat: str, quarter: str, street: str,
             description: str, side) -> Listing:
    kind, rent = _price(price)
    info = inline_text(side)
    address, precision = _place(quarter, street)
    amenities = _amenities(side)
    return Listing(
        source="espolubydleni",
        source_id=source_id,
        url=url,
        title=title,
        kind=kind,
        rent=rent,
        # En una habitación es la disposición del piso donde está
        disposition=normalize_disposition(flat),
        area_m2=_area(info, kind),
        address_text=address,
        description=f"{description}\nPopis bytu a pokoje: {amenities}" if amenities else description,
        available_from=_available_from(info),
        location_precision=precision,
    )


def _from_row(tree, row) -> Listing:
    link = row.css_first("a.re_2")
    href = link.attributes.get("href") if link is not None else None
    match = _ID.search(href or "")
    card = tree.css_first(f'#i{row.attributes["id"][2:]}inz')
    text = card.css_first(".popis1") if card is not None else None
    if not match or text is None:
        raise ValueError("eSpolubydleni: anuncio sin enlace o ficha (¿cambió el formato?)")
    heading = inline_text(row.css_first("strong.s1"))
    flat = inline_text(row.css_first('.res_div3 b[style*="font-size:90%"]'))
    # El atributo title separa barrio y calle ("city/quarter Štýřice, street Renneská Třída"); el texto visible los mezcla
    place = row.css_first('.res_div3 div[title^="city/quarter"]')
    location = (place.attributes.get("title") or "") if place is not None else ""
    quarter, _, street = location.removeprefix("city/quarter ").partition(", street")
    return _listing(
        source_id=match.group(1),
        url=urljoin(BASE, href),
        title=f"{heading} | {flat}" if flat else heading,
        price=inline_text(row.css_first('.res_div3 div[style*="float:right"]')),
        flat=flat,
        quarter=quarter,
        street=street,
        description=block_text(text),
        side=card.css_first(".res_div_prava"),
    )


def parse_list(html: str) -> list[Listing]:
    tree = HTMLParser(html)
    listings = [
        _from_row(tree, row)
        for row in tree.css('#result1 tr[id^="tr"]')
        if inline_text(row.css_first("strong.s1")).startswith("Nabízím")  # "Hledám" = gente que busca piso
    ]
    # La primera página de Brno siempre trae ofertas: un [] silencioso taparía un cambio de formato
    if not listings:
        raise ValueError(_NO_LISTINGS)
    return listings


def parse_detail(html: str, url: str) -> Listing | None:
    tree = HTMLParser(html)
    if not inline_text(tree.css_first("title")).startswith("Nabízím"):
        return None
    root = tree.css_first("#realita1")
    heading = root.css_first('a[href$=".htm"] > strong') if root is not None else None
    text = root.css_first("div.popis1") if root is not None else None
    match = _ID.search(url)
    if heading is None or text is None:
        raise ValueError("eSpolubydleni: el detalle no trae la ficha del anuncio (¿cambió el formato?)")
    if not match:
        raise ValueError(f"eSpolubydleni: la URL no lleva el id del anuncio ({url})")
    title = inline_text(heading)
    price = root.css_first('b[style*="font-size:18px"]')
    # "Královo Pole, <b>Božetěchova</b>": la calle va en negrita, lo anterior es el barrio
    place = tree.css_first("h2.karta")
    street = inline_text(place.css_first("b")) if place is not None else ""
    quarter = inline_text(place).removesuffix(street) if street else inline_text(place)
    return _listing(
        source_id=match.group(1),
        url=url,
        title=title,
        # El importe y su unidad ("/(osobu)") son hermanos dentro del mismo span
        price=inline_text(price.parent) if price is not None else "",
        # La celda del titular también trae el tipo de piso ("Panelový byt 2+1(kk) 65m2")
        flat=inline_text(heading.parent.parent).replace(title, "", 1),
        quarter=quarter,
        street=street,
        description=block_text(text),
        side=root.css_first("div.r_div_prava"),
    )


class Espolubydleni:
    name = "espolubydleni"

    def __init__(self, http: httpx.Client):
        self.http = http

    def fetch_list(self) -> list[Listing]:
        response = self.http.get(LIST_URL)
        # La web responde 404 a esta URL pero sirve la lista completa
        if response.status_code not in (200, 404) or 'id="result1"' not in response.text:
            raise RuntimeError(f"eSpolubydleni: respuesta inesperada (HTTP {response.status_code})")
        return parse_list(response.text)

    def fetch_detail(self, listing: Listing) -> Listing:
        return listing
