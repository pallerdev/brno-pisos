import pytest

from pisobot.collectors.base import HTMLParser, block_text, inline_text, price_czk


@pytest.mark.parametrize("raw,expected", [
    ("8.500Kč/měsíc", 8500),
    ("20 000 Kč", 20000),
    ("16 400 Kč", 16400),
    # NBSP como separador de miles y un carácter de ancho cero (U+200D) antes de "Kč"
    ("16\xa0400\u200d Kč/měsíc", 16400),
    ("13\xa0500 Kč", 13500),
    ("20\u202f000 Kč", 20000),
    # Con decimales no hay importe fiable: None antes que un número falso
    ("8 500,50 Kč", None),
    ("8 500.50 Kč", None),
    # Un número previo (Praha 6, "2 pokoje") no se pega al importe
    ("Praha 6 18 000 Kč", 18000),
    ("2 18 000 Kč", 18000),
    ("20 000,- Kč", 20000),
    ("20 000,-", 20000),
    ("20.000,-Kč", 20000),
    ("20 000 kč", 20000),
    ("0 Kč", None),
    ("Dohodou", None),
    ("", None),
    (None, None),
])
def test_price_czk(raw, expected):
    assert price_czk(raw) == expected


def test_text_helpers():
    tree = HTMLParser("<div id='a'>  Štýřice ,\n  Renneská </div><div id='b'><p>uno</p><p>dos</p></div>")
    assert inline_text(tree.css_first("#a")) == "Štýřice , Renneská"
    assert block_text(tree.css_first("#b")) == "uno\ndos"
    assert inline_text(None) == "" and block_text(None) == ""
