"""Rutas en transporte público con Transitous (api.transitous.org, MOTIS).

Normas de uso de Transitous: proyecto open source no comercial, User-Agent con contacto
(lo pone el cliente httpx) y atribución visible ("Rutas: Transitous" en cada mensaje).
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from pisobot.models import Point, Trip

TRANSITOUS_URL = "https://api.transitous.org/api/v5/plan"
TROLLEYBUS_ROUTE_TYPE = 800  # en el GTFS de IDS JMK; Transitous los devuelve como BUS
# Con Transitous caído cada ruta cuesta un timeout: tras dos fallos seguidos se deja de llamar en esa ejecución
MAX_CONSECUTIVE_FAILURES = 2


def _trip(itinerary: dict) -> Trip:
    legs = itinerary.get("legs", [])
    transit_legs = [leg for leg in legs if leg.get("mode") != "WALK"]
    first_walk = legs[0]["duration"] if legs and legs[0].get("mode") == "WALK" else 0
    return Trip(
        minutes=math.ceil(itinerary["duration"] / 60),
        transfers=int(itinerary.get("transfers", 0)),
        first_walk_min=math.ceil(first_walk / 60),
        modes=[
            "TROLLEYBUS" if leg.get("routeType") == TROLLEYBUS_ROUTE_TYPE else leg["mode"]
            for leg in transit_legs
        ],
        lines=[str(leg.get("routeShortName") or "") for leg in transit_legs],
    )


def parse_plan(payload: dict) -> list[Trip]:
    # MOTIS pone las opciones solo a pie en "direct", no en "itineraries"
    trips = []
    for itinerary in [*payload.get("itineraries", []), *payload.get("direct", [])]:
        try:
            trips.append(_trip(itinerary))
        except (KeyError, TypeError, ValueError, AttributeError):
            # Un itinerario malformado no debe tirar toda la respuesta
            continue
    return trips


def next_weekday_departure(now: datetime, hhmm: str, tz: str) -> datetime:
    """Siempre un día laborable futuro: el horario de hoy puede haber pasado ya."""
    zone = ZoneInfo(tz)
    hour, minute = (int(x) for x in hhmm.split(":"))
    day = now.astimezone(zone).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).astimezone(timezone.utc)


def _round(value: float) -> float:
    # ~50 m: pisos del mismo portal o del mismo edificio comparten caché
    return round(value * 2000) / 2000


class TransitClient:
    def __init__(self, http: httpx.Client, cache: dict, dest_labels: Mapping[Point, str] | None = None):
        self.http = http
        self.cache = cache
        # El caché se commitea en un repo público: los destinos sensibles (oficina) usan etiqueta, nunca coordenadas
        self.dest_labels = dest_labels or {}
        self.failures = 0

    def _key(self, origin: Point, dest: Point) -> str:
        if dest in self.dest_labels:
            dest_part = self.dest_labels[dest]
        else:
            dest_part = f"{dest.lat:.4f},{dest.lon:.4f}"
        return f"{_round(origin.lat):.4f},{_round(origin.lon):.4f}>{dest_part}"

    def trips(self, origin: Point, dest: Point, depart_utc: datetime) -> list[Trip] | None:
        key = self._key(origin, dest)
        if key in self.cache:
            return [Trip(**t) for t in self.cache[key]]
        if self.failures >= MAX_CONSECUTIVE_FAILURES:
            return None
        try:
            response = self.http.get(TRANSITOUS_URL, params={
                "fromPlace": f"{origin.lat},{origin.lon}",
                "toPlace": f"{dest.lat},{dest.lon}",
                "time": depart_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "numItineraries": 3,
            }, timeout=20)
            response.raise_for_status()
            trips = parse_plan(response.json())
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            self.failures += 1
            return None
        self.failures = 0
        # Una lista vacía también es una respuesta válida ("sin ruta"): se cachea para no repetir la petición
        self.cache[key] = [asdict(t) for t in trips]
        return trips
