"""Piezas comunes a los recolectores."""
from __future__ import annotations

import re
from typing import Protocol

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from pisobot.models import Listing

__all__ = ["Collector", "HTMLParser", "block_text", "inline_text", "price_czk"]


class Collector(Protocol):
    name: str

    def fetch_list(self) -> list[Listing]: ...

    def fetch_detail(self, listing: Listing) -> Listing: ...


def inline_text(node) -> str:
    return " ".join(node.text().split()) if node is not None else ""


def block_text(node) -> str:
    return node.text(separator="\n", strip=True, skip_empty=True) if node is not None else ""


# Cifra con miles (espacio o punto) o simple, que no sea parte de un número mayor ni lleve decimales,
# seguida de "Kč" o ",-". Los decimales ("8 500,50 Kč") se descartan: mejor None que un importe falso.
_CZK = re.compile(r"(?<![\d.,])(\d{1,3}(?:[ .]\d{3})+|\d+)(?![.,]\d)\s*(?:,-\s*)?(?:Kč|,-)", re.I)
# Espacios de ancho cero y similares que algunos anuncios cuelan entre la cifra y la moneda.
_INVISIBLE = re.compile(r"[\u200b-\u200d\u2060\ufeff]")


def price_czk(raw: str | None) -> int | None:
    """'8.500Kč/měsíc' -> 8500, '20 000,-' -> 20000, '8 500,50 Kč' / 'Dohodou' / '0 Kč' -> None.

    Devuelve el PRIMER importe del texto: hay que pasarle el campo del precio, no texto libre.
    """
    text = re.sub(r"\s+", " ", _INVISIBLE.sub("", raw or ""))
    match = _CZK.search(text)
    return (int(re.sub(r"\D", "", match.group(1))) or None) if match else None
