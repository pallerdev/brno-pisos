import dataclasses
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from pisobot.collectors.permanentia import _PARAMS_LABEL, BASE, LIST_URL, Permanentia, parse_detail, parse_list
from pisobot.models import Kind
from pisobot.parse_text import TextFacts, parse_text

FIXTURES = Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "permanentia_list.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "permanentia_detail.html").read_text(encoding="utf-8")
DETAIL_URL = f"{BASE}/reality/pronajem-bytu-1-kk-v-brne-na-ulici-bayerova-453121/"
DETAIL_ID = "453121"
FIRST_ID = "453484"
_CARD = r"<article.*?</article>"


def _replaced(html, pattern, replacement, count=1):
    changed, found = re.subn(pattern, replacement, html, flags=re.S)
    assert found == count, f"el fixture trae {pattern!r} {found} veces, no {count}"
    return changed


def _first(html, pattern, replacement):
    """Cambia solo la primera coincidencia (en el listado, la tarjeta del primer anuncio)."""
    changed, found = re.subn(pattern, replacement, html, count=1, flags=re.S)
    assert found == 1, f"el fixture ya no trae {pattern!r}"
    return changed


def _first_price(content):
    return _first(
        LIST_HTML, r'(<span class="cena">)(.*?)(</span>\s*<div class="clearing">)',
        lambda match: f"{match[1]}{content}{match[3]}",
    )


def _first_title(title):
    return _first(
        LIST_HTML, r'(<h2 class="headlines-link js-sameHeight">\s*<a href="[^"]*">)(.*?)(</a>)',
        lambda match: f"{match[1]}{title}{match[3]}",
    )


def _first_place(place):
    return _first(
        LIST_HTML, r'(<div class="parametry">.*?</svg>\s*)([^<]*?)(\s*</span>)',
        lambda match: f"{match[1]}{place}{match[3]}",
    )


def _with_total(html, total):
    """Página con el recuento del JSON de vista previa cambiado (a un valor JSON cualquiera)."""
    return _replaced(html, r'"totalCount":4,', f'"totalCount":{total},')


# Una página de resultados que de verdad está vacía: sin tarjetas y con el recuento a cero
EMPTY_HTML = _with_total(re.sub(_CARD, "", LIST_HTML, flags=re.S), 0)
# Con las tarjetas y los enlaces renombrados no queda rastro de los anuncios en el marcado, pero el recuento sigue
UNREADABLE_HTML = LIST_HTML.replace("estateListItem", "estateOtro").replace('href="/reality/', 'href="/inmueble/')


def _by_id(listings):
    return {listing.source_id: listing for listing in listings}


def _listed(source_id=DETAIL_ID):
    return _by_id(parse_list(LIST_HTML))[source_id]


def _with_text(text, note=""):
    """Detalle con la descripción y la nota de la ficha cambiadas (la nota del fixture también habla de importes)."""
    html = _replaced(
        DETAIL_HTML, r'(<p class="detailPopis" id="JS-popis">)(.*?)(</p>)', lambda match: f"{match[1]}{text}{match[3]}",
    )
    return _replaced(
        html, r'(Poznámka:\s*</span>\s*<span class="parameter-value">)(.*?)(</span>)',
        lambda match: f"{match[1]}{note}{match[3]}",
    )


def _with_param(label, value, html=DETAIL_HTML):
    return _replaced(html, rf"(<span>{label}</span>\s*<span>)(.*?)(</span>)", lambda match: f"{match[1]}{value}{match[3]}")


def _relabel(old, new, html=DETAIL_HTML):
    return _replaced(html, rf"<span>{old}</span>", f"<span>{new}</span>")


_BLOCKS = {
    "description": 'class="detailPopis"',
    "params": 'class="estateParams"',
    "note": 'class="estate-parameters"',
    "address": 'class="estateDetail-adresa"',
    "price": 'class="estateDetail-cena"',
    "map": 'class="estateDetail-map"',
}


def _without_blocks(*names):
    html = DETAIL_HTML
    for name in names:
        assert _BLOCKS[name] in html
        html = html.replace(_BLOCKS[name], 'class="x"')
    return html


def _with_title(title):
    return _replaced(DETAIL_HTML, r"(<h1>)(.*?)(</h1>)", lambda match: f"{match[1]}{title}{match[3]}")


def _with_price(content, html=DETAIL_HTML):
    return _replaced(
        html, r'(<div class="estateDetail-cena">)(.*?)(</div>)', lambda match: f"{match[1]}{content}{match[3]}",
    )


def _with_address(text):
    return _replaced(
        DETAIL_HTML, r'(<div class="estateDetail-adresa">\s*)(.*?)(\s*<a class="js-scroll remaxLink")',
        lambda match: f"{match[1]}{text}{match[3]}",
    )


def _with_map_link(href):
    return _replaced(DETAIL_HTML, r'(<a class="navigovat"[^>]*href=")[^"]*(")', lambda match: f"{match[1]}{href}{match[2]}")


def _without_map(html=DETAIL_HTML):
    return html.replace('class="navigovat"', 'class="other"')


def _facts(listing):
    return parse_text(f"{listing.title}\n{listing.description}")


def _collector(handler):
    return Permanentia(httpx.Client(transport=httpx.MockTransport(handler)))


def test_parse_list():
    listings = parse_list(LIST_HTML)
    assert [listing.source_id for listing in listings] == ["453484", "453121", "450359", "449707"]
    first = listings[0]
    assert first.source == "permanentia" and first.key == "permanentia:453484"
    assert first.url == f"{BASE}/reality/byt-2-kk-v-novostavbe-s-moznosti-parkovaciho-stani-v-brnenskych-cernych-polich-453484/"
    assert (first.kind, first.rent, first.disposition, first.area_m2) == (Kind.FLAT, 18600, "2+kk", None)
    assert first.needs_detail is True and first.point is None
    assert (first.address_text, first.location_precision) == ("", "none")


def test_parse_list_titles_prices_and_dispositions():
    table = {
        "453484": ("Byt 2+kk v novostavbě s možností parkovacího stání v brněnských Černých Polích", 18600, "2+kk"),
        "453121": ("Pronájem bytu 1+kk v Brně na ulici Bayerova", 9500, "1+kk"),
        "450359": ("Byt 1+1 v 1.NP na ulici Střední, Brno - Ponava", 12000, "1+1"),
        "449707": ("Byt 1+, Brno Bezručova", 15500, None),
    }
    listings = _by_id(parse_list(LIST_HTML))
    assert {key: (item.title, item.rent, item.disposition) for key, item in listings.items()} == table
    assert {item.kind for item in listings.values()} == {Kind.FLAT}


def test_list_url_is_only_the_brno_filter():
    url = httpx.URL(LIST_URL)
    assert (url.host, url.path) == ("www.permanentia.cz", "/pronajem/byty/jihomoravsky/")
    # robots.txt veta ordenar y filtrar por precio o superficie: solo vale el filtro de ciudad
    assert dict(url.params) == {"idMesta[0]": "582786"}


def test_truncated_list_title_is_completed_from_the_photo_caption():
    assert LIST_HTML.count("parkovacího st...</a>") == 1
    assert _listed(FIRST_ID).title.endswith("parkovacího stání v brněnských Černých Polích")
    # Sin un pie de foto que lo confirme se queda el título tal cual
    other = LIST_HTML.replace("Fotografie nemovitosti - Byt 2+kk v novostavbě", "Fotografie nemovitosti - Otro byt")
    assert _by_id(parse_list(other))[FIRST_ID].title == "Byt 2+kk v novostavbě s možností parkovacího st..."
    renamed = LIST_HTML.replace('alt="Fotografie nemovitosti - Byt 2+kk', 'title="Byt 2+kk')
    assert _by_id(parse_list(renamed))[FIRST_ID].title == "Byt 2+kk v novostavbě s možností parkovacího st..."


@pytest.mark.parametrize("price,rent", [
    ("18&nbsp;600 <span class=\"mena\"><span class=\"kc\">Kč</span> / měsíc</span>", 18600),
    ("18 600 Kč/měsíc", 18600),
    ("18&nbsp;600 Kč / měsíc s DPH", 18600),
    # Otra unidad o periodo no es un alquiler mensual: mejor sin precio que uno falso
    ("650 Kč / den", None),
    ("239&nbsp;000 Kč / rok", None),
    ("180 Kč / m² / měsíc", None),
    ("18&nbsp;600 Kč", None),
    ("Cena na dotaz", None),
    ("18&nbsp;600,50 Kč / měsíc", None),
    ("0 Kč / měsíc", None),
    ("800 € / měsíc", None),
    ("", None),
    ("18&nbsp;600 Kč/měs.", 18600),
    # Otra unidad detrás del mes: el importe es por m², por persona, por día o por año
    ("180 Kč / měsíc / m²", None),
    ("180 Kč / měsíc / m2", None),
    ("1&nbsp;500 Kč / měsíc / osobu", None),
    ("650 Kč / měsíc / den", None),
    ("239&nbsp;000 Kč / měsíc / rok", None),
    ("180 Kč/měs./m²", None),
    ("1&nbsp;500 Kč / měsíc na osobu", None),
    ("180 Kč / měsíc za m²", None),
])
def test_list_rent_needs_a_monthly_czk_amount(price, rent):
    first = parse_list(_first_price(price))[0]
    assert (first.source_id, first.rent) == (FIRST_ID, rent)
    # Un precio que no se entiende no tira el anuncio: el resto de datos sigue ahí
    assert (first.disposition, first.needs_detail) == ("2+kk", True)


@pytest.mark.parametrize("title,kind,area", [
    ("Pronájem pokoje v bytě 3+1", Kind.ROOM, None),
    ("Pokoj 15 m2 v bytě 3+1", Kind.ROOM, None),
    ("Hledám spolubydlícího, 12 m2", Kind.ROOM, None),
    ("Pronájem dvoupokojového bytu 50 m2", Kind.FLAT, 50),
    ("Pronájem 2-pokojového bytu 50 m2", Kind.FLAT, 50),
    ("Pronájem bytu o 3 pokojích, 70 m2", Kind.FLAT, 70),
    ("Pronájem bytu 2+kk, 43 m²", Kind.FLAT, 43),
    ("Pronájem bytu 2+kk", Kind.FLAT, None),
])
def test_list_kind_from_the_title_and_room_without_area(title, kind, area):
    listing = parse_list(_first_title(title))[0]
    assert (listing.source_id, listing.kind, listing.area_m2) == (FIRST_ID, kind, area)


@pytest.mark.parametrize("place,address,precision", [
    # El listado solo da el distrito: "Brno-město" es la ciudad entera y no sitúa el anuncio
    ("Brno-město", "", "none"),
    ("Brno - město", "", "none"),
    ("Brno", "", "none"),
    ("", "", "none"),
    # Un barrio sí, pero su centroide queda a kilómetros de la casa
    ("Brno-Líšeň", "Brno-Líšeň", "postcode"),
    ("Brno - Židenice", "Brno - Židenice", "postcode"),
    ("Žilkova, Brno", "Žilkova, Brno", "street"),
])
def test_list_location_follows_the_district(place, address, precision):
    first = parse_list(_first_place(place))[0]
    assert (first.address_text, first.location_precision) == (address, precision)
    assert (first.source_id, first.rent) == (FIRST_ID, 18600)


def test_parse_list_skips_cards_without_a_listing_link():
    broken = _first(
        LIST_HTML, r'(<h2 class="headlines-link js-sameHeight">\s*<a href=")[^"]*(")',
        lambda match: f"{match[1]}/o-nas/{match[2]}",
    )
    listings = parse_list(broken)
    assert [listing.source_id for listing in listings] == ["453121", "450359", "449707"]


def test_empty_results_page_is_allowed_by_default():
    assert "estateListItem" not in EMPTY_HTML and 'class="estateList js-estateList"' in EMPTY_HTML
    assert '"totalCount":0,' in EMPTY_HTML
    # La agencia puede no tener ningún piso en Brno: no es un fallo
    assert parse_list(EMPTY_HTML) == []
    assert parse_list(EMPTY_HTML, allow_empty=True) == []
    with pytest.raises(ValueError, match=re.escape("Permanentia: el listado no trae anuncios (¿cambió el formato?)")):
        parse_list(EMPTY_HTML, allow_empty=False)


@pytest.mark.parametrize("html", [
    "<html><body>captcha</body></html>",
    "",
    re.sub(_CARD, "", LIST_HTML, flags=re.S).replace('class="estateList js-estateList"', 'class="otro js-otro"'),
])
def test_page_without_the_results_structure_always_raises(html):
    for allow_empty in (True, False):
        with pytest.raises(ValueError, match="Permanentia: el listado no trae anuncios"):
            parse_list(html, allow_empty=allow_empty)


@pytest.mark.parametrize("broken", [
    # Con las tarjetas renombradas, los enlaces a las fichas siguen ahí: no es una página vacía
    LIST_HTML.replace("estateListItem", "estateOtro"),
    # Con los enlaces renombrados o sin id, las tarjetas siguen ahí
    LIST_HTML.replace('href="/reality/', 'href="/inmueble/'),
    re.sub(r'(href="/reality/[^"]*?)-\d+/"', r'\1/"', LIST_HTML),
    LIST_HTML.replace("<h2 ", "<h3 ").replace("</h2>", "</h3>"),
])
def test_page_that_loses_its_cards_or_links_raises_even_if_empty_is_allowed(broken):
    assert broken != LIST_HTML
    for allow_empty in (True, False):
        with pytest.raises(ValueError, match="Permanentia: el listado no trae anuncios"):
            parse_list(broken, allow_empty=allow_empty)


def test_count_in_the_page_json_matches_the_fixture_cards():
    assert '"totalCount":4,' in LIST_HTML and len(parse_list(LIST_HTML)) == 4


@pytest.mark.parametrize("total", [1, 4, 38])
def test_unreadable_cards_with_a_positive_count_raise(total):
    # El JSON de la página cuenta los anuncios aunque el marcado de las tarjetas cambie: no es una lista vacía
    page = _with_total(UNREADABLE_HTML, total)
    for allow_empty in (True, False):
        with pytest.raises(ValueError, match="Permanentia: el listado no trae anuncios"):
            parse_list(page, allow_empty=allow_empty)


def test_empty_page_with_a_positive_count_raises():
    page = _with_total(re.sub(_CARD, "", LIST_HTML, flags=re.S), 4)
    with pytest.raises(ValueError, match="Permanentia: el listado no trae anuncios"):
        parse_list(page)


def test_unreadable_cards_with_a_zero_count_are_an_empty_list():
    assert parse_list(_with_total(UNREADABLE_HTML, 0)) == []


@pytest.mark.parametrize("html", [
    # Sin el JSON, o con uno que no trae el recuento, manda la estructura vacía de la página
    UNREADABLE_HTML.replace('id="data-default-preview-data"', 'id="data-other-data"'),
    UNREADABLE_HTML.replace('"totalCount":4,', ""),
    _with_total(UNREADABLE_HTML, '"4"'),
    _with_total(UNREADABLE_HTML, "null"),
    _with_total(UNREADABLE_HTML, "true"),
    _with_total(UNREADABLE_HTML, "-1"),
    _with_total(UNREADABLE_HTML, "4.5"),
    _replaced(UNREADABLE_HTML, r'(<script id="data-default-preview-data"[^>]*>).*?(</script>)', r"\1{no es json\2"),
    _replaced(UNREADABLE_HTML, r'(<script id="data-default-preview-data"[^>]*>).*?(</script>)', r"\1[4]\2"),
])
def test_page_json_without_a_usable_count_falls_back_to_the_empty_structure(html):
    assert html != UNREADABLE_HTML
    assert parse_list(html) == []


def test_count_in_the_json_never_hides_listings_that_do_parse():
    assert len(parse_list(_with_total(LIST_HTML, 0))) == 4


def test_parse_detail_from_url():
    listing = parse_detail(DETAIL_HTML, url=DETAIL_URL)
    assert (listing.source, listing.source_id, listing.url) == ("permanentia", DETAIL_ID, DETAIL_URL)
    assert listing.title == "Pronájem bytu 1+kk v Brně na ulici Bayerova"
    assert (listing.kind, listing.rent, listing.fees, listing.deposit) == (Kind.FLAT, 9500, 2500, 19000)
    assert (listing.disposition, listing.area_m2, listing.commission) == ("1+kk", 18, True)
    assert listing.available_from == date(2026, 12, 1)
    assert (listing.lat, listing.lon, listing.location_precision) == (49.2078094, 16.6016636, "exact")
    assert listing.address_text == "Bayerova, Brno"
    assert "Velkou předností bytu je samotná lokalita" in listing.description
    assert listing.needs_detail is False


def test_parse_detail_completes_a_list_listing():
    original = _listed()
    assert (original.needs_detail, original.point, original.available_from) == (True, None, None)
    assert (original.address_text, original.location_precision) == ("", "none")
    listing = parse_detail(DETAIL_HTML, original)
    assert listing is not original
    # Lo que ya traía el listado se queda
    assert (listing.source_id, listing.url, listing.title, listing.rent) == (DETAIL_ID, original.url, original.title, 9500)
    # Y el detalle aporta dirección, coordenadas, fecha, importes y texto
    assert (listing.address_text, listing.location_precision) == ("Bayerova, Brno", "exact")
    assert (listing.lat, listing.lon) == (49.2078094, 16.6016636)
    assert (listing.fees, listing.deposit, listing.commission, listing.area_m2) == (2500, 19000, True, 18)
    assert listing.available_from == date(2026, 12, 1)
    assert len(listing.description) > 1000
    assert listing.needs_detail is False


def test_description_has_the_text_the_note_and_the_parameters():
    description = parse_detail(DETAIL_HTML, url=DETAIL_URL).description
    assert description.startswith("Hledáte příjemné bydlení v Brně")
    assert "\nPodmínky pronájmu: měsíční nájemné činí 9 500 Kč, zálohy na služby 2 500 Kč a vratná kauce 19 000 Kč.\n" in description
    assert (
        "\nPoznámka: Zálohy na služby ve výši 2.500 Kč / měsíc. Zprostředkovatelský poplatek a právní servis činí"
        " 14.520 Kč vč. DPH\n"
    ) in description
    assert description.endswith(
        "\nParametry inzerátu: Vlastnictví: Osobní | Stav bytu: Velmi dobrý | Voda: Vodovod | Orientace: Severovýchod"
        " | Odpad: Kanalizace | Druh objektu: Cihlová | Umístění objektu: Centrum obce | Nevybaveno"
        " | Plocha balkónu m2: 1 | Elektřina: 230 V | Ostatní: Výtah | Zástavba: Bydlení | Ústřední dálkové topení"
        " | Doprava: Vlak, dálnice, silnice, MHD, autobus | Komunikace: Asfaltová | Počet podlaží objektu: 7"
        " | Číslo podlaží v domě: 2 | Třída: G - Mimořádně nehospodárná | Vyhláška: 148/2007 Sb."
    )
    # Lo que ya se lee aparte y los campos vacíos del CRM no se repiten en el texto
    for skipped in ("Užitná plocha", "Celková podlahová plocha", "K dispozici od", "Datum nastěhování", "Počet parkovišť"):
        assert skipped not in description


def test_parameters_label_adds_no_false_facts():
    # El encabezado que une texto y parámetros no puede disparar reglas por sí mismo ("Vybavení" daría furnished=True)
    assert parse_text(_PARAMS_LABEL) == TextFacts()
    listing = parse_detail(DETAIL_HTML, url=DETAIL_URL)
    base = listing.description.split(f"\n{_PARAMS_LABEL} ")[0]
    assert base != listing.description
    # Antes de los parámetros solo habla el texto libre: la cocina "vybavena" da amueblado y nada más
    assert parse_text(f"{listing.title}\n{base}") == TextFacts(furnished=True)
    # Después gana "Vybaveno: Ne" y no aparece ningún otro dato
    assert _facts(listing) == TextFacts(furnished=False)


@pytest.mark.parametrize("value,furnished", [("Ne", False), ("Ano", True), ("Částečně", True), ("Dohodou", None)])
def test_furnished_parameter_reaches_the_text_rules(value, furnished):
    html = _with_param("Vybaveno", value, _with_text("Pěkný byt."))
    listing = parse_detail(html, url=DETAIL_URL)
    assert _facts(listing).furnished is furnished
    if furnished is None:
        # Un valor desconocido no se escribe: contendría "vybaven" y daría amueblado
        assert "ybaven" not in listing.description


@pytest.mark.parametrize("row,label,value", [
    ("Voda", "Vybavení", "Kuchyňská linka"),
    ("Voda", "Vybavenost", "Ano"),
    ("Voda", "Vybavení bytu", "Ne"),
    ("Voda", "Stav", "Furnished"),
    ("Ostatní", "Ostatní", "Vybavená kuchyň"),
    ("Ostatní", "Ostatní", "Zařízeno"),
])
def test_other_furnished_like_parameters_are_not_written(row, label, value):
    # Solo "Vybaveno" se reescribe; cualquier otro atributo con esas palabras daría amueblado sin serlo.
    # "Vybaveno: Dohodou" no se escribe, así que nada más habla del mobiliario
    html = _with_param(label, value, _relabel(row, label, _with_text("Pěkný byt.")))
    html = _with_param("Vybaveno", "Dohodou", html)
    listing = parse_detail(html, url=DETAIL_URL)
    assert f"{label}: {value}" not in listing.description
    assert _facts(listing).furnished is None
    # Los demás atributos siguen ahí
    assert "Orientace: Severovýchod" in listing.description


def test_furnished_label_is_still_written_next_to_the_skipped_ones():
    html = _with_param("Vybavení", "Kuchyňská linka", _relabel("Voda", "Vybavení", _with_text("Pěkný byt.")))
    listing = parse_detail(html, url=DETAIL_URL)
    assert "Nevybaveno" in listing.description and "Kuchyňská linka" not in listing.description
    assert _facts(listing).furnished is False


def test_unfurnished_parameter_beats_the_loose_text():
    # El texto libre dice "Kuchyňská linka je vybavena...": solo el parámetro dice que el piso no está amueblado
    html = _with_param("Vybaveno", "Ano")
    assert _facts(parse_detail(html, url=DETAIL_URL)).furnished is True
    assert _facts(parse_detail(DETAIL_HTML, url=DETAIL_URL)).furnished is False


@pytest.mark.parametrize("heating,electric", [
    ("Lokální elektrické", True),
    ("Ústřední elektrické", True),
    ("Elektrické", True),
    ("Ústřední plynové", False),
    ("Ústřední dálkové", False),
])
def test_heating_parameter_reaches_the_electric_rule(heating, electric):
    listing = parse_detail(_with_param("Topení", heating), url=DETAIL_URL)
    assert f"{heating} topení" in listing.description
    assert _facts(listing).electric_heating is electric


def test_other_parameter_reaches_the_washing_machine_rule():
    assert _facts(parse_detail(DETAIL_HTML, url=DETAIL_URL)).washing_machine is None
    listing = parse_detail(_with_param("Ostatní", "Výtah, Pračka"), url=DETAIL_URL)
    assert "Ostatní: Výtah, Pračka" in listing.description
    assert _facts(listing).washing_machine is True


@pytest.mark.parametrize("text,fees", [
    ("Podmínky: měsíční nájemné činí 9 500 Kč, zálohy na služby 2 500 Kč a vratná kauce 19 000 Kč.", 2500),
    ("Nájem 9 500 Kč + zálohy na služby ve výši 2.500 Kč / měsíc.", 2500),
    ("Zálohy na služby: 2&nbsp;500 Kč", 2500),
    ("Nájem 9 500 Kč + záloha na služby 2 500 Kč.", 2500),
    ("Zálohy na služby činí cca 2 500 Kč měsíčně.", 2500),
    ("Poplatek za služby 2500,- Kč.", 2500),
    ("Nájem 9 500 Kč, zálohy na služby 2.500,- Kč.", 2500),
    ("Nájem 9 500 Kč, zálohy na služby 2 500 CZK.", 2500),
    # "záloha" sin más suele ser la fianza: solo cuenta como gastos si es un anticipo de servicios
    ("Nájem 9 500 Kč + záloha 19 000 Kč.", None),
    ("Nájem 9 500 Kč, záloha na kauci 19 000 Kč.", None),
    ("Vratná kauce 19 000 Kč, záloha 5 000 Kč.", None),
    ("Nájem 9 500 Kč včetně záloh na služby.", None),
    ("Nájem 9 500 Kč, zálohy na služby jsou v ceně, kauce 19 000 Kč.", None),
    ("Nájem 9 500 Kč, zálohy na služby dle spotřeby.", None),
    ("Nájem 9 500 Kč, zálohy na služby 2 500,50 Kč.", None),
    ("Nájem 9 500 Kč, zálohy na služby 0 Kč.", None),
    ("Pěkný byt.", None),
    # "Provize za služby" es la comisión con otro nombre, no unos gastos
    ("Provize za služby činí 14 520 Kč.", None),
    ("Zprostředkovatelský poplatek za služby 14 520 Kč.", None),
    ("Provize za služby činí 14 520 Kč. Zálohy na služby 2 500 Kč.", 2500),
    ("Provize 14 520 Kč, zálohy na služby 2 500 Kč.", 2500),
    ("Provize činí 14 520 Kč. Nájem 9 500 Kč + zálohy na služby 2.500 Kč.", 2500),
])
def test_fees_are_read_only_from_the_services_advance(text, fees):
    assert parse_detail(_with_text(text), url=DETAIL_URL).fees == fees


def test_fees_can_come_from_the_note():
    html = _with_text("Pěkný byt.", note="Zálohy na služby ve výši 3.000 Kč / měsíc.")
    assert parse_detail(html, url=DETAIL_URL).fees == 3000


@pytest.mark.parametrize("text,deposit", [
    ("Vratná kauce 19 000 Kč.", 19000),
    ("Kauce: 19.000 Kč", 19000),
    ("Kauce činí 19&nbsp;000 Kč.", 19000),
    ("Kauce ve výši 38 000,- Kč.", 38000),
    ("Kauce 2 nájmy.", None),
    ("Bez kauce.", None),
    ("Kauce 0 Kč.", None),
    ("Kauce 19 000,50 Kč.", None),
    # La fianza no son los gastos, ni al revés; un "záloha" a secas es ambiguo y no se lee como fianza
    ("Zálohy na služby 2 500 Kč.", None),
    ("Nájem 9 500 Kč + záloha 19 000 Kč.", None),
    ("Pěkný byt.", None),
])
def test_deposit_is_read_only_from_the_kauce(text, deposit):
    listing = parse_detail(_with_text(text), url=DETAIL_URL)
    assert listing.deposit == deposit


def test_fees_and_deposit_are_not_mixed_up():
    listing = parse_detail(_with_text("Kauce 19 000 Kč, zálohy na služby 2 500 Kč."), url=DETAIL_URL)
    assert (listing.fees, listing.deposit) == (2500, 19000)
    listing = parse_detail(_with_text("Zálohy na služby 2 500 Kč, kauce 19 000 Kč."), url=DETAIL_URL)
    assert (listing.fees, listing.deposit) == (2500, 19000)


@pytest.mark.parametrize("text,note,commission", [
    ("Pěkný byt.", "Zprostředkovatelský poplatek činí 14.520 Kč vč. DPH", True),
    ("Pěkný byt.", "Zprostředkovatelský poplatek a právní servis činí 14 520 Kč", True),
    ("Provize: 14 520 Kč.", "", True),
    ("Provize RK činí 14&nbsp;520 Kč.", "", True),
    ("Pěkný byt.", "Provize 0 Kč", None),
    ("Pěkný byt.", "Zprostředkovatelský poplatek 0 Kč", None),
    # Sin importe no se afirma nada: el texto libre ya dice "provize" por su cuenta
    ("Pěkný byt.", "Zprostředkovatelský poplatek dle dohody", None),
    ("Provize není účtována, nájem 9 500 Kč.", "", None),
    # "bez provize" gana aunque haya un importe a continuación
    ("Byt bez provize - nájem 9 500 Kč.", "", None),
    ("Pěkný byt.", "", None),
    # Lo que va entre "provize" y el importe puede negarla: el importe es el del alquiler
    ("Provize pro RK je zahrnuta v nájmu 9 500 Kč.", "", None),
    ("Provize není účtována nájem 9 500 Kč.", "", None),
    ("Provize nejsou účtovány nájem 9 500 Kč.", "", None),
    ("Pěkný byt.", "Zprostředkovatelský poplatek je zahrnut v nájemném 9 500 Kč", None),
    ("Pěkný byt.", "Žádná provize ani poplatek 9 500 Kč", None),
    # "bez DPH" solo aclara el importe
    ("Provize bez DPH činí 12 000 Kč.", "", True),
    ("Provize za služby činí 14 520 Kč.", "", True),
    ("Provize 0 Kč. Zprostředkovatelský poplatek 14 520 Kč.", "", True),
])
def test_commission_needs_a_broker_fee_with_an_amount(text, note, commission):
    assert parse_detail(_with_text(text, note), url=DETAIL_URL).commission is commission


@pytest.mark.parametrize("price,rent", [
    ("9&nbsp;500 <span class=\"mena\"><span class=\"kc\">Kč</span> / měsíc</span> <span class=\"dph-info\">s DPH</span>", 9500),
    ("9 500 Kč/měsíc", 9500),
    ("650 Kč / den", None),
    ("114&nbsp;000 Kč / rok", None),
    ("200 Kč / m² / měsíc", None),
    ("9&nbsp;500 Kč", None),
    ("Info v RK", None),
    ("9&nbsp;500,50 Kč / měsíc", None),
    ("0 Kč / měsíc", None),
    ("400 € / měsíc", None),
    ("", None),
    ("9&nbsp;500 Kč / měsíc s DPH", 9500),
    ("200 Kč / měsíc / m²", None),
    ("1&nbsp;500 Kč / měsíc / osobu", None),
    ("650 Kč / měsíc / den", None),
    ("114&nbsp;000 Kč / měsíc / rok", None),
])
def test_detail_rent_needs_a_monthly_czk_amount(price, rent):
    listing = parse_detail(_with_price(price), url=DETAIL_URL)
    assert listing.rent == rent
    assert (listing.fees, listing.area_m2) == (2500, 18)


def test_detail_rent_fills_a_list_listing_without_one():
    original = dataclasses.replace(_listed(), rent=None)
    assert parse_detail(DETAIL_HTML, original).rent == 9500


def test_detail_rent_never_replaces_the_list_one_with_none():
    original = _listed()
    assert parse_detail(_with_price("Info v RK"), original).rent == 9500


@pytest.mark.parametrize("old,new,area", [
    ("<span>18</span>", "<span>18,7</span>", 18),
    ("<span>18</span>", "<span>18 m²</span>", 18),
    ("<span>18</span>", "<span>mnoho</span>", 20),
    ("<span>18</span>", "<span>0</span>", 20),
])
def test_area_is_the_usable_area_with_the_floor_area_as_fallback(old, new, area):
    html = _replaced(DETAIL_HTML, rf"(Užitná plocha m<sup>2</sup></span>\s*){old}", lambda match: f"{match[1]}{new}")
    assert parse_detail(html, url=DETAIL_URL).area_m2 == area


def test_floor_area_is_used_when_the_usable_area_is_missing():
    html = _replaced(DETAIL_HTML, "<span>Užitná plocha m<sup>2</sup></span>", "<span>Jiná plocha m<sup>2</sup></span>")
    assert parse_detail(html, url=DETAIL_URL).area_m2 == 20
    # Sin ninguna de las dos se queda lo del listado
    none = _replaced(html, "<span>Celková podlahová plocha m<sup>2</sup></span>", "<span>Jiná m<sup>2</sup></span>")
    assert parse_detail(none, url=DETAIL_URL).area_m2 is None
    assert parse_detail(none, dataclasses.replace(_listed(), area_m2=33)).area_m2 == 33


def test_area_in_the_title_is_kept_when_the_page_lacks_one():
    title = _with_title("Pronájem bytu 2+kk, 43&nbsp;m²")
    bare = _replaced(title, "<span>Užitná plocha m<sup>2</sup></span>", "<span>Jiná m<sup>2</sup></span>")
    bare = _replaced(bare, "<span>Celková podlahová plocha m<sup>2</sup></span>", "<span>Jiná m<sup>2</sup></span>")
    assert parse_detail(bare, url=DETAIL_URL).area_m2 == 43


def test_structured_area_wins_over_the_one_in_the_title():
    # El título cuenta la superficie total y el portal da la útil: la ficha manda
    original = dataclasses.replace(_listed(), area_m2=25)
    assert parse_detail(DETAIL_HTML, original).area_m2 == 18


def test_detail_of_a_room_has_no_area():
    listing = parse_detail(_with_title("Pronájem pokoje 18&nbsp;m² v bytě"), url=DETAIL_URL)
    # La "Užitná plocha" de la ficha (18 m²) puede ser la del piso: sin dato antes que uno que despiste
    assert (listing.kind, listing.area_m2, listing.rent) == (Kind.ROOM, None, 9500)
    room = parse_list(_first_title("Pronájem pokoje"))[0]
    assert (room.kind, parse_detail(DETAIL_HTML, room).area_m2) == (Kind.ROOM, None)


def test_detail_disposition_fills_a_gap_and_survives_an_unreadable_value():
    original = _listed("449707")
    assert original.disposition is None
    assert parse_detail(DETAIL_HTML, original).disposition == "1+kk"
    atypical = _with_param("Dispozice bytu", "<a href=\"/x\">Atypický</a>")
    assert parse_detail(atypical, dataclasses.replace(original, disposition="2+1")).disposition == "2+1"
    assert parse_detail(atypical, url=DETAIL_URL).disposition == "1+kk"


@pytest.mark.parametrize("first,second,expected", [
    ("01.12.2026", "02.12.2026", date(2026, 12, 1)),
    ("5. 2. 2027", "01.12.2026", date(2027, 2, 5)),
    # Una fecha inválida no se da por buena: se prueba con la otra y, si tampoco, sin fecha
    ("31.02.2027", "01.12.2026", date(2026, 12, 1)),
    ("ihned", "02.12.2026", date(2026, 12, 2)),
    ("00.00.0000", "", None),
    ("31.02.2027", "ihned", None),
    ("", "", None),
])
def test_available_from_reads_the_first_valid_date(first, second, expected):
    html = _with_param("K dispozici od", first, _with_param("Datum nastěhování", second))
    assert parse_detail(html, url=DETAIL_URL).available_from == expected


def test_available_from_can_come_from_the_move_in_date():
    html = _replaced(DETAIL_HTML, "<span>K dispozici od</span>", "<span>Jiný údaj</span>")
    assert parse_detail(html, url=DETAIL_URL).available_from == date(2026, 12, 1)


def test_gps_comes_from_the_navigation_link_and_makes_the_location_exact():
    listing = parse_detail(DETAIL_HTML, url=DETAIL_URL)
    assert (listing.lat, listing.lon, listing.location_precision) == (49.2078094, 16.6016636, "exact")
    # Aunque el listado solo supiera el barrio, el punto es el del inmueble
    original = dataclasses.replace(_listed(), address_text="Brno - Veveří", location_precision="postcode")
    detail = parse_detail(DETAIL_HTML, original)
    assert (detail.location_precision, detail.address_text) == ("exact", "Bayerova, Brno")


def test_gps_in_a_link_outside_the_map_is_ignored():
    stray = '<a href="https://www.google.com/maps/dir/Current+Location/50.0,17.0">otro mapa</a>'
    html = _with_text(f"Pěkný byt {stray}", note="")
    assert parse_detail(html, url=DETAIL_URL).lat == 49.2078094
    # Sin su propio enlace de navegación tampoco vale el de otro sitio
    listing = parse_detail(_without_map(html), url=DETAIL_URL)
    assert (listing.lat, listing.lon) == (None, None)


@pytest.mark.parametrize("href", [
    "https://www.google.com/maps/dir/Current+Location/95.0,16.6",
    "https://www.google.com/maps/dir/Current+Location/49.2,200.0",
    "https://www.google.com/maps/dir/Current+Location/abc,def",
    "https://www.google.com/maps/dir/Current+Location/49.2",
    "https://www.google.com/maps/search/?api=1&query=Brno",
    "",
])
def test_unusable_gps_is_ignored(href):
    listing = parse_detail(_with_map_link(href), url=DETAIL_URL)
    # El detalle no se rompe: sin coordenadas se queda la dirección, que el pipeline geocodifica
    assert (listing.lat, listing.lon) == (None, None)
    assert (listing.address_text, listing.location_precision) == ("Bayerova, Brno", "street")
    assert (listing.rent, listing.available_from) == (9500, date(2026, 12, 1))


def test_listing_without_the_navigation_link_has_no_point():
    listing = parse_detail(_without_map(), url=DETAIL_URL)
    assert (listing.lat, listing.lon, listing.point) == (None, None, None)
    assert (listing.address_text, listing.location_precision) == ("Bayerova, Brno", "street")


@pytest.mark.parametrize("text,address,precision", [
    ("Bayerova, Brno", "Bayerova, Brno", "street"),
    ("Střední, Brno - Ponava", "Střední, Brno - Ponava", "street"),
    ("Brno - Veveří", "Brno - Veveří", "postcode"),
    ("Brno-Líšeň", "Brno-Líšeň", "postcode"),
    # Sin calle ni barrio no hay ubicación: no se geocodifica el centro de Brno, el perfil pregunta
    ("Brno", "", "none"),
    ("Brno-město", "", "none"),
    ("", "", "none"),
    ("Kuřim", "Kuřim", "postcode"),
])
def test_detail_location_without_gps(text, address, precision):
    listing = parse_detail(_without_map(_with_address(text)), url=DETAIL_URL)
    assert (listing.address_text, listing.location_precision, listing.point) == (address, precision, None)


def test_detail_address_does_not_include_the_map_link_text():
    assert "mapa" in DETAIL_HTML.split('class="estateDetail-adresa"')[1][:400]
    assert parse_detail(DETAIL_HTML, url=DETAIL_URL).address_text == "Bayerova, Brno"


def test_detail_without_a_better_address_keeps_the_list_one():
    original = dataclasses.replace(_listed(), address_text="Brno-Líšeň", location_precision="postcode")
    listing = parse_detail(_without_map(_with_address("Brno")), original)
    assert (listing.address_text, listing.location_precision) == ("Brno-Líšeň", "postcode")
    # Una calle del detalle sí mejora al barrio del listado
    street = parse_detail(_without_map(), original)
    assert (street.address_text, street.location_precision) == ("Bayerova, Brno", "street")


def test_detail_keeps_list_data_the_page_lacks():
    original = dataclasses.replace(
        _listed(), rent=9400, fees=111, deposit=222, commission=True, lat=49.1, lon=16.6, area_m2=33,
        available_from=date(2026, 11, 1), disposition="2+kk", description="Texto del listado",
        address_text="Brno-Líšeň", location_precision="postcode",
    )
    # Sin ningún bloque de datos la página ya no es un anuncio (se prueba aparte): aquí queda solo el precio, sin importe
    bare = _with_price("Info v RK", _without_blocks("description", "params", "note", "address", "map"))
    listing = parse_detail(bare, original)
    assert (listing.rent, listing.fees, listing.deposit, listing.commission) == (9400, 111, 222, True)
    assert (listing.lat, listing.lon, listing.area_m2, listing.disposition) == (49.1, 16.6, 33, "2+kk")
    assert (listing.available_from, listing.description) == (date(2026, 11, 1), "Texto del listado")
    assert (listing.address_text, listing.location_precision, listing.needs_detail) == ("Brno-Líšeň", "postcode", False)


def test_detail_without_any_text_keeps_the_list_description():
    bare = DETAIL_HTML.replace('class="detailPopis"', 'class="x"').replace('class="estate-parameters"', 'class="y"')
    bare = bare.replace('class="estateParams"', 'class="z"')
    assert parse_detail(bare, dataclasses.replace(_listed(), description="Texto del listado")).description == "Texto del listado"
    assert parse_detail(bare, url=DETAIL_URL).description == ""


def test_parse_detail_without_the_card_raises():
    broken = DETAIL_HTML.replace("estateDetail-title", "estateDetail-otro")
    with pytest.raises(ValueError, match="Permanentia: el detalle no trae la ficha del anuncio"):
        parse_detail(broken, url=DETAIL_URL)
    with pytest.raises(ValueError, match="el detalle no trae la ficha"):
        parse_detail(broken, _listed())


def test_page_with_a_title_but_no_data_block_raises():
    bare = _without_blocks(*_BLOCKS)
    assert "<h1>Pronájem bytu 1+kk v Brně na ulici Bayerova</h1>" in bare
    with pytest.raises(ValueError, match="Permanentia: el detalle no trae datos del anuncio"):
        parse_detail(bare, url=DETAIL_URL)
    with pytest.raises(ValueError, match="Permanentia: el detalle no trae datos del anuncio"):
        parse_detail(bare, _listed())
    title_only = '<html><body><div class="estateDetail-title"><h1>Pronájem bytu</h1></div></body></html>'
    with pytest.raises(ValueError, match="Permanentia"):
        parse_detail(title_only, url=DETAIL_URL)


@pytest.mark.parametrize("kept", list(_BLOCKS))
def test_any_single_data_block_is_enough_for_a_detail(kept):
    bare = _without_blocks(*(name for name in _BLOCKS if name != kept))
    listing = parse_detail(bare, _listed())
    assert (listing.source_id, listing.needs_detail) == (DETAIL_ID, False)


def test_parse_detail_of_a_list_page_raises():
    # Un anuncio retirado puede redirigir al listado: no es un detalle y no debe darse por bueno
    with pytest.raises(ValueError, match="el detalle no trae la ficha"):
        parse_detail(LIST_HTML, _listed())
    with pytest.raises(ValueError, match="el detalle no trae la ficha"):
        parse_detail("", url=DETAIL_URL)


@pytest.mark.parametrize("url", [
    "",
    f"{BASE}/pronajem/byty/jihomoravsky/?idMesta%5B0%5D=582786",
    f"{BASE}/reality/byty/",
    f"{BASE}/reality/pronajem-bytu-v-brne/",
])
def test_parse_detail_without_listing_id_in_url_raises(url):
    with pytest.raises(ValueError, match="la URL no lleva el id"):
        parse_detail(DETAIL_HTML, url=url)


def test_parse_detail_takes_the_id_from_a_url_with_a_query():
    assert parse_detail(DETAIL_HTML, url=f"{DETAIL_URL}?utm_source=x").source_id == DETAIL_ID


def test_fetch_list_requests_only_the_brno_listing():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=LIST_HTML)

    listings = _collector(handler).fetch_list()
    assert seen == [LIST_URL]
    assert [listing.source_id for listing in listings] == ["453484", "453121", "450359", "449707"]


def test_fetch_list_follows_a_redirect():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if str(request.url) == LIST_URL:
            return httpx.Response(301, headers={"Location": f"{BASE}/pronajem/byty/jihomoravsky/brno/"})
        return httpx.Response(200, text=LIST_HTML)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert len(Permanentia(client).fetch_list()) == 4
    assert requests == [LIST_URL, f"{BASE}/pronajem/byty/jihomoravsky/brno/"]


def test_fetch_list_with_no_flats_is_not_a_failure():
    assert _collector(lambda request: httpx.Response(200, text=EMPTY_HTML)).fetch_list() == []


@pytest.mark.parametrize("status", [404, 500])
def test_fetch_list_raises_on_http_error(status):
    with pytest.raises(httpx.HTTPStatusError):
        _collector(lambda request: httpx.Response(status, text="error")).fetch_list()


def test_fetch_list_raises_on_a_page_without_the_results_structure():
    with pytest.raises(ValueError, match="Permanentia"):
        _collector(lambda request: httpx.Response(200, text="<html><body>captcha</body></html>")).fetch_list()


def test_fetch_detail_requests_the_listing_page():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=DETAIL_HTML)

    original = _listed()
    listing = _collector(handler).fetch_detail(original)
    assert requests == [original.url]
    assert (listing.source_id, listing.needs_detail, listing.fees) == (DETAIL_ID, False, 2500)
    assert (listing.lat, listing.lon) == (49.2078094, 16.6016636)


@pytest.mark.parametrize("status", [404, 500])
def test_fetch_detail_raises_on_http_error(status):
    with pytest.raises(httpx.HTTPStatusError):
        _collector(lambda request: httpx.Response(status, text="error")).fetch_detail(_listed())
