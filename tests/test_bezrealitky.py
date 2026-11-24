import dataclasses
import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from pisobot.collectors.bezrealitky import BASE, LIST_URL, Bezrealitky, parse_detail, parse_list
from pisobot.models import Kind

FIXTURES = Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "bezrealitky_list.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "bezrealitky_detail.html").read_text(encoding="utf-8")

_NEXT_DATA = re.compile(r'(<script id="__NEXT_DATA__" type="application/json">)(.*?)(</script>)', re.S)
_MISSING = object()


def _mutate(html, change):
    """Devuelve el HTML del fixture con el JSON de __NEXT_DATA__ modificado por `change(data)`."""
    def rewrite(match):
        data = json.loads(match.group(2))
        change(data)
        # "<" escapado para que el JSON no pueda cerrar el <script> antes de tiempo
        return match.group(1) + json.dumps(data, ensure_ascii=False).replace("<", "\\u003c") + match.group(3)

    assert _NEXT_DATA.search(html)
    return _NEXT_DATA.sub(rewrite, html, count=1)


def _list_advert(data, advert_id="1092907"):
    return data["props"]["pageProps"]["apolloCache"][f"Advert:{advert_id}"]


def _detail_advert(data):
    return data["props"]["pageProps"]["origAdvert"]


def _set(advert, **fields):
    for name, value in fields.items():
        if value is _MISSING:
            advert.pop(name, None)
        else:
            advert[name] = value


def _collector(handler):
    return Bezrealitky(httpx.Client(transport=httpx.MockTransport(handler)))


def test_parse_list():
    listings = parse_list(LIST_HTML)
    assert len(listings) == 15
    first = listings[0]
    assert first.source_id == "1092907"
    assert first.url == f"{BASE}/nemovitosti-byty-domy/1092907-nabidka-pronajem-bytu-stefanikova-brno"
    assert first.title == "Pronájem bytu 1+1 22 m², Štefánikova, Brno"
    assert (first.kind, first.rent, first.fees, first.disposition, first.area_m2) == (Kind.FLAT, 13500, 2500, "1+1", 22)
    assert (first.lat, first.lon) == (49.2141264, 16.601307)
    assert first.needs_detail is True and first.commission is False
    assert all(listing.url.startswith(BASE) for listing in listings)


def test_parse_detail_completes_listing():
    listing = parse_detail(DETAIL_HTML, parse_list(LIST_HTML)[0])
    assert listing.description.startswith("Pronajmu renovovaný byt 1+1")
    assert listing.available_from == date(2026, 11, 24)
    assert listing.fees == 2500 and listing.fees_include_energy is True
    assert listing.furnished is True
    assert listing.needs_detail is False


def test_parse_detail_without_base_listing():
    listing = parse_detail(DETAIL_HTML)
    assert (listing.source_id, listing.rent, listing.lat) == ("1092907", 13500, 49.2141264)
    assert listing.description


def test_collector_uses_public_pages_only():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=LIST_HTML if str(request.url) == LIST_URL else DETAIL_HTML)

    collector = Bezrealitky(httpx.Client(transport=httpx.MockTransport(handler)))
    listings = collector.fetch_list()
    collector.fetch_detail(listings[0])
    assert seen == [LIST_URL, listings[0].url]
    assert not any("api.bezrealitky.cz" in url for url in seen)


def test_parse_list_without_adverts_raises():
    def rename_prefix(data):
        cache = data["props"]["pageProps"]["apolloCache"]
        data["props"]["pageProps"]["apolloCache"] = {key.replace("Advert:", "Ad:"): value for key, value in cache.items()}

    with pytest.raises(ValueError, match="no trae anuncios"):
        parse_list(_mutate(LIST_HTML, rename_prefix))


def test_next_data_is_found_regardless_of_attribute_order():
    swapped = LIST_HTML.replace(
        '<script id="__NEXT_DATA__" type="application/json">',
        '<script type="application/json" id="__NEXT_DATA__">',
    )
    assert swapped != LIST_HTML
    assert len(parse_list(swapped)) == 15


def test_room_has_no_area():
    room = next(listing for listing in parse_list(LIST_HTML) if listing.source_id == "934310")
    assert room.kind is Kind.ROOM
    assert room.area_m2 is None
    # La disposición del piso se conserva: es informativa
    assert (room.disposition, room.rent) == ("4+1", 5000)


@pytest.mark.parametrize("charges,service,utility,list_fees,expected", [
    (3000, 500, 2000, 2500, 3000),
    (None, None, None, 2500, 2500),
    (_MISSING, None, None, 2500, 2500),
    (None, 500, 2000, None, 2500),
    (None, 500, 2000, 999, 2500),
    (_MISSING, 500, 2000, None, 2500),
    (None, 500, None, None, 500),
    (None, None, 2000, None, 2000),
    (None, None, None, None, None),
])
def test_parse_detail_fees(charges, service, utility, list_fees, expected):
    html = _mutate(DETAIL_HTML, lambda data: _set(
        _detail_advert(data), charges=charges, serviceCharges=service, utilityCharges=utility,
    ))
    base = dataclasses.replace(parse_list(LIST_HTML)[0], fees=list_fees)
    assert parse_detail(html, base).fees == expected


@pytest.mark.parametrize("raw", [10**18, "2026-12-01", None, 0, True, _MISSING])
def test_parse_detail_invalid_available_from(raw):
    html = _mutate(DETAIL_HTML, lambda data: _set(_detail_advert(data), availableFrom=raw))
    listing = parse_detail(html, parse_list(LIST_HTML)[0])
    assert listing.available_from is None
    assert listing.description


def test_non_czk_currency_drops_prices():
    first = parse_list(_mutate(LIST_HTML, lambda data: _set(_list_advert(data), currency="EUR")))[0]
    assert (first.rent, first.fees) == (None, None)
    assert first.disposition == "1+1"

    html = _mutate(DETAIL_HTML, lambda data: _set(_detail_advert(data), currency="EUR", deposit=500))
    detail = parse_detail(html, parse_list(LIST_HTML)[0])
    assert (detail.fees, detail.deposit) == (None, None)


def test_missing_currency_is_taken_as_czk():
    first = parse_list(_mutate(LIST_HTML, lambda data: _set(_list_advert(data), currency=_MISSING)))[0]
    assert (first.rent, first.fees) == (13500, 2500)


@pytest.mark.parametrize("status", [404, 500])
def test_collector_raises_on_http_error(status):
    collector = _collector(lambda request: httpx.Response(status, text="error"))
    with pytest.raises(httpx.HTTPStatusError):
        collector.fetch_list()
    with pytest.raises(httpx.HTTPStatusError):
        collector.fetch_detail(parse_list(LIST_HTML)[0])


def test_collector_raises_without_next_data():
    collector = _collector(lambda request: httpx.Response(200, text="<html><body>captcha</body></html>"))
    with pytest.raises(ValueError, match="__NEXT_DATA__"):
        collector.fetch_list()
    with pytest.raises(ValueError, match="__NEXT_DATA__"):
        collector.fetch_detail(parse_list(LIST_HTML)[0])
