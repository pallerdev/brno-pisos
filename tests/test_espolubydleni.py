import dataclasses
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from pisobot.collectors.espolubydleni import LIST_URL, Espolubydleni, parse_detail, parse_list
from pisobot.models import Kind
from pisobot.parse_text import parse_text

FIXTURES = Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "espolubydleni_list.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "espolubydleni_detail.html").read_text(encoding="utf-8")
DETAIL_URL = "https://www.espolubydleni.cz/podnajem-spolubydlici/554939-pronajem-panelovy-byt-2-1-kralovo-pole-brno.htm"
OFFER_IDS = ["535554", "554409", "555906", "555895", "555836"]
# Precio de la primera oferta tal como sale en su tarjeta (el mismo texto aparece también en un atributo de la foto)
FIRST_PRICE = "<b>6.000Kč/měsíc</b>/os.</div>"
FIRST_LOCATION = (
    '<div style="padding:4px;color:#A80F2E;" title="city/quarter Štýřice, street Renneská Třída">'
    '<strong style="color:#A80F2E;">Štýřice</strong>, Renneská Třída </div>'
)


def _by_id(listings):
    return {listing.source_id: listing for listing in listings}


def _replaced(html, old, new):
    assert html.count(old) == 1, f"el fixture ya no trae {old!r} exactamente una vez"
    return html.replace(old, new)


def _collector(handler):
    return Espolubydleni(httpx.Client(transport=httpx.MockTransport(handler)))


def test_parse_list_keeps_only_offers():
    listings = parse_list(LIST_HTML)
    assert [listing.source_id for listing in listings] == OFFER_IDS
    first = listings[0]
    assert first.source_id == "535554"
    assert first.title.startswith("Nabízím 2 místa 2L pokoj")
    assert (first.kind, first.rent, first.disposition) == (Kind.ROOM, 6000, "2+kk")
    assert first.address_text == "Štýřice, Renneská Třída"
    assert first.location_precision == "street"
    assert first.url == "https://www.espolubydleni.cz/podnajem-spolubydlici/535554-pronajem-cihlovy-byt-2-1-styrice-brno.htm"
    assert first.description.startswith("Nabízím pronájem neprůchozího dvoulůžkového pokoje")
    assert first.needs_detail is False
    assert all(listing.title.startswith("Nabízím") for listing in listings)


def test_parse_list_reads_room_facts_from_the_card():
    rooms = _by_id(parse_list(LIST_HTML))
    # Solo vale la superficie del propio cuarto ("plocha pokoje"), nunca la del piso
    assert [rooms[i].area_m2 for i in ("535554", "554409", "555906", "555836")] == [18, 13, None, 8]
    assert [rooms[i].rent for i in ("535554", "554409", "555906", "555836")] == [6000, 9800, 6500, 8500]
    # La coma final de "Cejl 542/97," se queda fuera para que el geocodificador no la vea
    assert rooms["555836"].address_text == "Zábrdovice, Cejl 542/97"
    assert rooms["554409"].available_from is None


def test_parse_list_whole_flat_is_not_a_room():
    flat = _by_id(parse_list(LIST_HTML))["555895"]
    # "17.000Kč/měsíc/byt" es el precio del piso entero: tratarlo como habitación daría un alquiler por persona falso
    assert (flat.kind, flat.rent, flat.disposition, flat.area_m2) == (Kind.FLAT, 17000, "4+kk", 170)
    assert flat.address_text == "Veveří, Jaselská"


def test_parse_detail():
    listing = parse_detail(DETAIL_HTML, DETAIL_URL)
    assert listing.source_id == "554939"
    assert listing.url == DETAIL_URL
    assert listing.title == "Nabízím 2 místa v 2L pokoji 17m2"
    assert (listing.kind, listing.rent, listing.area_m2) == (Kind.ROOM, 7000, 17)
    assert listing.disposition == "2+kk"
    assert listing.available_from == date(2027, 1, 1)
    assert listing.address_text == "Královo Pole, Božetěchova"
    assert listing.location_precision == "street"
    assert listing.description.startswith("Nabízíme neprůchozí pokoj")
    assert "Provize: 0 Kč" in listing.description


@pytest.mark.parametrize("source_id,expected", [
    ("535554", True),   # "2L pokoj" en el titular
    ("555906", True),
    ("554409", False),  # "1L pokoj": cuarto individual
    ("555836", False),
])
def test_shared_bedroom_text_reaches_the_listing(source_id, expected):
    listing = _by_id(parse_list(LIST_HTML))[source_id]
    # El pipeline analiza título + descripción: la cama en cuarto compartido tiene que llegar en alguno de los dos
    assert parse_text(f"{listing.title}\n{listing.description}").shared_bedroom is expected


def test_shared_bedroom_text_reaches_the_detail():
    listing = parse_detail(DETAIL_HTML, DETAIL_URL)
    assert parse_text(f"{listing.title}\n{listing.description}").shared_bedroom is True


def test_parse_detail_of_a_seeker_is_none():
    seeker = _replaced(
        DETAIL_HTML,
        "<title>Nabízím Spolubydlení/pokoj",
        "<title>Hledám Spolubydlení/pokoj",
    )
    assert parse_detail(seeker, DETAIL_URL) is None


def test_parse_detail_without_the_card_raises():
    broken = _replaced(DETAIL_HTML, 'id="realita1"', 'id="realita9"')
    with pytest.raises(ValueError, match="el detalle no trae la ficha"):
        parse_detail(broken, DETAIL_URL)


def test_parse_detail_without_listing_id_in_url_raises():
    with pytest.raises(ValueError, match="la URL no lleva el id"):
        parse_detail(DETAIL_HTML, "https://www.espolubydleni.cz/podnajem-spolubydlici/brno")


@pytest.mark.parametrize("raw", ["31.02.2027", "00.00.0000", "ihned", ""])
def test_invalid_available_from_is_none(raw):
    html = _replaced(DETAIL_HTML, "<b>Volné od:</b> 01.01.2027<br>", f"<b>Volné od:</b> {raw}<br>")
    listing = parse_detail(html, DETAIL_URL)
    assert listing.available_from is None
    assert listing.rent == 7000


def test_available_from_is_read_from_a_list_card():
    # La tarjeta del listado repite la columna derecha del detalle, que es donde está "Volné od"
    html = _replaced(LIST_HTML, "<b>Místo pro: </b> 2 osoby<br><br><b>Popis bytu:</b><br>plocha bytu: 53m",
                     "<b>Volné od:</b> 15.02.2027<br><b>Místo pro: </b> 2 osoby<br><br><b>Popis bytu:</b><br>plocha bytu: 53m")
    assert _by_id(parse_list(html))["535554"].available_from == date(2027, 2, 15)


@pytest.mark.parametrize("price,rent,kind", [
    ("<b>5 500Kč/měsíc</b>/os.</div>", 5500, Kind.ROOM),
    ("<b>5.500Kč/měsíc</b>/(osobu)</div>", 5500, Kind.ROOM),
    ("<b>17.000Kč/měsíc</b>/byt</div>", 17000, Kind.FLAT),
    # Otra unidad o periodo no es un alquiler mensual: mejor sin precio que uno falso
    ("<b>600Kč/den</b>/os.</div>", None, Kind.ROOM),
    ("<b>6.000Kč/měsíc</b>/pokoj</div>", None, Kind.ROOM),
    ("<b>6.000Kč</b></div>", None, Kind.ROOM),
    ("<b>Dohodou</b></div>", None, Kind.ROOM),
    ("<b>6.000,50Kč/měsíc</b>/os.</div>", None, Kind.ROOM),
    ("<b>0Kč/měsíc</b>/os.</div>", None, Kind.ROOM),
])
def test_rent_depends_on_the_price_unit(price, rent, kind):
    first = parse_list(_replaced(LIST_HTML, FIRST_PRICE, price))[0]
    assert (first.source_id, first.rent, first.kind) == ("535554", rent, kind)
    # Un precio que no se entiende no tira el anuncio: el resto de datos sigue ahí
    assert first.address_text == "Štýřice, Renneská Třída"


def test_detail_price_unit():
    per_person = parse_detail(DETAIL_HTML, DETAIL_URL)
    assert per_person.rent == 7000
    daily = _replaced(DETAIL_HTML, "7.000Kč/měsíc</b>/(osobu)", "700Kč/den</b>/(osobu)")
    assert parse_detail(daily, DETAIL_URL).rent is None


def _with_first_location(quarter, street, shown):
    return _replaced(
        LIST_HTML,
        FIRST_LOCATION,
        f'<div style="padding:4px;color:#A80F2E;" title="city/quarter {quarter}, street {street}">{shown}</div>',
    )


@pytest.mark.parametrize("quarter", ["Brno", "Brno - město", "Brno-město", "brno", ""])
def test_city_only_location_is_unknown(quarter):
    # Sin barrio ni calle no hay ubicación: no se geocodifica el centro de Brno, el perfil pregunta
    first = parse_list(_with_first_location(quarter, "", f"<strong>{quarter}</strong> "))[0]
    assert (first.address_text, first.location_precision) == ("", "none")
    assert first.rent == 6000


def test_listing_without_location_markup_is_unknown():
    html = _replaced(LIST_HTML, 'title="city/quarter Štýřice, street Renneská Třída"', 'title="otro"')
    first = parse_list(html)[0]
    assert (first.address_text, first.location_precision) == ("", "none")


@pytest.mark.parametrize("quarter,street,address,precision", [
    # Solo barrio: su centroide queda a kilómetros, la ubicación es aproximada
    ("Štýřice", "", "Štýřice", "postcode"),
    ("Černovice, Královo Pole, Střed", "", "Černovice, Královo Pole, Střed", "postcode"),
    # Con calle se geocodifica la calle
    ("Štýřice", "Renneská Třída", "Štýřice, Renneská Třída", "street"),
    ("Štýřice", "Cejl 542/97,", "Štýřice, Cejl 542/97", "street"),
    ("Brno", "Cejl 542/97", "Brno, Cejl 542/97", "street"),
])
def test_precision_follows_the_quarter_and_street_attribute(quarter, street, address, precision):
    first = parse_list(_with_first_location(quarter, street, "visible"))[0]
    assert (first.address_text, first.location_precision) == (address, precision)


@pytest.mark.parametrize("karta,address,precision", [
    ("Královo Pole, <b>Božetěchova</b>", "Královo Pole, Božetěchova", "street"),
    ("Královo Pole", "Královo Pole", "postcode"),
    ("Brno", "", "none"),
])
def test_detail_location(karta, address, precision):
    html = _replaced(DETAIL_HTML, '<h2 class="karta">Královo Pole, <b>Božetěchova</b></h2>',
                     f'<h2 class="karta">{karta}</h2>')
    listing = parse_detail(html, DETAIL_URL)
    assert (listing.address_text, listing.location_precision) == (address, precision)


def test_amenities_reach_the_description():
    rooms = _by_id(parse_list(LIST_HTML))
    # En 554409 el texto libre no dice nada de la lavadora: solo la lista de equipamiento de la tarjeta
    description = rooms["554409"].description
    assert "pračka" not in description.split("\nPopis bytu a pokoje: ")[0]
    assert "\nPopis bytu a pokoje: internet | lednička | pračka | sporák" in description
    # Las dos listas (piso y cuarto) van juntas: también aparecen "postel", "skříň"...
    assert "neprůchozí | světlý pokoj" in description


@pytest.mark.parametrize("source_id", OFFER_IDS)
def test_list_amenities_feed_the_washing_machine_rule(source_id):
    listing = _by_id(parse_list(LIST_HTML))[source_id]
    assert parse_text(f"{listing.title}\n{listing.description}").washing_machine is True


def test_detail_amenities_feed_the_washing_machine_rule():
    listing = parse_detail(DETAIL_HTML, DETAIL_URL)
    assert "\nPopis bytu a pokoje: internet | lednička | pračka | sporák" in listing.description
    assert parse_text(f"{listing.title}\n{listing.description}").washing_machine is True


def test_amenities_label_adds_no_false_facts():
    # El encabezado que une texto y listas no puede disparar reglas por sí mismo ("Vybavení" daría furnished=True)
    listings = [*parse_list(LIST_HTML), parse_detail(DETAIL_HTML, DETAIL_URL)]
    for listing in listings:
        base = listing.description.split("\nPopis bytu a pokoje: ")[0]
        with_lists = parse_text(f"{listing.title}\n{listing.description}")
        without = parse_text(f"{listing.title}\n{base}")
        assert dataclasses.replace(with_lists, washing_machine=without.washing_machine) == without, listing.source_id


def test_amenities_do_not_override_a_negation_in_the_text():
    # El texto aparece también en atributos de la fila; el que cuenta es el de .popis1
    html = LIST_HTML.replace("K dispozici kuchyň, lednice, pračka, myčka.", "K dispozici kuchyň, lednice, bez pračky.")
    assert html != LIST_HTML
    listing = _by_id(parse_list(html))["535554"]
    assert "pračka" in listing.description.split("\nPopis bytu a pokoje: ")[1]
    assert parse_text(f"{listing.title}\n{listing.description}").washing_machine is False


def test_card_without_amenities_keeps_the_plain_description():
    # Una tarjeta sin listas de equipamiento no añade el encabezado vacío
    html = LIST_HTML.replace('<div style="text-align:justify;">', '<div style="text-align:left;">')
    assert all("Popis bytu a pokoje" not in listing.description for listing in parse_list(html))


def test_fetch_list_accepts_404_with_content():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, text=LIST_HTML)))
    assert len(Espolubydleni(client).fetch_list()) == 5


def test_fetch_list_accepts_200():
    assert len(_collector(lambda r: httpx.Response(200, text=LIST_HTML)).fetch_list()) == 5


def test_fetch_list_makes_a_single_request_to_the_list_url():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(404, text=LIST_HTML)

    collector = _collector(handler)
    listings = collector.fetch_list()
    # El detalle ya viene en la tarjeta: nada más que una petición por ejecución (Crawl-delay: 10)
    assert [str(request.url) for request in requests] == [LIST_URL]
    assert collector.fetch_detail(listings[0]) is listings[0]
    assert len(requests) == 1


def test_fetch_list_rejects_real_errors():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, text="<html>nic</html>")))
    with pytest.raises(RuntimeError):
        Espolubydleni(client).fetch_list()


@pytest.mark.parametrize("status,body", [
    (500, LIST_HTML),
    (503, "<html>mantenimiento</html>"),
    (403, LIST_HTML),
    (200, "<html>captcha</html>"),
])
def test_fetch_list_rejects_other_responses(status, body):
    with pytest.raises(RuntimeError, match="eSpolubydleni"):
        _collector(lambda r: httpx.Response(status, text=body)).fetch_list()


def test_parse_list_without_the_results_table_raises():
    broken = _replaced(LIST_HTML, 'id="result1"', 'id="result9"')
    with pytest.raises(ValueError, match=re.escape("eSpolubydleni: el listado no trae anuncios (¿cambió el formato?)")):
        parse_list(broken)


def test_parse_list_without_offers_raises():
    # Si el portal renombra "Nabízím" todo parecería gente que busca piso y el bot se quedaría ciego sin avisar
    renamed = LIST_HTML.replace(">Nabízím ", ">Offering ")
    assert renamed != LIST_HTML
    with pytest.raises(ValueError, match="no trae anuncios"):
        parse_list(renamed)


def test_offer_without_link_raises():
    broken = LIST_HTML.replace('class="re_2"', 'class="re_9"')
    with pytest.raises(ValueError, match="anuncio sin enlace o ficha"):
        parse_list(broken)


def test_offer_without_card_raises():
    broken = _replaced(LIST_HTML, 'id="i1inz"', 'id="i9inz"')
    with pytest.raises(ValueError, match="anuncio sin enlace o ficha"):
        parse_list(broken)


def test_seeker_rows_are_skipped_even_without_a_link():
    # Solo las ofertas necesitan identificarse: una fila de "Hledám" rota no debe tirar el portal
    html = _replaced(LIST_HTML, 'href="/podnajem-spolubydlici/549386-pronajem-brno-brno.htm" class="re_2"', 'class="re_2"')
    assert [listing.source_id for listing in parse_list(html)] == OFFER_IDS
