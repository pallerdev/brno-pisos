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
    ("7\u00a0900 Kč", 7900),
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


# Textos reales del dry-run del 2026-11-25 (UlovDomov "Spolubydlení"): una cama en habitación compartida
# que se colaba como ✅. Cada uno se prueba entero y por fragmentos: cualquiera de las frases debe bastar.
REAL_BED_IN_SHARED_ROOM = [
    "Nabízíme Vám k podnájmu (ke sdílenému bydlení) lůžko v pokoji, ve sdíleném bytě o dispozici 3+1",
    "Pronájem bytu 6+1 ... Každý pokoj je vybaven nábytkem pro 2 osoby. Stůl, židle, postel, skříň.",
    "Ponúkame k prenájmu 1lužko Holka v pokoji pre 2 os. v byte 2+1 ... Celkom sú v byte 2 pokoje s 2 lůžkami.",
]


@pytest.mark.parametrize("text", REAL_BED_IN_SHARED_ROOM)
def test_shared_bedroom_real_dryrun_texts(text):
    assert parse_text(text).shared_bedroom is True


@pytest.mark.parametrize("text,expected", [
    # una cama ofrecida dentro de una habitación
    ("lůžko v pokoji", True),
    ("Nabízíme lůžko v pokoji, ve sdíleném bytě", True),
    ("1 lůžko v pokoji", True),
    ("1lůžko v pokoji", True),
    ("jedno lůžko v pokoji", True),
    ("lůžka v pokoji", True),
    ("1lužko Holka v pokoji", True),
    ("lůžko ve sdíleném pokoji", True),
    ("lôžko v izbe", True),
    ("Ponúkame lôžko v izbe pre 2 osoby", True),
    ("Lůžko\nv pokoji", True),
    ("volné místo v pokoji", True),
    ("Nabízím místo v pokoji", True),
    ("1 místo v pokoji", True),
    ("2 místa v pokoji", True),
    # habitación para 2-4 personas
    ("pokoj pro 2 osoby", True),
    ("Pokoj je pro 2 osoby", True),
    ("pokoje pro 3 osoby", True),
    ("pokoj pro 4 osoby", True),
    ("pokoji pre 2 os.", True),
    ("izba pre 2 osoby", True),
    ("izba s 2 lôžkami", True),
    ("pokoj pro 2 os", True),
    ("pokoj pro 2 lidi", True),
    ("Každý pokoj je vybaven nábytkem pro 2 osoby", True),
    ("pokoj je plně vybaven nábytkem pro 2 osoby", True),
    ("každý pokoj vybaven pro 2 osoby", True),
    ("pokoj zařízen pro 2 osoby", True),
    ("pokoj vhodný pro 2 osoby", True),
    ("pokoj\nje vybaven\nnábytkem pro 2 osoby", True),
    # habitación con 2-4 camas
    ("pokoje s 2 lůžkami", True),
    ("pokoj se 2 lůžky", True),
    ("pokoj s 3 lůžky", True),
    ("pokoj se dvěma lůžky", True),
    ("v bytě 2 pokoje s 2 lůžkami", True),
    ("dvoulůžkový pokoj", True),
    # piso entero, ropa de cama o habitación privada: no es cama compartida
    ("byt pro 2 osoby", False),
    ("Byt je vhodný pro 2 osoby", False),
    ("vhodný pro 2 osoby", False),
    ("Byt 2+kk, obývací pokoj a ložnice, vhodný pro 2 osoby", False),
    ("pokoj v bytě pro 2 osoby", False),
    ("Pokoj 14 m2, byt vhodný pro 2 osoby", False),
    ("Každý pokoj je vybaven nábytkem. Byt pro 2 osoby", False),
    ("Pokoj je světlý. Vhodné pro 2 osoby", False),
    ("pokoj pro 1 osobu", False),
    ("pokoj pro 5 osob", False),
    ("postel 140x200", False),
    ("manželská postel", False),
    ("pokoj s dvoulůžkovou postelí", False),
    ("lůžkoviny", False),
    ("Lůžkoviny v pokoji", False),
    ("povlečení a lůžkoviny v ceně", False),
    ("povlečení", False),
    ("jednolůžkový pokoj", False),
    ("samostatný pokoj", False),
    ("Samostatný pokoj, lůžko a skříň v pokoji", False),
    ("pokoj má jedno lůžko, skříň a stůl", False),
    ("pokoj s jedním lůžkem", False),
    ("dost místa v pokoji pro psací stůl", False),
])
def test_shared_bedroom_dryrun_round(text, expected):
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


@pytest.mark.parametrize("text,expected", [
    ("Byt nemá balkon, ale pračka je v bytě", True),
    ("Nemá výtah. Pračka je ve sklepě.", True),
    ("Nemáme parkování, pračka ano", True),
    ("Byt nemá myčku, ale je tu pračka", True),
    ("Nemá balkon\nPračka ve společných prostorách", True),
    ("Nemáme pračku", False),
    ("nemá pračku", False),
    ("Nemáme tu pračku", False),
    ("Nemáme tu žádnou pračku", False),
    ("Pračku nemáme", False),
])
def test_washing_machine_negation_window(text, expected):
    assert parse_text(text).washing_machine is expected


def test_washing_machine_mirror_negation_needs_accusative():
    assert parse_text("Pračka nemá poruchu").washing_machine is True


@pytest.mark.parametrize("text,expected", [
    ("V bytě je připojená pračka", True),
    ("Zapojená pračka v koupelně", True),
    ("Přípojka na pračku", None),
    ("Možnost zapojení vlastní pračky", None),
    ("Připojení pro pračku", None),
    ("Přípojka vody pro pračku", None),
])
def test_washing_machine_hookup_nouns(text, expected):
    assert parse_text(text).washing_machine is expected


@pytest.mark.parametrize("text,expected", [
    ("Provize 0,5 nájmu", True),
    ("Provize 0.5 nájmu", True),
    ("Provize: 0 Kč", False),
])
def test_commission_zero_vs_fraction(text, expected):
    assert parse_text(text).commission is expected


def test_furnished_not_furnished_with_vybaveny():
    assert parse_text("Byt není vybavený").furnished is False
