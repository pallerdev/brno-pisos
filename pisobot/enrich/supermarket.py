"""Supermercados de OpenStreetMap. Se descargan una vez por semana y se miden en local."""
from __future__ import annotations

import json
import math
import sys
from datetime import date
from pathlib import Path

import httpx

from pisobot.models import Point, Shop

OVERPASS_URL = "https://overpass-api.de/api/interpreter"


def haversine_m(a: Point, b: Point) -> float:
    r = 6_371_000
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def fetch_supermarkets(http: httpx.Client, bbox: list[float]) -> list[Shop]:
    south, west, north, east = bbox
    query = f'[out:json][timeout:60];nwr["shop"="supermarket"]({south},{west},{north},{east});out center tags;'
    response = http.post(OVERPASS_URL, data={"data": query}, timeout=90)
    response.raise_for_status()
    body = response.json()
    # Overpass puede responder 200 con un "remark" de error (p. ej. timeout) y sin elementos
    if not isinstance(body, dict) or "remark" in body:
        raise ValueError("Respuesta de Overpass no válida")
    elements = body.get("elements")
    if not isinstance(elements, list):
        raise ValueError("Respuesta de Overpass sin elementos")
    shops = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        lat = element.get("lat", element.get("center", {}).get("lat"))
        lon = element.get("lon", element.get("center", {}).get("lon"))
        if lat is None or lon is None:
            continue
        tags = element.get("tags", {})
        shops.append(Shop(name=tags.get("brand") or tags.get("name") or "Supermercado", lat=lat, lon=lon))
    # Brno tiene cientos de supermercados: cero siempre es un fallo, no un resultado
    if not shops:
        raise ValueError("Overpass no devolvió supermercados")
    return shops


def load_or_refresh(path: Path, http: httpx.Client, bbox: list[float], refresh_days: int, today: date) -> list[Shop]:
    stored = _read_stored(path)
    if stored and (today - stored["fetched"]).days < refresh_days:
        return stored["shops"]
    # Cada petición puede tardar 90 s y el bot corre cada 15 min: un fallo por día basta
    if stored and stored["attempted"] == today:
        return stored["shops"]
    try:
        shops = fetch_supermarkets(http, bbox)
    except (httpx.HTTPError, ValueError) as exc:
        # Sin esta línea un fallo de Overpass en Actions no deja rastro en el log
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        print(f"overpass: {f'HTTP {status}' if status else type(exc).__name__}", file=sys.stderr)
        # Overpass falla a veces: mejor datos de hace unas semanas que ninguno
        if stored is None:
            return []
        _write(path, stored["fetched"], stored["shops"], attempted=today)
        return stored["shops"]
    _write(path, today, shops)
    return shops


def nearest(shops: list[Shop], point: Point) -> tuple[Shop, float] | None:
    if not shops:
        return None
    return min(((s, haversine_m(point, Point(s.lat, s.lon))) for s in shops), key=lambda pair: pair[1])


def _read_stored(path: Path) -> dict | None:
    """Lee el fichero guardado. Si está corrupto o incompleto, se trata como si no existiera."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        attempted = raw.get("attempted")
        return {
            "fetched": date.fromisoformat(raw["fetched"]),
            "attempted": date.fromisoformat(attempted) if attempted else None,
            "shops": [Shop(**s) for s in raw["shops"]],
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _write(path: Path, fetched: date, shops: list[Shop], attempted: date | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "fetched": fetched.isoformat(),
        "attempted": attempted.isoformat() if attempted else None,
        "shops": [s.__dict__ for s in shops],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
