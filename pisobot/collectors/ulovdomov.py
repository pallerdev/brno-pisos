"""UlovDomov: API JSON de su propia web (robots.txt: Allow /). Habitaciones = offerType "coliving"."""
from __future__ import annotations

import re
from datetime import date

import httpx

from pisobot.collectors.base import price_czk
from pisobot.models import Kind, Listing
from pisobot.parse_text import norm, normalize_disposition

API_URL = "https://ud.api.ulovdomov.cz/v1/offer/find"
BOUNDS = {"northEast": {"lat": 49.30, "lng": 16.75}, "southWest": {"lat": 49.10, "lng": 16.45}}
MAX_PRICE = 48000  # techo del perfil grupo con 4 personas; el resto lo decide el prefiltro
_OCCUPANCY_FEES = re.compile(
    r"\bzaloh[ay] na sluzby (?:je )?(?P<base>\d[\d .]*?)\s*kc"
    r"[ /a-z.]*1 osob[au](?:\s*/\s*mes[a-z.]*)?\s*,\s*"
    r"kazda dalsi osoba\s*\+\s*(?P<extra>\d[\d .]*?)\s*kc"
)


def _whole(value) -> int | None:
    """Entero >= 0 tal cual; texto, decimales, bool o negativos dan None: mejor sin dato que un importe falso."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _area(value) -> int | None:
    """Superficie positiva (el portal a veces la manda con decimales) redondeada a entero; el resto es None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        return None
    try:
        return round(value) or None
    except OverflowError:
        return None


def _money(offer: dict, field: str) -> int | None:
    # Un importe por día o por año leído como mensual sería un precio falso
    if offer.get("priceUnit") not in (None, "perMonth"):
        return None
    price = offer.get(field)
    if not isinstance(price, dict):
        return None
    currency = price.get("currency")
    # Un importe en otra moneda leído como coronas sería un precio falso
    if currency and str(currency).upper() != "CZK":
        return None
    return _whole(price.get("value"))


def _available_from(raw) -> date | None:
    """Una fecha inválida deja el campo sin informar, no rompe el listado."""
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _identity(offer) -> tuple[str, str]:
    """id y URL son lo mínimo para identificar un anuncio: sin ellos el formato de la API ha cambiado."""
    if isinstance(offer, dict):
        offer_id, url = offer.get("id"), offer.get("absoluteUrl")
        if offer_id is not None and isinstance(url, str) and url:
            return str(offer_id), url
    raise ValueError("UlovDomov: oferta sin id/url (¿cambió el formato?)")


def _listing(offer: dict) -> Listing:
    source_id, url = _identity(offer)
    geo = offer.get("geoCoordinates") or {}
    street = (offer.get("street") or {}).get("title") or ""
    part = (offer.get("villagePart") or {}).get("title") or ""
    description = offer.get("description") or ""
    if offer.get("priceNote"):
        description += f"\n{offer['priceNote']}"
    room = offer.get("offerType") == "coliving"
    fees = _money(offer, "monthlyFeesPrice")
    extra = 0
    rent = _money(offer, "rentalPrice") or None
    if fees is None and offer.get("monthlyFeesPrice") is None and rent is not None and not room:
        match = _OCCUPANCY_FEES.search(norm(description))
        if match:
            fees = price_czk(f"{match['base']} Kč")
            extra = price_czk(f"{match['extra']} Kč") or 0
    # En una habitación `area` puede ser la del piso entero (62 m² para un cuarto de tres camas)
    area = None if room else _area(offer.get("area"))
    return Listing(
        source="ulovdomov",
        source_id=source_id,
        url=url,
        title=offer.get("title") or "",
        kind=Kind.ROOM if room else Kind.FLAT,
        # Un alquiler de 0 es un dato roto: pasaría el filtro de precio como si fuera gratis
        rent=rent,
        fees=fees,
        fees_per_extra_person=extra,
        disposition=normalize_disposition(offer.get("disposition")),
        area_m2=area,
        deposit=_money(offer, "depositPrice"),
        commission=False if offer.get("isNoCommission") else None,
        lat=geo.get("lat"),
        lon=geo.get("lng"),
        address_text=", ".join(x for x in (street, part) if x),
        description=description,
        available_from=_available_from(offer.get("availableFrom")),
    )


def parse_offers(payload: dict, *, allow_empty: bool = False) -> list[Listing]:
    """allow_empty: una consulta con pocos anuncios (habitaciones) puede quedarse en cero sin que sea un fallo."""
    data = payload.get("data") if isinstance(payload, dict) else None
    offers = data.get("offers") if isinstance(data, dict) else None
    if not isinstance(offers, list):
        raise ValueError("UlovDomov: la respuesta no trae data.offers (¿cambió el formato?)")
    # Los pisos en Brno son cientos: un [] silencioso taparía un cambio de API. La estructura rota falla siempre.
    if not offers and not allow_empty:
        raise ValueError("UlovDomov: el listado no trae anuncios (¿cambió el formato?)")
    return [_listing(offer) for offer in offers]


class UlovDomov:
    name = "ulovdomov"

    def __init__(self, http: httpx.Client):
        self.http = http

    def fetch_list(self) -> list[Listing]:
        listings = []
        for offer_type, extra in (("rent", {"price": {"max": MAX_PRICE}}), ("coliving", {})):
            response = self.http.post(
                API_URL,
                params={"page": 1, "perPage": 20, "sorting": "latest"},
                json={"offerType": offer_type, "propertyType": "flat", "bounds": BOUNDS, **extra},
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError("UlovDomov: la respuesta no es JSON (¿captcha o cambió la API?)") from exc
            listings += parse_offers(payload, allow_empty=offer_type == "coliving")
        return listings

    def fetch_detail(self, listing: Listing) -> Listing:
        return listing
