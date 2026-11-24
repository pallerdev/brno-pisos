import dataclasses
import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from pisobot.collectors.idnes import _PARAMS_LABEL, BASE, LIST_URLS, Idnes, parse_detail, parse_list
from pisobot.models import Kind
from pisobot.parse_text import TextFacts, parse_text

FIXTURES = Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "idnes_list.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "idnes_detail.html").read_text(encoding="utf-8")
DETAIL_URL = "https://reality.idnes.cz/detail/pronajem/byt/brno-bratislavska/6af5ba0b0e659b5db409eed4/"
DETAIL_ID = "6af5ba0b0e659b5db409eed4"
SIMILAR_ID = "64ece00c69e9e636500ce43c"  # otro piso de la misma calle que el mapa del detalle también dibuja
FIRST_ID = "6b0575cbe2d589687f04f544"
_MAP_SCRIPT = re.compile(r'(<script type="application/json" data-maptiler-json>)(.*?)(</script>)', re.S)


def _first(html, css_class, content):
    """Cambia el contenido del primer elemento con esa clase (en el listado, la tarjeta del primer anuncio)."""
    pattern = re.compile(rf'(class="{css_class}">)(.*?)(</(?:p|h1|h2)>)', re.S)
    assert pattern.search(html), f"el fixture ya no trae {css_class}"
    return pattern.sub(lambda match: f"{match[1]}{content}{match[3]}", html, count=1)


def _replaced(html, pattern, replacement):
    changed, count = re.subn(pattern, replacement, html)
    assert count == 1, f"el fixture ya no trae {pattern!r} exactamente una vez"
    return changed


def _with_map_script(text):
    assert _MAP_SCRIPT.search(DETAIL_HTML)
    return _MAP_SCRIPT.sub(lambda match: f"{match[1]}{text}{match[3]}", DETAIL_HTML, count=1)


def _with_geojson(mutate):
    """Detalle con el JSON del mapa modificado por `mutate`, que recibe el dict y lo cambia in situ."""
    data = json.loads(_MAP_SCRIPT.search(DETAIL_HTML)[2])
    mutate(data)
    return _with_map_script(json.dumps(data))


def _own_feature(data):
    return next(feature for feature in data["geojson"]["features"] if feature["properties"]["id"] == DETAIL_ID)


def _by_id(listings):
    return {listing.source_id: listing for listing in listings}


def _collector(handler):
    return Idnes(httpx.Client(transport=httpx.MockTransport(handler)))


def test_parse_list():
    listings = parse_list(LIST_HTML)
    assert len(listings) == 25
    first = listings[0]
    assert first.source_id == "6b0575cbe2d589687f04f544"
    assert first.title == "pronájem bytu 2+kk 50 m²"
    assert (first.kind, first.rent, first.disposition, first.area_m2) == (Kind.FLAT, 19900, "2+kk", 50)
    assert first.address_text == "Životského, Brno - Židenice"
    assert first.needs_detail is True and first.point is None


def test_parse_list_skips_advertisement_cards():
    # El listado trae 27 tarjetas: dos son huecos de publicidad sin enlace al detalle
    assert LIST_HTML.count('class="c-products__item"') == 25
    assert LIST_HTML.count("c-products__item-advertisment") == 2
    listings = parse_list(LIST_HTML)
    assert len({listing.source_id for listing in listings}) == 25
    assert all(listing.url.startswith(f"{BASE}/detail/") and listing.url.endswith(f"/{listing.source_id}/")
               for listing in listings)


def test_list_urls_are_plain_routes():
    # robots.txt prohíbe combinar filtro de precio con orden: solo rutas simples, sin parámetros
    assert LIST_URLS == (f"{BASE}/s/pronajem/byty/brno/", f"{BASE}/s/pronajem/byty/pokoj/brno/")
    assert all("?" not in url for url in LIST_URLS)


def test_parse_detail_from_url():
    listing = parse_detail(DETAIL_HTML, url=DETAIL_URL)
    assert listing.source_id == "6af5ba0b0e659b5db409eed4"
    assert listing.title == "Pronájem bytu 1+kk 35 m²"
    assert (listing.rent, listing.area_m2, listing.disposition) == (16400, 35, "1+kk")
    assert listing.address_text == "Bratislavská, Brno - Zábrdovice"
    assert listing.available_from == date(2027, 1, 1)
    assert (listing.lat, listing.lon) == (49.197984, 16.616712)
    assert listing.location_precision == "exact"
    assert "zálohy na služby" in listing.description
    assert len(listing.description) > 1000
    assert listing.needs_detail is False


def test_parse_detail_completes_a_list_listing():
    original = _by_id(parse_list(LIST_HTML))[DETAIL_ID]
    assert (original.needs_detail, original.point, original.available_from) == (True, None, None)
    listing = parse_detail(DETAIL_HTML, original)
    assert listing is not original
    # Lo que ya traía el listado se queda
    assert (listing.source_id, listing.url, listing.title) == (DETAIL_ID, original.url, original.title)
    assert listing.rent == 16400
    assert (listing.disposition, listing.area_m2) == ("1+kk", 35)
    assert listing.address_text == "Bratislavská, Brno - Zábrdovice"
    # Y el detalle aporta texto, fecha y coordenadas
    assert listing.available_from == date(2027, 1, 1)
    assert (listing.lat, listing.lon, listing.location_precision) == (49.197984, 16.616712, "exact")
    assert "zálohy na služby" in listing.description
    assert listing.needs_detail is False


def test_price_notes_and_parameters_reach_the_description():
    description = parse_detail(DETAIL_HTML, url=DETAIL_URL).description
    assert description.startswith("Pronájem novostavby bytu 1+kk")
    assert "\nPoznámka k ceně: + pakrovací stání + zálohy na služby" in description
    assert description.endswith(
        "\nParametry inzerátu: Konstrukce budovy: cihlová | Stav bytu: novostavba | Vlastnictví: osobní"
        " | Podlaží: 2. patro (3. NP) | Počet podlaží budovy: 4 podlaží | Terasa: 11 m2 | Výtah"
        " | Vybavení: zařízený | PENB: B (vyhl. č. 78/2013 Sb.) | Bezbariérový přístup"
    )
    # Los datos que ya se leen aparte, la publicidad y el id interno no se repiten en el texto
    assert "Číslo zakázky" not in description and "Spočítej" not in description
    assert "Datum nastěhování" not in description and "Užitná plocha" not in description


def test_parameters_label_adds_no_false_facts():
    # El encabezado que une texto y parámetros no puede disparar reglas por sí mismo ("Vybavení" daría furnished=True).
    # Solo, porque en el fixture el texto libre ya activa furnished, commission y washing_machine y taparía el fallo
    assert parse_text(_PARAMS_LABEL) == TextFacts()
    listing = parse_detail(DETAIL_HTML, url=DETAIL_URL)
    base = listing.description.split(f"\n{_PARAMS_LABEL} ")[0]
    assert base != listing.description
    assert parse_text(f"{listing.title}\n{listing.description}") == parse_text(f"{listing.title}\n{base}")


def test_structured_parameters_reach_the_text_rules():
    # Sin lavadora en el texto libre: solo el parámetro "Pračka" de la ficha la declara
    html = _replaced(DETAIL_HTML, "a pračku\\.", "a sušičku.")
    plain = parse_detail(html, url=DETAIL_URL)
    assert parse_text(f"{plain.title}\n{plain.description}").washing_machine is None
    with_flag = _replaced(html, r"(vybaveni=vytah\">\s*)Výtah", r"\1Pračka")
    listing = parse_detail(with_flag, url=DETAIL_URL)
    assert parse_text(f"{listing.title}\n{listing.description}").washing_machine is True


def test_parameter_with_a_cross_icon_is_not_an_amenity():
    # Un dd vacío con un icono distinto del "sí" ("Pračka" marcada como ausente) no puede declarar la lavadora
    html = _replaced(DETAIL_HTML, "a pračku\\.", "a sušičku.")
    html = _replaced(
        html,
        r'(vybaveni=vytah\">\s*)Výtah(\s*</a>\s*</dt>\s*<dd><span class=\"icon )icon--check',
        r"\1Pračka\2icon--cross",
    )
    listing = parse_detail(html, url=DETAIL_URL)
    assert "Pračka" not in listing.description
    assert parse_text(f"{listing.title}\n{listing.description}").washing_machine is None
    # Los que sí llevan el icono de "sí" se siguen emitiendo, sin valor
    assert listing.description.endswith(" | Vybavení: zařízený | PENB: B (vyhl. č. 78/2013 Sb.) | Bezbariérový přístup")
    assert " | Terasa: 11 m2 | Vybavení: zařízený" in listing.description


def test_unfurnished_parameter_beats_the_loose_text():
    # El texto libre dice "Koupelna je vybavená...": solo el parámetro dice que el piso no está amueblado
    listing = parse_detail(DETAIL_HTML, url=DETAIL_URL)
    assert parse_text(f"{listing.title}\n{listing.description}").furnished is True
    html = _replaced(DETAIL_HTML, r"(vybaveni=zarizeny\">\s*)zařízený", r"\1nezařízený")
    listing = parse_detail(html, url=DETAIL_URL)
    assert parse_text(f"{listing.title}\n{listing.description}").furnished is False


def test_gps_is_the_one_of_the_listing_not_of_the_similar_ones():
    data = json.loads(_MAP_SCRIPT.search(DETAIL_HTML)[2])
    similar = next(f for f in data["geojson"]["features"] if f["properties"]["id"] == SIMILAR_ID)
    lon, lat = similar["geometry"]["coordinates"]
    assert (lat, lon) != (49.197984, 16.616712)
    other = dataclasses.replace(_by_id(parse_list(LIST_HTML))[DETAIL_ID], source_id=SIMILAR_ID)
    listing = parse_detail(DETAIL_HTML, other)
    assert (listing.lat, listing.lon, listing.location_precision) == (lat, lon, "exact")


def test_broken_similar_features_do_not_hide_the_own_gps():
    def break_others(data):
        features = data["geojson"]["features"]
        own = _own_feature(data)
        others = [feature for feature in features if feature is not own]
        # La propia va la última y delante hay features sin properties, con properties nulas y que no son un objeto
        data["geojson"]["features"] = [{"type": "Feature"}, {"properties": None}, "feature", None, 7, *others, own]

    listing = parse_detail(_with_geojson(break_others), url=DETAIL_URL)
    assert (listing.lat, listing.lon, listing.location_precision) == (49.197984, 16.616712, "exact")


def test_listing_not_on_the_map_keeps_its_location():
    # Sin su feature en el mapa (o sin mapa) el anuncio conserva la ubicación aproximada que ya traía
    original = dataclasses.replace(_by_id(parse_list(LIST_HTML))[DETAIL_ID], source_id="6b0000000000000000000000")
    for html in (DETAIL_HTML, DETAIL_HTML.replace("data-maptiler-json", "data-other-json")):
        listing = parse_detail(html, original)
        assert (listing.lat, listing.lon, listing.location_precision) == (None, None, "street")
        assert listing.needs_detail is False


@pytest.mark.parametrize("mutate", [
    lambda data: _own_feature(data)["geometry"].update(coordinates=["16.616712", "49.197984"]),
    lambda data: _own_feature(data)["geometry"].update(coordinates=[16.616712]),
    lambda data: _own_feature(data)["geometry"].update(coordinates=None),
    lambda data: _own_feature(data)["geometry"].update(coordinates=[16.616712, 149.197984]),
    lambda data: _own_feature(data)["geometry"].update(coordinates=[True, False]),
    lambda data: _own_feature(data)["geometry"].update(coordinates=[float("nan"), 49.197984]),
    lambda data: _own_feature(data).pop("geometry"),
    lambda data: data.pop("geojson"),
    lambda data: data["geojson"].update(features=None),
])
def test_unusable_gps_is_ignored(mutate):
    listing = parse_detail(_with_geojson(mutate), url=DETAIL_URL)
    # El detalle no se rompe: sin coordenadas se queda la dirección, que el pipeline geocodifica
    assert (listing.lat, listing.lon) == (None, None)
    assert (listing.address_text, listing.location_precision) == ("Bratislavská, Brno - Zábrdovice", "street")
    assert (listing.rent, listing.available_from) == (16400, date(2027, 1, 1))


def test_map_script_that_is_not_json_is_ignored():
    listing = parse_detail(_with_map_script("{no es json"), url=DETAIL_URL)
    assert (listing.lat, listing.lon, listing.rent) == (None, None, 16400)


def test_detail_keeps_list_data_the_page_lacks():
    original = dataclasses.replace(
        _by_id(parse_list(LIST_HTML))[DETAIL_ID], lat=49.1, lon=16.6, available_from=date(2026, 12, 1),
    )
    bare = _replaced(DETAIL_HTML, "<dt>Datum nastěhování</dt>", "<dt>Jiný údaj</dt>")
    bare = bare.replace("data-maptiler-json", "data-other-json")
    listing = parse_detail(bare, original)
    # Un dato que el detalle no trae no borra el del listado
    assert (listing.lat, listing.lon, listing.available_from) == (49.1, 16.6, date(2026, 12, 1))
    assert (listing.location_precision, listing.rent, listing.needs_detail) == ("street", 16400, False)


def test_detail_without_any_text_keeps_the_list_description():
    original = dataclasses.replace(_by_id(parse_list(LIST_HTML))[DETAIL_ID], description="Texto del listado")
    bare = DETAIL_HTML.replace('class="b-desc', 'class="b-other').replace('class="wrapper-price-notes', 'class="x')
    bare = bare.replace('class="b-definition-columns', 'class="y')
    assert parse_detail(bare, original).description == "Texto del listado"


@pytest.mark.parametrize("raw", ["31.02.2027", "00.00.0000", "ihned", ""])
def test_invalid_available_from_is_none(raw):
    html = _replaced(DETAIL_HTML, "<dd>01.01.2027</dd>", f"<dd>{raw}</dd>")
    listing = parse_detail(html, url=DETAIL_URL)
    assert listing.available_from is None
    assert listing.rent == 16400


def test_available_from_accepts_short_dates():
    html = _replaced(DETAIL_HTML, "<dd>01.01.2027</dd>", "<dd>5. 2. 2027</dd>")
    assert parse_detail(html, url=DETAIL_URL).available_from == date(2027, 2, 5)


def test_parse_detail_without_the_card_raises():
    broken = DETAIL_HTML.replace("b-detail__title", "b-detail__other")
    with pytest.raises(ValueError, match="iDNES: el detalle no trae la ficha"):
        parse_detail(broken, url=DETAIL_URL)
    with pytest.raises(ValueError, match="el detalle no trae la ficha"):
        parse_detail(broken, _by_id(parse_list(LIST_HTML))[DETAIL_ID])


def test_parse_detail_of_a_list_page_raises():
    # Un anuncio retirado redirige al listado: no es un detalle y no debe darse por bueno
    with pytest.raises(ValueError, match="el detalle no trae la ficha"):
        parse_detail(LIST_HTML, _by_id(parse_list(LIST_HTML))[DETAIL_ID])


@pytest.mark.parametrize("url", [
    "",
    "https://reality.idnes.cz/s/pronajem/byty/brno/",
    "https://reality.idnes.cz/detail/x/12345/",
])
def test_parse_detail_without_listing_id_in_url_raises(url):
    with pytest.raises(ValueError, match="la URL no lleva el id"):
        parse_detail(DETAIL_HTML, url=url)


@pytest.mark.parametrize("price,rent", [
    ("<strong>19 900 Kč/měsíc</strong>", 19900),
    ("<strong>19\u00a0900 Kč / měsíc</strong>", 19900),
    ("<strong>19 900 Kč/měs.</strong>", 19900),
    # Otra unidad o periodo no es un alquiler mensual: mejor sin precio que uno falso
    ("<strong>650 Kč/den</strong>", None),
    ("<strong>239 000 Kč/rok</strong>", None),
    ("<strong>180 Kč/m²/měsíc</strong>", None),
    ("<strong>19 900 Kč</strong>", None),
    ("<strong>Info o ceně u RK</strong>", None),
    ("<strong>19 900,50 Kč/měsíc</strong>", None),
    ("<strong>0 Kč/měsíc</strong>", None),
    ("<strong>800 €/měsíc</strong>", None),
    ("", None),
])
def test_rent_needs_a_monthly_czk_amount(price, rent):
    first = parse_list(_first(LIST_HTML, "c-products__price", price))[0]
    assert (first.source_id, first.rent) == (FIRST_ID, rent)
    # Un precio que no se entiende no tira el anuncio: el resto de datos sigue ahí
    assert (first.disposition, first.address_text) == ("2+kk", "Životského, Brno - Židenice")


def test_detail_rent_follows_the_same_rule():
    assert parse_detail(DETAIL_HTML, url=DETAIL_URL).rent == 16400
    daily = _first(DETAIL_HTML, "b-detail__price", "<strong>16\u200d\u00a0400\u200d\u00a0Kč/den</strong>")
    assert parse_detail(daily, url=DETAIL_URL).rent is None
    # La cifra va con espacios de ancho cero entre los dígitos y la moneda
    zero_width = _first(DETAIL_HTML, "b-detail__price", "<strong>16\u200d\u00a0400\u200d\u00a0Kč\u200d/měsíc</strong>")
    assert parse_detail(zero_width, url=DETAIL_URL).rent == 16400


@pytest.mark.parametrize("info,address,precision", [
    # Con calle se geocodifica la calle
    ("Životského, Brno - Židenice", "Životského, Brno - Židenice", "street"),
    ("Žilkova, Brno", "Žilkova, Brno", "street"),
    ("třída Generála Píky, Brno - Černá Pole", "třída Generála Píky, Brno - Černá Pole", "street"),
    ("Kobližná, Brno - Brno-město", "Kobližná, Brno - Brno-město", "street"),
    # Solo barrio: su centroide queda a kilómetros de la casa, la ubicación es aproximada
    ("Brno - Obřany", "Brno - Obřany", "postcode"),
    ("Brno-Líšeň", "Brno-Líšeň", "postcode"),
    # Sin barrio ni calle no hay ubicación: no se geocodifica el centro de Brno, el perfil pregunta
    ("Brno", "", "none"),
    ("Brno - město", "", "none"),
    ("Brno - Brno-město", "", "none"),
    ("brno", "", "none"),
    ("", "", "none"),
    # Una forma que no se reconoce se trata como aproximada
    ("Kuřim", "Kuřim", "postcode"),
])
def test_precision_follows_the_street_and_quarter(info, address, precision):
    first = parse_list(_first(LIST_HTML, "c-products__info", info))[0]
    assert (first.address_text, first.location_precision) == (address, precision)
    assert (first.source_id, first.rent) == (FIRST_ID, 19900)
    assert first.needs_detail is True


@pytest.mark.parametrize("info,address,precision", [
    ("Bratislavská, Brno - Zábrdovice", "Bratislavská, Brno - Zábrdovice", "street"),
    ("Brno - Zábrdovice", "Brno - Zábrdovice", "postcode"),
    ("Brno", "", "none"),
])
def test_detail_location_without_gps(info, address, precision):
    html = _first(DETAIL_HTML, "b-detail__info", info).replace("data-maptiler-json", "data-other-json")
    listing = parse_detail(html, url=DETAIL_URL)
    assert (listing.address_text, listing.location_precision, listing.point) == (address, precision, None)


def test_gps_makes_the_location_exact_even_if_the_list_was_approximate():
    # iDNES solo pinta en el mapa inmuebles con dirección exacta
    original = dataclasses.replace(
        _by_id(parse_list(LIST_HTML))[DETAIL_ID], address_text="Brno - Zábrdovice", location_precision="postcode",
    )
    listing = parse_detail(DETAIL_HTML, original)
    assert (listing.location_precision, listing.address_text) == ("exact", "Brno - Zábrdovice")


def test_rooms_are_recognised_and_have_no_area():
    title = "pronájem pokoje 18\u00a0m²"
    room = parse_list(_first(LIST_HTML, "c-products__title", title))[0]
    # No se sabe si "18 m²" es el cuarto o el piso: sin dato antes que uno que despiste
    assert (room.kind, room.area_m2, room.rent, room.title) == (Kind.ROOM, None, 19900, "pronájem pokoje 18 m²")
    flat = parse_list(LIST_HTML)[0]
    assert (flat.kind, flat.area_m2) == (Kind.FLAT, 50)


def test_detail_of_a_room_has_no_area():
    html = _first(DETAIL_HTML, "b-detail__title", "<span>Pronájem pokoje 18\u00a0m²</span>")
    listing = parse_detail(html, url=DETAIL_URL)
    assert (listing.kind, listing.area_m2, listing.rent) == (Kind.ROOM, None, 16400)
    # La "Užitná plocha" de la ficha (35 m²) tampoco se cuela
    assert parse_detail(html, dataclasses.replace(listing, needs_detail=True)).area_m2 is None


def test_detail_area_fills_a_list_listing_without_one():
    original = dataclasses.replace(_by_id(parse_list(LIST_HTML))[DETAIL_ID], area_m2=None)
    assert parse_detail(DETAIL_HTML, original).area_m2 == 35
    # Los valores de la ficha pueden traer espacios de ancho cero entre la cifra y la unidad
    zero_width = _replaced(DETAIL_HTML, "<dd>35 m<sup>2</sup></dd>", "<dd>35\u200d m<sup>2</sup></dd>")
    assert parse_detail(zero_width, original).area_m2 == 35


def test_parse_list_without_cards_raises():
    broken = LIST_HTML.replace("c-products__item", "c-products__other")
    with pytest.raises(ValueError, match=re.escape("iDNES: el listado no trae anuncios (¿cambió el formato?)")):
        parse_list(broken)


def test_parse_list_without_links_raises():
    # Si el portal renombra el enlace de las tarjetas todo parecería una página vacía
    renamed = LIST_HTML.replace('class="c-products__link"', 'class="c-products__other"')
    assert renamed != LIST_HTML
    for allow_empty in (False, True):
        with pytest.raises(ValueError, match="no trae anuncios"):
            parse_list(renamed, allow_empty=allow_empty)


def test_parse_list_without_ids_raises():
    broken = re.sub(r'href="(https://reality\.idnes\.cz/detail/[^"]*?)[0-9a-f]{24}/"', r'href="\1/"', LIST_HTML)
    assert broken != LIST_HTML
    for allow_empty in (False, True):
        with pytest.raises(ValueError, match="no trae anuncios"):
            parse_list(broken, allow_empty=allow_empty)


def test_empty_results_page_is_allowed_only_on_request():
    empty = LIST_HTML.replace("c-products__item", "c-products__other")
    assert 'id="snippet-s-result"' in empty
    assert parse_list(empty, allow_empty=True) == []
    with pytest.raises(ValueError, match="no trae anuncios"):
        parse_list(empty)


@pytest.mark.parametrize("html", [
    "<html><body>captcha</body></html>",
    "",
    LIST_HTML.replace("c-products__item", "c-products__other").replace('id="snippet-s-result"', 'id="snippet-s-other"'),
])
def test_page_without_the_results_structure_always_raises(html):
    with pytest.raises(ValueError, match="iDNES"):
        parse_list(html, allow_empty=True)


def test_fetch_list_reads_flats_and_rooms():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=LIST_HTML)

    listings = Idnes(httpx.Client(transport=httpx.MockTransport(handler))).fetch_list()
    assert seen == list(LIST_URLS)
    assert len(listings) == 25  # mismos ids en las dos páginas del mock: se deduplican


def test_fetch_list_keeps_the_first_copy_of_a_repeated_listing():
    rooms_page = _first(LIST_HTML, "c-products__title", "pronájem pokoje 18\u00a0m²")

    def handler(request):
        return httpx.Response(200, text=LIST_HTML if str(request.url) == LIST_URLS[0] else rooms_page)

    listings = _collector(handler).fetch_list()
    assert len(listings) == 25
    assert listings[0].kind is Kind.FLAT


def test_fetch_list_follows_a_redirect():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if str(request.url) == LIST_URLS[0]:
            return httpx.Response(301, headers={"Location": f"{BASE}/s/pronajem/byty/brno-mesto/"})
        return httpx.Response(200, text=LIST_HTML)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert len(Idnes(client).fetch_list()) == 25
    assert requests == [LIST_URLS[0], f"{BASE}/s/pronajem/byty/brno-mesto/", LIST_URLS[1]]


@pytest.mark.parametrize("status", [404, 500])
@pytest.mark.parametrize("failing", [0, 1])
def test_fetch_list_raises_on_http_error(status, failing):
    def handler(request):
        if str(request.url) == LIST_URLS[failing]:
            return httpx.Response(status, text="error")
        return httpx.Response(200, text=LIST_HTML)

    with pytest.raises(httpx.HTTPStatusError):
        _collector(handler).fetch_list()


def test_empty_rooms_does_not_fail_the_portal():
    empty = LIST_HTML.replace("c-products__item", "c-products__other")

    def handler(request):
        return httpx.Response(200, text=LIST_HTML if str(request.url) == LIST_URLS[0] else empty)

    listings = _collector(handler).fetch_list()
    assert len(listings) == 25
    assert {listing.kind for listing in listings} == {Kind.FLAT}


def test_empty_flats_still_fails_the_portal():
    empty = LIST_HTML.replace("c-products__item", "c-products__other")

    def handler(request):
        return httpx.Response(200, text=empty if str(request.url) == LIST_URLS[0] else LIST_HTML)

    with pytest.raises(ValueError, match="iDNES: el listado no trae anuncios"):
        _collector(handler).fetch_list()


def test_rooms_page_that_loses_the_structure_fails_the_portal():
    def handler(request):
        return httpx.Response(200, text=LIST_HTML if str(request.url) == LIST_URLS[0] else "<html>captcha</html>")

    with pytest.raises(ValueError, match="iDNES"):
        _collector(handler).fetch_list()


def test_fetch_detail_requests_the_listing_page():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=DETAIL_HTML)

    original = _by_id(parse_list(LIST_HTML))[DETAIL_ID]
    listing = _collector(handler).fetch_detail(original)
    assert requests == [original.url]
    assert (listing.source_id, listing.needs_detail, listing.available_from) == (DETAIL_ID, False, date(2027, 1, 1))
    assert (listing.lat, listing.lon) == (49.197984, 16.616712)


@pytest.mark.parametrize("status", [404, 500])
def test_fetch_detail_raises_on_http_error(status):
    original = _by_id(parse_list(LIST_HTML))[DETAIL_ID]
    with pytest.raises(httpx.HTTPStatusError):
        _collector(lambda request: httpx.Response(status, text="error")).fetch_detail(original)
