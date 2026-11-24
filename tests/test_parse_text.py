import pytest

from pisobot.parse_text import bedrooms, normalize_disposition, parse_area, parse_price, parse_text


@pytest.mark.parametrize("text,expected", [
    ("V bytě je pračka a myčka.", True),
    ("Automatická pračka ve společných prostorách", True),
    ("K dispozici pračkosušička", True),
    ("Washing machine in the flat", True),
    ("Byt je bez pračky.", False),
    ("Krásný slunný pokoj", None),
])
def test_washing_machine(text, expected):
    assert parse_text(text).washing_machine is expected


@pytest.mark.parametrize("text,expected", [
    ("Cena 8 500 Kč včetně energií a internetu", True),
    ("Nájem 7000 Kč, vše v ceně", True),
    ("All inclusive, utilities included", True),
    ("Nájem 12 000 Kč + energie dle spotřeby", False),
    ("Elektřina na vlastní smlouvu", False),
    ("Včetně poplatků, energie zvlášť", False),
    ("Nájem 9000 Kč + poplatky 2000 Kč", None),
])
def test_energy_included(text, expected):
    assert parse_text(text).energy_included is expected


@pytest.mark.parametrize("text,expected", [
    ("Byt je plně zařízený", True),
    ("Pokoj je vybavený nábytkem", True),
    ("Nezařízený byt, jen kuchyňská linka", False),
    ("Byt je nevybavený", False),
    ("Unfurnished apartment", False),
    ("Byt 2+kk v Žabovřeskách", None),
])
def test_furnished(text, expected):
    assert parse_text(text).furnished is expected


def test_flags():
    facts = parse_text("Průchozí pokoj, přímotop, bez provize")
    assert facts.walkthrough_room is True
    assert facts.electric_heating is True
    assert facts.commission is False
    assert parse_text("Provize RK 1 nájem").commission is True
    assert parse_text("Elektrické topení v celém bytě").electric_heating is True
    assert parse_text("Klidný byt").commission is None


@pytest.mark.parametrize("text,expected", [
    ("Nabízím 2 místa v 2L pokoji 17m2", True),
    ("Volné místo ve dvoulůžkovém pokoji", True),
    ("Pokoj 1L pro jednu osobu", False),
    ("Samostatný pokoj 14 m2", False),
])
def test_shared_bedroom(text, expected):
    assert parse_text(text).shared_bedroom is expected


@pytest.mark.parametrize("text,expected", [
    ("pronájem bytu 2+kk 50 m²", 50),
    ("Pokoj 17m2", 17),
    ("byt 58,5 m2", 58),
    ("bez plochy", None),
])
def test_parse_area(text, expected):
    assert parse_area(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("8 500 Kč", 8500),
    ("12.000,- Kč", 12000),
    ("Cena: 9500 CZK/měsíc", 9500),
    ("7 900 Kč", 7900),
    ("3+1 75 m2 15000 Kč", 15000),
    ("Dohodou", None),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize("raw,expected", [
    ("DISP_3_1", "3+1"),
    ("DISP_1_KK", "1+kk"),
    ("threePlusOne", "3+1"),
    ("onePlusKk", "1+kk"),
    ("Pronájem bytu 2+1 50 m2", "2+1"),
    ("4 + KK", "4+kk"),
    ("pronájem bytu 2KK", "2+kk"),
    ("byt 1.kk", "1+kk"),
    ("byt 2 + 1, 20 000 Kč", "2+1"),
    ("Pronájem 20 000 Kč", None),
    ("garsoniéra", "1+kk"),
    ("atypical", None),
    (None, None),
])
def test_normalize_disposition(raw, expected):
    assert normalize_disposition(raw) == expected


@pytest.mark.parametrize("disposition,expected", [
    ("3+1", 3), ("4+kk", 3), ("4+1", 4), ("5+kk", 4), ("1+kk", 1), ("2+kk", 1), (None, None),
])
def test_bedrooms(disposition, expected):
    assert bedrooms(disposition) == expected


@pytest.mark.parametrize("text,expected", [
    ("Byt je bez\npračky", False),
    ("bez  pračky", False),
    ("Nemáme pračku", False),
    ("nemá pračku", False),
    ("Pračku nemáme", False),
    ("Přípojka na pračku", None),
    ("Možnost zapojení vlastní pračky", None),
    ("Připojení pro pračku", None),
    ("Klidný byt bez práce", None),
])
def test_washing_machine_fix_round(text, expected):
    assert parse_text(text).washing_machine is expected


@pytest.mark.parametrize("text,expected", [
    ("Nájem není včetně energií", False),
    ("Včetně všech poplatků (vč. energií)", True),
])
def test_energy_included_fix_round(text, expected):
    assert parse_text(text).energy_included is expected


@pytest.mark.parametrize("text,expected", [
    ("Byt není zařízený", False),
    ("Pronájem bez zařízení", False),
    ("Byt bez vybavení", False),
    ("Nezařízený byt", False),
])
def test_furnished_fix_round(text, expected):
    assert parse_text(text).furnished is expected


@pytest.mark.parametrize("text,expected", [
    ("elektrické vytápění a ohřev vody", True),
    ("Topení: elektrické", True),
    ("Vytápění: elektrické", True),
    ("Elektrické podlahové topení", True),
    ("Byt s elektrokotlem", True),
    ("přímotop v pokoji", True),
    ("plynový kotel", False),
    ("ústřední topení", False),
])
def test_electric_heating(text, expected):
    assert parse_text(text).electric_heating is expected


@pytest.mark.parametrize("text,expected", [
    ("místo v 2L pokoji", True),
    ("2L pokoj", True),
    ("dvoulůžkový pokoj", True),
    ("neprůchozího dvoulůžkového pokoje", True),
    ("pokoj je dvoulůžkový", True),
    ("2lůžkový pokoj", True),
    ("2-lůžkový pokoj", True),
    ("dvojlůžkový pokoj", True),
    ("třílůžkový pokoj", True),
    ("trojlůžkový pokoj", True),
    ("ložnice s dvoulůžkovou postelí", False),
    ("dvoulůžková postel", False),
    ("bojler 3l", False),
    ("jednolůžkový pokoj", False),
])
def test_shared_bedroom_fix_round(text, expected):
    assert parse_text(text).shared_bedroom is expected


@pytest.mark.parametrize("text,expected", [
    ("pokoje nejsou průchozí", False),
    ("bez průchozích pokojů", False),
    ("průchozí pokoj", True),
    ("neprůchozí pokoj", False),
])
def test_walkthrough_room(text, expected):
    assert parse_text(text).walkthrough_room is expected


@pytest.mark.parametrize("text,expected", [
    ("Provize: 0 Kč", False),
    ("Žádná provize", False),
    ("Nehradíte provizi", False),
    ("Neúčtujeme provizi", False),
    ("bez provize", False),
])
def test_commission_fix_round(text, expected):
    assert parse_text(text).commission is expected


@pytest.mark.parametrize("raw,expected", [
    ("Panelový byt 2+1(kk)", "2+kk"),
    ("Panelový byt 1+1(kk)", "1+kk"),
    ("Panelový byt 2+1", "2+1"),
])
def test_normalize_disposition_fix_round(raw, expected):
    assert normalize_disposition(raw) == expected
