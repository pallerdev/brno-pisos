"""Bezrealitky: página pública del listado (permitida por robots.txt; su API GraphQL no).

Los datos vienen en el JSON de Next.js (__NEXT_DATA__), en la caché de Apollo.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from pisobot.collectors.base import HTMLParser
from pisobot.models import Kind, Listing
from pisobot.parse_text import normalize_disposition

BASE = "https://www.bezrealitky.cz"
LIST_URL = f"{BASE}/vypis/nabidka-pronajem/byt/brno"
_PRAGUE = ZoneInfo("Europe/Prague")
_FURNISHED = {"VYBAVENY": True, "CASTECNE": True, "NEVYBAVENY": False}


def _page_props(html: str) -> dict:
    # Por id y no por regex: el orden de los atributos del <script> no está garantizado.
    node = HTMLParser(html).css_first("script#__NEXT_DATA__")
    if node is None:
        raise ValueError("Bezrealitky: la página no trae __NEXT_DATA__")
    return json.loads(node.text())["props"]["pageProps"]


def _field(advert: dict, name: str):
    """En la caché de Apollo algunos campos llevan argumentos: 'address({"locale":"CS"})'."""
    if name in advert:
        return advert[name]
    for key, value in advert.items():
        if key.startswith(name + "("):
            return value
    return None


def _is_czk(advert: dict) -> bool:
    currency = advert.get("currency")
    return not currency or str(currency).upper() == "CZK"


def _available_from(raw) -> date | None:
    """Un timestamp inválido (fuera de rango, texto, null) deja la fecha sin informar, no rompe el detalle."""
    if not raw or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(raw, _PRAGUE).date()
    except (OverflowError, OSError, ValueError):
        return None


def _fees(advert: dict) -> int | None:
    """charges, o servicios + energías si falta (el detalle los trae por separado)."""
    if advert.get("charges") is not None:
        return advert["charges"]
    parts = [advert.get("serviceCharges"), advert.get("utilityCharges")]
    known = [part for part in parts if part is not None]
    return sum(known) if known else None


def _listing(advert: dict) -> Listing:
    gps = advert.get("gps") or {}
    czk = _is_czk(advert)
    room = bool(advert.get("roommate"))
    return Listing(
        source="bezrealitky",
        source_id=str(advert["id"]),
        url=f"{BASE}/nemovitosti-byty-domy/{advert['uri']}",
        title=_field(advert, "imageAltText") or advert["uri"],
        kind=Kind.ROOM if room else Kind.FLAT,
        # Un importe en otra moneda leído como coronas sería un precio falso
        rent=advert.get("price") if czk else None,
        fees=_fees(advert) if czk else None,
        disposition=normalize_disposition(advert.get("disposition")),
        # En una habitación el portal da la superficie del piso entero
        area_m2=None if room else advert.get("surface"),
        lat=gps.get("lat"),
        lon=gps.get("lng"),
        address_text=_field(advert, "address") or "",
        commission=False,  # "bez realitky": anuncios de particulares
        needs_detail=True,
    )


def parse_list(html: str) -> list[Listing]:
    cache = _page_props(html)["apolloCache"]
    listings = [
        _listing(advert)
        for key, advert in cache.items()
        if key.startswith("Advert:") and advert.get("offerType") == "PRONAJEM" and advert.get("estateType") == "BYT"
    ]
    # La primera página de pisos en alquiler en Brno nunca está vacía: un [] silencioso taparía un cambio de formato.
    if not listings:
        raise ValueError("Bezrealitky: el listado no trae anuncios (¿cambió el formato?)")
    return listings


def parse_detail(html: str, listing: Listing | None = None) -> Listing:
    advert = _page_props(html)["origAdvert"]
    base = listing or _listing(advert)
    czk = _is_czk(advert)
    fees = _fees(advert)
    return dataclasses.replace(
        base,
        description=advert.get("description") or "",
        # charges nulo no pisa los poplatky que ya trajo el listado
        fees=(fees if fees is not None else base.fees) if czk else None,
        # utilityCharges es el anticipo de energías: si existe, ya está sumado en charges
        fees_include_energy=True if advert.get("utilityCharges") else None,
        furnished=_FURNISHED.get(advert.get("equipped")),
        deposit=advert.get("deposit") if czk else None,
        available_from=_available_from(advert.get("availableFrom")),
        needs_detail=False,
    )


class Bezrealitky:
    name = "bezrealitky"

    def __init__(self, http: httpx.Client):
        self.http = http

    def fetch_list(self) -> list[Listing]:
        response = self.http.get(LIST_URL)
        response.raise_for_status()
        return parse_list(response.text)

    def fetch_detail(self, listing: Listing) -> Listing:
        response = self.http.get(listing.url)
        response.raise_for_status()
        return parse_detail(response.text, listing)
