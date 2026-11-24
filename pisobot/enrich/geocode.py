"""Geocodificación con Nominatim (OSM). Política: máximo 1 petición por segundo."""
from __future__ import annotations

import time
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass

import httpx

from pisobot.models import Point

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


@dataclass
class _RequestTiming:
    last: float | None = None


class Geocoder:
    def __init__(self, http: httpx.Client, cache: dict,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 *, min_interval: float = 1.0, endpoint: str = NOMINATIM_URL):
        self.http = http
        self.cache = cache
        self.sleep = sleep
        self.clock = clock
        self.min_interval = min_interval
        self.endpoint = endpoint
        self._timing = _RequestTiming()

    def with_cache(self, cache: dict) -> Geocoder:
        """Separa el caché de un uso sin duplicar su turno de peticiones al servidor."""
        geocoder = copy(self)
        geocoder.cache = cache
        return geocoder

    def geocode(self, query: str) -> Point | None:
        key = " ".join(query.lower().split())
        if key in self.cache:
            value = self.cache[key]
            return Point(*value) if value else None
        if self._timing.last is not None:
            wait = self.min_interval - (self.clock() - self._timing.last)
            if wait > 0:
                self.sleep(wait)
        try:
            # Se marca antes de la petición: también cuenta si falla por timeout
            self._timing.last = self.clock()
            response = self.http.get(self.endpoint, params={
                "q": query, "format": "jsonv2", "limit": 1, "countrycodes": "cz",
            }, timeout=20)
            response.raise_for_status()
            results = response.json()
            if not results:
                self.cache[key] = None
                return None
            point = Point(float(results[0]["lat"]), float(results[0]["lon"]))
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            # Un fallo de Nominatim no debe abortar la ejecución: no se cachea para reintentar luego
            return None
        self.cache[key] = [point.lat, point.lon]
        return point
