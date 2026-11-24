"""Tipos compartidos por todo el bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

class Kind(str, Enum):
    ROOM = "room"  # habitación en piso compartido
    FLAT = "flat"  # piso entero

class Verdict(str, Enum):
    PASS = "pass"
    MAYBE = "maybe"
    FAIL = "fail"

@dataclass(frozen=True)
class Point:
    lat: float
    lon: float

@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    kind: Kind
    rent: int | None
    fees: int | None = None
    # True: los poplatky ya cubren luz/gas; False: van aparte; None: no se sabe
    fees_include_energy: bool | None = None
    disposition: str | None = None
    area_m2: int | None = None
    deposit: int | None = None
    commission: bool | None = None
    lat: float | None = None
    lon: float | None = None
    location_precision: str = "exact"  # exact | street | postcode | none
    address_text: str = ""
    description: str = ""
    available_from: date | None = None
    furnished: bool | None = None
    # El listado no trae todo (texto, fecha...): hay que pedir el detalle antes de evaluar
    needs_detail: bool = False

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def point(self) -> Point | None:
        if self.lat is None or self.lon is None:
            return None
        return Point(self.lat, self.lon)

@dataclass
class Trip:
    minutes: int
    transfers: int
    first_walk_min: int
    modes: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

@dataclass
class Shop:
    name: str
    lat: float
    lon: float
