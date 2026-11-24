"""Reglas de los perfiles. Funciones puras: todo lo externo llega ya calculado en Candidate."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from pisobot.config import Config
from pisobot.models import Kind, Listing, Shop, Trip, Verdict
from pisobot.parse_text import TextFacts, bedrooms


@dataclass
class Price:
    total: int
    rent: int
    fees: int
    energy_estimate: int
    energy_included: bool
    fees_per_extra_person: int = 0

    def total_for(self, people: int) -> int:
        return self.total + self.fees_per_extra_person * (people - 1)


@dataclass
class Candidate:
    listing: Listing
    facts: TextFacts
    trips: dict[str, list[Trip] | None] = field(default_factory=dict)
    shop: tuple[Shop, float] | None = None
    summary: str = ""


@dataclass
class Evaluation:
    profile: str
    verdict: Verdict
    star: bool = False
    price: Price | None = None
    per_person: dict[int, int] = field(default_factory=dict)
    trip: Trip | None = None
    fails: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


def price_of(listing: Listing, facts: TextFacts, estimates: dict) -> Price | None:
    if listing.rent is None:
        return None
    fees = listing.fees or 0
    included = listing.fees_include_energy
    if included is None:
        included = facts.energy_included
    if listing.kind is Kind.ROOM:
        # Las habitaciones casi siempre se anuncian con todo incluido: solo se suma si lo dicen aparte
        estimate = estimates["room"] if included is False else 0
    else:
        estimate = 0 if included else estimates.get(listing.disposition or "", estimates["default"])
    return Price(
        total=listing.rent + fees + estimate,
        rent=listing.rent,
        fees=fees,
        energy_estimate=estimate,
        energy_included=bool(included),
        fees_per_extra_person=listing.fees_per_extra_person,
    )


def transit_ok(trips: list[Trip], rule: dict) -> tuple[bool, Trip | None]:
    if not trips:
        return False, None
    best = min(trips, key=lambda t: t.minutes)
    if best.minutes <= rule["max_minutes"]:
        return True, best
    direct = rule.get("direct")
    if direct:
        options = [
            t for t in trips
            if t.transfers == 0
            and len(t.modes) == 1
            and t.modes[0] in direct["modes"]
            and t.first_walk_min <= direct["max_walk_minutes"]
            and t.minutes <= direct["max_minutes"]
        ]
        if options:
            return True, min(options, key=lambda t: t.minutes)
    return False, best


def prefilter(listing: Listing, facts: TextFacts, cfg: Config) -> list[str]:
    """Descarte barato antes de gastar llamadas a APIs externas."""
    if facts.washing_machine is False:
        return []
    price = price_of(listing, facts, cfg.raw["energy_estimates"])
    names = []
    for name, profile in cfg.profiles.items():
        if listing.kind.value not in profile["kinds"]:
            continue
        if "people" in profile:
            _, options = _group_split(listing, profile)
            if not options:
                continue
            if price is not None and all(price.total_for(p) > profile["per_person_max"] * p for p in options):
                continue
        elif price is not None and price.total > profile["max_total"]:
            continue
        names.append(name)
    return names


def evaluate(c: Candidate, name: str, cfg: Config) -> Evaluation:
    profile = cfg.profiles[name]
    listing, facts = c.listing, c.facts
    ev = Evaluation(profile=name, verdict=Verdict.PASS)

    if listing.kind.value not in profile["kinds"]:
        ev.fails.append("tipo de vivienda no buscado")

    ev.price = price_of(listing, facts, cfg.raw["energy_estimates"])
    if "people" in profile:
        _check_group(ev, listing, profile)
    elif ev.price is None:
        ev.questions.append("precio no indicado")
    else:
        if ev.price.total > profile["max_total"]:
            ev.fails.append(f"precio {ev.price.total} > {profile['max_total']}")
        ev.star = ev.price.total <= profile["star_total"]

    if listing.kind is Kind.ROOM and ev.price and ev.price.energy_estimate:
        ev.questions.append("energías aparte (estimadas)")
    if facts.electric_heating:
        ev.questions.append("calefacción eléctrica: energías más altas")
    if facts.walkthrough_room:
        ev.questions.append("habitación de paso (průchozí)")
    if listing.kind is Kind.ROOM and facts.shared_bedroom:
        ev.fails.append("habitación compartida (2L)")

    _check_transit(ev, c, profile)
    _check_shop(ev, c, cfg)

    if facts.washing_machine is False:
        ev.fails.append("sin lavadora")
    elif facts.washing_machine is None:
        ev.questions.append("lavadora no mencionada")

    furnished = listing.furnished if listing.furnished is not None else facts.furnished
    if furnished is False:
        ev.questions.append("sin amueblar")
    if listing.point is None:
        ev.questions.append("ubicación desconocida: falta la calle")

    if ev.fails:
        ev.verdict = Verdict.FAIL
        ev.star = False
    elif ev.questions:
        ev.verdict = Verdict.MAYBE
    return ev


def _group_split(listing: Listing, profile: dict) -> tuple[int | None, list[int]]:
    """Dormitorios conocidos (None si no se sabe) y tamaños de grupo que caben en la vivienda."""
    try:
        beds = bedrooms(listing.disposition)
    except ValueError:
        # Disposición sin normalizar (p. ej. "garsonka"): se trata como desconocida
        beds = None
    if beds is None:
        return None, list(profile["people"])
    return beds, [p for p in profile["people"] if beds >= p]


def _check_group(ev: Evaluation, listing: Listing, profile: dict) -> None:
    # Los dormitorios van antes que el precio: un piso 2+1 no cabe aunque no sepamos el alquiler
    beds, options = _group_split(listing, profile)
    if beds is None:
        ev.questions.append("disposición desconocida")
    elif not options:
        ev.fails.append(f"solo {beds} dormitorios")
        return
    if ev.price is None:
        ev.questions.append("precio no indicado")
        return
    ev.per_person = {p: math.ceil(ev.price.total_for(p) / p) for p in options}
    best = min(ev.per_person.values())
    if best > profile["per_person_max"]:
        ev.fails.append(f"{best} por persona > {profile['per_person_max']}")
    elif best > profile["per_person_ok"]:
        ev.questions.append("caro, solo si compensa")


def _check_transit(ev: Evaluation, c: Candidate, profile: dict) -> None:
    trips = c.trips.get(profile["destination"])
    if trips is None:
        ev.questions.append("transporte sin calcular")
        return
    ok, trip = transit_ok(trips, profile["transit"])
    ev.trip = trip
    if trip is None:
        ev.questions.append("sin ruta en transporte público")
    elif not ok:
        _location_fail(ev, c.listing, f"transporte {trip.minutes} min")


def _check_shop(ev: Evaluation, c: Candidate, cfg: Config) -> None:
    if c.shop is None:
        if c.listing.point is not None:
            ev.questions.append("sin datos de supermercado")
        return
    _, distance = c.shop
    if distance > cfg.raw["supermarket"]["max_straight_m"]:
        _location_fail(ev, c.listing, f"supermercado a {math.ceil(distance)} m")


def _location_fail(ev: Evaluation, listing: Listing, reason: str) -> None:
    # Con solo código postal el punto es aproximado: una distancia o un trayecto pueden estar mal
    if listing.location_precision == "postcode":
        ev.questions.append(f"{reason} (ubicación aproximada)")
    else:
        ev.fails.append(reason)
