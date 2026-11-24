import copy
import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest
import yaml

from pisobot.collectors.ulovdomov import API_URL, BOUNDS, MAX_PRICE, UlovDomov, parse_offers
from pisobot.models import Kind

FIXTURES = Path(__file__).parent / "fixtures"
FLATS = json.loads((FIXTURES / "ulovdomov_flats.json").read_text(encoding="utf-8"))
ROOMS = json.loads((FIXTURES / "ulovdomov_rooms.json").read_text(encoding="utf-8"))
_MISSING = object()


def _with_offer(payload, **fields):
    """Copia del payload con campos de su primera oferta cambiados (_MISSING quita el campo)."""
    payload = copy.deepcopy(payload)
    offer = payload["data"]["offers"][0]
    for name, value in fields.items():
        if value is _MISSING:
            offer.pop(name, None)
        else:
            offer[name] = value
    return payload


def _without_offers(payload):
    payload = copy.deepcopy(payload)
    payload["data"]["offers"] = []
    return payload


def _collector(handler):
    return UlovDomov(httpx.Client(transport=httpx.MockTransport(handler)))


def test_parse_flats():
    listings = parse_offers(FLATS)
    assert len(listings) == 3
    first = listings[0]
    assert first.source_id == "5683439"
    assert first.url == "https://www.ulovdomov.cz/inzerat/pronajem-brno-brno-mesto-pekarska-2-1/5683439"
    assert (first.kind, first.rent, first.fees, first.disposition, first.area_m2) == (Kind.FLAT, 18900, 2300, "2+1", 50)
    assert first.fees_per_extra_person == 600
    assert (first.lat, first.lon) == (49.19216, 16.60144)
    assert first.location_precision == "exact"
    assert first.available_from == date(2026, 12, 1)
    assert first.address_text == "Pekařská, Brno-město"
    assert first.description.startswith("V zastoupení majitele")
    assert first.needs_detail is False


def test_parse_rooms():
    listings = parse_offers(ROOMS)
    assert [listing.kind for listing in listings] == [Kind.ROOM] * 3
    second = listings[1]
    assert (second.rent, second.fees, second.deposit) == (4500, 900, 5400)
    assert second.available_from == date(2026, 10, 1)
    assert listings[2].commission is False
    assert listings[0].commission is None
    # La disposición "atypical" no es un dato útil; la superficie de una habitación es dudosa
    assert [listing.disposition for listing in listings] == [None] * 3
    assert [listing.area_m2 for listing in listings] == [None] * 3


def test_price_note_is_appended_to_description():
    second = parse_offers(FLATS)[1]
    assert (second.fees, second.fees_per_extra_person) == (2550, 850)
    assert second.description.endswith("\n+ zálohy na  služby 2.550 Kč/měs./1 osoba, každá další osoba +850 Kč/měs.,"
                                       "zálohu na elektřinu hradní nájemce přímo dodavateli, "
                                       "vratná kauce 1 měsíční nájem (platba vč. služeb), + provize RK")


def test_fetch_list_queries_rent_and_coliving():
    bodies = []

    def handler(request):
        assert str(request.url).startswith(API_URL)
        assert request.url.params["sorting"] == "latest"
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=FLATS if body["offerType"] == "rent" else ROOMS)

    listings = UlovDomov(httpx.Client(transport=httpx.MockTransport(handler))).fetch_list()
    assert [b["offerType"] for b in bodies] == ["rent", "coliving"]
    assert bodies[0]["price"] == {"max": 48000}
    assert len(listings) == 6


def test_fetch_list_request_shape():
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json=FLATS if body["offerType"] == "rent" else ROOMS)

    _collector(handler).fetch_list()
    assert [request.method for request in requests] == ["POST", "POST"]
    assert [request.url.path for request in requests] == ["/v1/offer/find"] * 2
    assert all(dict(request.url.params) == {"page": "1", "perPage": "20", "sorting": "latest"} for request in requests)
    bodies = [json.loads(request.content) for request in requests]
    assert all(body["propertyType"] == "flat" and body["bounds"] == BOUNDS for body in bodies)
    assert bodies[0]["price"] == {"max": MAX_PRICE}
    assert "price" not in bodies[1]


def test_fetch_detail_returns_the_listing_without_requests():
    def handler(request):
        raise AssertionError(f"petición inesperada: {request.url}")

    listing = parse_offers(FLATS)[0]
    assert _collector(handler).fetch_detail(listing) is listing


def test_parse_offers_without_offers_raises():
    with pytest.raises(ValueError, match="no trae anuncios"):
        parse_offers(_without_offers(FLATS))


@pytest.mark.parametrize("payload", [
    {},
    {"success": False},
    {"data": None},
    {"data": {}},
    {"data": {"offers": None}},
    {"data": {"offers": {}}},
    [],
    None,
])
@pytest.mark.parametrize("allow_empty", [False, True])
def test_parse_offers_without_structure_raises(payload, allow_empty):
    with pytest.raises(ValueError, match="data.offers"):
        parse_offers(payload, allow_empty=allow_empty)


@pytest.mark.parametrize("status", [404, 500])
def test_collector_raises_on_http_error(status):
    collector = _collector(lambda request: httpx.Response(status, text="error"))
    with pytest.raises(httpx.HTTPStatusError):
        collector.fetch_list()


def test_collector_raises_on_non_json_response():
    collector = _collector(lambda request: httpx.Response(200, text="<html><body>captcha</body></html>"))
    with pytest.raises(ValueError, match="UlovDomov"):
        collector.fetch_list()


_NO_ID_URL = "UlovDomov: oferta sin id/url (¿cambió el formato?)"


@pytest.mark.parametrize("fields", [
    {"id": _MISSING},
    {"id": None},
    {"absoluteUrl": _MISSING},
    {"absoluteUrl": None},
    {"absoluteUrl": ""},
    {"absoluteUrl": 5683439},
])
def test_offer_without_id_or_url_raises(fields):
    with pytest.raises(ValueError, match=re.escape(_NO_ID_URL)):
        parse_offers(_with_offer(FLATS, **fields))


@pytest.mark.parametrize("offer", [None, "5683439", 5683439, []])
def test_non_dict_offer_raises(offer):
    payload = copy.deepcopy(FLATS)
    payload["data"]["offers"][1] = offer
    with pytest.raises(ValueError, match=re.escape(_NO_ID_URL)):
        parse_offers(payload)


def test_empty_coliving_does_not_fail_the_portal():
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=FLATS if body["offerType"] == "rent" else _without_offers(ROOMS))

    listings = _collector(handler).fetch_list()
    assert [listing.source_id for listing in listings] == ["5683439", "5688278", "5688246"]
    assert {listing.kind for listing in listings} == {Kind.FLAT}


def test_empty_rent_still_fails_the_portal():
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=_without_offers(FLATS) if body["offerType"] == "rent" else ROOMS)

    with pytest.raises(ValueError, match="no trae anuncios"):
        _collector(handler).fetch_list()


@pytest.mark.parametrize("offer_type", ["rent", "coliving"])
def test_collector_raises_when_a_query_loses_the_structure(offer_type):
    def handler(request):
        body = json.loads(request.content)
        if body["offerType"] == offer_type:
            return httpx.Response(200, json={"success": True, "data": {}})
        return httpx.Response(200, json=FLATS if body["offerType"] == "rent" else ROOMS)

    with pytest.raises(ValueError, match="data.offers"):
        _collector(handler).fetch_list()


def test_parse_offers_allow_empty_returns_an_empty_list():
    assert parse_offers(_without_offers(ROOMS), allow_empty=True) == []
    with pytest.raises(ValueError, match="no trae anuncios"):
        parse_offers(_without_offers(ROOMS), allow_empty=False)


@pytest.mark.parametrize("raw,expected", [
    ({"value": 18900, "currency": "CZK"}, 18900),
    ({"value": 18900, "currency": "czk"}, 18900),
    ({"value": 18900}, 18900),
    ({"value": 18900, "currency": "EUR"}, None),
    ({"value": 18900.5, "currency": "CZK"}, None),
    ({"value": 18900.0, "currency": "CZK"}, None),
    ({"value": "18900", "currency": "CZK"}, None),
    ({"value": -1, "currency": "CZK"}, None),
    ({"value": True, "currency": "CZK"}, None),
    ({"value": 0, "currency": "CZK"}, None),
    ({"value": None, "currency": "CZK"}, None),
    ({"currency": "CZK"}, None),
    (18900, None),
    ("18 900 Kč", None),
    ([], None),
    (None, None),
    (_MISSING, None),
])
def test_rent_is_a_czk_integer_or_none(raw, expected):
    first = parse_offers(_with_offer(FLATS, rentalPrice=raw))[0]
    assert first.rent == expected
    # Un precio inválido no tira el anuncio: el resto de datos sigue ahí
    assert (first.source_id, first.disposition) == ("5683439", "2+1")


@pytest.mark.parametrize("raw,expected", [
    ({"value": 900, "currency": "CZK"}, 900),
    ({"value": 0, "currency": "CZK"}, 0),
    ({"value": 900, "currency": "EUR"}, None),
    ({"value": -900, "currency": "CZK"}, None),
    ({"value": 9.5, "currency": "CZK"}, None),
    ({"value": "900", "currency": "CZK"}, None),
    ({"value": False, "currency": "CZK"}, None),
    (900, None),
    (None, None),
    (_MISSING, None),
])
def test_fees_and_deposit_follow_the_same_rules(raw, expected):
    listing = parse_offers(_with_offer(FLATS, description="", monthlyFeesPrice=raw, depositPrice=raw))[0]
    assert (listing.fees, listing.deposit) == (expected, expected)


def test_deposit_in_text_does_not_supply_monthly_fees():
    listing = parse_offers(_with_offer(FLATS, description="Vratná záloha 40 000 Kč."))[0]
    assert listing.fees is None and listing.fees_per_extra_person == 0


def test_non_czk_currency_drops_every_price():
    eur = {"value": 500, "currency": "EUR"}
    first = parse_offers(_with_offer(FLATS, rentalPrice=eur, monthlyFeesPrice=eur, depositPrice=eur))[0]
    assert (first.rent, first.fees, first.deposit) == (None, None, None)
    assert first.disposition == "2+1"


@pytest.mark.parametrize("raw", ["2026-13-45", "pronto", "", "01.12.2026", 20261201, 1.5, True, [], None, _MISSING])
def test_invalid_available_from_is_none(raw):
    first = parse_offers(_with_offer(FLATS, availableFrom=raw))[0]
    assert first.available_from is None
    assert first.rent == 18900


@pytest.mark.parametrize("unit,kept", [
    ("perMonth", True),
    (None, True),
    ("perDay", False),
    ("perYear", False),
    ("x", False),
    ("", False),
    (["perMonth"], False),
])
def test_prices_are_kept_only_for_a_monthly_unit(unit, kept):
    price = {"value": 900, "currency": "CZK"}
    listing = parse_offers(_with_offer(FLATS, priceUnit=unit, monthlyFeesPrice=price, depositPrice=price))[0]
    assert (listing.rent, listing.fees, listing.deposit) == ((18900, 900, 900) if kept else (None, None, None))
    assert listing.disposition == "2+1"


def test_missing_price_unit_keeps_the_prices():
    assert parse_offers(_with_offer(FLATS, priceUnit=_MISSING))[0].rent == 18900


@pytest.mark.parametrize("raw,expected", [
    (54, 54),
    (54.4, 54),
    (54.5, round(54.5)),
    (54.6, 55),
    (0.3, None),
    (0, None),
    (-3, None),
    (-3.5, None),
    (float("nan"), None),
    (float("inf"), None),
    ("54", None),
    (True, None),
    (None, None),
    (_MISSING, None),
])
def test_flat_area_is_rounded_when_positive(raw, expected):
    assert parse_offers(_with_offer(FLATS, area=raw))[0].area_m2 == expected


def test_max_price_covers_the_most_demanding_profile():
    # MAX_PRICE es el filtro del servidor: si config.yaml sube un tope y esto no, se perderían anuncios sin avisar
    config = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
    grupo = config["profiles"]["grupo"]
    assert MAX_PRICE >= max(grupo["people"]) * grupo["per_person_max"]
    assert MAX_PRICE >= config["profiles"]["yo"]["max_total"]


def test_room_area_is_never_taken_from_the_portal():
    # En coliving `area` puede ser la del piso entero (62 m² para un cuarto de tres camas)
    assert parse_offers(_with_offer(ROOMS, area=13))[0].area_m2 is None
