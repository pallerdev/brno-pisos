import dataclasses
import re
from pathlib import Path

import httpx
import pytest

from pisobot.collectors import build_collectors
from pisobot.collectors.bazos import LIST_URL, Bazos, parse_detail, parse_list
from pisobot.config import load_config
from pisobot.models import Kind

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
LIST_HTML = (FIXTURES / "bazos_list.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "bazos_detail.html").read_text(encoding="utf-8")
BRNO = ["60", "61", "62", "63", "64"]
DETAIL_URL = "https://reality.bazos.cz/inzerat/226623653/brno-bystrc-pronajem-bytu-3kk-zarizeny-vytah-byt.php"
# Los tres anuncios de la página con código postal fuera de Brno (664xx: Brno-venkov, Modřice...)
OUTSIDE_BRNO = {"226701322", "226700078", "226692608"}


def _replaced(html, pattern, replacement, count=1):
    changed, found = re.subn(pattern, replacement, html, flags=re.S)
    assert found == count, f"el fixture trae {pattern!r} {found} veces, no {count}"
    return changed


def _first_card(*, title=None, price=None):
    """Listado con la primera tarjeta (la del anuncio 226623653) cambiada: título y/o precio."""
    html = LIST_HTML
    if title is not None:
        html = _replaced(
            html,
            r'(<h2 class=nadpis><a href="/inzerat/226623653/[^"]*">)[^<]*(</a></h2>)',
            lambda match: f"{match[1]}{title}{match[2]}",
        )
    if price is not None:
        html = _replaced(
            html,
            r'(<h2 class=nadpis><a href="/inzerat/226623653/.*?<div class="inzeratycena">)(.*?)(</div>)',
            lambda match: f"{match[1]}{price}{match[3]}",
        )
    return html


def _detail_with_text(text):
    pattern = r"(<div class=popisdetail>)(.*?)(</div>)"
    return _replaced(DETAIL_HTML, pattern, lambda match: f"{match[1]}{text}{match[3]}")


def _first(html):
    return parse_list(html, BRNO)[0]


def _second_page():
    """Segunda página: los anuncios de la primera con otro id, salvo dos que se repiten (uno publicado entre las
    dos peticiones desplaza la paginación y el último de una página reaparece al principio de la siguiente)."""
    repeated = {"226623653", "225944087"}
    return re.sub(r"/inzerat/(\d+)/", lambda m: m[0] if m[1] in repeated else f"/inzerat/9{m[1]}/", LIST_HTML)


SECOND_PAGE = _second_page()
SECOND_PAGE_PATH = "/pronajmu/byt/40/"


def _site(second_page, second_status=200):
    """Servidor falso: la primera página es el fixture y la segunda lo que se le pase."""

    def handler(request):
        if request.url.path == SECOND_PAGE_PATH:
            return httpx.Response(second_status, text=second_page)
        return httpx.Response(200, text=LIST_HTML)

    return handler


def _collector(handler):
    return Bazos(httpx.Client(transport=httpx.MockTransport(handler)), BRNO)


def test_parse_list_filters_by_postcode():
    listings = parse_list(LIST_HTML, BRNO)
    assert 0 < len(listings) <= 40
    first = listings[0]
    assert first.source_id == "226623653"
    assert first.title == "Brno - Bystrc, pronájem bytu 3+kk, zařízený, výtah - byt"
    assert (first.kind, first.rent, first.disposition) == (Kind.FLAT, 20000, "3+kk")
    assert first.address_text == "Brno 63500"
    assert first.location_precision == "postcode" and first.needs_detail
    assert parse_list(LIST_HTML, ["99"]) == []
    assert LIST_URL == "https://reality.bazos.cz/pronajmu/byt/"


def test_parse_list_leaves_out_the_postcodes_outside_brno():
    everything = {listing.source_id for listing in parse_list(LIST_HTML, ["6"])}
    in_brno = {listing.source_id for listing in parse_list(LIST_HTML, BRNO)}
    assert OUTSIDE_BRNO <= everything
    assert not OUTSIDE_BRNO & in_brno
    assert len(everything) == len(in_brno) + len(OUTSIDE_BRNO)


def test_parse_detail_completes_listing():
    listing = parse_detail(DETAIL_HTML, parse_list(LIST_HTML, BRNO)[0])
    assert (listing.rent, listing.fees, listing.area_m2) == (20000, 4000, 58)
    assert (listing.lat, listing.lon) == (49.235758, 16.489105)
    assert listing.location_precision == "postcode"
    assert len(listing.description) > 1000
    assert listing.needs_detail is False


def test_parse_detail_from_url():
    listing = parse_detail(DETAIL_HTML, url=DETAIL_URL)
    assert (listing.source_id, listing.rent, listing.disposition) == ("226623653", 20000, "3+kk")
    assert (listing.kind, listing.url, listing.title) == (
        Kind.FLAT, DETAIL_URL, "Brno - Bystrc, pronájem bytu 3+kk, zařízený, výtah - byt",
    )
    assert (listing.fees, listing.area_m2, listing.needs_detail) == (4000, 58, False)
    assert (listing.lat, listing.lon) == (49.235758, 16.489105)
    assert (listing.address_text, listing.location_precision) == ("Brno 63500", "postcode")


def test_detail_address_matches_the_list_one():
    assert parse_detail(DETAIL_HTML, url=DETAIL_URL).address_text == _first(LIST_HTML).address_text


def test_detail_without_location_row_has_no_address():
    html = _replaced(DETAIL_HTML, r'<tr><td height="22px" width="25%">Lokalita:</td>.*?</tr>', "")
    listing = parse_detail(html, url=DETAIL_URL)
    assert (listing.address_text, listing.location_precision) == ("", "none")
    assert (listing.lat, listing.lon) == (None, None)


def test_detail_location_without_a_postcode_has_no_address():
    # Solo "Brno": no sitúa el anuncio, y geocodificar la ciudad pondría el piso en el centro
    html = _replaced(DETAIL_HTML, r'<a href="https://www\.google\.com/maps[^>]*>635 00</a> ', "")
    listing = parse_detail(html, url=DETAIL_URL)
    assert (listing.address_text, listing.location_precision) == ("", "none")
    assert (listing.lat, listing.lon) == (None, None)


def test_build_collectors():
    cfg = load_config(ROOT / "config.yaml", env={"WORK_LATLON": "49.18,16.60"})
    names = [c.name for c in build_collectors(None, cfg)]
    assert names == ["bezrealitky", "ulovdomov", "espolubydleni", "idnes", "bazos"]


def test_build_collectors_passes_the_postcode_prefixes_to_bazos():
    cfg = load_config(ROOT / "config.yaml", env={"WORK_LATLON": "49.18,16.60"})
    bazos = build_collectors(None, cfg)[-1]
    assert bazos.psc_prefixes == BRNO


@pytest.mark.parametrize("price, rent", [
    ("<b><span translate=\"no\">  20 000 Kč</span></b>", 20000),
    ("<b><span translate=\"no\">  Dohodou</span></b>", None),
    ("<b><span translate=\"no\">  1 000 €</span></b>", None),
    ("", None),
])
def test_list_rent_is_a_czk_amount_or_none(price, rent):
    assert _first(_first_card(price=price)).rent == rent


@pytest.mark.parametrize("title, kind, area", [
    ("pronájem pokoje", Kind.ROOM, None),
    ("Pokoj 15 m2 v bytě 3+1", Kind.ROOM, None),
    ("Hledám spolubydlícího, 12 m2", Kind.ROOM, None),
    ("Pronájem dvoupokojového bytu 50 m2", Kind.FLAT, 50),
    ("Pronájem bytu o 3 pokojích, 70 m2", Kind.FLAT, 70),
    ("Pronájem 2-pokojového bytu 50 m2", Kind.FLAT, 50),
    ("Byt 1+kk, 1-pokojový, 35 m2", Kind.FLAT, 35),
    ("Pronájem bytu 2+kk, 43 m2", Kind.FLAT, 43),
])
def test_kind_from_the_title_and_room_without_area(title, kind, area):
    listing = _first(_first_card(title=title))
    assert (listing.kind, listing.area_m2) == (kind, area)


def test_kind_known_limits_of_the_title_rule():
    # Límites conocidos de mirar solo el título: con el numeral escrito en letra parece una habitación y
    # "1 pokoje" parece un piso. Si la regla cambia, este test obliga a decidirlo a propósito.
    assert _first(_first_card(title="Pronájem bytu o dvou pokojích")).kind is Kind.ROOM
    assert _first(_first_card(title="Pronájem 1 pokoje v bytě 3+1")).kind is Kind.FLAT


def test_detail_of_a_room_has_no_area():
    room = _first(_first_card(title="pronájem pokoje"))
    # El detalle habla del piso entero (58 m²): en una habitación no es la superficie del cuarto
    assert parse_detail(DETAIL_HTML, room).area_m2 is None


def test_detail_area_fills_a_list_listing_without_one():
    flat = _first(_first_card(title="Pronájem bytu 3+kk"))
    assert flat.area_m2 is None
    assert parse_detail(DETAIL_HTML, flat).area_m2 == 58


@pytest.mark.parametrize("text, fees", [
    ("Nájemné 20.000,- Kč + 4.000,- Kč za služby.", 4000),
    ("Nájem 20 000 Kč + 3 500 Kč poplatky", 3500),
    ("Nájem 20000 Kč +2500Kč energie", 2500),
    ("Nájemné 20.000,- Kč + 4.000,- Kč inkaso.", 4000),
    ("Nájem 20 000 Kč + 4 000 Kč služba.", 4000),
    ("Nájem 20 000 Kč + 4 000 Kč záloha na služby.", 4000),
    ("Nájem 20 000 Kč + 4 000 Kč zálohy na služby.", 4000),
    ("Nájem 20 000 Kč + služby 4 000 Kč.", 4000),
    ("Nájem 20 000 Kč + služby: 3.500,- Kč.", 3500),
    ("Nájem 20 000 Kč + inkaso 4000 Kč.", 4000),
    ("Nájem 20 000 Kč + energie 2500Kč.", 2500),
    ("Nájem 20 000 Kč + zálohy na služby 3 500 Kč.", 3500),
    ("Nájem 20 000 Kč + záloha na energie 3 500,- Kč.", 3500),
    ("Nájem 20 000 Kč + záloha na energie 2000 Kč.", 2000),
    ("Nájem 20 000 Kč + 2 500 Kč záloha na poplatky.", 2500),
    # "záloha" sin más suele ser la fianza: solo vale si es un anticipo de servicios
    ("Nájem 20 000 Kč + záloha 40 000 Kč.", None),
    ("Nájem 20 000 Kč + 40 000 Kč záloha.", None),
    ("Nájem 20 000 Kč + 40 000 Kč záloha na kauci.", None),
    ("Kauce 40 000 Kč + záloha 40 000 Kč.", None),
    ("Nájem 20 000 Kč včetně služeb.", None),
    ("Nájem 20 000 Kč + služby v ceně.", None),
    ("Nájem 20 000 Kč + poplatky dohodou.", None),
    ("Garáž za + 1 000 Kč měsíčně.", None),
    ("Kauce + 40 000 Kč.", None),
])
def test_fees_are_read_only_from_the_services_surcharge(text, fees):
    assert parse_detail(_detail_with_text(text), _first(LIST_HTML)).fees == fees


def test_detail_keeps_list_data_the_page_lacks():
    original = dataclasses.replace(_first(LIST_HTML), fees=1234, lat=49.1, lon=16.5, description="del listado")
    html = _replaced(DETAIL_HTML, r'<tr><td height="22px" width="25%"><br>Cena:</td>.*?</tr>', "")
    html = _replaced(html, r'<a href="https://www.google.com/maps[^>]*>[^<]*</a>', "")
    html = _replaced(html, r"(<div class=popisdetail>)(.*?)(</div>)", lambda match: f"{match[1]}{match[3]}")
    detail = parse_detail(html, original)
    assert (detail.rent, detail.fees, detail.lat, detail.lon) == (20000, 1234, 49.1, 16.5)
    assert (detail.description, detail.needs_detail) == ("del listado", False)


def test_detail_fills_a_missing_rent():
    original = dataclasses.replace(_first(LIST_HTML), rent=None)
    assert parse_detail(DETAIL_HTML, original).rent == 20000


@pytest.mark.parametrize("href", [
    "https://www.google.com/maps/place/95.0,16.4/@95.0,16.4,12z",
    "https://www.google.com/maps/place/49.2,200.0/@49.2,200.0,12z",
    "https://www.google.com/maps/search/?api=1&query=Brno",
])
def test_unusable_gps_is_ignored(href):
    html = _replaced(DETAIL_HTML, r'<a href="https://www\.google\.com/maps[^"]*"', f'<a href="{href}"')
    listing = parse_detail(html, dataclasses.replace(_first(LIST_HTML), lat=49.1, lon=16.5))
    assert (listing.lat, listing.lon) == (49.1, 16.5)


def test_gps_comes_from_the_location_row_only():
    stray = '<a href="https://www.google.com/maps/place/50.0,17.0/@50.0,17.0,12z">otro mapa</a>'
    listing = parse_detail(_detail_with_text(f"Texto {stray}"), _first(LIST_HTML))
    assert (listing.lat, listing.lon) == (49.235758, 16.489105)


def test_parse_list_without_cards_raises():
    with pytest.raises(ValueError, match=re.escape("Bazoš: el listado no trae anuncios (¿cambió el formato?)")):
        parse_list("<html><body><p>Mantenimiento</p></body></html>", BRNO)


def test_parse_list_with_renamed_cards_raises():
    with pytest.raises(ValueError, match="no trae anuncios"):
        parse_list(LIST_HTML.replace("inzeraty inzeratyflex", "anuncios"), BRNO)


def test_parse_list_without_links_or_ids_raises():
    for html in (
        LIST_HTML.replace("<h2 class=nadpis>", "<h2 class=titulo>"),
        LIST_HTML.replace("/inzerat/", "/anuncio/"),
    ):
        with pytest.raises(ValueError, match="no trae anuncios"):
            parse_list(html, BRNO)


def test_parse_list_without_postcodes_raises():
    # Si cambia el marcado de la localidad todo se descarta por filtro: tiene que verse como fallo, no como "0 en Brno"
    with pytest.raises(ValueError, match="Bazoš: el listado no trae el código postal"):
        parse_list(LIST_HTML.replace('class="inzeratylok"', 'class="lugar"'), BRNO)


def test_listings_outside_brno_are_not_a_failure():
    assert parse_list(LIST_HTML, ["99"]) == []
    assert parse_list(LIST_HTML, []) == []


def test_parse_detail_without_the_ad_raises():
    with pytest.raises(ValueError, match="Bazoš: el detalle no trae la ficha del anuncio"):
        parse_detail("<html><body><p>Inzerát neexistuje</p></body></html>", url=DETAIL_URL)
    with pytest.raises(ValueError, match="el detalle no trae la ficha"):
        parse_detail("<html><body><p>Inzerát neexistuje</p></body></html>", _first(LIST_HTML))


def test_parse_detail_without_title_and_listing_raises():
    html = _replaced(DETAIL_HTML, r"<H1 class=nadpisdetail>", "<H1 class=otro>")
    with pytest.raises(ValueError, match="el detalle no trae la ficha"):
        parse_detail(html, url=DETAIL_URL)
    # Con el anuncio del listado el título no hace falta
    assert parse_detail(html, _first(LIST_HTML)).needs_detail is False


@pytest.mark.parametrize("url", ["", "https://reality.bazos.cz/pronajmu/byt/"])
def test_parse_detail_without_listing_id_in_url_raises(url):
    with pytest.raises(ValueError, match="la URL no lleva el id"):
        parse_detail(DETAIL_HTML, url=url)


def test_fetch_list_reads_two_pages_of_the_national_category_without_filters():
    requests = []
    handler = _site(SECOND_PAGE)

    def recording(request):
        requests.append(request.url)
        return handler(request)

    listings = _collector(recording).fetch_list()
    assert [str(url) for url in requests] == [LIST_URL, "https://reality.bazos.cz/pronajmu/byt/40/"]
    assert all(url.query == b"" for url in requests)
    first, second = parse_list(LIST_HTML, BRNO), parse_list(SECOND_PAGE, BRNO)
    known = {listing.key for listing in first}
    assert len(first) == len(second) == 37 and len(known & {listing.key for listing in second}) == 2
    # Los repetidos salen una vez, con el orden de la primera página; después, lo nuevo de la segunda
    assert listings == first + [listing for listing in second if listing.key not in known]
    assert len(listings) == 37 + 35


@pytest.mark.parametrize("status", [404, 500])
def test_fetch_list_raises_on_http_error(status):
    with pytest.raises(httpx.HTTPStatusError):
        _collector(lambda request: httpx.Response(status, text="error")).fetch_list()


@pytest.mark.parametrize("status", [404, 500])
def test_fetch_list_raises_when_the_second_page_fails(status):
    with pytest.raises(httpx.HTTPStatusError):
        _collector(_site("error", status)).fetch_list()


def test_fetch_list_raises_on_a_page_without_cards():
    with pytest.raises(ValueError, match="Bazoš"):
        _collector(lambda request: httpx.Response(200, text="<html><body></body></html>")).fetch_list()


def test_fetch_list_raises_when_the_second_page_has_no_cards():
    with pytest.raises(ValueError, match="el listado no trae anuncios"):
        _collector(_site("<html><body></body></html>")).fetch_list()


def test_fetch_list_raises_when_the_second_page_has_no_postcodes():
    with pytest.raises(ValueError, match="el listado no trae el código postal"):
        _collector(_site(SECOND_PAGE.replace('class="inzeratylok"', 'class="lugar"'))).fetch_list()


def test_second_page_without_ads_from_brno_is_not_a_failure():
    elsewhere = _replaced(SECOND_PAGE, r"<br>\d{3} \d{2}</div>", "<br>700 00</div>", count=40)
    assert _collector(_site(elsewhere)).fetch_list() == parse_list(LIST_HTML, BRNO)


def test_fetch_detail_requests_the_listing_page():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=DETAIL_HTML)

    original = _first(LIST_HTML)
    listing = _collector(handler).fetch_detail(original)
    assert requests == [original.url]
    assert (listing.source_id, listing.fees, listing.needs_detail) == ("226623653", 4000, False)


@pytest.mark.parametrize("status", [404, 500])
def test_fetch_detail_raises_on_http_error(status):
    with pytest.raises(httpx.HTTPStatusError):
        _collector(lambda request: httpx.Response(status, text="error")).fetch_detail(_first(LIST_HTML))
