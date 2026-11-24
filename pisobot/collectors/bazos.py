"""Bazoš Reality: anuncios clasificados, muchos de particulares. Sin campos estructurados en el detalle."""
from __future__ import annotations

import dataclasses
import re
from urllib.parse import urljoin

import httpx

from pisobot.collectors.base import HTMLParser, block_text, inline_text, price_czk
from pisobot.models import Kind, Listing
from pisobot.parse_text import norm, normalize_disposition, parse_area

BASE = "https://reality.bazos.cz"
LIST_URL = f"{BASE}/pronajmu/byt/"
# Los anuncios de Brno van mezclados con los de todo el país: con 40 por página, una sola se queda corta. La segunda
# se pide por ruta (el desplazamiento no es un parámetro de filtro)
LIST_URLS = (LIST_URL, f"{LIST_URL}40/")
_ID = re.compile(r"/inzerat/(\d+)/")
_GMAPS = re.compile(r"/maps/place/(-?\d+\.\d+),(-?\d+\.\d+)")
# "záloha" a secas suele ser la fianza: en singular solo cuenta como anticipo de servicios ("záloha na energie")
_FEE_WORD = r"(?:sluzba|sluzby|energie|poplatky|zalohy|inkaso|zaloha\s+na\s+(?:sluzby|energie|poplatky))\b"
# "+ 4 000 Kč za služby", "+ 4.000,- Kč inkaso" y, con el concepto delante, "+ služby: 4 000 Kč" o
# "+ zálohy na služby 4 000 Kč"
_FEES = re.compile(
    rf"\+\s*(?:(?P<first>\d[\d .]*)(?:,-)?\s*kc\s*(?:za\s+)?{_FEE_WORD}"
    rf"|(?:zalohy\s+na\s+)?{_FEE_WORD}\s*:?\s*(?P<last>\d[\d .]*)(?:,-)?\s*kc)"
)
# "pokoj" suelto es una habitación; "2 pokoje", "dvoupokojový" o "2-pokojový" describen un piso entero
_ROOM = re.compile(r"(?<![\w-])(?<!\d )pokoj|spolubydl")
# "635 00 Brno": código postal (con o sin espacio) y localidad
_PLACE = re.compile(r"(\d{3})\s?(\d{2})\s*(.*)")
_NO_LISTINGS = "Bazoš: el listado no trae anuncios (¿cambió el formato?)"
_NO_POSTCODES = "Bazoš: el listado no trae el código postal (¿cambió el formato?)"
_NO_DETAIL = "Bazoš: el detalle no trae la ficha del anuncio (¿cambió el formato?)"


def _kind(title: str) -> Kind:
    return Kind.ROOM if _ROOM.search(norm(title)) else Kind.FLAT


def _area(kind: Kind, text: str) -> int | None:
    # En una habitación no se sabe si los m² son los del cuarto o los del piso: mejor sin dato que uno que despiste
    return None if kind is Kind.ROOM else parse_area(text)


def parse_list(html: str, psc_prefixes: list[str]) -> list[Listing]:
    tree = HTMLParser(html)
    prefixes = tuple(psc_prefixes)
    listings = []
    cards = located = 0
    for card in tree.css("div.inzeraty.inzeratyflex"):
        link = card.css_first("h2.nadpis a")
        href = link.attributes.get("href") if link is not None else None
        match = _ID.search(href or "")
        if not match:
            continue
        cards += 1
        location = card.css_first("div.inzeratylok")
        parts = location.text(separator="|", strip=True).split("|") if location is not None else []
        town = parts[0] if parts else ""
        psc = re.sub(r"\D", "", parts[1]) if len(parts) > 1 else ""
        located += bool(psc)
        if not psc.startswith(prefixes):  # categoría nacional: Brno se filtra por código postal
            continue
        title = inline_text(link)
        kind = _kind(title)
        listings.append(Listing(
            source="bazos",
            source_id=match.group(1),
            url=urljoin(BASE, href),
            title=title,
            kind=kind,
            rent=price_czk(inline_text(card.css_first("div.inzeratycena"))),
            disposition=normalize_disposition(title),
            area_m2=_area(kind, title),
            address_text=f"{town} {psc}".strip(),
            location_precision="postcode",
            needs_detail=True,
        ))
    # Quedarse sin anuncios de Brno es normal; sin tarjetas o sin códigos postales, no: el marcado habría cambiado
    if not cards:
        raise ValueError(_NO_LISTINGS)
    if not located:
        raise ValueError(_NO_POSTCODES)
    return listings


def _rows(tree) -> dict[str, object]:
    rows = {}
    for row in tree.css("td.listadvlevo tr"):
        cells = row.css("td")
        if len(cells) >= 2:
            # "Lokalita" lleva una celda con el icono del mapa entre la etiqueta y el valor: el valor es la última
            rows[inline_text(cells[0]).rstrip(":")] = cells[-1]
    return rows


def _place(cell) -> tuple[str, str]:
    """(address_text, location_precision) de la fila "Lokalita", con el mismo formato que el listado."""
    match = _PLACE.fullmatch(inline_text(cell))
    # Sin código postal (solo "Brno") no se sabe dónde está: geocodificar la ciudad pondría el piso en el centro
    if match is None:
        return "", "none"
    return f"{match[3]} {match[1]}{match[2]}".strip(), "postcode"


def _gps(cell) -> tuple[float | None, float | None]:
    """Las coordenadas son las de "Přibližná lokalita" (centro del código postal), no las del inmueble."""
    link = cell.css_first('a[href*="/maps/place/"]') if cell is not None else None
    match = _GMAPS.search(link.attributes.get("href") or "") if link is not None else None
    if not match:
        return None, None
    lat, lon = float(match.group(1)), float(match.group(2))
    return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else (None, None)


def parse_detail(html: str, listing: Listing | None = None, url: str = "") -> Listing:
    tree = HTMLParser(html)
    heading = tree.css_first("h1.nadpisdetail")
    body = tree.css_first("div.popisdetail")
    # Un anuncio retirado no trae ficha: sin ella no es un detalle
    if body is None or (listing is None and heading is None):
        raise ValueError(_NO_DETAIL)
    rows = _rows(tree)
    place = rows.get("Lokalita")
    price = price_czk(inline_text(rows.get("Cena")))
    if listing is None:
        match = _ID.search(url)
        if not match:
            raise ValueError(f"Bazoš: la URL no lleva el id del anuncio ({url})")
        title = inline_text(heading)
        kind = _kind(title)
        address, precision = _place(place)
        listing = Listing(
            source="bazos",
            source_id=match.group(1),
            url=url,
            title=title,
            kind=kind,
            rent=price,
            disposition=normalize_disposition(title),
            area_m2=_area(kind, title),
            address_text=address,
            location_precision=precision,
        )
    description = block_text(body)
    lat, lon = _gps(place)
    fees = _FEES.search(norm(description))
    amount = (fees["first"] or fees["last"]) if fees else None
    # Lo que el detalle no trae no borra lo que ya había
    return dataclasses.replace(
        listing,
        description=description or listing.description,
        rent=listing.rent or price,
        fees=int(re.sub(r"\D", "", amount)) if amount else listing.fees,
        lat=lat if lat is not None else listing.lat,
        lon=lon if lon is not None else listing.lon,
        area_m2=listing.area_m2 or _area(listing.kind, description),
        disposition=listing.disposition or normalize_disposition(description),
        needs_detail=False,
    )


class Bazos:
    name = "bazos"

    def __init__(self, http: httpx.Client, psc_prefixes: list[str]):
        self.http = http
        self.psc_prefixes = psc_prefixes

    def fetch_list(self) -> list[Listing]:
        listings: dict[str, Listing] = {}
        for url in LIST_URLS:
            response = self.http.get(url)
            response.raise_for_status()
            # Un anuncio publicado entre las dos peticiones aparece al final de la primera y al principio de la segunda
            for listing in parse_list(response.text, self.psc_prefixes):
                listings.setdefault(listing.key, listing)
        return list(listings.values())

    def fetch_detail(self, listing: Listing) -> Listing:
        response = self.http.get(listing.url)
        response.raise_for_status()
        return parse_detail(response.text, listing)
