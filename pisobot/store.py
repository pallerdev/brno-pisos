"""Estado persistente en data/*.json. El workflow lo commitea al acabar cada ejecución."""
from __future__ import annotations

import copy
import json
import os
from datetime import date, timedelta
from pathlib import Path

from pisobot.models import Listing

_DEFAULTS = {
    "seen.json": {"ids": {}, "fingerprints": {}},
    "cache.json": {"transit": {}, "geo": {}},
    "state.json": {
        "revisar_last_id": None,
        "revisar_retries": {},
        "pending": {},
        "last_digest": None,
        "failures": {},
        "failure_alerted": {},
        "transit_retries": {},
        "deliveries": {},
        "llm": {"date": None, "count": 0},
    },
}


def fingerprint(listing: Listing) -> str | None:
    """Mismo piso en varios portales: precio redondeado + m² + coordenada a ~100 m."""
    if listing.rent is None or listing.area_m2 is None or listing.lat is None or listing.lon is None:
        return None
    if listing.location_precision != "exact":
        return None
    total = listing.rent + (listing.fees or 0)
    # Un 0 o negativo viene de un dato roto; mejor no deduplicar que colapsar anuncios distintos
    if total <= 0 or listing.area_m2 <= 0:
        return None
    # int() evita que 17500.0 y 17500 generen claves distintas
    return f"{int(round(total, -2))}|{int(round(listing.area_m2))}|{round(listing.lat, 3)}|{round(listing.lon, 3)}"


class Store:
    def __init__(self, data_dir: Path, *, persist: bool = True):
        self.dir = data_dir
        # En --dry-run el estado se lee pero nunca se escribe, ni en los guardados intermedios de una ejecución
        self.persist = persist
        self.seen = self._load("seen.json")
        self.cache = self._load("cache.json")
        self.state = self._load("state.json")

    def _load(self, name: str) -> dict:
        default = copy.deepcopy(_DEFAULTS[name])
        path = self.dir / name
        if not path.exists():
            return default
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            # No se resetea en silencio: un estado vacío volvería a notificar todos los anuncios
            raise ValueError(f"No se puede leer {path}: {exc}") from exc
        for key, value in default.items():
            if key not in data:
                data[key] = value
            elif isinstance(value, dict):
                # Un nivel de profundidad: añade las claves que falten en diccionarios anidados (p. ej. llm.count)
                for sub_key, sub_value in value.items():
                    data[key].setdefault(sub_key, sub_value)
        return data

    def is_new(self, listing: Listing) -> bool:
        if listing.key in self.seen["ids"]:
            return False
        fp = fingerprint(listing)
        return fp is None or fp not in self.seen["fingerprints"]

    def mark_seen(self, listing: Listing, today: date) -> None:
        self.seen["ids"][listing.key] = today.isoformat()
        fp = fingerprint(listing)
        if fp:
            self.seen["fingerprints"][fp] = today.isoformat()

    def refresh_seen(self, listing: Listing, today: date) -> None:
        """Renueva los ids y las huellas ya vistos mientras los portales aún los publiquen."""
        stamp = today.isoformat()
        self.seen["ids"][listing.key] = stamp
        fp = fingerprint(listing)
        if fp in self.seen["fingerprints"]:
            self.seen["fingerprints"][fp] = stamp

    def prune(self, today: date, days: int = 60) -> None:
        limit = (today - timedelta(days=days)).isoformat()
        for table in ("ids", "fingerprints"):
            self.seen[table] = {k: v for k, v in self.seen[table].items() if v >= limit}

    def save(self) -> None:
        if not self.persist:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        # Escritura a .tmp + os.replace: un corte a mitad no deja un JSON truncado.
        # La cola debe guardarse antes que los ids vistos: un corte entre archivos conserva los avisos pendientes.
        for name, data in (("state.json", self.state), ("seen.json", self.seen), ("cache.json", self.cache)):
            path = self.dir / name
            tmp = path.with_name(name + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8",
            )
            os.replace(tmp, path)
