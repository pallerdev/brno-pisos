"""Carga de config.yaml y de los secretos (variables de entorno o .env en local)."""
from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from pisobot.models import Point

SECRET_KEYS = (
    "DISCORD_WEBHOOK_YO",
    "DISCORD_WEBHOOK_GRUPO",
    "DISCORD_BOT_TOKEN",
    "DISCORD_REVISAR_CHANNEL_ID",
    "GEMINI_API_KEY",
    "WORK_LATLON",
)

@dataclass
class Config:
    raw: dict
    destinations: dict[str, Point]
    secrets: dict[str, str]

    @property
    def profiles(self) -> dict[str, dict]:
        return self.raw["profiles"]

def parse_latlon(raw: str) -> Point:
    lat, lon = (float(part.strip()) for part in raw.split(","))
    return Point(lat, lon)

def load_dotenv(path: Path, env: MutableMapping[str, str] = os.environ) -> None:
    """Carga KEY=VALUE sin pisar lo ya definido: en Actions mandan los Secrets."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env.setdefault(key.strip(), value.strip())

def load_config(path: Path, env: Mapping[str, str] = os.environ) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    destinations: dict[str, Point] = {}
    for name, dest in raw["destinations"].items():
        if "env" in dest:
            value = env.get(dest["env"], "")
            if not value:
                raise ValueError(f"Falta la variable {dest['env']} para el destino '{name}'")
            destinations[name] = parse_latlon(value)
        else:
            destinations[name] = Point(float(dest["lat"]), float(dest["lon"]))
    secrets = {key: env.get(key, "") for key in SECRET_KEYS}
    return Config(raw=raw, destinations=destinations, secrets=secrets)
