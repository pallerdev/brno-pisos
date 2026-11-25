import copy
import dataclasses
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from pisobot.collectors.single_url import (
    SREALITY_API, SREALITY_ID, UnsupportedReviewURL, fetch, fetch_review_url, page_text, parse_sreality,
)
from pisobot.models import Kind, Listing
from pisobot.parse_text import parse_text

FIXTURES = Path(__file__).parent / "fixtures"
SREALITY = json.loads((FIXTURES / "sreality_detail.json").read_text(encoding="utf-8"))
SR_URL = "https://www.sreality.cz/detail/pronajem/byt/pokoj/brno-dolni-herspice-chleboradova/1704005708"
IDNES_URL = "https://reality.idnes.cz/detail/pronajem/byt/brno-bratislavska/6af5ba0b0e659b5db409eed4/"
BAZOS_URL = "https://reality.bazos.cz/inzerat/226623653/x.php"
BEZREALITKY_URL = "https://www.bezrealitky.cz/nemovitosti-byty-domy/1092907-nabidka-pronajem-bytu-stefanikova-brno"
ESPOLUBYDLENI_URL = (
    "https://www.espolubydleni.cz/podnajem-spolubydlici/554939-pronajem-panelovy-byt-2-1-kralovo-pole-brno.htm"
)
PERMANENTIA_URL = "https://www.permanentia.cz/reality/pronajem-bytu-1-kk-v-brne-na-ulici-bayerova-453121/"
PERMANENTIA_LIST_URL = "https://www.permanentia.cz/pronajem/byty/jihomoravsky/?idMesta%5B0%5D=582786"
_MISSING = object()


@pytest.mark.parametrize("url", [SR_URL, SR_URL + "?foo=1", SR_URL + "#x", SR_URL + "/"])
def test_sreality_id(url):
    # El id se busca solo en la ruta: lo que haya en la consulta o el fragmento no cuenta
    assert SREALITY_ID.match(urlparse(url).path).group(1) == "1704005708"


def _sreality(**fields):
    """Copia del payload con campos de result cambiados (_MISSING quita el campo)."""
    payload = copy.deepcopy(SREALITY)
    for name, value in fields.items():
        if value is _MISSING:
            payload["result"].pop(name, None)
        else:
            payload["result"][name] = value
    return payload


def _located(**fields):
    """Copia del payload con campos de locality cambiados."""
    payload = copy.deepcopy(SREALITY)
    payload["result"]["locality"].update(fields)
    return payload


def test_parse_sreality():
    listing = parse_sreality(SREALITY, SR_URL)
    assert listing.source == "sreality" and listing.url == SR_URL
    assert listing.source_id == "1704005708"
    assert listing.title == "Pronájem bytu pokoje 14 m²"
    assert (listing.kind, listing.rent, listing.area_m2) == (Kind.ROOM, 13000, 14)
    assert listing.lat == pytest.approx(49.1519993224724)
    assert listing.lon == pytest.approx(16.6193184132719)
    assert listing.location_precision == "street"
    assert listing.available_from == date(2026, 11, 1)
    assert listing.address_text == "Chleborádova, Dolní Heršpice, Brno"
    assert listing.furnished is True
    assert (listing.fees, listing.deposit, listing.disposition) == (None, None, None)
    assert len(listing.description) > 1000


def test_parse_sreality_accepts_the_bare_result():
    assert parse_sreality(SREALITY["result"], SR_URL) == parse_sreality(SREALITY, SR_URL)


def test_parse_sreality_flat():
    payload = _sreality(category_sub_cb={"name": "2+kk", "value": 4})
    listing = parse_sreality(payload, SR_URL)
    assert (listing.kind, listing.disposition) == (Kind.FLAT, "2+kk")


@pytest.mark.parametrize("payload", [
    _sreality(hash_id=_MISSING),
    _sreality(hash_id=None),
    {"status_code": 404, "status_message": "Not found"},
    {"result": None},
    {"result": []},
    [],
    None,
])
def test_parse_sreality_without_an_ad_raises(payload):
    with pytest.raises(ValueError, match="Sreality"):
        parse_sreality(payload, SR_URL)


@pytest.mark.parametrize("value,expected", [
    (13000.0, 13000),  # el API manda los importes como float
    (13000, 13000),
    # Texto, bool, negativos, cero, decimales y no finitos no son un importe fiable: mejor sin dato que uno falso
    ("13000", None),
    (True, None),
    (-5, None),
    (0, None),
    (1, None),  # 1 Kč es el "cena na vyžádání" de Sreality
    (13000.5, None),
    (float("inf"), None),
    (None, None),
])
def test_sreality_rent_is_a_whole_positive_number(value, expected):
    assert parse_sreality(_sreality(price_czk=value), SR_URL).rent == expected


def test_sreality_rent_without_price_field():
    assert parse_sreality(_sreality(price_czk=_MISSING), SR_URL).rent is None


@pytest.mark.parametrize("unit,expected", [
    ({"name": "za měsíc", "value": 2}, 13000),
    (None, 13000),
    # Con otro periodo o por metro cuadrado el importe no es el alquiler mensual
    ({"name": "za den", "value": 1}, None),
    ({"name": "za rok", "value": 5}, None),
    ({"name": "za m² / měsíc", "value": 4}, None),
    ({"name": "za nemovitost", "value": 1}, None),
])
def test_sreality_rent_needs_a_monthly_unit(unit, expected):
    assert parse_sreality(_sreality(price_unit_cb=unit), SR_URL).rent == expected


def test_sreality_other_currency_gives_no_amounts():
    payload = _sreality(
        price_currency_cb={"name": "€", "value": 2}, cost_of_living="100 €", refundable_deposit=20000,
    )
    listing = parse_sreality(payload, SR_URL)
    assert (listing.rent, listing.fees, listing.deposit) == (None, None, None)


@pytest.mark.parametrize("value,expected", [
    ("3 000 Kč", 3000),
    ("3.000,- Kč měsíčně", 3000),
    # El primer importe, nunca las cifras de todo el texto pegadas
    ("3000 Kč + energie 2000 Kč", 3000),
    (3000, 3000),
    (3000.0, 3000),
    (0, 0),
    ("", None),
    ("dle spotřeby", None),
    ("3 000,50 Kč", None),
    (3000.5, None),
    (True, None),
    (-1, None),
    (None, None),
])
def test_sreality_fees_and_deposit(value, expected):
    listing = parse_sreality(_sreality(cost_of_living=value, refundable_deposit=value), SR_URL)
    assert (listing.fees, listing.deposit) == (expected, expected)


@pytest.mark.parametrize("value,expected", [
    ("2026-11-01", date(2026, 11, 1)),
    ("2026-11-01T00:00:00", date(2026, 11, 1)),
    ("ihned", None),
    ("2026-13-45", None),
    (20261101, None),
    (None, None),
    (_MISSING, None),
])
def test_sreality_ready_date_never_raises(value, expected):
    assert parse_sreality(_sreality(ready_date=value), SR_URL).available_from == expected


@pytest.mark.parametrize("value,expected", [
    (14, 14),
    (14.4, 14),
    ("14", None),
    (0, None),
    (True, None),
    (None, None),
])
def test_sreality_area(value, expected):
    assert parse_sreality(_sreality(usable_area=value), SR_URL).area_m2 == expected


@pytest.mark.parametrize("furnished,expected", [
    ({"name": "Ano", "value": 1}, True),
    ({"name": "Částečně", "value": 3}, True),
    ({"name": "Ne", "value": 2}, False),
    ({"name": "- vyber možnost", "value": 0}, None),
    (None, None),
    (_MISSING, None),
])
def test_sreality_furnished(furnished, expected):
    assert parse_sreality(_sreality(furnished=furnished), SR_URL).furnished is expected


@pytest.mark.parametrize("kind,precision", [
    ("address", "exact"),
    ("street", "street"),
    ("quarter", "postcode"),
    ("citypart", "postcode"),
    ("district", "postcode"),
    # Un tipo que no conocemos nunca se da por exacto
    ("algo-nuevo", "postcode"),
    (None, "postcode"),
])
def test_sreality_location_precision(kind, precision):
    listing = parse_sreality(_located(inaccuracy_type=kind), SR_URL)
    assert listing.location_precision == precision
    assert listing.point is not None


@pytest.mark.parametrize("kind", [["street"], {"name": "street"}, 5, True])
def test_sreality_accuracy_that_is_not_text_is_unknown(kind):
    listing = parse_sreality(_located(inaccuracy_type=kind), SR_URL)
    assert listing.location_precision == "postcode"
    assert listing.point is not None


@pytest.mark.parametrize("kind", ["city", "municipality", "region"])
def test_sreality_city_level_location_is_not_geocodable(kind):
    # El centro de la ciudad despistaría a transporte y supermercado: ni dirección ni punto
    listing = parse_sreality(_located(inaccuracy_type=kind, street=None, citypart=None), SR_URL)
    assert (listing.location_precision, listing.address_text) == ("none", "")
    assert listing.point is None


@pytest.mark.parametrize("lat,lon", [(91, 16.6), (49.1, 181), ("49.1", 16.6), (True, 16.6), (None, 16.6), (49.1, None)])
def test_sreality_invalid_coordinates_give_no_point(lat, lon):
    listing = parse_sreality(_located(gps_lat=lat, gps_lon=lon), SR_URL)
    assert (listing.lat, listing.lon) == (None, None)


def test_sreality_notes_and_heating_reach_the_text_rules():
    payload = _sreality(price_note="+ energie dle spotřeby", cost_of_living="3 000 Kč")
    listing = parse_sreality(payload, SR_URL)
    assert "+ energie dle spotřeby" in listing.description and "3 000 Kč" in listing.description
    assert parse_text(listing.description).energy_included is False
    # El elektrokotel solo sale en las listas de calefacción: sin añadirlas, parse_text no lo ve
    assert "Elektrokotel" in listing.description


def test_sreality_heating_block_changes_only_electric_heating():
    raw = SREALITY["result"]["advert_description"]
    before, after = parse_text(raw), parse_text(parse_sreality(SREALITY, SR_URL).description)
    assert before.electric_heating is False and after.electric_heating is True
    assert dataclasses.replace(before, electric_heating=True) == after


@pytest.mark.parametrize("heating,electric", [
    ("Lokální elektrické", True),
    ("Ústřední elektrické", True),
    ("Ústřední plynové", False),
    ("Ústřední dálkové", False),
])
def test_sreality_heating_type_reaches_the_electric_rule(heating, electric):
    payload = _sreality(
        heating_set=[{"name": heating, "value": 1}], heating_source_set=[], heating_element_set=[],
    )
    description = parse_sreality(payload, SR_URL).description
    assert f"{heating} topení" in description
    assert parse_text(description).electric_heating is electric


def test_sreality_heating_lists_keep_their_labels():
    payload = _sreality(heating_set=[{"name": "Ústřední plynové", "value": 1}], heating_source_set=[
        {"name": "Plynový kotel", "value": 2}, {"name": "Dálkové", "value": 3},
    ])
    block = parse_sreality(payload, SR_URL).description.splitlines()[-1]
    assert block == (
        "Parametry inzerátu: Ústřední plynové topení | Zdroj topení: Plynový kotel, Dálkové | Topná tělesa: Radiátory"
    )


def test_sreality_description_without_notes_or_heating_is_unchanged():
    payload = _sreality(heating_set=[], heating_source_set=[], heating_element_set=[])
    assert parse_sreality(payload, SR_URL).description == SREALITY["result"]["advert_description"]


ROUTES = {
    SREALITY_API.format(id="1704005708"): ("json", "sreality_detail.json"),
    IDNES_URL: ("html", "idnes_detail.html"),
    BAZOS_URL: ("html", "bazos_detail.html"),
    BEZREALITKY_URL: ("html", "bezrealitky_detail.html"),
    ESPOLUBYDLENI_URL: ("html", "espolubydleni_detail.html"),
    PERMANENTIA_URL: ("html", "permanentia_detail.html"),
    "https://example.com/inzerat": ("text", "<html><body><p>Pronajmu pokoj, 7000 Kč</p></body></html>"),
}


def router(request):
    kind, value = ROUTES[str(request.url)]
    if kind == "json":
        return httpx.Response(200, json=json.loads((FIXTURES / value).read_text(encoding="utf-8")))
    if kind == "html":
        return httpx.Response(200, text=(FIXTURES / value).read_text(encoding="utf-8"))
    return httpx.Response(200, text=value)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _serving(body, status=200):
    return _client(lambda request: httpx.Response(status, text=body))


@pytest.mark.parametrize("url,source", [
    (SR_URL, "sreality"),
    (IDNES_URL, "idnes"),
    (BAZOS_URL, "bazos"),
    (BEZREALITKY_URL, "bezrealitky"),
    (ESPOLUBYDLENI_URL, "espolubydleni"),
    (PERMANENTIA_URL, "permanentia"),
])
def test_fetch_known_portals(url, source):
    result = fetch(_client(router), url)
    assert result.source == source and result.rent


def test_fetch_permanentia_returns_its_listing():
    result = fetch(_client(router), PERMANENTIA_URL)
    assert isinstance(result, Listing)
    assert (result.source_id, result.rent, result.fees, result.deposit) == ("453121", 9500, 2500, 19000)
    assert (result.lat, result.lon, result.location_precision) == (49.2078094, 16.6016636, "exact")


def test_fetch_unknown_returns_page_text():
    result = fetch(_client(router), "https://example.com/inzerat")
    assert result == "Pronajmu pokoj, 7000 Kč"


def test_fetch_sreality_asks_only_the_api_once():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return router(request)

    fetch(_client(handler), SR_URL + "?utm=1")
    assert seen == [SREALITY_API.format(id="1704005708")]


@pytest.mark.parametrize("url", [
    "https://www.sreality.cz/hledani/pronajem/byty/brno",
    "https://www.sreality.cz/detail/pronajem/byt/brno",
    "https://sreality.cz/",
])
def test_fetch_sreality_page_without_a_detail_id_makes_no_request(url):
    def handler(request):
        raise AssertionError(f"no debe pedir nada: {request.url}")

    # Su robots.txt veta rastrear la web: sin id de anuncio no hay nada que pedir, ni la propia página
    assert fetch(_client(handler), url) == ""


def test_fetch_sreality_http_error_raises():
    with pytest.raises(httpx.HTTPStatusError):
        fetch(_serving("", status=404), SR_URL)


def test_fetch_sreality_non_json_raises():
    with pytest.raises(ValueError, match="Sreality"):
        fetch(_serving("<html>captcha</html>"), SR_URL)


def test_fetch_sreality_payload_without_an_ad_raises():
    with pytest.raises(ValueError, match="Sreality"):
        fetch(_serving('{"status_code": 404, "status_message": "Not found"}'), SR_URL)


@pytest.mark.parametrize("status", [404, 500])
def test_fetch_http_error_raises(status):
    with pytest.raises(httpx.HTTPStatusError):
        fetch(_serving("<html><body>nada</body></html>", status=status), BAZOS_URL)


def test_fetch_espolubydleni_serves_the_ad_with_a_404():
    html = (FIXTURES / "espolubydleni_detail.html").read_text(encoding="utf-8")
    result = fetch(_serving(html, status=404), ESPOLUBYDLENI_URL)
    assert isinstance(result, Listing) and result.source_id == "554939"


def test_fetch_espolubydleni_server_error_raises():
    with pytest.raises(httpx.HTTPStatusError):
        fetch(_serving("<html><head><title>Nabízím</title></head></html>", status=500), ESPOLUBYDLENI_URL)


def test_fetch_espolubydleni_seeker_page_returns_page_text():
    html = "<html><head><title>Hledám pokoj</title></head><body><p>Hledám pokoj do 7000 Kč</p></body></html>"
    assert fetch(_serving(html), ESPOLUBYDLENI_URL) == "Hledám pokoj do 7000 Kč"


def test_fetch_ulovdomov_has_no_detail_parser_and_returns_page_text():
    html = "<html><body><h1>Pokoj Brno</h1><p>6500 Kč</p></body></html>"
    assert fetch(_serving(html), "https://www.ulovdomov.cz/fulldetail/123") == "Pokoj Brno\n6500 Kč"


_NEXT = '<script id="__NEXT_DATA__" type="application/json">{}</script>'


@pytest.mark.parametrize("url,html", [
    # Anuncio retirado o página que ya no es un detalle: cada parser lo señala a su manera
    (IDNES_URL, "<html><body><p>retirado</p></body></html>"),
    (BAZOS_URL, "<html><body><p>retirado</p></body></html>"),
    (BEZREALITKY_URL, "<html><body><p>retirado</p></body></html>"),
    (BEZREALITKY_URL, _NEXT.replace("{}", "{") + "<p>retirado</p>"),
    (BEZREALITKY_URL, _NEXT.replace("{}", '{"props": {"pageProps": {}}}') + "<p>retirado</p>"),
    (BEZREALITKY_URL, _NEXT.replace("{}", '{"props": {"pageProps": {"origAdvert": null}}}') + "<p>retirado</p>"),
    (ESPOLUBYDLENI_URL, "<html><head><title>Nabízím pokoj</title></head><body><p>retirado</p></body></html>"),
    (PERMANENTIA_URL, "<html><body><p>retirado</p></body></html>"),
])
def test_fetch_parser_failure_falls_back_to_page_text(url, html):
    assert fetch(_serving(html), url) == "retirado"


@pytest.mark.parametrize("url,fixture", [
    # El portal solo cuenta si es el host, no si su nombre aparece en la ruta o en el parámetro de otra web
    ("https://example.com/?u=https://reality.idnes.cz/detail/x/6af5ba0b0e659b5db409eed4/", "idnes_detail.html"),
    ("https://reality.idnes.cz.example.com/detail/x/6af5ba0b0e659b5db409eed4/", "idnes_detail.html"),
    ("https://example.com/inzerat/226623653/x.php", "bazos_detail.html"),
    ("https://bezrealitky.cz.example.com/nemovitosti-byty-domy/1092907-x", "bezrealitky_detail.html"),
    ("https://espolubydleni.cz.example.com/podnajem/554939-x.htm", "espolubydleni_detail.html"),
    ("https://example.com/podnajem/554939-x.htm?from=espolubydleni.cz", "espolubydleni_detail.html"),
    ("https://permanentia.cz.example.com/reality/x-453121/", "permanentia_detail.html"),
    ("https://example.com/reality/x-453121/?from=permanentia.cz", "permanentia_detail.html"),
    # Páginas del portal que no son un anuncio
    ("https://reality.idnes.cz/s/pronajem/byty/brno/", "idnes_detail.html"),
    ("https://reality.bazos.cz/pronajmu/byt/", "bazos_detail.html"),
    ("https://www.bezrealitky.cz/vypis/nabidka-pronajem/byt/brno", "bezrealitky_detail.html"),
    (PERMANENTIA_LIST_URL, "permanentia_detail.html"),
    ("https://www.permanentia.cz/reality/byty/", "permanentia_detail.html"),
    ("https://www.permanentia.cz/reality/vse/", "permanentia_detail.html"),
])
def test_fetch_only_parses_real_ad_urls_of_each_portal(url, fixture):
    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    assert isinstance(fetch(_serving(html), url), str)


def test_fetch_does_not_parse_sreality_lookalike_hosts():
    url = "https://sreality.cz.example.com/detail/pronajem/byt/pokoj/brno/1704005708"
    assert fetch(_serving("<html><body><p>texto</p></body></html>"), url) == "texto"


def _redirecting(target, listing_html):
    """Cliente que sigue redirecciones (como make_client): todo redirige a target, que sirve listing_html."""
    def handler(request):
        if str(request.url) == target:
            return httpx.Response(200, text=listing_html)
        return httpx.Response(302, headers={"Location": target})

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.mark.parametrize("url,target,fixture", [
    # Un anuncio retirado acaba en el listado del portal, cuyo texto mezcla otros anuncios
    (IDNES_URL, "https://reality.idnes.cz/s/pronajem/byty/brno/", "idnes_list.html"),
    (IDNES_URL, "https://www.idnes.cz/", "idnes_list.html"),
    (BAZOS_URL, "https://reality.bazos.cz/pronajmu/byt/", "bazos_list.html"),
    (BEZREALITKY_URL, "https://www.bezrealitky.cz/vypis/nabidka-pronajem/byt/brno", "bezrealitky_list.html"),
    (ESPOLUBYDLENI_URL, "https://www.espolubydleni.cz/podnajem-spolubydlici/brno", "espolubydleni_list.html"),
    (PERMANENTIA_URL, PERMANENTIA_LIST_URL, "permanentia_list.html"),
    (PERMANENTIA_URL, "https://www.permanentia.cz/", "permanentia_list.html"),
])
def test_fetch_removed_ad_redirected_to_a_list_returns_nothing(url, target, fixture):
    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    assert fetch(_redirecting(target, html), url) == ""


def test_fetch_ad_redirected_to_its_canonical_url_is_still_parsed():
    html = (FIXTURES / "bezrealitky_detail.html").read_text(encoding="utf-8")
    result = fetch(_redirecting(BEZREALITKY_URL, html), "https://www.bezrealitky.cz/nemovitosti-byty-domy/1092907")
    assert isinstance(result, Listing) and result.source == "bezrealitky"


def test_fetch_other_page_redirected_keeps_its_text():
    client = _redirecting("https://example.com/otra", "<html><body><p>texto</p></body></html>")
    assert fetch(client, "https://example.com/inzerat") == "texto"


def test_fetch_traces_a_swallowed_parser_failure(capsys):
    html = "<html><body><p>retirado</p></body></html>"
    assert fetch(_serving(html), IDNES_URL + "?token=secreto") == "retirado"
    # Solo tipo y host: ni la URL con su consulta ni el contenido de la página
    assert capsys.readouterr().err == "single_url: ValueError reality.idnes.cz\n"


def test_fetch_does_not_trace_when_the_parser_works(capsys):
    fetch(_client(router), IDNES_URL)
    fetch(_client(router), "https://example.com/inzerat")
    assert capsys.readouterr().err == ""


def test_page_text_drops_scripts_and_styles():
    html = "<body><style>.a { color: red }</style><p>uno</p><script>var x = 1;</script><p>dos</p></body>"
    assert page_text(html) == "uno\ndos"


def test_page_text_is_capped():
    assert len(page_text("<body><p>" + "a" * 9000 + "</p></body>")) == 8000


def test_page_text_of_an_empty_page():
    assert page_text("") == ""


@pytest.mark.parametrize("url", [
    "https://www.sreality.cz/hledani/pronajem/byty?ref=https://www.sreality.cz/detail/a/b/c/d/123",
    "https://www.sreality.cz/hledani/pronajem/byty#/detail/a/b/c/d/123",
    "https://www.sreality.cz/hledani?x=sreality.cz/detail/a/b/c/d/123",
])
def test_fetch_sreality_id_in_the_query_or_fragment_makes_no_request(url):
    def handler(request):
        raise AssertionError(f"no debe pedir nada: {request.url}")

    assert fetch(_client(handler), url) == ""
    assert fetch_review_url(_client(handler), url) == ""


def _counting(handler):
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    return _client(record), requests


@pytest.mark.parametrize("url", [
    "http://reality.idnes.cz/detail/1",
    "https://example.com/detail/1",
    "https://reality.idnes.cz.example.com/detail/1",
    "https://reality.idnes.cz:8443/detail/1",
    "https://user:pass@reality.idnes.cz/detail/1",
    "https://reality.idnes.cz@example.com/detail/1",
    "https://permanentia.cz.example.com/reality/x-453121/",
    "https://sub.permanentia.cz/reality/x-453121/",
    "https://www.permanentia.cz:8443/reality/x-453121/",
    "http://www.permanentia.cz/reality/x-453121/",
    "https://user:pass@www.permanentia.cz/reality/x-453121/",
    "https://www.permanentia.cz@example.com/reality/x-453121/",
    "https://169.254.169.254/latest/meta-data/",
    "ftp://reality.idnes.cz/detail/1",
    "reality.idnes.cz/detail/1",
])
def test_review_url_outside_the_allowed_portals_is_rejected_without_requests(url):
    client, requests = _counting(lambda request: httpx.Response(200, text="hola"))
    with pytest.raises(UnsupportedReviewURL):
        fetch_review_url(client, url)
    assert requests == []


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/",
    "https://169.254.169.254/latest/meta-data/",
    "https://example.com/detail/1",
    "https://reality.idnes.cz:8443/detail/1",
    "http://reality.idnes.cz/detail/1",
    "https://www.sreality.cz/detail/a/b/c/d/1",
])
def test_review_redirect_to_a_forbidden_target_is_rejected_after_one_request(target):
    client, requests = _counting(lambda request: httpx.Response(302, headers={"Location": target}))
    with pytest.raises(UnsupportedReviewURL):
        fetch_review_url(client, IDNES_URL)
    assert [str(request.url) for request in requests] == [IDNES_URL]


@pytest.mark.parametrize("target", [
    "https://example.com/reality/x-453121/",
    "https://sub.permanentia.cz/reality/x-453121/",
    "https://www.permanentia.cz:8443/reality/x-453121/",
    "http://www.permanentia.cz/reality/x-453121/",
    "https://169.254.169.254/latest/meta-data/",
])
def test_review_redirect_away_from_permanentia_is_rejected_after_one_request(target):
    client, requests = _counting(lambda request: httpx.Response(302, headers={"Location": target}))
    with pytest.raises(UnsupportedReviewURL):
        fetch_review_url(client, PERMANENTIA_URL)
    assert [str(request.url) for request in requests] == [PERMANENTIA_URL]


@pytest.mark.parametrize("url", [
    PERMANENTIA_URL,
    "https://permanentia.cz/reality/pronajem-bytu-1-kk-v-brne-na-ulici-bayerova-453121/",
])
def test_review_permanentia_with_or_without_www_is_parsed(url):
    # El portal redirige el dominio sin www al canónico: los dos dominios están permitidos
    html = (FIXTURES / "permanentia_detail.html").read_text(encoding="utf-8")

    def handler(request):
        if request.url.host == "permanentia.cz":
            return httpx.Response(301, headers={"Location": PERMANENTIA_URL})
        return httpx.Response(200, text=html)

    client, requests = _counting(handler)
    result = fetch_review_url(client, url)
    assert isinstance(result, Listing) and result.source == "permanentia"
    assert result.url == PERMANENTIA_URL
    assert {request.url.host for request in requests} <= {"permanentia.cz", "www.permanentia.cz"}


def test_review_relative_redirect_to_a_list_page_returns_nothing():
    html = (FIXTURES / "bazos_list.html").read_text(encoding="utf-8")

    def handler(request):
        if request.url.path == "/pronajmu/byt/":
            return httpx.Response(200, text=html)
        return httpx.Response(302, headers={"Location": "/pronajmu/byt/"})

    client, requests = _counting(handler)
    assert fetch_review_url(client, BAZOS_URL) == ""
    assert [request.url.host for request in requests] == ["reality.bazos.cz"] * 2


def test_review_redirect_loop_is_bounded():
    client, requests = _counting(lambda request: httpx.Response(302, headers={"Location": "/otra"}))
    with pytest.raises(httpx.TooManyRedirects):
        fetch_review_url(client, IDNES_URL)
    assert len(requests) == 6


def test_review_url_happy_path_returns_the_listing_with_a_single_request():
    html = (FIXTURES / "bezrealitky_detail.html").read_text(encoding="utf-8")
    client, requests = _counting(lambda request: httpx.Response(200, text=html))
    result = fetch_review_url(client, BEZREALITKY_URL)
    assert isinstance(result, Listing) and result.source == "bezrealitky"
    assert [str(request.url) for request in requests] == [BEZREALITKY_URL]
