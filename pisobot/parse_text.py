"""Reglas de palabras clave para anuncios en checo (y algo de inglés).

Se trabaja sobre texto normalizado (minúsculas, sin diacríticos) porque los anuncios
mezclan "pračka" y "pracka". En cada campo las reglas negativas van primero:
"bez pračky" contiene "pračky" y debe ganar.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass
class TextFacts:
    washing_machine: bool | None = None
    energy_included: bool | None = None
    furnished: bool | None = None
    walkthrough_room: bool = False
    electric_heating: bool = False
    commission: bool | None = None
    shared_bedroom: bool = False


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _tri(text: str, negative: list[str], positive: list[str]) -> bool | None:
    if any(re.search(p, text) for p in negative):
        return False
    if any(re.search(p, text) for p in positive):
        return True
    return None


_WASHER_NEG = [r"\bbez prac", r"\bneni (?:tu |zde )?prac", r"\bprac\w* (?:tu |zde )?neni\b", r"\bno washing machine"]
_WASHER_POS = [r"\bprac(?:ka|ky|ku|kou|ce|kosusick\w*)\b", r"\bwashing machine", r"\bwasher\b"]

_ENERGY_NEG = [
    r"\+\s*energi", r"\bplus energi", r"\benergie (?:dle|podle) spotreby", r"\benergie zvlast",
    r"\b(?:energie|elektrina|elektrinu|plyn) (?:na|pres) vlastni smlouv", r"\bbez energi",
    r"\+\s*elektrin", r"\butilities not included", r"\+\s*utilities",
]
_ENERGY_POS = [
    r"\bvcetne (?:vsech )?energi", r"\bvcetne (?:sluzeb|poplatku) a energi", r"\bvse v cene",
    r"\benergie v cene", r"\ball inclusive", r"\butilities included", r"\bincluding utilities",
]

_FURNISHED_NEG = [r"\bnezarizen", r"\bnevybaven", r"\bbez nabytku", r"\bunfurnished"]
_FURNISHED_POS = [r"\bzarizen", r"\bvybaven", r"\bfurnished"]

_COMMISSION_NEG = [r"\bbez provize", r"\bbez rk\b", r"\bneplatite (?:zadnou )?provizi", r"\bno commission"]
_COMMISSION_POS = [r"\bproviz", r"\bcommission"]


def parse_text(text: str) -> TextFacts:
    t = norm(text)
    return TextFacts(
        washing_machine=_tri(t, _WASHER_NEG, _WASHER_POS),
        energy_included=_tri(t, _ENERGY_NEG, _ENERGY_POS),
        furnished=_tri(t, _FURNISHED_NEG, _FURNISHED_POS),
        walkthrough_room=bool(re.search(r"\bpruchoz", t)),
        electric_heating=bool(re.search(r"\belektrick\w* topen|\bprimotop|\bakumulacni kamna", t)),
        commission=_tri(t, _COMMISSION_NEG, _COMMISSION_POS),
        shared_bedroom=bool(re.search(r"\b[2-4]\s*l\s+pokoj|\b[2-4]l\b|\bdvouluzkov|\btrojluzkov|\bshared room", t)),
    )


_PRICE = re.compile(r"\b(\d{1,3}(?:[ .]\d{3})+|\d+)\s*(?:,-\s*)?(?:kc|czk|,-)")


def parse_price(text: str) -> int | None:
    match = _PRICE.search(norm(text))
    if not match:
        return None
    return int(re.sub(r"[ .]", "", match.group(1)))


def parse_area(text: str) -> int | None:
    # norm() convierte "m²" en "m2"
    match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*m2\b", norm(text))
    if not match:
        return None
    return int(float(match.group(1).replace(",", ".")))


_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def normalize_disposition(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    m = re.fullmatch(r"DISP_(\d)_(KK|1)", s, re.IGNORECASE)
    if m:
        return f"{m.group(1)}+{m.group(2).lower()}"
    m = re.fullmatch(r"(one|two|three|four|five|six)plus(kk|one)", s, re.IGNORECASE)
    if m:
        suffix = "kk" if m.group(2).lower() == "kk" else "1"
        return f"{_WORDS[m.group(1).lower()]}+{suffix}"
    n = norm(s)
    if "garson" in n:
        return "1+kk"
    # Variantes reales: "2+kk", "2 + KK", "2KK", "1.kk", "2 + 1". Nunca leer "20 000" como disposición.
    m = re.search(r"\b([1-7])\s*[+.]?\s*kk\b", n)
    if m:
        return f"{m.group(1)}+kk"
    m = re.search(r"\b([1-7])\s*\+\s*1\b", n)
    if m:
        return f"{m.group(1)}+1"
    return None


def bedrooms(disposition: str | None) -> int | None:
    """X+1 → X dormitorios; X+kk → X-1 (una habitación es salón con cocina)."""
    if not disposition:
        return None
    rooms, kitchen = disposition.split("+")
    n = int(rooms)
    if kitchen == "kk":
        return max(n - 1, 1)
    return n
