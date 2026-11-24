# brno-pisos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bot gratuito en GitHub Actions que vigila portales de alquiler de Brno, evalúa cada anuncio contra dos perfiles (`yo`, `grupo`) y avisa por Discord; además evalúa a demanda lo que el usuario pega en `#revisar`.

**Architecture:** Un proceso Python por ejecución (cron cada 15 min). Recolectores por portal producen `Listing` normalizados; se deduplican, prefiltran por precio, se enriquecen (Transitous, supermercados OSM, Nominatim, Gemini) y se evalúan con reglas puras. El estado vive en `data/*.json`, que el workflow commitea.

**Tech Stack:** Python ≥3.12, `httpx`, `selectolax`, `pyyaml`, `pytest`. Servicios: Transitous, Overpass, Nominatim, Gemini API (capa gratuita), Discord (webhooks + bot REST).

**Spec:** `docs/superpowers/specs/2026-11-24-brno-pisos-design.md`

## Global Constraints

- Python `>=3.12` (CI usa 3.12; en local hay 3.14). Dependencias de runtime SOLO `httpx`, `selectolax`, `pyyaml`; dev SOLO `pytest`. Sin SDKs (Gemini y Discord vía REST con `httpx`).
- selectolax ≥1.0 ya no trae `selectolax.parser.HTMLParser` (ImportError). Importar siempre `from selectolax.lexbor import LexborHTMLParser as HTMLParser`. Texto de nodos con `<br>`/párrafos: `node.text(separator="\n", strip=True, skip_empty=True)`; texto en línea: `" ".join(node.text().split())`.
- Respetar `robots.txt`: nada de `api.bezrealitky.cz/graphql`, nada de rastrear Sreality (solo una URL pegada por el usuario en `#revisar`), en Bazoš nada de parámetros de filtro en la URL.
- `User-Agent` de todas las peticiones: el valor `user_agent` de `config.yaml`. Nunca incluir email ni datos personales del usuario en peticiones.
- Tests sin red: `httpx.MockTransport` o fixtures en `tests/fixtures/`.
- Identificadores en inglés; comentarios, mensajes de Discord y textos de usuario en español. Comentarios solo para explicar el porqué, con densidad baja.
- **No hacer `git commit` durante la ejecución.** Cada tarea termina con la suite de tests en verde; el controlador enseña el diff al usuario y le pide permiso antes de cualquier commit. Mensajes de commit sin atribución a IA.
- Comando de tests: `.venv/bin/pytest -q` desde la raíz del repo.

## File Structure

```
brno-pisos/
  pyproject.toml              dependencias + config de pytest
  config.yaml                 umbrales, perfiles, destinos públicos
  .env.example                nombres de los secretos (sin valores)
  LICENSE                     MIT
  README.md                   puesta en marcha
  pisobot/
    __init__.py
    __main__.py               punto de entrada: python -m pisobot
    models.py                 Listing, Trip, Shop, Point, Kind, Verdict
    config.py                 load_config, load_dotenv, parse_latlon
    http.py                   make_client (cliente httpx con UA)
    parse_text.py             TextFacts, parse_text, parse_price, normalize_disposition, bedrooms
    profiles.py               Price, Candidate, Evaluation, price_of, prefilter, transit_ok, evaluate
    store.py                  Store, fingerprint
    llm.py                    Gemini, LlmResult, merge_facts
    notify.py                 build_embed, send_webhook, chunk
    review.py                 DiscordBot, handle_message
    pipeline.py               run (orquestación de una ejecución)
    enrich/
      __init__.py
      transit.py              TransitClient, parse_plan, next_weekday_departure
      supermarket.py          fetch_supermarkets, load_or_refresh, nearest, haversine_m
      geocode.py              Geocoder
    collectors/
      __init__.py             build_collectors
      base.py                 Collector (Protocol), helpers de HTML y precio
      bezrealitky.py
      ulovdomov.py
      espolubydleni.py
      idnes.py
      bazos.py
      single_url.py           una URL pegada en #revisar
  data/                       estado persistente (lo commitea el workflow)
  tests/
    fixtures/                 respuestas reales guardadas (ya copiadas)
    test_*.py
  .github/workflows/run.yml
  .github/workflows/test.yml
```

Fixtures ya presentes en `tests/fixtures/` (copiadas de las sondas del 2026-11-24): `bezrealitky_list.html`, `bezrealitky_detail.html`, `ulovdomov_flats.json`, `ulovdomov_rooms.json`, `transitous_plan.json`, `sreality_detail.json`, `espolubydleni_list.html`, `espolubydleni_detail.html`, `idnes_list.html`, `idnes_detail.html`, `bazos_list.html`, `bazos_detail.html`.

---

### Task 1: Esqueleto, modelos y configuración

**Files:**
- Create: `pyproject.toml`, `config.yaml`, `.env.example`, `pisobot/__init__.py`, `pisobot/models.py`, `pisobot/config.py`, `pisobot/http.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `pisobot.models`: `Kind(ROOM="room", FLAT="flat")`, `Verdict(PASS, MAYBE, FAIL)`, `Point(lat: float, lon: float)` (frozen), `Listing` (campos abajo, propiedades `key -> str` y `point -> Point | None`), `Trip(minutes, transfers, first_walk_min, modes: list[str], lines: list[str])`, `Shop(name, lat, lon)`.
  - `pisobot.config`: `Config(raw: dict, destinations: dict[str, Point], secrets: dict[str, str])` con propiedad `profiles -> dict[str, dict]`; `load_config(path: Path, env: Mapping[str, str] = os.environ) -> Config`; `load_dotenv(path: Path, env: MutableMapping[str, str] = os.environ) -> None`; `parse_latlon(raw: str) -> Point`; `SECRET_KEYS: tuple[str, ...]`.
  - `pisobot.http`: `make_client(user_agent: str) -> httpx.Client`.

- [ ] **Step 1: Crear `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "brno-pisos"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["httpx>=0.27", "selectolax>=1.0", "pyyaml>=6.0"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.setuptools.packages.find]
include = ["pisobot*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Crear el entorno e instalar**

Run: `python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"`
Expected: termina sin errores.

- [ ] **Step 3: Crear `config.yaml`**

```yaml
# Umbrales y parámetros del bot. Los secretos (webhooks, tokens, coordenadas de la oficina)
# van en .env en local y en GitHub Secrets en Actions.
user_agent: "brno-pisos/0.1 (+https://github.com/pallerdev/brno-pisos)"

departure:
  time: "08:00"
  tz: "Europe/Prague"

destinations:
  work:
    env: WORK_LATLON        # en secreto: el repo es público
    label: "oficina"
  station:
    lat: 49.1906
    lon: 16.6127
    label: "hlavní nádraží"

landmarks:
  luzanky:
    lat: 49.2068
    lon: 16.6083
    radius_m: 1000

supermarket:
  max_straight_m: 640       # ≈ 10 min andando con factor de rodeo 1,3
  bbox: [49.10, 16.45, 49.30, 16.75]
  refresh_days: 7

energy_estimates:           # CZK/mes, cifras a ojo; ajustar con la experiencia
  room: 800
  "1+kk": 1000
  "1+1": 1000
  "2+kk": 1500
  "2+1": 1500
  "3+kk": 2000
  "3+1": 2500
  "4+kk": 2500
  "4+1": 3000
  "5+kk": 3000
  default: 2500

profiles:
  yo:
    webhook_env: DISCORD_WEBHOOK_YO
    kinds: [room, flat]
    max_total: 10000
    star_total: 8000
    destination: work
    transit:
      max_minutes: 30
      direct:
        modes: [TRAM]
        max_minutes: 40
        max_walk_minutes: 10
  grupo:
    webhook_env: DISCORD_WEBHOOK_GRUPO
    kinds: [flat]
    people: [3, 4]
    per_person_ok: 11000
    per_person_max: 12000
    destination: station
    transit:
      max_minutes: 30

digest:
  hour: 19

llm:
  model: "gemini-2.5-flash"
  daily_limit: 200

collectors:
  bazos_psc_prefixes: ["60", "61", "62", "63", "64"]
  failure_alert_after: 3
```

- [ ] **Step 4: Crear `.env.example`**

```
DISCORD_WEBHOOK_YO=
DISCORD_WEBHOOK_GRUPO=
DISCORD_BOT_TOKEN=
DISCORD_REVISAR_CHANNEL_ID=
GEMINI_API_KEY=
WORK_LATLON=
```

- [ ] **Step 5: Escribir el test que falla** — `tests/test_config.py`

```python
from pathlib import Path

import pytest

from pisobot.config import load_config, load_dotenv, parse_latlon
from pisobot.models import Kind, Listing, Point

ROOT = Path(__file__).resolve().parents[1]


def test_parse_latlon():
    assert parse_latlon(" 49.19, 16.61 ") == Point(49.19, 16.61)


def test_load_config_resolves_destinations_from_env():
    cfg = load_config(ROOT / "config.yaml", env={"WORK_LATLON": "49.18,16.60"})
    assert cfg.destinations["work"] == Point(49.18, 16.60)
    assert cfg.destinations["station"] == Point(49.1906, 16.6127)
    assert cfg.profiles["yo"]["max_total"] == 10000
    assert cfg.secrets["DISCORD_BOT_TOKEN"] == ""


def test_load_config_fails_without_work_coordinates():
    with pytest.raises(ValueError, match="WORK_LATLON"):
        load_config(ROOT / "config.yaml", env={})


def test_load_dotenv_does_not_override_existing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("A_KEY=from_file\n# comentario\nB_KEY=b\n\n", encoding="utf-8")
    env = {"A_KEY": "from_env"}
    load_dotenv(env_file, env)
    assert env == {"A_KEY": "from_env", "B_KEY": "b"}


def test_listing_key_and_point():
    listing = Listing(source="bezrealitky", source_id="42", url="u", title="t", kind=Kind.ROOM, rent=7000)
    assert listing.key == "bezrealitky:42"
    assert listing.point is None
    listing.lat, listing.lon = 49.2, 16.6
    assert listing.point == Point(49.2, 16.6)
```

- [ ] **Step 6: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: FAIL con `ModuleNotFoundError: No module named 'pisobot'`.

- [ ] **Step 7: Implementar** — `pisobot/__init__.py` vacío y `pisobot/models.py`:

```python
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
```

`pisobot/config.py`:

```python
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
```

`pisobot/http.py`:

```python
import httpx


def make_client(user_agent: str) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent, "Accept-Language": "cs,en;q=0.8"},
        timeout=30,
        follow_redirects=True,
    )
```

- [ ] **Step 8: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: `5 passed`.

---

### Task 2: Lectura de texto en checo

**Files:**
- Create: `pisobot/parse_text.py`
- Test: `tests/test_parse_text.py`

**Interfaces:**
- Produces: `TextFacts(washing_machine: bool|None=None, energy_included: bool|None=None, furnished: bool|None=None, walkthrough_room: bool=False, electric_heating: bool=False, commission: bool|None=None, shared_bedroom: bool=False)`; `parse_text(text: str) -> TextFacts`; `parse_price(text: str) -> int | None`; `parse_area(text: str) -> int | None`; `normalize_disposition(raw: str | None) -> str | None` (devuelve `"N+kk"` o `"N+1"`); `bedrooms(disposition: str | None) -> int | None`; `norm(text: str) -> str` (minúsculas sin diacríticos).
- `shared_bedroom`: el usuario quiere habitación propia; en eSpolubydleni abundan "místo v 2L pokoji" (cama en habitación doble).

- [ ] **Step 1: Escribir el test que falla** — `tests/test_parse_text.py`

```python
import pytest

from pisobot.parse_text import bedrooms, normalize_disposition, parse_area, parse_price, parse_text


@pytest.mark.parametrize("text,expected", [
    ("V bytě je pračka a myčka.", True),
    ("Automatická pračka ve společných prostorách", True),
    ("K dispozici pračkosušička", True),
    ("Washing machine in the flat", True),
    ("Byt je bez pračky.", False),
    ("Krásný slunný pokoj", None),
])
def test_washing_machine(text, expected):
    assert parse_text(text).washing_machine is expected


@pytest.mark.parametrize("text,expected", [
    ("Cena 8 500 Kč včetně energií a internetu", True),
    ("Nájem 7000 Kč, vše v ceně", True),
    ("All inclusive, utilities included", True),
    ("Nájem 12 000 Kč + energie dle spotřeby", False),
    ("Elektřina na vlastní smlouvu", False),
    ("Včetně poplatků, energie zvlášť", False),
    ("Nájem 9000 Kč + poplatky 2000 Kč", None),
])
def test_energy_included(text, expected):
    assert parse_text(text).energy_included is expected


@pytest.mark.parametrize("text,expected", [
    ("Byt je plně zařízený", True),
    ("Pokoj je vybavený nábytkem", True),
    ("Nezařízený byt, jen kuchyňská linka", False),
    ("Byt je nevybavený", False),
    ("Unfurnished apartment", False),
    ("Byt 2+kk v Žabovřeskách", None),
])
def test_furnished(text, expected):
    assert parse_text(text).furnished is expected


def test_flags():
    facts = parse_text("Průchozí pokoj, přímotop, bez provize")
    assert facts.walkthrough_room is True
    assert facts.electric_heating is True
    assert facts.commission is False
    assert parse_text("Provize RK 1 nájem").commission is True
    assert parse_text("Elektrické topení v celém bytě").electric_heating is True
    assert parse_text("Klidný byt").commission is None


@pytest.mark.parametrize("text,expected", [
    ("Nabízím 2 místa v 2L pokoji 17m2", True),
    ("Volné místo ve dvoulůžkovém pokoji", True),
    ("Pokoj 1L pro jednu osobu", False),
    ("Samostatný pokoj 14 m2", False),
])
def test_shared_bedroom(text, expected):
    assert parse_text(text).shared_bedroom is expected


@pytest.mark.parametrize("text,expected", [
    ("pronájem bytu 2+kk 50 m²", 50),
    ("Pokoj 17m2", 17),
    ("byt 58,5 m2", 58),
    ("bez plochy", None),
])
def test_parse_area(text, expected):
    assert parse_area(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("8 500 Kč", 8500),
    ("12.000,- Kč", 12000),
    ("Cena: 9500 CZK/měsíc", 9500),
    ("7 900 Kč", 7900),
    ("3+1 75 m2 15000 Kč", 15000),
    ("Dohodou", None),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize("raw,expected", [
    ("DISP_3_1", "3+1"),
    ("DISP_1_KK", "1+kk"),
    ("threePlusOne", "3+1"),
    ("onePlusKk", "1+kk"),
    ("Pronájem bytu 2+1 50 m2", "2+1"),
    ("4 + KK", "4+kk"),
    ("pronájem bytu 2KK", "2+kk"),
    ("byt 1.kk", "1+kk"),
    ("byt 2 + 1, 20 000 Kč", "2+1"),
    ("Pronájem 20 000 Kč", None),
    ("garsoniéra", "1+kk"),
    ("atypical", None),
    (None, None),
])
def test_normalize_disposition(raw, expected):
    assert normalize_disposition(raw) == expected


@pytest.mark.parametrize("disposition,expected", [
    ("3+1", 3), ("4+kk", 3), ("4+1", 4), ("5+kk", 4), ("1+kk", 1), ("2+kk", 1), (None, None),
])
def test_bedrooms(disposition, expected):
    assert bedrooms(disposition) == expected
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_parse_text.py -q`
Expected: FAIL con `ModuleNotFoundError: No module named 'pisobot.parse_text'`.

- [ ] **Step 3: Implementar** — `pisobot/parse_text.py`

```python
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
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_parse_text.py -q`
Expected: todos pasan. Si alguna frase falla, ajustar el patrón (no el test): los tests son frases reales del dominio.

---

### Task 3: Perfiles y evaluación

**Files:**
- Create: `pisobot/profiles.py`
- Test: `tests/test_profiles.py`

**Interfaces:**
- Consumes: `Listing`, `Kind`, `Trip`, `Shop`, `Verdict` (Task 1); `Config` (Task 1); `TextFacts`, `bedrooms` (Task 2).
- Produces:
  - `Price(total: int, rent: int, fees: int, energy_estimate: int, energy_included: bool)`
  - `Candidate(listing: Listing, facts: TextFacts, trips: dict[str, list[Trip] | None] = {}, shop: tuple[Shop, float] | None = None, summary: str = "")` — `trips` va indexado por nombre de destino (`"work"`, `"station"`); ausente o `None` = no calculado.
  - `Evaluation(profile: str, verdict: Verdict, star: bool=False, price: Price|None=None, per_person: dict[int,int]={}, trip: Trip|None=None, fails: list[str]=[], questions: list[str]=[])`
  - `price_of(listing, facts, estimates: dict) -> Price | None`
  - `prefilter(listing, facts, cfg) -> list[str]` (nombres de perfil que podrían encajar)
  - `transit_ok(trips: list[Trip], rule: dict) -> tuple[bool, Trip | None]`
  - `evaluate(candidate, profile_name: str, cfg) -> Evaluation`

- [ ] **Step 1: Escribir el test que falla** — `tests/test_profiles.py`

```python
from pathlib import Path

import pytest

from pisobot.config import load_config
from pisobot.models import Kind, Listing, Shop, Trip, Verdict
from pisobot.parse_text import TextFacts
from pisobot.profiles import Candidate, evaluate, prefilter, price_of, transit_ok

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg():
    return load_config(ROOT / "config.yaml", env={"WORK_LATLON": "49.18,16.60"})


def make(**kw) -> Listing:
    base = dict(source="t", source_id="1", url="https://x", title="t", kind=Kind.ROOM, rent=7000, lat=49.2, lon=16.6)
    base.update(kw)
    return Listing(**base)


FAST = Trip(minutes=20, transfers=0, first_walk_min=4, modes=["TRAM"], lines=["12"])
NEAR_SHOP = (Shop("Albert", 49.2, 16.6), 300.0)
GOOD = TextFacts(washing_machine=True)


def cand(listing, facts=GOOD, trip=FAST, shop=NEAR_SHOP):
    trips = {"work": [trip], "station": [trip]} if trip else {}
    return Candidate(listing=listing, facts=facts, trips=trips, shop=shop)


# --- precio ---

def test_room_price_without_energy_info_adds_nothing(cfg):
    p = price_of(make(rent=7000, fees=1000), TextFacts(), cfg.raw["energy_estimates"])
    assert (p.total, p.energy_estimate) == (8000, 0)


def test_room_with_separate_energy_adds_room_estimate(cfg):
    p = price_of(make(rent=7000), TextFacts(energy_included=False), cfg.raw["energy_estimates"])
    assert (p.total, p.energy_estimate) == (7800, 800)


def test_flat_without_energy_info_adds_disposition_estimate(cfg):
    p = price_of(make(kind=Kind.FLAT, rent=30000, fees=3000, disposition="4+kk"), TextFacts(), cfg.raw["energy_estimates"])
    assert (p.total, p.energy_estimate) == (35500, 2500)


def test_flat_with_energy_in_fees_adds_nothing(cfg):
    listing = make(kind=Kind.FLAT, rent=30000, fees=5000, disposition="3+1", fees_include_energy=True)
    assert price_of(listing, TextFacts(), cfg.raw["energy_estimates"]).total == 35000


def test_price_none_without_rent(cfg):
    assert price_of(make(rent=None), TextFacts(), cfg.raw["energy_estimates"]) is None


# --- transporte ---

YO_RULE = {"max_minutes": 30, "direct": {"modes": ["TRAM"], "max_minutes": 40, "max_walk_minutes": 10}}


@pytest.mark.parametrize("trip,ok", [
    (Trip(25, 1, 3, ["BUS", "TRAM"], ["44", "4"]), True),
    (Trip(35, 0, 5, ["TRAM"], ["4"]), True),
    (Trip(35, 1, 5, ["TRAM", "TRAM"], ["4", "1"]), False),
    (Trip(35, 0, 5, ["BUS"], ["44"]), False),
    (Trip(35, 0, 12, ["TRAM"], ["4"]), False),
    (Trip(45, 0, 5, ["TRAM"], ["4"]), False),
])
def test_transit_rule_yo(trip, ok):
    assert transit_ok([trip], YO_RULE)[0] is ok


def test_transit_rule_without_direct_option():
    assert transit_ok([Trip(35, 0, 5, ["TRAM"], ["4"])], {"max_minutes": 30})[0] is False


def test_transit_picks_direct_tram_even_if_not_fastest():
    slow_direct = Trip(38, 0, 6, ["TRAM"], ["4"])
    fast_transfer = Trip(33, 2, 2, ["BUS", "TRAM", "BUS"], ["1", "2", "3"])
    ok, trip = transit_ok([fast_transfer, slow_direct], YO_RULE)
    assert ok and trip is slow_direct


def test_transit_empty_list():
    assert transit_ok([], YO_RULE) == (False, None)


# --- evaluación yo ---

def test_yo_pass_with_star(cfg):
    ev = evaluate(cand(make(rent=7000, fees=800)), "yo", cfg)
    assert ev.verdict is Verdict.PASS and ev.star


def test_yo_fail_price(cfg):
    ev = evaluate(cand(make(rent=9500, fees=1500)), "yo", cfg)
    assert ev.verdict is Verdict.FAIL
    assert "precio" in ev.fails[0]


def test_yo_maybe_when_washer_unknown(cfg):
    ev = evaluate(cand(make(), facts=TextFacts()), "yo", cfg)
    assert ev.verdict is Verdict.MAYBE
    assert "lavadora no mencionada" in ev.questions


def test_yo_fail_without_washer(cfg):
    assert evaluate(cand(make(), facts=TextFacts(washing_machine=False)), "yo", cfg).verdict is Verdict.FAIL


def test_yo_maybe_when_transit_not_computed(cfg):
    ev = evaluate(cand(make(), trip=None), "yo", cfg)
    assert ev.verdict is Verdict.MAYBE
    assert "transporte sin calcular" in ev.questions


def test_yo_fail_supermarket_far(cfg):
    ev = evaluate(cand(make(), shop=(Shop("Lidl", 49.2, 16.6), 900.0)), "yo", cfg)
    assert ev.verdict is Verdict.FAIL


def test_yo_maybe_unfurnished(cfg):
    ev = evaluate(cand(make(furnished=False)), "yo", cfg)
    assert "sin amueblar" in ev.questions


def test_yo_fail_shared_bedroom(cfg):
    ev = evaluate(cand(make(), facts=TextFacts(washing_machine=True, shared_bedroom=True)), "yo", cfg)
    assert ev.verdict is Verdict.FAIL
    assert "habitación compartida (2L)" in ev.fails


# --- evaluación grupo ---

def test_grupo_four_bedrooms_uses_best_split(cfg):
    listing = make(kind=Kind.FLAT, rent=37000, fees=3000, fees_include_energy=True, disposition="4+1")
    ev = evaluate(cand(listing), "grupo", cfg)
    assert ev.per_person == {3: 13334, 4: 10000}
    assert ev.verdict is Verdict.PASS


def test_grupo_expensive_is_maybe(cfg):
    listing = make(kind=Kind.FLAT, rent=31000, fees=3500, fees_include_energy=True, disposition="3+1")
    ev = evaluate(cand(listing), "grupo", cfg)
    assert ev.per_person == {3: 11500}
    assert "caro, solo si compensa" in ev.questions


def test_grupo_too_few_bedrooms_fails(cfg):
    listing = make(kind=Kind.FLAT, rent=20000, fees_include_energy=True, disposition="3+kk")
    ev = evaluate(cand(listing), "grupo", cfg)
    assert ev.verdict is Verdict.FAIL


def test_grupo_rejects_rooms(cfg):
    assert evaluate(cand(make(kind=Kind.ROOM)), "grupo", cfg).verdict is Verdict.FAIL


def test_grupo_walkthrough_is_maybe(cfg):
    listing = make(kind=Kind.FLAT, rent=27000, fees=3000, fees_include_energy=True, disposition="3+1")
    ev = evaluate(cand(listing, facts=TextFacts(washing_machine=True, walkthrough_room=True)), "grupo", cfg)
    assert "habitación de paso (průchozí)" in ev.questions


# --- prefiltro ---

def test_prefilter(cfg):
    assert prefilter(make(rent=12000), TextFacts(), cfg) == []
    assert prefilter(make(rent=9000), TextFacts(), cfg) == ["yo"]
    flat = make(kind=Kind.FLAT, rent=30000, fees=3000, disposition="4+kk")
    assert prefilter(flat, TextFacts(), cfg) == ["grupo"]
    assert prefilter(make(rent=7000), TextFacts(washing_machine=False), cfg) == []
    assert prefilter(make(rent=None), TextFacts(), cfg) == ["yo"]
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_profiles.py -q`
Expected: FAIL con `ModuleNotFoundError: No module named 'pisobot.profiles'`.

- [ ] **Step 3: Implementar** — `pisobot/profiles.py`

```python
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
        if price is not None:
            if "people" in profile:
                beds = bedrooms(listing.disposition)
                options = [p for p in profile["people"] if beds is None or beds >= p]
                if not options or price.total / max(options) > profile["per_person_max"]:
                    continue
            elif price.total > profile["max_total"]:
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
    if ev.price is None:
        ev.questions.append("precio no indicado")
    elif "people" in profile:
        _check_group_price(ev, listing, profile)
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
    elif ev.questions:
        ev.verdict = Verdict.MAYBE
    return ev


def _check_group_price(ev: Evaluation, listing: Listing, profile: dict) -> None:
    beds = bedrooms(listing.disposition)
    if beds is None:
        ev.questions.append("disposición desconocida")
        options = list(profile["people"])
    else:
        options = [p for p in profile["people"] if beds >= p]
        if not options:
            ev.fails.append(f"solo {beds} dormitorios")
            return
    ev.per_person = {p: math.ceil(ev.price.total / p) for p in options}
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
        ev.fails.append(f"transporte {trip.minutes} min")


def _check_shop(ev: Evaluation, c: Candidate, cfg: Config) -> None:
    if c.shop is None:
        if c.listing.point is not None:
            ev.questions.append("sin datos de supermercado")
        return
    _, distance = c.shop
    if distance > cfg.raw["supermarket"]["max_straight_m"]:
        ev.fails.append(f"supermercado a {round(distance)} m")
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_profiles.py -q`
Expected: todos pasan.

---

### Task 4: Transporte con Transitous

**Files:**
- Create: `pisobot/enrich/__init__.py` (vacío), `pisobot/enrich/transit.py`
- Test: `tests/test_transit.py`

**Interfaces:**
- Consumes: `Trip`, `Point` (Task 1).
- Produces: `TRANSITOUS_URL`; `parse_plan(payload: dict) -> list[Trip]`; `next_weekday_departure(now: datetime, hhmm: str, tz: str) -> datetime` (UTC, aware); `TransitClient(http: httpx.Client, cache: dict)` con `trips(origin: Point, dest: Point, depart_utc: datetime) -> list[Trip] | None` (`None` = fallo de red/HTTP; lista vacía = sin itinerarios). `cache` es el dict `store.cache["transit"]` (Task 6), mutado in situ.

- [ ] **Step 1: Escribir el test que falla** — `tests/test_transit.py`

```python
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from pisobot.enrich.transit import TransitClient, next_weekday_departure, parse_plan
from pisobot.models import Point

FIXTURES = Path(__file__).parent / "fixtures"
PLAN = json.loads((FIXTURES / "transitous_plan.json").read_text(encoding="utf-8"))


def test_parse_plan_real_fixture():
    trips = parse_plan(PLAN)
    assert len(trips) == 4
    first = trips[0]
    assert (first.minutes, first.transfers, first.first_walk_min) == (24, 0, 4)
    assert first.modes == ["TRAM"] and first.lines == ["12"]


def test_parse_plan_marks_trolleybus_and_walk_only():
    payload = {"itineraries": [
        {"duration": 900, "transfers": 0, "legs": [
            {"mode": "WALK", "duration": 120},
            {"mode": "BUS", "routeType": 800, "routeShortName": "25", "duration": 600},
        ]},
        {"duration": 600, "transfers": 0, "legs": [{"mode": "WALK", "duration": 600}]},
    ]}
    trolley, walk = parse_plan(payload)
    assert trolley.modes == ["TROLLEYBUS"] and trolley.lines == ["25"]
    assert walk.modes == [] and walk.minutes == 10


def test_next_weekday_departure():
    tuesday = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    assert next_weekday_departure(tuesday, "08:00", "Europe/Prague") == datetime(2026, 11, 25, 7, 0, tzinfo=timezone.utc)
    friday = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)
    assert next_weekday_departure(friday, "08:00", "Europe/Prague") == datetime(2026, 11, 30, 7, 0, tzinfo=timezone.utc)


def _client(handler, cache):
    return TransitClient(httpx.Client(transport=httpx.MockTransport(handler)), cache)


def test_client_requests_and_caches():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=PLAN)

    cache = {}
    client = _client(handler, cache)
    depart = datetime(2026, 11, 25, 7, 0, tzinfo=timezone.utc)
    trips = client.trips(Point(49.21, 16.593), Point(49.1906, 16.6127), depart)
    assert len(trips) == 4
    params = calls[0].url.params
    assert params["fromPlace"] == "49.21,16.593"
    assert params["toPlace"] == "49.1906,16.6127"
    assert params["time"] == "2026-11-25T07:00:00Z"
    again = client.trips(Point(49.21001, 16.59301), Point(49.1906, 16.6127), depart)
    assert len(calls) == 1 and len(again) == 4
    assert len(cache) == 1


def test_client_returns_none_on_error_and_does_not_cache():
    cache = {}
    client = _client(lambda r: httpx.Response(503), cache)
    depart = datetime(2026, 11, 25, 7, 0, tzinfo=timezone.utc)
    assert client.trips(Point(49.2, 16.6), Point(49.19, 16.61), depart) is None
    assert cache == {}
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_transit.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/enrich/transit.py`

```python
"""Rutas en transporte público con Transitous (api.transitous.org, MOTIS).

Normas de uso de Transitous: proyecto open source no comercial, User-Agent con contacto
(lo pone el cliente httpx) y atribución visible ("Rutas: Transitous" en cada mensaje).
"""
from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from pisobot.models import Point, Trip

TRANSITOUS_URL = "https://api.transitous.org/api/v5/plan"
TROLLEYBUS_ROUTE_TYPE = 800  # en el GTFS de IDS JMK; Transitous los devuelve como BUS


def parse_plan(payload: dict) -> list[Trip]:
    trips = []
    for itinerary in payload.get("itineraries", []):
        legs = itinerary.get("legs", [])
        transit_legs = [leg for leg in legs if leg.get("mode") != "WALK"]
        first_walk = legs[0]["duration"] if legs and legs[0].get("mode") == "WALK" else 0
        trips.append(Trip(
            minutes=math.ceil(itinerary["duration"] / 60),
            transfers=int(itinerary.get("transfers", 0)),
            first_walk_min=math.ceil(first_walk / 60),
            modes=[
                "TROLLEYBUS" if leg.get("routeType") == TROLLEYBUS_ROUTE_TYPE else leg["mode"]
                for leg in transit_legs
            ],
            lines=[str(leg.get("routeShortName") or "") for leg in transit_legs],
        ))
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
    def __init__(self, http: httpx.Client, cache: dict):
        self.http = http
        self.cache = cache

    def trips(self, origin: Point, dest: Point, depart_utc: datetime) -> list[Trip] | None:
        key = f"{_round(origin.lat):.4f},{_round(origin.lon):.4f}>{dest.lat:.4f},{dest.lon:.4f}"
        if key in self.cache:
            return [Trip(**t) for t in self.cache[key]]
        try:
            response = self.http.get(TRANSITOUS_URL, params={
                "fromPlace": f"{origin.lat},{origin.lon}",
                "toPlace": f"{dest.lat},{dest.lon}",
                "time": depart_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "numItineraries": 3,
            }, timeout=20)
            response.raise_for_status()
            trips = parse_plan(response.json())
        except (httpx.HTTPError, ValueError, KeyError):
            return None
        if trips:
            self.cache[key] = [asdict(t) for t in trips]
        return trips
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_transit.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Verificación en vivo (una petición)**

Run: `.venv/bin/python -c "from datetime import datetime, timezone; from pisobot.http import make_client; from pisobot.enrich.transit import *; from pisobot.models import Point; c=TransitClient(make_client('brno-pisos/0.1 (+https://github.com/pallerdev/brno-pisos)'), {}); print(c.trips(Point(49.21,16.593), Point(49.1906,16.6127), next_weekday_departure(datetime.now(timezone.utc),'08:00','Europe/Prague')))"`
Expected: lista de `Trip` con algún `TRAM`. Si devuelve `None`, revisar los nombres de parámetros contra https://transitous.org/api/ y corregir `TransitClient` (y su test).

---

### Task 5: Supermercados y geocodificación

**Files:**
- Create: `pisobot/enrich/supermarket.py`, `pisobot/enrich/geocode.py`
- Test: `tests/test_supermarket.py`, `tests/test_geocode.py`

**Interfaces:**
- Consumes: `Shop`, `Point` (Task 1).
- Produces:
  - `supermarket`: `OVERPASS_URL`; `haversine_m(a: Point, b: Point) -> float`; `fetch_supermarkets(http, bbox: list[float]) -> list[Shop]`; `load_or_refresh(path: Path, http, bbox, refresh_days: int, today: date) -> list[Shop]`; `nearest(shops: list[Shop], point: Point) -> tuple[Shop, float] | None`.
  - `geocode`: `NOMINATIM_URL`; `Geocoder(http, cache: dict, sleep=time.sleep, clock=time.monotonic)` con `geocode(query: str) -> Point | None`. `cache` es `store.cache["geo"]`.

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_supermarket.py`:

```python
import json
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest

from pisobot.enrich.supermarket import fetch_supermarkets, haversine_m, load_or_refresh, nearest
from pisobot.models import Point, Shop

OVERPASS = {"elements": [
    {"type": "node", "lat": 49.2001, "lon": 16.6001, "tags": {"shop": "supermarket", "brand": "Albert"}},
    {"type": "way", "center": {"lat": 49.21, "lon": 16.61}, "tags": {"shop": "supermarket", "name": "Lidl Lesná"}},
    {"type": "relation", "tags": {"shop": "supermarket"}},
]}


def overpass_client(calls):
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=OVERPASS)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_haversine_known_distance():
    # 0,001 grados de latitud ≈ 111 m
    assert haversine_m(Point(49.2, 16.6), Point(49.201, 16.6)) == pytest.approx(111, abs=1)


def test_fetch_supermarkets_parses_nodes_and_ways():
    calls = []
    shops = fetch_supermarkets(overpass_client(calls), [49.10, 16.45, 49.30, 16.75])
    assert shops == [Shop("Albert", 49.2001, 16.6001), Shop("Lidl Lesná", 49.21, 16.61)]
    query = parse_qs(calls[0].content.decode())["data"][0]
    assert "(49.1,16.45,49.3,16.75)" in query


def test_nearest():
    shops = [Shop("A", 49.2, 16.6), Shop("B", 49.25, 16.6)]
    shop, distance = nearest(shops, Point(49.201, 16.6))
    assert shop.name == "A" and distance == pytest.approx(111, abs=1)
    assert nearest([], Point(49.2, 16.6)) is None


def test_load_or_refresh_uses_fresh_file(tmp_path):
    path = tmp_path / "supermarkets.json"
    path.write_text(json.dumps({"fetched": "2026-11-20", "shops": [{"name": "A", "lat": 1.0, "lon": 2.0}]}))
    calls = []
    shops = load_or_refresh(path, overpass_client(calls), [0, 0, 1, 1], 7, date(2026, 11, 24))
    assert shops == [Shop("A", 1.0, 2.0)] and calls == []


def test_load_or_refresh_refetches_stale_file(tmp_path):
    path = tmp_path / "supermarkets.json"
    path.write_text(json.dumps({"fetched": "2026-11-01", "shops": []}))
    calls = []
    shops = load_or_refresh(path, overpass_client(calls), [0, 0, 1, 1], 7, date(2026, 11, 24))
    assert len(shops) == 2 and len(calls) == 1
    assert json.loads(path.read_text())["fetched"] == "2026-11-24"


def test_load_or_refresh_keeps_old_file_on_error(tmp_path):
    path = tmp_path / "supermarkets.json"
    path.write_text(json.dumps({"fetched": "2026-11-01", "shops": [{"name": "A", "lat": 1.0, "lon": 2.0}]}))
    failing = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(504)))
    assert load_or_refresh(path, failing, [0, 0, 1, 1], 7, date(2026, 11, 24)) == [Shop("A", 1.0, 2.0)]
```

`tests/test_geocode.py`:

```python
import httpx

from pisobot.enrich.geocode import Geocoder
from pisobot.models import Point


def make(handler, cache, sleeps):
    clock = iter([0.0, 0.2, 5.0, 5.0]).__next__
    return Geocoder(httpx.Client(transport=httpx.MockTransport(handler)), cache, sleep=sleeps.append, clock=clock)


def test_geocode_hit_caches_and_throttles():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[{"lat": "49.2071", "lon": "16.5963"}])

    cache, sleeps = {}, []
    geocoder = make(handler, cache, sleeps)
    assert geocoder.geocode("Kotlářská, Brno") == Point(49.2071, 16.5963)
    assert geocoder.geocode("Božetěchova, Brno") == Point(49.2071, 16.5963)
    assert geocoder.geocode("KOTLÁŘSKÁ, Brno ") == Point(49.2071, 16.5963)
    assert len(calls) == 2
    assert calls[0].url.params["countrycodes"] == "cz"
    assert sleeps and 0 < sleeps[0] <= 1.0


def test_geocode_miss_and_error():
    cache, sleeps = {}, []
    assert make(lambda r: httpx.Response(200, json=[]), cache, sleeps).geocode("Nowhere") is None
    assert cache == {"nowhere": None}
    assert make(lambda r: httpx.Response(500), {}, []).geocode("X") is None
```

- [ ] **Step 2: Ejecutar y ver que fallan**

Run: `.venv/bin/pytest tests/test_supermarket.py tests/test_geocode.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/enrich/supermarket.py`

```python
"""Supermercados de OpenStreetMap. Se descargan una vez por semana y se miden en local."""
from __future__ import annotations

import json
import math
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
    shops = []
    for element in response.json().get("elements", []):
        lat = element.get("lat", element.get("center", {}).get("lat"))
        lon = element.get("lon", element.get("center", {}).get("lon"))
        if lat is None or lon is None:
            continue
        tags = element.get("tags", {})
        shops.append(Shop(name=tags.get("brand") or tags.get("name") or "Supermercado", lat=lat, lon=lon))
    return shops


def load_or_refresh(path: Path, http: httpx.Client, bbox: list[float], refresh_days: int, today: date) -> list[Shop]:
    stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if stored and (today - date.fromisoformat(stored["fetched"])).days < refresh_days:
        return [Shop(**s) for s in stored["shops"]]
    try:
        shops = fetch_supermarkets(http, bbox)
    except (httpx.HTTPError, ValueError):
        # Overpass falla a veces: mejor datos de hace unas semanas que ninguno
        return [Shop(**s) for s in stored["shops"]] if stored else []
    path.write_text(json.dumps(
        {"fetched": today.isoformat(), "shops": [s.__dict__ for s in shops]}, ensure_ascii=False, indent=1,
    ), encoding="utf-8")
    return shops


def nearest(shops: list[Shop], point: Point) -> tuple[Shop, float] | None:
    if not shops:
        return None
    return min(((s, haversine_m(point, Point(s.lat, s.lon))) for s in shops), key=lambda pair: pair[1])
```

`pisobot/enrich/geocode.py`:

```python
"""Geocodificación con Nominatim (OSM). Política: máximo 1 petición por segundo."""
from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from pisobot.models import Point

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


class Geocoder:
    def __init__(self, http: httpx.Client, cache: dict,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.http = http
        self.cache = cache
        self.sleep = sleep
        self.clock = clock
        self._last: float | None = None

    def geocode(self, query: str) -> Point | None:
        key = " ".join(query.lower().split())
        if key in self.cache:
            value = self.cache[key]
            return Point(*value) if value else None
        if self._last is not None:
            wait = 1.0 - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        try:
            response = self.http.get(NOMINATIM_URL, params={
                "q": query, "format": "jsonv2", "limit": 1, "countrycodes": "cz",
            }, timeout=20)
            self._last = self.clock()
            response.raise_for_status()
            results = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not results:
            self.cache[key] = None
            return None
        point = Point(float(results[0]["lat"]), float(results[0]["lon"]))
        self.cache[key] = [point.lat, point.lon]
        return point
```

- [ ] **Step 4: Ejecutar y ver que pasan**

Run: `.venv/bin/pytest tests/test_supermarket.py tests/test_geocode.py -q`
Expected: todos pasan. (En `test_geocode_hit_caches_and_throttles` el reloj falso devuelve 0.0 tras la 1ª petición y 0.2 antes de la 2ª: la espera debe ser 0,8 s.)

---

### Task 6: Almacén de estado y deduplicación

**Files:**
- Create: `pisobot/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Listing`, `Kind` (Task 1).
- Produces: `fingerprint(listing) -> str | None`; `Store(data_dir: Path)` con atributos `seen: dict` (`{"ids": {key: iso}, "fingerprints": {fp: iso}}`), `cache: dict` (`{"transit": {}, "geo": {}}`), `state: dict` (`{"revisar_last_id": None, "pending": {}, "last_digest": None, "failures": {}, "transit_retries": {}, "llm": {"date": None, "count": 0}}`) y métodos `is_new(listing) -> bool`, `mark_seen(listing, today: date) -> None`, `prune(today: date, days: int = 60) -> None`, `save() -> None`. Ficheros: `data/seen.json`, `data/cache.json`, `data/state.json`.

- [ ] **Step 1: Escribir el test que falla** — `tests/test_store.py`

```python
from datetime import date

from pisobot.models import Kind, Listing
from pisobot.store import Store, fingerprint


def make(**kw):
    base = dict(source="ulovdomov", source_id="1", url="u", title="t", kind=Kind.FLAT,
                rent=17500, area_m2=40, lat=49.20893, lon=16.60166)
    base.update(kw)
    return Listing(**base)


def test_fingerprint_matches_same_flat_on_other_portal():
    a = make()
    b = make(source="bezrealitky", source_id="9", rent=17490, lat=49.2091, lon=16.6017)
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(make(rent=None)) is None
    assert fingerprint(make(location_precision="postcode")) is None


def test_store_roundtrip_and_dedupe(tmp_path):
    store = Store(tmp_path)
    first = make()
    assert store.is_new(first)
    store.mark_seen(first, date(2026, 11, 24))
    assert not store.is_new(first)
    duplicate = make(source_id="2")  # mismo piso publicado dos veces (pasa en UlovDomov)
    assert not store.is_new(duplicate)
    store.state["revisar_last_id"] = "123"
    store.cache["transit"]["k"] = []
    store.save()

    reloaded = Store(tmp_path)
    assert not reloaded.is_new(first)
    assert reloaded.state["revisar_last_id"] == "123"
    assert reloaded.cache["transit"] == {"k": []}
    assert reloaded.state["llm"] == {"date": None, "count": 0}


def test_prune_forgets_old_entries(tmp_path):
    store = Store(tmp_path)
    old = make(source_id="old", area_m2=99)
    store.mark_seen(old, date(2026, 9, 1))
    store.mark_seen(make(), date(2026, 11, 20))
    store.prune(date(2026, 11, 24))
    assert store.is_new(old)
    assert not store.is_new(make())
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_store.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/store.py`

```python
"""Estado persistente en data/*.json. El workflow lo commitea al acabar cada ejecución."""
from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

from pisobot.models import Listing

_DEFAULTS = {
    "seen.json": {"ids": {}, "fingerprints": {}},
    "cache.json": {"transit": {}, "geo": {}},
    "state.json": {
        "revisar_last_id": None,
        "pending": {},
        "last_digest": None,
        "failures": {},
        "transit_retries": {},
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
    return f"{round(total, -2)}|{listing.area_m2}|{round(listing.lat, 3)}|{round(listing.lon, 3)}"


class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir
        self.seen = self._load("seen.json")
        self.cache = self._load("cache.json")
        self.state = self._load("state.json")

    def _load(self, name: str) -> dict:
        default = copy.deepcopy(_DEFAULTS[name])
        path = self.dir / name
        if not path.exists():
            return default
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in default.items():
            data.setdefault(key, value)
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

    def prune(self, today: date, days: int = 60) -> None:
        limit = (today - timedelta(days=days)).isoformat()
        for table in ("ids", "fingerprints"):
            self.seen[table] = {k: v for k, v in self.seen[table].items() if v >= limit}

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        for name, data in (("seen.json", self.seen), ("cache.json", self.cache), ("state.json", self.state)):
            (self.dir / name).write_text(
                json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8",
            )
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_store.py -q`
Expected: `3 passed`.

---

### Task 7: IA con Gemini (capa gratuita) y fusión con las reglas

**Files:**
- Create: `pisobot/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `TextFacts` (Task 2).
- Produces: `GEMINI_URL` (con `{model}`); `LlmResult(summary_es: str, washing_machine: bool|None, energy_included: bool|None, furnished: bool|None, rent: int|None, fees: int|None, street: str|None, available_from: str|None)`; `Gemini(http, api_key: str, model: str, daily_limit: int, usage: dict, today: date)` con `available() -> bool` y `analyze(text: str) -> LlmResult | None`; `merge_facts(rules: TextFacts, llm: LlmResult | None) -> TextFacts`. `usage` es `store.state["llm"]`, mutado in situ.

- [ ] **Step 1: Escribir el test que falla** — `tests/test_llm.py`

```python
import json
from datetime import date

import httpx

from pisobot.llm import Gemini, LlmResult, merge_facts
from pisobot.parse_text import TextFacts

ANSWER = {
    "summary_es": "Habitación de 16 m² en Veveří, piso con dos estudiantes.",
    "washing_machine": True, "energy_included": None, "furnished": True,
    "rent": 6500, "fees": 1800, "street": "Kotlářská, Veveří", "available_from": "2027-02-01",
}


def gemini_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


def make(handler, usage, limit=5, key="k"):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return Gemini(http, key, "gemini-2.5-flash", limit, usage, date(2026, 11, 24))


def test_analyze_parses_json_and_counts_usage():
    calls = []

    def handler(request):
        calls.append(request)
        return gemini_response(ANSWER)

    usage = {"date": "2026-11-23", "count": 4}
    result = make(handler, usage).analyze("Pokoj 16 m2, pračka, 6500 Kč + 1800 Kč")
    assert result == LlmResult(**ANSWER)
    assert usage == {"date": "2026-11-24", "count": 1}
    assert calls[0].headers["x-goog-api-key"] == "k"
    assert "gemini-2.5-flash:generateContent" in str(calls[0].url)
    body = json.loads(calls[0].content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_daily_limit_and_missing_key_skip_http():
    def handler(request):
        raise AssertionError("no debería llamar")

    assert make(handler, {"date": "2026-11-24", "count": 5}).analyze("x") is None
    assert make(handler, {"date": None, "count": 0}, key="").analyze("x") is None


def test_errors_return_none():
    assert make(lambda r: httpx.Response(429), {"date": None, "count": 0}).analyze("x") is None
    bad = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "no es json"}]}}]})
    assert make(lambda r: bad, {"date": None, "count": 0}).analyze("x") is None


def test_merge_facts_only_fills_unknowns():
    rules = TextFacts(washing_machine=False, energy_included=None, furnished=None, walkthrough_room=True)
    merged = merge_facts(rules, LlmResult(**ANSWER))
    assert merged.washing_machine is False  # la regla explícita manda
    assert merged.furnished is True
    assert merged.energy_included is None
    assert merged.walkthrough_room is True
    assert merge_facts(rules, None) == rules
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_llm.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/llm.py`

```python
"""Lectura de anuncios con Gemini (capa gratuita). Si falla o se agota la cuota, el bot sigue solo con reglas."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import date

import httpx

from pisobot.parse_text import TextFacts

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = """Lees anuncios de alquiler de Brno escritos en checo o inglés.
Responde solo con JSON:
- summary_es: resumen en español de 2-3 frases (qué es, zona, condiciones importantes).
- washing_machine: true si hay lavadora en el piso (aunque sea compartida), false si dice que no hay, null si no lo menciona.
- energy_included: true si el precio incluye luz/gas, false si van aparte, null si no se sabe.
- furnished: true si está amueblado, false si no, null si no se sabe.
- rent: alquiler mensual en CZK (entero) o null.
- fees: gastos/poplatky mensuales en CZK (entero) o null.
- street: calle y barrio de Brno si aparecen, o null.
- available_from: fecha de entrada AAAA-MM-DD o null.

Anuncio:
"""

_NULLABLE = {"nullable": True}
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary_es": {"type": "STRING"},
        "washing_machine": {"type": "BOOLEAN", **_NULLABLE},
        "energy_included": {"type": "BOOLEAN", **_NULLABLE},
        "furnished": {"type": "BOOLEAN", **_NULLABLE},
        "rent": {"type": "INTEGER", **_NULLABLE},
        "fees": {"type": "INTEGER", **_NULLABLE},
        "street": {"type": "STRING", **_NULLABLE},
        "available_from": {"type": "STRING", **_NULLABLE},
    },
    "required": ["summary_es"],
}


@dataclass
class LlmResult:
    summary_es: str
    washing_machine: bool | None = None
    energy_included: bool | None = None
    furnished: bool | None = None
    rent: int | None = None
    fees: int | None = None
    street: str | None = None
    available_from: str | None = None


class Gemini:
    def __init__(self, http: httpx.Client, api_key: str, model: str, daily_limit: int, usage: dict, today: date):
        self.http = http
        self.api_key = api_key
        self.model = model
        self.daily_limit = daily_limit
        self.usage = usage
        self.today = today.isoformat()

    def available(self) -> bool:
        if not self.api_key:
            return False
        count = self.usage["count"] if self.usage.get("date") == self.today else 0
        return count < self.daily_limit

    def analyze(self, text: str) -> LlmResult | None:
        if not self.available():
            return None
        if self.usage.get("date") != self.today:
            self.usage["date"], self.usage["count"] = self.today, 0
        self.usage["count"] += 1
        body = {
            "contents": [{"parts": [{"text": PROMPT + text[:6000]}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0},
        }
        try:
            response = self.http.post(
                GEMINI_URL.format(model=self.model), headers={"x-goog-api-key": self.api_key}, json=body, timeout=60,
            )
            response.raise_for_status()
            raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(raw)
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            return None
        names = {f.name for f in dataclasses.fields(LlmResult)}
        return LlmResult(**{k: v for k, v in data.items() if k in names})


def merge_facts(rules: TextFacts, llm: LlmResult | None) -> TextFacts:
    """Las coincidencias explícitas de las reglas mandan; la IA solo rellena lo desconocido."""
    if llm is None:
        return rules
    merged = dataclasses.replace(rules)
    for name in ("washing_machine", "energy_included", "furnished"):
        if getattr(merged, name) is None:
            setattr(merged, name, getattr(llm, name))
    return merged
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_llm.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Verificar el modelo de la capa gratuita (solo si existe `GEMINI_API_KEY` en `.env`)**

Run: `set -a; . ./.env; set +a; curl -s -H "x-goog-api-key: $GEMINI_API_KEY" "https://generativelanguage.googleapis.com/v1beta/models?pageSize=100" | grep -o '"name": "models/gemini[^"]*flash[^"]*"' | head`
Expected: lista de modelos `flash`. Si `gemini-2.5-flash` no aparece, poner en `config.yaml` (`llm.model`) el `flash` estable más reciente de la lista. No imprimir la clave.

---

### Task 8: Base de recolectores y Bezrealitky

**Files:**
- Create: `pisobot/collectors/base.py`, `pisobot/collectors/bezrealitky.py`, `pisobot/collectors/__init__.py` (vacío por ahora; `build_collectors` llega en Task 12)
- Test: `tests/test_collectors_base.py`, `tests/test_bezrealitky.py`

**Interfaces:**
- Consumes: `Listing`, `Kind` (Task 1); `normalize_disposition` (Task 2).
- Produces:
  - `collectors.base`: `Collector` (Protocol: `name: str`, `fetch_list() -> list[Listing]`, `fetch_detail(listing) -> Listing`); `inline_text(node) -> str`; `block_text(node) -> str`; `price_czk(raw: str | None) -> int | None`; `HTMLParser` (re-export de `LexborHTMLParser`).
  - `collectors.bezrealitky`: `BASE`, `LIST_URL`, `parse_list(html) -> list[Listing]`, `parse_detail(html, listing: Listing | None = None) -> Listing`, clase `Bezrealitky(http)` con `name = "bezrealitky"`.

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_collectors_base.py`:

```python
import pytest

from pisobot.collectors.base import HTMLParser, block_text, inline_text, price_czk


@pytest.mark.parametrize("raw,expected", [
    ("8.500Kč/měsíc", 8500),
    ("20 000 Kč", 20000),
    ("16 400‍ Kč/měsíc", 16400),
    ("Dohodou", None),
    (None, None),
])
def test_price_czk(raw, expected):
    assert price_czk(raw) == expected


def test_text_helpers():
    tree = HTMLParser("<div id='a'>  Štýřice ,\n  Renneská </div><div id='b'><p>uno</p><p>dos</p></div>")
    assert inline_text(tree.css_first("#a")) == "Štýřice , Renneská"
    assert block_text(tree.css_first("#b")) == "uno\ndos"
    assert inline_text(None) == "" and block_text(None) == ""
```

`tests/test_bezrealitky.py`:

```python
from datetime import date
from pathlib import Path

import httpx

from pisobot.collectors.bezrealitky import BASE, LIST_URL, Bezrealitky, parse_detail, parse_list
from pisobot.models import Kind

FIXTURES = Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "bezrealitky_list.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "bezrealitky_detail.html").read_text(encoding="utf-8")


def test_parse_list():
    listings = parse_list(LIST_HTML)
    assert len(listings) == 15
    first = listings[0]
    assert first.source_id == "1092907"
    assert first.url == f"{BASE}/nemovitosti-byty-domy/1092907-nabidka-pronajem-bytu-stefanikova-brno"
    assert first.title == "Pronájem bytu 1+1 22 m², Štefánikova, Brno"
    assert (first.kind, first.rent, first.fees, first.disposition, first.area_m2) == (Kind.FLAT, 13500, 2500, "1+1", 22)
    assert (first.lat, first.lon) == (49.2141264, 16.601307)
    assert first.needs_detail is True and first.commission is False
    assert all(listing.url.startswith(BASE) for listing in listings)


def test_parse_detail_completes_listing():
    listing = parse_detail(DETAIL_HTML, parse_list(LIST_HTML)[0])
    assert listing.description.startswith("Pronajmu renovovaný byt 1+1")
    assert listing.available_from == date(2026, 11, 24)
    assert listing.fees == 2500 and listing.fees_include_energy is True
    assert listing.furnished is True
    assert listing.needs_detail is False


def test_parse_detail_without_base_listing():
    listing = parse_detail(DETAIL_HTML)
    assert (listing.source_id, listing.rent, listing.lat) == ("1092907", 13500, 49.2141264)
    assert listing.description


def test_collector_uses_public_pages_only():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=LIST_HTML if str(request.url) == LIST_URL else DETAIL_HTML)

    collector = Bezrealitky(httpx.Client(transport=httpx.MockTransport(handler)))
    listings = collector.fetch_list()
    collector.fetch_detail(listings[0])
    assert seen == [LIST_URL, listings[0].url]
    assert not any("api.bezrealitky.cz" in url for url in seen)
```

- [ ] **Step 2: Ejecutar y ver que fallan**

Run: `.venv/bin/pytest tests/test_collectors_base.py tests/test_bezrealitky.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/collectors/base.py`

```python
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


_CZK = re.compile(r"(\d[\d \xa0.]*)\s*Kč")


def price_czk(raw: str | None) -> int | None:
    """'8.500Kč/měsíc' -> 8500, '16 400 Kč' -> 16400, 'Dohodou' -> None."""
    text = (raw or "").replace("‍", "")
    match = _CZK.search(text)
    return int(re.sub(r"\D", "", match.group(1))) if match else None
```

`pisobot/collectors/bezrealitky.py`:

```python
"""Bezrealitky: página pública del listado (permitida por robots.txt; su API GraphQL no).

Los datos vienen en el JSON de Next.js (__NEXT_DATA__), en la caché de Apollo.
"""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from pisobot.models import Kind, Listing
from pisobot.parse_text import normalize_disposition

BASE = "https://www.bezrealitky.cz"
LIST_URL = f"{BASE}/vypis/nabidka-pronajem/byt/brno"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_FURNISHED = {"VYBAVENY": True, "CASTECNE": True, "NEVYBAVENY": False}


def _page_props(html: str) -> dict:
    match = _NEXT_DATA.search(html)
    if not match:
        raise ValueError("Bezrealitky: la página no trae __NEXT_DATA__")
    return json.loads(match.group(1))["props"]["pageProps"]


def _field(advert: dict, name: str):
    """En la caché de Apollo algunos campos llevan argumentos: 'address({"locale":"CS"})'."""
    if name in advert:
        return advert[name]
    for key, value in advert.items():
        if key.startswith(name + "("):
            return value
    return None


def _listing(advert: dict) -> Listing:
    gps = advert.get("gps") or {}
    return Listing(
        source="bezrealitky",
        source_id=str(advert["id"]),
        url=f"{BASE}/nemovitosti-byty-domy/{advert['uri']}",
        title=_field(advert, "imageAltText") or advert["uri"],
        kind=Kind.ROOM if advert.get("roommate") else Kind.FLAT,
        rent=advert.get("price"),
        fees=advert.get("charges"),
        disposition=normalize_disposition(advert.get("disposition")),
        area_m2=advert.get("surface"),
        lat=gps.get("lat"),
        lon=gps.get("lng"),
        address_text=_field(advert, "address") or "",
        commission=False,  # "bez realitky": anuncios de particulares
        needs_detail=True,
    )


def parse_list(html: str) -> list[Listing]:
    cache = _page_props(html)["apolloCache"]
    return [
        _listing(advert)
        for key, advert in cache.items()
        if key.startswith("Advert:") and advert.get("offerType") == "PRONAJEM" and advert.get("estateType") == "BYT"
    ]


def parse_detail(html: str, listing: Listing | None = None) -> Listing:
    advert = _page_props(html)["origAdvert"]
    base = listing or _listing(advert)
    available = advert.get("availableFrom")
    return dataclasses.replace(
        base,
        description=advert.get("description") or "",
        fees=advert.get("charges", base.fees),
        # utilityCharges es el anticipo de energías: si existe, ya está sumado en charges
        fees_include_energy=True if advert.get("utilityCharges") else None,
        furnished=_FURNISHED.get(advert.get("equipped")),
        deposit=advert.get("deposit"),
        available_from=datetime.fromtimestamp(available, ZoneInfo("Europe/Prague")).date() if available else None,
        needs_detail=False,
    )


class Bezrealitky:
    name = "bezrealitky"

    def __init__(self, http: httpx.Client):
        self.http = http

    def fetch_list(self) -> list[Listing]:
        response = self.http.get(LIST_URL)
        response.raise_for_status()
        return parse_list(response.text)

    def fetch_detail(self, listing: Listing) -> Listing:
        response = self.http.get(listing.url)
        response.raise_for_status()
        return parse_detail(response.text, listing)
```

- [ ] **Step 4: Ejecutar y ver que pasan**

Run: `.venv/bin/pytest tests/test_collectors_base.py tests/test_bezrealitky.py -q`
Expected: todos pasan. Si `block_text` falla porque la firma de `text()` de selectolax no admite `skip_empty`, comprobar `help(LexborHTMLParser.css_first('...').text)` y ajustar `block_text`, no el test.

---

### Task 9: UlovDomov

**Files:**
- Create: `pisobot/collectors/ulovdomov.py`
- Test: `tests/test_ulovdomov.py`

**Interfaces:**
- Consumes: `Listing`, `Kind` (Task 1); `normalize_disposition` (Task 2).
- Produces: `API_URL`, `BOUNDS`, `MAX_PRICE`, `parse_offers(payload: dict) -> list[Listing]`, clase `UlovDomov(http)` con `name = "ulovdomov"` (`fetch_detail` devuelve el listing tal cual: la API ya trae todo).

- [ ] **Step 1: Escribir el test que falla** — `tests/test_ulovdomov.py`

```python
import json
from datetime import date
from pathlib import Path

import httpx

from pisobot.collectors.ulovdomov import API_URL, UlovDomov, parse_offers
from pisobot.models import Kind

FIXTURES = Path(__file__).parent / "fixtures"
FLATS = json.loads((FIXTURES / "ulovdomov_flats.json").read_text(encoding="utf-8"))
ROOMS = json.loads((FIXTURES / "ulovdomov_rooms.json").read_text(encoding="utf-8"))


def test_parse_flats():
    listings = parse_offers(FLATS)
    assert len(listings) == 3
    first = listings[0]
    assert first.source_id == "5683439"
    assert first.url == "https://www.ulovdomov.cz/inzerat/pronajem-brno-brno-mesto-pekarska-2-1/5683439"
    assert (first.kind, first.rent, first.fees, first.disposition, first.area_m2) == (Kind.FLAT, 18900, None, "2+1", 50)
    assert (first.lat, first.lon) == (49.19216, 16.60144)
    assert first.available_from == date(2026, 12, 1)
    assert first.address_text == "Pekařská, Brno-město"
    assert first.description.startswith("V zastoupení majitele")
    assert first.needs_detail is False


def test_parse_rooms():
    listings = parse_offers(ROOMS)
    assert [listing.kind for listing in listings] == [Kind.ROOM] * 3
    second = listings[1]
    assert (second.rent, second.fees, second.deposit) == (4500, 900, 5400)
    assert second.available_from == date(2026, 10, 1)
    assert listings[2].commission is False
    assert listings[0].commission is None


def test_fetch_list_queries_rent_and_coliving():
    bodies = []

    def handler(request):
        assert str(request.url).startswith(API_URL)
        assert request.url.params["sorting"] == "latest"
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=FLATS if body["offerType"] == "rent" else ROOMS)

    listings = UlovDomov(httpx.Client(transport=httpx.MockTransport(handler))).fetch_list()
    assert [b["offerType"] for b in bodies] == ["rent", "coliving"]
    assert bodies[0]["price"] == {"max": 48000}
    assert len(listings) == 6
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_ulovdomov.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/collectors/ulovdomov.py`

```python
"""UlovDomov: API JSON de su propia web (robots.txt: Allow /). Habitaciones = offerType "coliving"."""
from __future__ import annotations

from datetime import date

import httpx

from pisobot.models import Kind, Listing
from pisobot.parse_text import normalize_disposition

API_URL = "https://ud.api.ulovdomov.cz/v1/offer/find"
BOUNDS = {"northEast": {"lat": 49.30, "lng": 16.75}, "southWest": {"lat": 49.10, "lng": 16.45}}
MAX_PRICE = 48000  # techo del perfil grupo con 4 personas; el resto lo decide el prefiltro


def _money(value: dict | None) -> int | None:
    return value.get("value") if value else None


def parse_offers(payload: dict) -> list[Listing]:
    listings = []
    for offer in payload.get("data", {}).get("offers", []):
        geo = offer.get("geoCoordinates") or {}
        street = (offer.get("street") or {}).get("title") or ""
        part = (offer.get("villagePart") or {}).get("title") or ""
        description = offer.get("description") or ""
        if offer.get("priceNote"):
            description += f"\n{offer['priceNote']}"
        available = offer.get("availableFrom")
        listings.append(Listing(
            source="ulovdomov",
            source_id=str(offer["id"]),
            url=offer["absoluteUrl"],
            title=offer.get("title") or "",
            kind=Kind.ROOM if offer.get("offerType") == "coliving" else Kind.FLAT,
            rent=_money(offer.get("rentalPrice")),
            fees=_money(offer.get("monthlyFeesPrice")),
            disposition=normalize_disposition(offer.get("disposition")),
            area_m2=offer.get("area"),
            deposit=_money(offer.get("depositPrice")),
            commission=False if offer.get("isNoCommission") else None,
            lat=geo.get("lat"),
            lon=geo.get("lng"),
            address_text=", ".join(x for x in (street, part) if x),
            description=description,
            available_from=date.fromisoformat(available) if available else None,
        ))
    return listings


class UlovDomov:
    name = "ulovdomov"

    def __init__(self, http: httpx.Client):
        self.http = http

    def fetch_list(self) -> list[Listing]:
        listings = []
        for offer_type, extra in (("rent", {"price": {"max": MAX_PRICE}}), ("coliving", {})):
            response = self.http.post(
                API_URL,
                params={"page": 1, "perPage": 20, "sorting": "latest"},
                json={"offerType": offer_type, "propertyType": "flat", "bounds": BOUNDS, **extra},
            )
            response.raise_for_status()
            listings += parse_offers(response.json())
        return listings

    def fetch_detail(self, listing: Listing) -> Listing:
        return listing
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_ulovdomov.py -q`
Expected: `3 passed`.

---

### Task 10: eSpolubydleni

**Files:**
- Create: `pisobot/collectors/espolubydleni.py`
- Test: `tests/test_espolubydleni.py`

**Interfaces:**
- Consumes: `Listing`, `Kind` (Task 1); `normalize_disposition` (Task 2); `HTMLParser`, `inline_text`, `block_text`, `price_czk` (Task 8).
- Produces: `BASE`, `LIST_URL`, `parse_list(html) -> list[Listing]` (solo ofertas "Nabízím"), `parse_detail(html, url) -> Listing | None` (para URLs pegadas; `None` si es un "Hledám"), clase `Espolubydleni(http)` con `name = "espolubydleni"`.

Notas de la web: la URL del listado de Brno responde **HTTP 404 pero con la lista completa** (no usar `raise_for_status`). El precio es por persona. La tarjeta del listado ya trae el texto completo (`.popis1` en la fila `#i{N}inz`), así que no hace falta pedir el detalle (y se respeta su `Crawl-delay: 10` con una sola petición por ejecución).

- [ ] **Step 1: Escribir el test que falla** — `tests/test_espolubydleni.py`

```python
from datetime import date
from pathlib import Path

import httpx
import pytest

from pisobot.collectors.espolubydleni import LIST_URL, Espolubydleni, parse_detail, parse_list
from pisobot.models import Kind

FIXTURES = Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "espolubydleni_list.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "espolubydleni_detail.html").read_text(encoding="utf-8")
DETAIL_URL = "https://www.espolubydleni.cz/podnajem-spolubydlici/554939-pronajem-panelovy-byt-2-1-kralovo-pole-brno.htm"


def test_parse_list_keeps_only_offers():
    listings = parse_list(LIST_HTML)
    assert len(listings) == 5
    first = listings[0]
    assert first.source_id == "535554"
    assert first.title.startswith("Nabízím 2 místa 2L pokoj")
    assert (first.kind, first.rent, first.disposition) == (Kind.ROOM, 6000, "2+1")
    assert first.address_text == "Štýřice, Renneská Třída"
    assert first.location_precision == "street"
    assert first.url.startswith("https://www.espolubydleni.cz/")
    assert first.description
    assert all(listing.title.startswith("Nabízím") for listing in listings)


def test_parse_detail():
    listing = parse_detail(DETAIL_HTML, DETAIL_URL)
    assert listing.source_id == "554939"
    assert listing.title == "Nabízím 2 místa v 2L pokoji 17m2"
    assert (listing.rent, listing.area_m2) == (7000, 17)
    assert listing.available_from == date(2027, 1, 1)
    assert listing.address_text == "Královo Pole, Božetěchova"
    assert listing.description.startswith("Nabízíme neprůchozí pokoj")


def test_fetch_list_accepts_404_with_content():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, text=LIST_HTML)))
    assert len(Espolubydleni(client).fetch_list()) == 5


def test_fetch_list_rejects_real_errors():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, text="<html>nic</html>")))
    with pytest.raises(RuntimeError):
        Espolubydleni(client).fetch_list()
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_espolubydleni.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/collectors/espolubydleni.py`

```python
"""eSpolubydleni: habitaciones en pisos compartidos. HTML del servidor, sin GPS (se geocodifica la calle)."""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin

import httpx

from pisobot.collectors.base import HTMLParser, block_text, inline_text, price_czk
from pisobot.models import Kind, Listing
from pisobot.parse_text import normalize_disposition

BASE = "https://www.espolubydleni.cz"
LIST_URL = f"{BASE}/podnajem-spolubydlici/brno"
_ID = re.compile(r"/(\d+)-[^/]*\.htm")


def parse_list(html: str) -> list[Listing]:
    tree = HTMLParser(html)
    listings = []
    for row in tree.css('#result1 tr[id^="tr"]'):
        heading = inline_text(row.css_first("strong.s1"))
        if not heading.startswith("Nabízím"):  # "Hledám" = gente que busca piso
            continue
        link = row.css_first("a.re_2")
        href = link.attributes.get("href") if link is not None else None
        match = _ID.search(href or "")
        if not match:
            continue
        flat = inline_text(row.css_first(".res_div3 b"))
        card = tree.css_first(f'#i{row.attributes["id"][2:]}inz')
        listings.append(Listing(
            source="espolubydleni",
            source_id=match.group(1),
            url=urljoin(BASE, href),
            title=f"{heading} | {flat}" if flat else heading,
            kind=Kind.ROOM,
            rent=price_czk(inline_text(row.css_first('.res_div3 div[style*="float:right"] b'))),
            disposition=normalize_disposition(flat),
            address_text=inline_text(row.css_first('.res_div3 div[title^="city/quarter"]')),
            description=block_text(card.css_first(".popis1")) if card is not None else "",
            location_precision="street",
        ))
    return listings


def parse_detail(html: str, url: str) -> Listing | None:
    tree = HTMLParser(html)
    if not inline_text(tree.css_first("title")).startswith("Nabízím"):
        return None
    root = tree.css_first("#realita1")
    info = " ".join(inline_text(node) for node in tree.css("div.r_div_prava"))
    available = re.search(r"Volné od:\s*(\d{2})\.(\d{2})\.(\d{4})", info)
    room_area = re.search(r"plocha pokoje:\s*(\d+)", info)
    title = inline_text(root.css_first('a[href$=".htm"] > strong')) if root is not None else ""
    return Listing(
        source="espolubydleni",
        source_id=_ID.search(url).group(1),
        url=url,
        title=title,
        kind=Kind.ROOM,
        rent=price_czk(inline_text(root.css_first('b[style*="font-size:18px"]'))) if root is not None else None,
        disposition=normalize_disposition(info),
        area_m2=int(room_area.group(1)) if room_area else None,
        address_text=inline_text(tree.css_first("h2.karta")),
        description=block_text(root.css_first("div.popis1")) if root is not None else "",
        available_from=date(int(available.group(3)), int(available.group(2)), int(available.group(1))) if available else None,
        location_precision="street",
    )


class Espolubydleni:
    name = "espolubydleni"

    def __init__(self, http: httpx.Client):
        self.http = http

    def fetch_list(self) -> list[Listing]:
        response = self.http.get(LIST_URL)
        # La web responde 404 a esta URL pero sirve la lista completa
        if response.status_code not in (200, 404) or 'id="result1"' not in response.text:
            raise RuntimeError(f"eSpolubydleni: respuesta inesperada (HTTP {response.status_code})")
        return parse_list(response.text)

    def fetch_detail(self, listing: Listing) -> Listing:
        return listing
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_espolubydleni.py -q`
Expected: `4 passed`. Si `'id="result1"'` no aparece literal en el fixture (p. ej. comillas simples), usar `tree.css_first("#result1") is not None` en su lugar.

---

### Task 11: iDNES

**Files:**
- Create: `pisobot/collectors/idnes.py`
- Test: `tests/test_idnes.py`

**Interfaces:**
- Consumes: `Listing`, `Kind` (Task 1); `normalize_disposition`, `parse_area`, `norm` (Task 2); helpers de `base` (Task 8).
- Produces: `BASE`, `LIST_URLS`, `parse_list(html) -> list[Listing]`, `parse_detail(html, listing: Listing | None = None, url: str = "") -> Listing`, clase `Idnes(http)` con `name = "idnes"`.

Notas: robots.txt permite rutas simples y prohíbe combinar filtro de precio con `sort=`; el listado por defecto sale ordenado por fecha. Se lee solo la página 0 (25 anuncios). El GPS está solo en el detalle, en `script[data-maptiler-json]` (GeoJSON con `[lon, lat]`; hay que coger la feature cuyo `properties.id` es el id del anuncio, el resto son pisos similares).

- [ ] **Step 1: Escribir el test que falla** — `tests/test_idnes.py`

```python
from datetime import date
from pathlib import Path

import httpx

from pisobot.collectors.idnes import LIST_URLS, Idnes, parse_detail, parse_list
from pisobot.models import Kind

FIXTURES = Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "idnes_list.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "idnes_detail.html").read_text(encoding="utf-8")
DETAIL_URL = "https://reality.idnes.cz/detail/pronajem/byt/brno-bratislavska/6af5ba0b0e659b5db409eed4/"


def test_parse_list():
    listings = parse_list(LIST_HTML)
    assert len(listings) == 25
    first = listings[0]
    assert first.source_id == "6b0575cbe2d589687f04f544"
    assert first.title == "pronájem bytu 2+kk 50 m²"
    assert (first.kind, first.rent, first.disposition, first.area_m2) == (Kind.FLAT, 19900, "2+kk", 50)
    assert first.address_text == "Životského, Brno - Židenice"
    assert first.needs_detail is True and first.point is None


def test_parse_detail_from_url():
    listing = parse_detail(DETAIL_HTML, url=DETAIL_URL)
    assert listing.source_id == "6af5ba0b0e659b5db409eed4"
    assert listing.title == "Pronájem bytu 1+kk 35 m²"
    assert (listing.rent, listing.area_m2, listing.disposition) == (16400, 35, "1+kk")
    assert listing.address_text == "Bratislavská, Brno - Zábrdovice"
    assert listing.available_from == date(2027, 1, 1)
    assert (listing.lat, listing.lon) == (49.197984, 16.616712)
    assert listing.location_precision == "exact"
    assert "zálohy na služby" in listing.description
    assert len(listing.description) > 1000
    assert listing.needs_detail is False


def test_fetch_list_reads_flats_and_rooms():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=LIST_HTML)

    listings = Idnes(httpx.Client(transport=httpx.MockTransport(handler))).fetch_list()
    assert seen == list(LIST_URLS)
    assert len(listings) == 25  # mismos ids en las dos páginas del mock: se deduplican
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_idnes.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/collectors/idnes.py`

```python
"""reality.idnes.cz: HTML del servidor. Mucho stock de agencias que también está en Sreality."""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import date
from urllib.parse import urljoin

import httpx

from pisobot.collectors.base import HTMLParser, block_text, inline_text, price_czk
from pisobot.models import Kind, Listing
from pisobot.parse_text import norm, normalize_disposition, parse_area

BASE = "https://reality.idnes.cz"
LIST_URLS = (f"{BASE}/s/pronajem/byty/brno/", f"{BASE}/s/pronajem/byty/pokoj/brno/")
_ID = re.compile(r"/([0-9a-f]{24})/?(?:[?#]|$)")


def _kind(title: str) -> Kind:
    return Kind.ROOM if "pokoj" in norm(title) else Kind.FLAT


def parse_list(html: str) -> list[Listing]:
    tree = HTMLParser(html)
    listings = []
    for card in tree.css("div.c-products__item"):
        link = card.css_first('a.c-products__link[href*="/detail/"]')
        href = link.attributes.get("href") if link is not None else None
        match = _ID.search(href or "")
        if not match:
            continue
        title = inline_text(card.css_first("h2.c-products__title"))
        listings.append(Listing(
            source="idnes",
            source_id=match.group(1),
            url=urljoin(BASE, href),
            title=title,
            kind=_kind(title),
            rent=price_czk(inline_text(card.css_first("p.c-products__price"))),
            disposition=normalize_disposition(title),
            area_m2=parse_area(title),
            address_text=inline_text(card.css_first("p.c-products__info")),
            location_precision="none",
            needs_detail=True,
        ))
    return listings


def _params(tree) -> dict[str, str]:
    params, key = {}, None
    for definition in tree.css(".b-definition-columns dl"):
        for node in definition.iter():
            if node.tag == "dt":
                key = inline_text(node)
            elif node.tag == "dd" and key and "advertisement" not in (node.attributes.get("class") or ""):
                params[key] = " ".join(node.text().replace("‍", "").split())
                key = None
    return params


def _gps(tree, source_id: str) -> tuple[float | None, float | None]:
    script = tree.css_first("script[data-maptiler-json]")
    if script is None:
        return None, None
    try:
        features = json.loads(script.text())["geojson"]["features"]
    except (ValueError, KeyError):
        return None, None
    for feature in features:
        if feature.get("properties", {}).get("id") == source_id:
            lon, lat = feature["geometry"]["coordinates"]
            return lat, lon
    return None, None


def _date(raw: str | None) -> date | None:
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", raw or "")
    return date(int(match.group(3)), int(match.group(2)), int(match.group(1))) if match else None


def parse_detail(html: str, listing: Listing | None = None, url: str = "") -> Listing:
    tree = HTMLParser(html)
    if listing is None:
        title = inline_text(tree.css_first("h1.b-detail__title"))
        listing = Listing(
            source="idnes",
            source_id=_ID.search(url).group(1),
            url=url,
            title=title,
            kind=_kind(title),
            rent=price_czk(inline_text(tree.css_first("p.b-detail__price"))),
            disposition=normalize_disposition(title),
            area_m2=parse_area(title),
            address_text=inline_text(tree.css_first("p.b-detail__info")),
            location_precision="none",
        )
    params = _params(tree)
    description = block_text(tree.css_first("div.b-desc"))
    notes = inline_text(tree.css_first(".wrapper-price-notes"))
    if notes:
        description = f"{description}\n{notes}"
    lat, lon = _gps(tree, listing.source_id)
    return dataclasses.replace(
        listing,
        description=description,
        lat=lat if lat is not None else listing.lat,
        lon=lon if lon is not None else listing.lon,
        location_precision="exact" if lat is not None else listing.location_precision,
        available_from=_date(params.get("Datum nastěhování")) or listing.available_from,
        area_m2=listing.area_m2 or parse_area(f"{params.get('Užitná plocha', '')}"),
        needs_detail=False,
    )


class Idnes:
    name = "idnes"

    def __init__(self, http: httpx.Client):
        self.http = http

    def fetch_list(self) -> list[Listing]:
        listings: dict[str, Listing] = {}
        for url in LIST_URLS:
            response = self.http.get(url)
            response.raise_for_status()
            for listing in parse_list(response.text):
                listings.setdefault(listing.source_id, listing)
        return list(listings.values())

    def fetch_detail(self, listing: Listing) -> Listing:
        response = self.http.get(listing.url)
        response.raise_for_status()
        return parse_detail(response.text, listing)
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_idnes.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Verificación en vivo de las URLs (pedir permiso al usuario: es red real)**

Run: `for u in https://reality.idnes.cz/s/pronajem/byty/brno/ https://reality.idnes.cz/s/pronajem/byty/pokoj/brno/; do curl -s -o /dev/null -w "%{http_code} %{url_effective}\n" -L -A "brno-pisos/0.1 (+https://github.com/pallerdev/brno-pisos)" "$u"; done`
Expected: `200` en ambas. Si la de `pokoj` no da 200, quitarla de `LIST_URLS` y del test.

---

### Task 12: Bazoš y registro de recolectores

**Files:**
- Create: `pisobot/collectors/bazos.py`
- Modify: `pisobot/collectors/__init__.py`
- Test: `tests/test_bazos.py`

**Interfaces:**
- Consumes: `Listing`, `Kind`, `Config` (Task 1); `normalize_disposition`, `parse_area`, `norm` (Task 2); helpers de `base` (Task 8); las cuatro clases de recolector (Tasks 8-11).
- Produces: `BASE`, `LIST_URL`, `parse_list(html, psc_prefixes: list[str]) -> list[Listing]`, `parse_detail(html, listing: Listing | None = None, url: str = "") -> Listing`, clase `Bazos(http, psc_prefixes)` con `name = "bazos"`; en `collectors/__init__.py`: `build_collectors(http, cfg) -> list[Collector]`.

Notas: robots.txt prohíbe los parámetros de filtro (`hlokalita=`, `humkreis`, `cenado=`), así que se lee la **categoría nacional** `/pronajmu/byt/` (página 1, más recientes primero salvo los TOP fijados arriba) y se filtra en local por código postal de Brno. El GPS del detalle es aproximado ("Přibližná lokalita"): `location_precision` sigue siendo `"postcode"`.

- [ ] **Step 1: Escribir el test que falla** — `tests/test_bazos.py`

```python
from pathlib import Path

from pisobot.collectors import build_collectors
from pisobot.collectors.bazos import LIST_URL, parse_detail, parse_list
from pisobot.config import load_config
from pisobot.models import Kind

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
LIST_HTML = (FIXTURES / "bazos_list.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "bazos_detail.html").read_text(encoding="utf-8")
BRNO = ["60", "61", "62", "63", "64"]


def test_parse_list_filters_by_postcode():
    listings = parse_list(LIST_HTML, BRNO)
    assert 0 < len(listings) <= 40
    first = listings[0]
    assert first.source_id == "226623653"
    assert first.title == "Brno - Bystrc, pronájem bytu 3+kk, zařízený, výtah - byt"
    assert (first.kind, first.rent, first.disposition) == (Kind.FLAT, 20000, "3+kk")
    assert first.address_text == "Brno 63500"
    assert first.location_precision == "postcode" and first.needs_detail
    assert parse_list(LIST_HTML, ["99"]) == []
    assert LIST_URL == "https://reality.bazos.cz/pronajmu/byt/"


def test_parse_detail_completes_listing():
    listing = parse_detail(DETAIL_HTML, parse_list(LIST_HTML, BRNO)[0])
    assert (listing.rent, listing.fees, listing.area_m2) == (20000, 4000, 58)
    assert (listing.lat, listing.lon) == (49.235758, 16.489105)
    assert listing.location_precision == "postcode"
    assert len(listing.description) > 1000
    assert listing.needs_detail is False


def test_parse_detail_from_url():
    url = "https://reality.bazos.cz/inzerat/226623653/brno-bystrc-pronajem-bytu-3kk-zarizeny-vytah-byt.php"
    listing = parse_detail(DETAIL_HTML, url=url)
    assert (listing.source_id, listing.rent, listing.disposition) == ("226623653", 20000, "3+kk")


def test_build_collectors():
    cfg = load_config(ROOT / "config.yaml", env={"WORK_LATLON": "49.18,16.60"})
    names = [c.name for c in build_collectors(None, cfg)]
    assert names == ["bezrealitky", "ulovdomov", "espolubydleni", "idnes", "bazos"]
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_bazos.py -q`
Expected: FAIL con `ModuleNotFoundError` / `ImportError`.

- [ ] **Step 3: Implementar** — `pisobot/collectors/bazos.py`

```python
"""Bazoš Reality: anuncios clasificados, muchos de particulares. Sin campos estructurados en el detalle."""
from __future__ import annotations

import dataclasses
import re
from urllib.parse import urljoin

import httpx

from pisobot.collectors.base import HTMLParser, block_text, inline_text, price_czk
from pisobot.models import Kind, Listing
from pisobot.parse_text import norm, normalize_disposition, parse_area

BASE = "https://reality.bazos.cz"
LIST_URL = f"{BASE}/pronajmu/byt/"
_ID = re.compile(r"/inzerat/(\d+)/")
_GMAPS = re.compile(r"/maps/place/(-?\d+\.\d+),(-?\d+\.\d+)")
_FEES = re.compile(r"\+\s*(\d[\d .]*)(?:,-)?\s*kc\s*(?:za\s+)?(?:sluzby|energie|poplatky|zalohy)")


def _kind(title: str) -> Kind:
    n = norm(title)
    return Kind.ROOM if "pokoj" in n or "spolubydl" in n else Kind.FLAT


def parse_list(html: str, psc_prefixes: list[str]) -> list[Listing]:
    tree = HTMLParser(html)
    listings = []
    for card in tree.css("div.inzeraty.inzeratyflex"):
        link = card.css_first("h2.nadpis a")
        href = link.attributes.get("href") if link is not None else None
        match = _ID.search(href or "")
        if not match:
            continue
        location = card.css_first("div.inzeratylok")
        parts = location.text(separator="|", strip=True).split("|") if location is not None else []
        town = parts[0] if parts else ""
        psc = re.sub(r"\D", "", parts[1]) if len(parts) > 1 else ""
        if psc[:2] not in psc_prefixes:  # categoría nacional: Brno se filtra por código postal
            continue
        title = inline_text(link)
        listings.append(Listing(
            source="bazos",
            source_id=match.group(1),
            url=urljoin(BASE, href),
            title=title,
            kind=_kind(title),
            rent=price_czk(inline_text(card.css_first("div.inzeratycena"))),
            disposition=normalize_disposition(title),
            area_m2=parse_area(title),
            address_text=f"{town} {psc}".strip(),
            location_precision="postcode",
            needs_detail=True,
        ))
    return listings


def _rows(tree) -> dict[str, object]:
    rows = {}
    for row in tree.css("td.listadvlevo tr"):
        cells = row.css("td")
        if len(cells) >= 2:
            rows[inline_text(cells[0]).rstrip(":")] = cells[1]
    return rows


def parse_detail(html: str, listing: Listing | None = None, url: str = "") -> Listing:
    tree = HTMLParser(html)
    rows = _rows(tree)
    price = price_czk(inline_text(rows["Cena"])) if "Cena" in rows else None
    if listing is None:
        title = inline_text(tree.css_first("h1.nadpisdetail"))
        location = inline_text(rows["Lokalita"]) if "Lokalita" in rows else ""
        listing = Listing(
            source="bazos",
            source_id=_ID.search(url).group(1),
            url=url,
            title=title,
            kind=_kind(title),
            rent=price,
            disposition=normalize_disposition(title),
            area_m2=parse_area(title),
            address_text=location,
            location_precision="postcode",
        )
    description = block_text(tree.css_first("div.popisdetail"))
    lat = lon = None
    maps = tree.css_first('a[href*="google.com/maps"]')
    match = _GMAPS.search(maps.attributes.get("href") or "") if maps is not None else None
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
    fees = _FEES.search(norm(description))
    return dataclasses.replace(
        listing,
        description=description,
        rent=listing.rent or price,
        fees=int(re.sub(r"\D", "", fees.group(1))) if fees else listing.fees,
        lat=lat if lat is not None else listing.lat,
        lon=lon if lon is not None else listing.lon,
        area_m2=listing.area_m2 or parse_area(description),
        disposition=listing.disposition or normalize_disposition(description),
        needs_detail=False,
    )


class Bazos:
    name = "bazos"

    def __init__(self, http: httpx.Client, psc_prefixes: list[str]):
        self.http = http
        self.psc_prefixes = psc_prefixes

    def fetch_list(self) -> list[Listing]:
        response = self.http.get(LIST_URL)
        response.raise_for_status()
        return parse_list(response.text, self.psc_prefixes)

    def fetch_detail(self, listing: Listing) -> Listing:
        response = self.http.get(listing.url)
        response.raise_for_status()
        return parse_detail(response.text, listing)
```

`pisobot/collectors/__init__.py`:

```python
from pisobot.collectors.base import Collector
from pisobot.collectors.bazos import Bazos
from pisobot.collectors.bezrealitky import Bezrealitky
from pisobot.collectors.espolubydleni import Espolubydleni
from pisobot.collectors.idnes import Idnes
from pisobot.collectors.ulovdomov import UlovDomov


def build_collectors(http, cfg) -> list[Collector]:
    return [
        Bezrealitky(http),
        UlovDomov(http),
        Espolubydleni(http),
        Idnes(http),
        Bazos(http, cfg.raw["collectors"]["bazos_psc_prefixes"]),
    ]
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_bazos.py -q`
Expected: `4 passed`. Si la fila "Cena" no se encuentra, inspeccionar `tests/fixtures/bazos_detail.html` alrededor de `listadvlevo` y ajustar `_rows` (la estructura real manda, el test no se toca).

---

### Task 13: URL pegada en `#revisar` (incluye Sreality)

**Files:**
- Create: `pisobot/collectors/single_url.py`
- Test: `tests/test_single_url.py`

**Interfaces:**
- Consumes: `Listing`, `Kind` (Task 1); `normalize_disposition`, `parse_price`, `norm` (Task 2); `parse_detail` de bezrealitky/idnes/bazos/espolubydleni (Tasks 8-12); `HTMLParser`, `block_text` (Task 8).
- Produces: `SREALITY_ID` (regex), `SREALITY_API`, `parse_sreality(payload: dict, url: str) -> Listing`, `fetch(http, url: str) -> Listing | str` (devuelve `str` con el texto de la página cuando no hay parser estructurado), `page_text(html: str) -> str`.

Nota: es una petición iniciada por el usuario (equivale a abrir el enlace en el navegador), no un rastreo; por eso se permite Sreality aquí.

- [ ] **Step 1: Escribir el test que falla** — `tests/test_single_url.py`

```python
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from pisobot.collectors.single_url import SREALITY_ID, fetch, parse_sreality
from pisobot.models import Kind

FIXTURES = Path(__file__).parent / "fixtures"
SREALITY = json.loads((FIXTURES / "sreality_detail.json").read_text(encoding="utf-8"))
SR_URL = "https://www.sreality.cz/detail/pronajem/byt/pokoj/brno-dolni-herspice-chleboradova/1704005708"


@pytest.mark.parametrize("url", [SR_URL, SR_URL + "?foo=1", SR_URL + "#x", SR_URL + "/"])
def test_sreality_id(url):
    assert SREALITY_ID.search(url).group(1) == "1704005708"


def test_parse_sreality():
    listing = parse_sreality(SREALITY, SR_URL)
    assert listing.source_id == "1704005708"
    assert listing.title == "Pronájem bytu pokoje 14 m²"
    assert (listing.kind, listing.rent, listing.area_m2) == (Kind.ROOM, 13000, 14)
    assert listing.lat == pytest.approx(49.1519993224724)
    assert listing.location_precision == "street"
    assert listing.available_from == date(2026, 11, 1)
    assert listing.address_text == "Chleborádova, Dolní Heršpice, Brno"
    assert len(listing.description) > 1000


ROUTES = {
    "https://www.sreality.cz/api/v1/estates/1704005708": ("json", "sreality_detail.json"),
    "https://reality.idnes.cz/detail/pronajem/byt/brno-bratislavska/6af5ba0b0e659b5db409eed4/": ("html", "idnes_detail.html"),
    "https://reality.bazos.cz/inzerat/226623653/x.php": ("html", "bazos_detail.html"),
    "https://www.bezrealitky.cz/nemovitosti-byty-domy/1092907-nabidka-pronajem-bytu-stefanikova-brno": ("html", "bezrealitky_detail.html"),
    "https://www.espolubydleni.cz/podnajem-spolubydlici/554939-pronajem-panelovy-byt-2-1-kralovo-pole-brno.htm": ("html", "espolubydleni_detail.html"),
    "https://example.com/inzerat": ("text", "<html><body><p>Pronajmu pokoj, 7000 Kč</p></body></html>"),
}


def router(request):
    kind, value = ROUTES[str(request.url)]
    if kind == "json":
        return httpx.Response(200, json=json.loads((FIXTURES / value).read_text(encoding="utf-8")))
    if kind == "html":
        return httpx.Response(200, text=(FIXTURES / value).read_text(encoding="utf-8"))
    return httpx.Response(200, text=value)


@pytest.mark.parametrize("url,source", [
    (SR_URL, "sreality"),
    ("https://reality.idnes.cz/detail/pronajem/byt/brno-bratislavska/6af5ba0b0e659b5db409eed4/", "idnes"),
    ("https://reality.bazos.cz/inzerat/226623653/x.php", "bazos"),
    ("https://www.bezrealitky.cz/nemovitosti-byty-domy/1092907-nabidka-pronajem-bytu-stefanikova-brno", "bezrealitky"),
    ("https://www.espolubydleni.cz/podnajem-spolubydlici/554939-pronajem-panelovy-byt-2-1-kralovo-pole-brno.htm", "espolubydleni"),
])
def test_fetch_known_portals(url, source):
    result = fetch(httpx.Client(transport=httpx.MockTransport(router)), url)
    assert result.source == source and result.rent


def test_fetch_unknown_returns_page_text():
    result = fetch(httpx.Client(transport=httpx.MockTransport(router)), "https://example.com/inzerat")
    assert result == "Pronajmu pokoj, 7000 Kč"
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_single_url.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/collectors/single_url.py`

```python
"""Una URL pegada por el usuario en #revisar.

Es una petición iniciada por el usuario, como abrir el enlace en el navegador; no es un rastreo.
Por eso aquí sí se admite Sreality (su robots.txt prohíbe rastrear).
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

import httpx

from pisobot.collectors import bazos, bezrealitky, espolubydleni, idnes
from pisobot.collectors.base import HTMLParser, block_text
from pisobot.models import Kind, Listing
from pisobot.parse_text import norm, normalize_disposition, parse_price

SREALITY_ID = re.compile(r"sreality\.cz/detail/(?:[^/?#]+/){4}(\d+)")
SREALITY_API = "https://www.sreality.cz/api/v1/estates/{id}"
_PRECISION = {"street": "street", "citypart": "postcode", "city": "postcode", "district": "postcode"}


def _money(value) -> int | None:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if isinstance(value, str) and re.search(r"\d", value):
        return parse_price(value) or int(re.sub(r"\D", "", value))
    return None


def _iso_date(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def parse_sreality(payload: dict, url: str) -> Listing:
    result = payload.get("result", payload)
    locality = result.get("locality") or {}
    category = (result.get("category_sub_cb") or {}).get("name") or ""
    description = result.get("advert_description") or ""
    for extra in (result.get("price_note"), result.get("cost_of_living")):
        if isinstance(extra, str) and extra:
            description += f"\n{extra}"
    address = ", ".join(str(x) for x in (locality.get("street"), locality.get("citypart"), locality.get("city")) if x)
    return Listing(
        source="sreality",
        source_id=str(result["hash_id"]),
        url=url,
        title=" ".join((result.get("advert_name") or "").split()),
        kind=Kind.ROOM if norm(category) == "pokoj" else Kind.FLAT,
        rent=int(result["price_czk"]) if result.get("price_czk") else None,
        fees=_money(result.get("cost_of_living")),
        deposit=_money(result.get("refundable_deposit")),
        disposition=normalize_disposition(category),
        area_m2=result.get("usable_area"),
        lat=locality.get("gps_lat"),
        lon=locality.get("gps_lon"),
        location_precision=_PRECISION.get(locality.get("inaccuracy_type"), "exact"),
        address_text=address,
        description=description,
        available_from=_iso_date(result.get("ready_date")),
    )


def page_text(html: str) -> str:
    return block_text(HTMLParser(html).css_first("body"))[:8000]


def fetch(http: httpx.Client, url: str) -> Listing | str:
    match = SREALITY_ID.search(url)
    if match:
        response = http.get(SREALITY_API.format(id=match.group(1)))
        response.raise_for_status()
        return parse_sreality(response.json(), url)
    host = urlparse(url).netloc
    response = http.get(url)
    if response.status_code >= 400 and "espolubydleni.cz" not in host:  # eSpolubydleni sirve contenido con 404
        response.raise_for_status()
    html = response.text
    if "bezrealitky.cz" in host and "/nemovitosti-byty-domy/" in url:
        return bezrealitky.parse_detail(html)
    if "reality.idnes.cz" in host and "/detail/" in url:
        return idnes.parse_detail(html, url=url)
    if "reality.bazos.cz" in host and "/inzerat/" in url:
        return bazos.parse_detail(html, url=url)
    if "espolubydleni.cz" in host:
        parsed = espolubydleni.parse_detail(html, url)
        if parsed is not None:
            return parsed
    return page_text(html)
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_single_url.py -q`
Expected: todos pasan. Ojo: `from pisobot.collectors import bazos, ...` importa los submódulos; si da import circular con `collectors/__init__.py`, cambiar a `from pisobot.collectors import bazos as bazos` no basta: usar `import pisobot.collectors.bazos as bazos` (y lo mismo para los otros tres).

---

### Task 14: Mensajes de Discord

**Files:**
- Create: `pisobot/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: `Config` (Task 1); `Candidate`, `Evaluation`, `evaluate` (Task 3); `haversine_m` (Task 5); modelos (Task 1).
- Produces: `FOOTER`; `czk(n: int) -> str` ("7.800"); `build_embed(c: Candidate, ev: Evaluation, cfg: Config, show_profile: bool = False) -> dict`; `chunk_embeds(embeds: list[dict], max_count: int = 10, max_chars: int = 5500) -> list[list[dict]]` (Discord: máx. 10 embeds y 6000 caracteres por mensaje); `send_webhook(http, url: str, embeds: list[dict], content: str | None = None, sleep=time.sleep) -> bool`.

- [ ] **Step 1: Escribir el test que falla** — `tests/test_notify.py`

```python
from datetime import date
from pathlib import Path

import httpx
import pytest

from pisobot.config import load_config
from pisobot.models import Kind, Listing, Shop, Trip
from pisobot.notify import FOOTER, build_embed, chunk_embeds, czk, send_webhook
from pisobot.parse_text import TextFacts
from pisobot.profiles import Candidate, evaluate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg():
    return load_config(ROOT / "config.yaml", env={"WORK_LATLON": "49.18,16.60"})


def candidate(**kw):
    base = dict(source="bezrealitky", source_id="1", url="https://www.bezrealitky.cz/x", title="Pokoj 16 m², Veveří",
                kind=Kind.ROOM, rent=7000, fees=800, lat=49.2, lon=16.59, commission=False,
                available_from=date(2027, 2, 1), deposit=14000)
    base.update(kw)
    trip = Trip(20, 0, 4, ["TRAM"], ["12"])
    return Candidate(listing=Listing(**base), facts=TextFacts(washing_machine=True),
                     trips={"work": [trip], "station": [trip]}, shop=(Shop("Albert", 49.2, 16.59), 300.0))


def test_czk():
    assert czk(7800) == "7.800" and czk(950) == "950"


def test_pass_embed_for_yo(cfg):
    c = candidate()
    embed = build_embed(c, evaluate(c, "yo", cfg), cfg)
    assert embed["title"] == "✅ ⭐ Pokoj 16 m², Veveří"
    assert embed["url"] == "https://www.bezrealitky.cz/x"
    assert embed["footer"]["text"] == FOOTER
    lines = embed["description"].splitlines()
    assert lines[0] == "🏠 Habitación · Bezrealitky · sin comisión"
    assert lines[1] == "💰 7.800 CZK/mes en total: 7.000 alquiler + 800 poplatky"
    assert lines[2] == "🚋 oficina: 20 min · tranvía 12 directo · 4 min andando a la parada"
    assert lines[3] == "🛒 Albert · 300 m · ~5 min"
    assert lines[4] == "🧺 Lavadora: sí · 🛋️ Amueblado: ?"
    assert "📅 Libre desde 01.02.2027 · Fianza 14.000" in lines


def test_group_embed_shows_price_per_person(cfg):
    c = candidate(kind=Kind.FLAT, rent=37000, fees=3000, fees_include_energy=True, disposition="4+1", area_m2=95)
    embed = build_embed(c, evaluate(c, "grupo", cfg), cfg)
    assert "🏠 Piso entero 4+1 · 95 m² · Bezrealitky · sin comisión" in embed["description"]
    assert "💰 3 pers. × 13.334 · 4 pers. × 10.000 (total 40.000): 37.000 alquiler + 3.000 poplatky (energías incluidas)" in embed["description"]
    assert "🚋 hlavní nádraží: 20 min" in embed["description"]


def test_maybe_embed_lists_questions_and_marks_approximate_location(cfg):
    c = candidate(location_precision="street", lat=49.2068, lon=16.6083)
    c.facts = TextFacts()
    embed = build_embed(c, evaluate(c, "yo", cfg), cfg, show_profile=True)
    assert embed["title"].startswith("⚠️ ⭐ [yo] ")
    assert "❓ Preguntar: lavadora no mencionada" in embed["description"]
    assert "📍 Ubicación aproximada" in embed["description"]
    assert "🌳 Lužánky 0 m" in embed["description"]


def test_pasted_text_without_url_has_no_url_key(cfg):
    c = candidate(url="", source="texto")
    assert "url" not in build_embed(c, evaluate(c, "yo", cfg), cfg)


def test_chunk_embeds_by_count_and_size():
    small = [{"title": "t", "description": "x"}] * 12
    assert [len(part) for part in chunk_embeds(small)] == [10, 2]
    big = [{"title": "t", "description": "x" * 3000}] * 3
    assert [len(part) for part in chunk_embeds(big)] == [1, 1, 1]


def test_send_webhook_success_and_retry_after_429():
    responses = iter([httpx.Response(429, json={"retry_after": 0.5}), httpx.Response(200, json={})])
    calls, sleeps = [], []

    def handler(request):
        calls.append(request)
        return next(responses)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert send_webhook(http, "https://discord.com/api/webhooks/1/abc", [{"title": "t"}], "hola", sleep=sleeps.append)
    assert sleeps == [0.5] and len(calls) == 2
    assert calls[0].url.params["wait"] == "true"


def test_send_webhook_failures():
    failing = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert send_webhook(failing, "https://discord.com/api/webhooks/1/abc", []) is False

    def never(request):
        raise AssertionError("sin URL no debe haber petición")

    assert send_webhook(httpx.Client(transport=httpx.MockTransport(never)), "", []) is False
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_notify.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/notify.py`

```python
"""Embeds de Discord y envío por webhook."""
from __future__ import annotations

import math
import time
from collections.abc import Callable

import httpx

from pisobot.config import Config
from pisobot.enrich.supermarket import haversine_m
from pisobot.models import Kind, Point, Verdict
from pisobot.profiles import Candidate, Evaluation

ICON = {Verdict.PASS: "✅", Verdict.MAYBE: "⚠️", Verdict.FAIL: "❌"}
COLOR = {Verdict.PASS: 0x2ECC71, Verdict.MAYBE: 0xF1C40F, Verdict.FAIL: 0xE74C3C}
KIND = {Kind.ROOM: "Habitación", Kind.FLAT: "Piso entero"}
SOURCES = {
    "bezrealitky": "Bezrealitky", "ulovdomov": "UlovDomov", "espolubydleni": "eSpolubydleni",
    "idnes": "iDNES", "bazos": "Bazoš", "sreality": "Sreality", "texto": "texto pegado",
}
MODES = {"TRAM": "tranvía", "BUS": "bus", "TROLLEYBUS": "trolebús", "RAIL": "tren", "REGIONAL_RAIL": "tren"}
FOOTER = "Rutas: Transitous (transitous.org)"  # atribución que exige su política de uso
WALK_M_PER_MIN = 83  # 5 km/h
DETOUR = 1.3


def czk(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _yes_no(value: bool | None) -> str:
    return {True: "sí", False: "no", None: "?"}[value]


def _snippet(text: str, limit: int = 300) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit].rstrip() + "…"


def _price_line(ev: Evaluation) -> str:
    price = ev.price
    if price is None:
        return "💰 Precio no indicado"
    if ev.per_person:
        people = " · ".join(f"{n} pers. × {czk(v)}" for n, v in sorted(ev.per_person.items()))
        head = f"💰 {people} (total {czk(price.total)})"
    else:
        head = f"💰 {czk(price.total)} CZK/mes en total"
    parts = [f"{czk(price.rent)} alquiler"]
    if price.fees:
        parts.append(f"{czk(price.fees)} poplatky")
    detail = " + ".join(parts)
    if price.energy_included:
        detail += " (energías incluidas)"
    elif price.energy_estimate:
        detail += f" + ~{czk(price.energy_estimate)} energías (estimado)"
    return f"{head}: {detail}"


def _trip_line(ev: Evaluation, label: str) -> str:
    trip = ev.trip
    if trip is None:
        return f"🚋 {label}: sin calcular"
    if not trip.modes:
        return f"🚋 {label}: {trip.minutes} min andando"
    if trip.transfers == 0 and len(trip.modes) == 1:
        route = f"{MODES.get(trip.modes[0], trip.modes[0].lower())} {trip.lines[0]} directo"
    else:
        route = " → ".join(f"{MODES.get(m, m.lower())} {line}" for m, line in zip(trip.modes, trip.lines))
    return f"🚋 {label}: {trip.minutes} min · {route} · {trip.first_walk_min} min andando a la parada"


def build_embed(c: Candidate, ev: Evaluation, cfg: Config, show_profile: bool = False) -> dict:
    listing = c.listing
    profile = cfg.profiles[ev.profile]
    label = cfg.raw["destinations"][profile["destination"]].get("label", profile["destination"])

    kind_line = f"🏠 {KIND[listing.kind]}"
    if listing.disposition:
        kind_line += f" {listing.disposition}"
    if listing.area_m2:
        kind_line += f" · {listing.area_m2} m²"
    kind_line += f" · {SOURCES.get(listing.source, listing.source)}"
    if listing.commission is False:
        kind_line += " · sin comisión"
    elif listing.commission or c.facts.commission:
        kind_line += " · con comisión"

    lines = [kind_line, _price_line(ev), _trip_line(ev, label)]
    if c.shop:
        shop, distance = c.shop
        minutes = math.ceil(distance * DETOUR / WALK_M_PER_MIN)
        lines.append(f"🛒 {shop.name} · {round(distance)} m · ~{minutes} min")
    else:
        lines.append("🛒 sin datos")

    furnished = listing.furnished if listing.furnished is not None else c.facts.furnished
    extras = f"🧺 Lavadora: {_yes_no(c.facts.washing_machine)} · 🛋️ Amueblado: {_yes_no(furnished)}"
    park = cfg.raw["landmarks"]["luzanky"]
    if listing.point is not None:
        distance = haversine_m(listing.point, Point(park["lat"], park["lon"]))
        if distance <= park["radius_m"]:
            extras += f" · 🌳 Lužánky {round(distance)} m"
    lines.append(extras)

    when = []
    if listing.available_from:
        when.append(f"Libre desde {listing.available_from:%d.%m.%Y}")
    if listing.deposit:
        when.append(f"Fianza {czk(listing.deposit)}")
    if when:
        lines.append("📅 " + " · ".join(when))
    if listing.location_precision != "exact":
        lines.append("📍 Ubicación aproximada: tiempos y distancias orientativos")
    if ev.fails:
        lines.append("❌ " + ", ".join(ev.fails))
    if ev.questions:
        lines.append("❓ Preguntar: " + ", ".join(ev.questions))
    summary = c.summary or _snippet(listing.description)
    if summary:
        lines.append(f"📝 {summary}")

    star = " ⭐" if ev.star else ""
    tag = f" [{ev.profile}]" if show_profile else ""
    embed = {
        "title": f"{ICON[ev.verdict]}{star}{tag} {listing.title}"[:256],
        "description": "\n".join(lines)[:4000],
        "color": COLOR[ev.verdict],
        "footer": {"text": FOOTER},
    }
    if listing.url:
        embed["url"] = listing.url
    return embed


def _size(embed: dict) -> int:
    return len(embed.get("title", "")) + len(embed.get("description", "")) + len(embed.get("footer", {}).get("text", ""))


def chunk_embeds(embeds: list[dict], max_count: int = 10, max_chars: int = 5500) -> list[list[dict]]:
    chunks, current, size = [], [], 0
    for embed in embeds:
        embed_size = _size(embed)
        if current and (len(current) == max_count or size + embed_size > max_chars):
            chunks.append(current)
            current, size = [], 0
        current.append(embed)
        size += embed_size
    if current:
        chunks.append(current)
    return chunks


def send_webhook(http: httpx.Client, url: str, embeds: list[dict], content: str | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
    if not url:
        return False
    body: dict = {"embeds": embeds, "allowed_mentions": {"parse": []}}
    if content:
        body["content"] = content
    for _ in range(2):
        try:
            response = http.post(url, params={"wait": "true"}, json=body, timeout=20)
        except httpx.HTTPError:
            return False
        if response.status_code == 429:
            try:
                retry_after = float(response.json().get("retry_after", 1))
            except ValueError:
                retry_after = 1.0
            sleep(min(retry_after, 10))
            continue
        return response.is_success
    return False
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_notify.py -q`
Expected: todos pasan.

---

### Task 15: Cliente del bot de Discord para `#revisar`

**Files:**
- Create: `pisobot/review.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Produces: `API`; `split_url(content: str) -> tuple[str | None, str]`; `DiscordBot(http, token: str)` con `new_messages(channel_id: str, after: str | None) -> list[dict]` (orden ascendente por id), `reply(channel_id, message_id, content: str | None, embeds: list[dict]) -> bool`, `post(channel_id, content: str) -> bool`.

- [ ] **Step 1: Escribir el test que falla** — `tests/test_review.py`

```python
import json

import httpx
import pytest

from pisobot.review import API, DiscordBot, split_url


@pytest.mark.parametrize("content,expected", [
    ("mira https://www.sreality.cz/detail/a/b/c/d/1 este", ("https://www.sreality.cz/detail/a/b/c/d/1", "mira  este".strip())),
    ("<https://reality.bazos.cz/inzerat/1/x.php>", ("https://reality.bazos.cz/inzerat/1/x.php", "")),
    ("Pronajmu pokoj, 7000 Kč", (None, "Pronajmu pokoj, 7000 Kč")),
])
def test_split_url(content, expected):
    url, text = split_url(content)
    assert url == expected[0]
    assert " ".join(text.split()) == " ".join(expected[1].split())


def test_new_messages_sorted_with_auth():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[{"id": "30"}, {"id": "12"}, {"id": "25"}])

    bot = DiscordBot(httpx.Client(transport=httpx.MockTransport(handler)), "tok")
    assert [m["id"] for m in bot.new_messages("555", "10")] == ["12", "25", "30"]
    assert str(seen[0].url).startswith(f"{API}/channels/555/messages")
    assert seen[0].url.params["after"] == "10"
    assert seen[0].headers["Authorization"] == "Bot tok"


def test_reply_references_message():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    bot = DiscordBot(httpx.Client(transport=httpx.MockTransport(handler)), "tok")
    assert bot.reply("555", "12", "nota", [{"title": "t"}])
    assert bodies[0]["message_reference"]["message_id"] == "12"
    assert bodies[0]["content"] == "nota" and bodies[0]["allowed_mentions"] == {"parse": []}
    assert bot.post("555", "aviso")
    assert bodies[1]["content"] == "aviso"
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_review.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/review.py`

```python
"""Bot de Discord por REST (sin conexión persistente): lee #revisar y responde."""
from __future__ import annotations

import re

import httpx

API = "https://discord.com/api/v10"
URL_RE = re.compile(r"https?://[^\s>]+")


def split_url(content: str) -> tuple[str | None, str]:
    match = URL_RE.search(content)
    if not match:
        return None, content.strip()
    # Discord envuelve en <...> los enlaces sin vista previa
    text = (content[:match.start()] + content[match.end():]).replace("<", " ").replace(">", " ")
    return match.group(0), " ".join(text.split())


class DiscordBot:
    def __init__(self, http: httpx.Client, token: str):
        self.http = http
        self.headers = {"Authorization": f"Bot {token}"}

    def new_messages(self, channel_id: str, after: str | None) -> list[dict]:
        params = {"limit": 50}
        if after:
            params["after"] = after
        response = self.http.get(f"{API}/channels/{channel_id}/messages", params=params, headers=self.headers, timeout=20)
        response.raise_for_status()
        return sorted(response.json(), key=lambda m: int(m["id"]))

    def _send(self, channel_id: str, body: dict) -> bool:
        body["allowed_mentions"] = {"parse": []}
        try:
            response = self.http.post(f"{API}/channels/{channel_id}/messages", json=body, headers=self.headers, timeout=20)
        except httpx.HTTPError:
            return False
        return response.is_success

    def reply(self, channel_id: str, message_id: str, content: str | None, embeds: list[dict]) -> bool:
        body: dict = {"embeds": embeds[:10], "message_reference": {"message_id": message_id, "fail_if_not_exists": False}}
        if content:
            body["content"] = content
        return self._send(channel_id, body)

    def post(self, channel_id: str, content: str) -> bool:
        return self._send(channel_id, {"content": content[:1900]})
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_review.py -q`
Expected: todos pasan.

---

### Task 16: Orquestación de una ejecución

**Files:**
- Create: `pisobot/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: todo lo anterior. Dobles de test: cualquier objeto con los mismos métodos (`fetch_list/fetch_detail`, `trips`, `geocode`, `analyze`, `new_messages/reply/post`).
- Produces: `Deps(http, collectors, transit, geocoder, llm, shops, send, discord)` (`send(url, embeds, content) -> bool`; `discord` puede ser `None`); `run(cfg, store, deps, now: datetime) -> dict` (informe con contadores); `build_candidate(...)`; `evaluate_pasted(content, message_id, cfg, deps, depart) -> tuple[str | None, list[dict]]`; `TRANSIT_ATTEMPTS = 3`.

Reglas de esta tarea (de la spec):
- ✅ se manda al momento; ⚠️ va a `state["pending"][perfil]` y sale en el resumen de las 19:00 (Europe/Prague), una vez al día.
- Un anuncio se marca como visto solo si se descartó o si todos sus envíos funcionaron.
- Si Transitous falla, no se notifica y se reintenta; al 3.er intento se evalúa con "transporte sin calcular".
- El detalle del portal y la IA solo se piden para anuncios que pasan el prefiltro. La IA se salta si el anuncio ya falla en todos los perfiles.
- 3 fallos seguidos de un portal → aviso en `#revisar`; al recuperarse, otro aviso.

- [ ] **Step 1: Escribir el test que falla** — `tests/test_pipeline.py`

```python
import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pisobot.config import load_config
from pisobot.llm import LlmResult
from pisobot.models import Kind, Listing, Point, Shop, Trip
from pisobot.pipeline import Deps, run
from pisobot.store import Store

ROOT = Path(__file__).resolve().parents[1]
ENV = {
    "WORK_LATLON": "49.18,16.60",
    "DISCORD_WEBHOOK_YO": "https://hook/yo",
    "DISCORD_WEBHOOK_GRUPO": "https://hook/grupo",
    "DISCORD_REVISAR_CHANNEL_ID": "555",
}
MORNING = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)   # 10:00 en Praga
EVENING = datetime(2026, 11, 24, 18, 30, tzinfo=timezone.utc)  # 19:30 en Praga
FAST = [Trip(20, 0, 4, ["TRAM"], ["12"])]
SHOPS = [Shop("Albert", 49.2005, 16.6)]


@pytest.fixture
def cfg():
    return load_config(ROOT / "config.yaml", env=ENV)


class FakeCollector:
    def __init__(self, name, listings, fail=False):
        self.name, self.listings, self.fail = name, listings, fail
        self.detail_calls = []

    def fetch_list(self):
        if self.fail:
            raise RuntimeError("boom")
        return list(self.listings)

    def fetch_detail(self, listing):
        self.detail_calls.append(listing.key)
        return dataclasses.replace(listing, needs_detail=False)


class FakeTransit:
    def __init__(self, trips):
        self.value, self.calls = trips, 0

    def trips(self, origin, dest, depart):
        self.calls += 1
        return self.value


class FakeGeocoder:
    def geocode(self, query):
        return Point(49.2, 16.6)


class FakeLlm:
    def __init__(self, result=None):
        self.result, self.calls = result, 0

    def analyze(self, text):
        self.calls += 1
        return self.result


class FakeDiscord:
    def __init__(self, messages=()):
        self.messages, self.replies, self.posts = list(messages), [], []

    def new_messages(self, channel, after):
        return [m for m in self.messages if after is None or int(m["id"]) > int(after)]

    def reply(self, channel, message_id, content, embeds):
        self.replies.append((message_id, content, embeds))
        return True

    def post(self, channel, content):
        self.posts.append(content)
        return True


class Sent(list):
    def __call__(self, url, embeds, content):
        self.append((url, embeds, content))
        return True


def room(source_id="1", **kw):
    base = dict(source="fake", source_id=source_id, url=f"https://x/{source_id}", title="Pokoj s pračkou",
                kind=Kind.ROOM, rent=7000, fees=800, lat=49.2, lon=16.6, area_m2=15)
    base.update(kw)
    return Listing(**base)


def deps_with(collectors, transit=None, llm=None, discord=None, sent=None):
    return Deps(http=None, collectors=collectors, transit=transit or FakeTransit(FAST), geocoder=FakeGeocoder(),
                llm=llm or FakeLlm(), shops=SHOPS, send=sent if sent is not None else Sent(), discord=discord)


def test_passing_room_is_sent_once(cfg, tmp_path):
    sent, store = Sent(), Store(tmp_path)
    deps = deps_with([FakeCollector("fake", [room()])], sent=sent)
    run(cfg, store, deps, MORNING)
    assert len(sent) == 1 and sent[0][0] == "https://hook/yo"
    assert sent[0][1][0]["title"].startswith("✅ ⭐")
    run(cfg, store, deps, MORNING)
    assert len(sent) == 1


def test_same_flat_on_two_portals_sent_once(cfg, tmp_path):
    sent = Sent()
    run(cfg, Store(tmp_path), deps_with([FakeCollector("fake", [room("1"), room("2")])], sent=sent), MORNING)
    assert len(sent) == 1


def test_maybe_waits_for_evening_digest(cfg, tmp_path):
    sent, store = Sent(), Store(tmp_path)
    deps = deps_with([FakeCollector("fake", [room(title="Pokoj")])], sent=sent)  # no menciona lavadora
    run(cfg, store, deps, MORNING)
    assert sent == [] and len(store.state["pending"]["yo"]) == 1
    run(cfg, store, deps, EVENING)
    assert len(sent) == 1 and sent[0][2].startswith("**Resumen diario")
    assert store.state["pending"]["yo"] == [] and store.state["last_digest"] == "2026-11-24"
    run(cfg, store, deps, EVENING)
    assert len(sent) == 1


def test_prefilter_skips_external_calls(cfg, tmp_path):
    transit, llm = FakeTransit(FAST), FakeLlm()
    collector = FakeCollector("fake", [room(rent=15000, needs_detail=True)])
    store = Store(tmp_path)
    run(cfg, store, deps_with([collector], transit=transit, llm=llm), MORNING)
    assert transit.calls == 0 and llm.calls == 0 and collector.detail_calls == []
    assert not store.is_new(room(rent=15000))


def test_detail_fetched_for_candidates(cfg, tmp_path):
    collector = FakeCollector("fake", [room(needs_detail=True)])
    run(cfg, Store(tmp_path), deps_with([collector]), MORNING)
    assert collector.detail_calls == ["fake:1"]


def test_transit_failure_retries_then_reports(cfg, tmp_path):
    store = Store(tmp_path)
    deps = deps_with([FakeCollector("fake", [room()])], transit=FakeTransit(None))
    run(cfg, store, deps, MORNING)
    run(cfg, store, deps, MORNING)
    assert store.state["pending"] == {} and store.is_new(room())
    run(cfg, store, deps, MORNING)
    assert "transporte sin calcular" in store.state["pending"]["yo"][0]["description"]
    assert not store.is_new(room())


def test_failing_collector_alerts_once_and_recovers(cfg, tmp_path):
    discord, store = FakeDiscord(), Store(tmp_path)
    broken = FakeCollector("roto", [], fail=True)
    for _ in range(4):
        run(cfg, store, deps_with([broken], discord=discord), MORNING)
    assert len(discord.posts) == 1 and "roto" in discord.posts[0]
    broken.fail = False
    run(cfg, store, deps_with([broken], discord=discord), MORNING)
    assert len(discord.posts) == 2 and "vuelve a funcionar" in discord.posts[1]


def test_revisar_replies_with_both_profiles(cfg, tmp_path):
    llm = FakeLlm(LlmResult(summary_es="Habitación en Veveří", washing_machine=True, rent=7500, fees=0,
                            street="Kotlářská, Veveří"))
    discord = FakeDiscord([
        {"id": "10", "author": {"bot": False}, "content": "Pronajmu pokoj v bytě 3+1, Kotlářská, 7500 Kč vše v ceně, pračka"},
        {"id": "11", "author": {"bot": True}, "content": "respuesta anterior del bot"},
    ])
    store = Store(tmp_path)
    run(cfg, store, deps_with([], llm=llm, discord=discord), MORNING)
    assert len(discord.replies) == 1 and llm.calls == 1
    message_id, note, embeds = discord.replies[0]
    assert message_id == "10" and note is None
    assert ["[yo]" in embeds[0]["title"], "[grupo]" in embeds[1]["title"]] == [True, True]
    assert "Habitación en Veveří" in embeds[0]["description"]
    assert store.state["revisar_last_id"] == "11"
    run(cfg, store, deps_with([], llm=llm, discord=discord), MORNING)
    assert len(discord.replies) == 1


def test_revisar_text_without_location_asks_for_street(cfg, tmp_path):
    discord = FakeDiscord([{"id": "20", "author": {}, "content": "Pronajmu pokoj, 7000 Kč, pračka, volný od prosince"}])
    run(cfg, Store(tmp_path), deps_with([], discord=discord), MORNING)
    assert "calle" in discord.replies[0][1]
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_pipeline.py -q`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementar** — `pisobot/pipeline.py`

```python
"""Una ejecución completa: portales → prefiltro → detalle → enriquecimiento → evaluación → Discord."""
from __future__ import annotations

import dataclasses
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from pisobot.collectors import single_url
from pisobot.config import Config
from pisobot.enrich.supermarket import nearest
from pisobot.enrich.transit import next_weekday_departure
from pisobot.llm import LlmResult, merge_facts
from pisobot.models import Kind, Listing, Shop, Verdict
from pisobot.notify import build_embed, chunk_embeds
from pisobot.parse_text import TextFacts, norm, normalize_disposition, parse_area, parse_price, parse_text
from pisobot.profiles import Candidate, evaluate, prefilter
from pisobot.review import split_url
from pisobot.store import Store

TRANSIT_ATTEMPTS = 3
_ROOM_WORDS = re.compile(r"\bpokoj|spolubydl|\broom\b|habitaci")


@dataclass
class Deps:
    http: httpx.Client | None
    collectors: list
    transit: object  # TransitClient o doble con .trips()
    geocoder: object  # Geocoder o doble con .geocode()
    llm: object  # Gemini o doble con .analyze()
    shops: list[Shop]
    send: Callable[[str, list[dict], str | None], bool]
    discord: object | None  # DiscordBot; None en --dry-run o sin token


def _log(message: str) -> None:
    print(message, flush=True)


def run(cfg: Config, store: Store, deps: Deps, now: datetime) -> dict:
    tz = cfg.raw["departure"]["tz"]
    today = now.astimezone(ZoneInfo(tz)).date()
    depart = next_weekday_departure(now, cfg.raw["departure"]["time"], tz)
    report: Counter = Counter()
    for collector in deps.collectors:
        try:
            listings = collector.fetch_list()
        except Exception as exc:  # noqa: BLE001 - un portal roto no debe parar a los demás
            _collector_failed(cfg, store, deps, collector.name, exc)
            report["portales_con_error"] += 1
            continue
        _collector_ok(cfg, store, deps, collector.name)
        report[f"leidos_{collector.name}"] += len(listings)
        for listing in listings:
            if store.is_new(listing):
                _process(listing, collector, cfg, store, deps, depart, today, report)
    _review(cfg, store, deps, depart, report)
    _digest(cfg, store, deps, now, report)
    store.prune(today)
    return dict(report)


def _process(listing, collector, cfg, store, deps, depart, today: date, report: Counter) -> None:
    facts = parse_text(f"{listing.title}\n{listing.description}")
    names = prefilter(listing, facts, cfg)
    if names and listing.needs_detail:
        try:
            listing = collector.fetch_detail(listing)
        except Exception as exc:  # noqa: BLE001 - se reintenta en la próxima ejecución
            _log(f"{listing.key}: detalle no disponible ({exc!r})")
            return
        facts = parse_text(f"{listing.title}\n{listing.description}")
        names = prefilter(listing, facts, cfg)
    if not names:
        store.mark_seen(listing, today)
        report["descartados_prefiltro"] += 1
        return
    candidate = build_candidate(listing, facts, names, cfg, deps, depart, store.state["transit_retries"])
    if candidate is None:
        report["transporte_reintentar"] += 1
        return
    delivered = True
    for name in names:
        ev = evaluate(candidate, name, cfg)
        report[f"{name}_{ev.verdict.value}"] += 1
        if ev.verdict is Verdict.PASS:
            url = cfg.secrets.get(cfg.profiles[name]["webhook_env"], "")
            delivered = deps.send(url, [build_embed(candidate, ev, cfg)], None) and delivered
        elif ev.verdict is Verdict.MAYBE:
            embed = build_embed(candidate, ev, cfg)
            pending = store.state["pending"].setdefault(name, [])
            if all(e.get("url") != embed.get("url") for e in pending):
                pending.append(embed)
    if delivered:
        store.mark_seen(candidate.listing, today)


def build_candidate(listing: Listing, facts: TextFacts, names: list[str], cfg: Config, deps: Deps, depart: datetime,
                    retries: dict | None, llm_result: LlmResult | None = None, use_llm: bool = True) -> Candidate | None:
    """None = Transitous ha fallado y conviene reintentar en otra ejecución (solo si se pasan `retries`)."""
    if listing.point is None and listing.address_text:
        point = deps.geocoder.geocode(f"{listing.address_text}, Brno")
        if point is not None:
            precision = listing.location_precision if listing.location_precision in ("street", "postcode") else "street"
            listing = dataclasses.replace(listing, lat=point.lat, lon=point.lon, location_precision=precision)

    trips: dict = {}
    if listing.point is not None:
        for name in names:
            dest = cfg.profiles[name]["destination"]
            if dest not in trips:
                trips[dest] = deps.transit.trips(listing.point, cfg.destinations[dest], depart)
        if retries is not None:
            if any(t is None for t in trips.values()):
                retries[listing.key] = retries.get(listing.key, 0) + 1
                if retries[listing.key] < TRANSIT_ATTEMPTS:
                    return None
            retries.pop(listing.key, None)

    shop = nearest(deps.shops, listing.point) if listing.point is not None else None
    candidate = Candidate(listing=listing, facts=facts, trips=trips, shop=shop)
    if llm_result is None and use_llm:
        # La IA solo rellena huecos: si ya falla en todos los perfiles, no se gasta cuota
        if any(evaluate(candidate, n, cfg).verdict is not Verdict.FAIL for n in names):
            llm_result = deps.llm.analyze(f"{listing.title}\n{listing.address_text}\n{listing.description}")
    if llm_result is not None:
        candidate.facts = merge_facts(facts, llm_result)
        candidate.summary = llm_result.summary_es
        candidate.listing = _fill_from_llm(listing, llm_result)
    return candidate


def _fill_from_llm(listing: Listing, llm: LlmResult) -> Listing:
    changes: dict = {}
    if listing.rent is None and llm.rent:
        changes["rent"] = llm.rent
    if listing.fees is None and llm.fees:
        changes["fees"] = llm.fees
    if listing.available_from is None and llm.available_from:
        try:
            changes["available_from"] = date.fromisoformat(llm.available_from)
        except ValueError:
            pass
    return dataclasses.replace(listing, **changes) if changes else listing


def _listing_from_text(text: str, url: str, message_id: str, deps: Deps) -> tuple[Listing, LlmResult | None]:
    llm = deps.llm.analyze(text)
    listing = Listing(
        source="texto",
        source_id=str(message_id),
        url=url,
        title=text.strip().splitlines()[0][:90],
        kind=Kind.ROOM if _ROOM_WORDS.search(norm(text)) else Kind.FLAT,
        rent=(llm.rent if llm else None) or parse_price(text),
        fees=llm.fees if llm else None,
        disposition=normalize_disposition(text),
        area_m2=parse_area(text),
        address_text=(llm.street or "") if llm else "",
        description=text,
        location_precision="street",
    )
    return listing, llm


def evaluate_pasted(content: str, message_id: str, cfg: Config, deps: Deps, depart: datetime) -> tuple[str | None, list[dict]]:
    url, text = split_url(content)
    listing, llm_result, from_text = None, None, False
    if url:
        try:
            fetched = single_url.fetch(deps.http, url)
        except httpx.HTTPError as exc:
            return f"No he podido abrir el enlace ({type(exc).__name__}).", []
        if isinstance(fetched, Listing):
            listing = fetched
        else:
            text = f"{text}\n{fetched}".strip()
    if listing is None:
        if len(text) < 30:
            return "No veo un anuncio: pega el enlace o el texto completo.", []
        listing, llm_result = _listing_from_text(text, url or "", message_id, deps)
        from_text = True
    facts = parse_text(f"{listing.title}\n{listing.description}")
    names = list(cfg.profiles)
    candidate = build_candidate(listing, facts, names, cfg, deps, depart, retries=None,
                                llm_result=llm_result, use_llm=not from_text)
    embeds = [build_embed(candidate, evaluate(candidate, n, cfg), cfg, show_profile=True) for n in names]
    note = None
    if candidate.listing.point is None:
        note = "No encuentro la ubicación: vuelve a pegarlo añadiendo la calle."
    return note, embeds


def _review(cfg: Config, store: Store, deps: Deps, depart: datetime, report: Counter) -> None:
    channel = cfg.secrets.get("DISCORD_REVISAR_CHANNEL_ID")
    if deps.discord is None or not channel:
        return
    try:
        messages = deps.discord.new_messages(channel, store.state["revisar_last_id"])
    except Exception as exc:  # noqa: BLE001
        _log(f"#revisar no disponible: {exc!r}")
        return
    for message in messages:
        store.state["revisar_last_id"] = message["id"]
        content = (message.get("content") or "").strip()
        if message.get("author", {}).get("bot") or not content:
            continue
        try:
            note, embeds = evaluate_pasted(content, message["id"], cfg, deps, depart)
        except Exception as exc:  # noqa: BLE001 - un mensaje raro no debe tumbar la ejecución
            note, embeds = f"No he podido evaluarlo ({type(exc).__name__}).", []
        deps.discord.reply(channel, message["id"], note, embeds)
        report["revisar_respondidos"] += 1


def _digest(cfg: Config, store: Store, deps: Deps, now: datetime, report: Counter) -> None:
    local = now.astimezone(ZoneInfo(cfg.raw["departure"]["tz"]))
    today = local.date().isoformat()
    if local.hour < cfg.raw["digest"]["hour"] or store.state["last_digest"] == today:
        return
    all_sent = True
    for name, embeds in store.state["pending"].items():
        if not embeds:
            continue
        url = cfg.secrets.get(cfg.profiles[name]["webhook_env"], "")
        sent = True
        for index, part in enumerate(chunk_embeds(embeds)):
            content = f"**Resumen diario ⚠️** · {len(embeds)} anuncios dudosos" if index == 0 else None
            sent = deps.send(url, part, content) and sent
        if sent:
            report["resumen_enviados"] += len(embeds)
            store.state["pending"][name] = []
        all_sent = all_sent and sent
    if all_sent:
        store.state["last_digest"] = today


def _alert(cfg: Config, deps: Deps, text: str) -> None:
    channel = cfg.secrets.get("DISCORD_REVISAR_CHANNEL_ID")
    if deps.discord is not None and channel:
        deps.discord.post(channel, text)
    else:
        _log(text)


def _collector_failed(cfg: Config, store: Store, deps: Deps, name: str, exc: Exception) -> None:
    failures = store.state["failures"]
    failures[name] = failures.get(name, 0) + 1
    _log(f"{name}: fallo {failures[name]} seguido: {exc!r}")
    if failures[name] == cfg.raw["collectors"]["failure_alert_after"]:
        _alert(cfg, deps, f"⚠️ El portal **{name}** lleva {failures[name]} ejecuciones fallando: `{type(exc).__name__}: {str(exc)[:300]}`")


def _collector_ok(cfg: Config, store: Store, deps: Deps, name: str) -> None:
    failures = store.state["failures"]
    if failures.get(name, 0) >= cfg.raw["collectors"]["failure_alert_after"]:
        _alert(cfg, deps, f"✅ El portal **{name}** vuelve a funcionar.")
    failures[name] = 0
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_pipeline.py -q`
Expected: todos pasan.

- [ ] **Step 5: Suite completa**

Run: `.venv/bin/pytest -q`
Expected: todo en verde.

---

### Task 17: Punto de entrada, Actions, documentación y primera ejecución real

**Files:**
- Create: `pisobot/__main__.py`, `.github/workflows/run.yml`, `.github/workflows/test.yml`, `LICENSE`, `README.md`
- Modify: `tests/fixtures/*` (anonimizar teléfonos y emails antes del primer commit: el repo es público)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: `python -m pisobot [--dry-run]`.

- [ ] **Step 1: Escribir el test que falla** — `tests/test_main.py`

```python
from pisobot.__main__ import build_parser


def test_parser_dry_run_flag():
    assert build_parser().parse_args(["--dry-run"]).dry_run is True
    assert build_parser().parse_args([]).dry_run is False
```

- [ ] **Step 2: Ejecutar y ver que falla**

Run: `.venv/bin/pytest tests/test_main.py -q`
Expected: FAIL con `ModuleNotFoundError`/`ImportError`.

- [ ] **Step 3: Implementar** — `pisobot/__main__.py`

```python
"""Punto de entrada: python -m pisobot [--dry-run]."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pisobot.collectors import build_collectors
from pisobot.config import load_config, load_dotenv
from pisobot.enrich.geocode import Geocoder
from pisobot.enrich.supermarket import load_or_refresh
from pisobot.enrich.transit import TransitClient
from pisobot.http import make_client
from pisobot.llm import Gemini
from pisobot.notify import send_webhook
from pisobot.pipeline import Deps, run
from pisobot.review import DiscordBot
from pisobot.store import Store

ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pisobot")
    parser.add_argument("--dry-run", action="store_true",
                        help="no envía nada a Discord ni guarda el estado; imprime los mensajes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(ROOT / ".env")
    cfg = load_config(ROOT / "config.yaml")
    store = Store(ROOT / "data")
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(cfg.raw["departure"]["tz"])).date()

    with make_client(cfg.raw["user_agent"]) as http:
        supermarket = cfg.raw["supermarket"]
        shops = load_or_refresh(ROOT / "data" / "supermarkets.json", http, supermarket["bbox"],
                                supermarket["refresh_days"], today)
        if args.dry_run:
            def send(url, embeds, content):
                print(json.dumps({"content": content, "embeds": embeds}, ensure_ascii=False, indent=1))
                return True
            discord = None
        else:
            def send(url, embeds, content):
                return send_webhook(http, url, embeds, content)
            token = cfg.secrets["DISCORD_BOT_TOKEN"]
            discord = DiscordBot(http, token) if token else None

        llm_cfg = cfg.raw["llm"]
        deps = Deps(
            http=http,
            collectors=build_collectors(http, cfg),
            transit=TransitClient(http, store.cache["transit"]),
            geocoder=Geocoder(http, store.cache["geo"]),
            llm=Gemini(http, cfg.secrets["GEMINI_API_KEY"], llm_cfg["model"], llm_cfg["daily_limit"], store.state["llm"], today),
            shops=shops,
            send=send,
            discord=discord,
        )
        report = run(cfg, store, deps, now)

    if not args.dry_run:
        store.save()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Ejecutar y ver que pasa**

Run: `.venv/bin/pytest tests/test_main.py -q`
Expected: `1 passed`.

- [ ] **Step 5: Workflows**

`.github/workflows/test.yml`:

```yaml
name: test
on:
  push:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -e ".[dev]"
      - run: pytest -q
```

`.github/workflows/run.yml`:

```yaml
name: run
on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch:
permissions:
  contents: write
concurrency:
  group: run
  cancel-in-progress: false
jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -e .
      - run: python -m pisobot
        env:
          DISCORD_WEBHOOK_YO: ${{ secrets.DISCORD_WEBHOOK_YO }}
          DISCORD_WEBHOOK_GRUPO: ${{ secrets.DISCORD_WEBHOOK_GRUPO }}
          DISCORD_BOT_TOKEN: ${{ secrets.DISCORD_BOT_TOKEN }}
          DISCORD_REVISAR_CHANNEL_ID: ${{ secrets.DISCORD_REVISAR_CHANNEL_ID }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          WORK_LATLON: ${{ secrets.WORK_LATLON }}
      # Guardar el estado también mantiene vivo el cron (Actions lo desactiva tras 60 días sin actividad)
      - name: Guardar estado
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data
          if ! git diff --cached --quiet; then
            git commit -m "data: actualizar estado"
            git pull --rebase
            git push
          fi
```

- [ ] **Step 6: `LICENSE` (MIT) y `README.md`**

`LICENSE`: texto MIT estándar, `Copyright (c) 2026 pallerdev`.

`README.md`:

````markdown
# brno-pisos

Bot que vigila portales de alquiler de Brno y avisa por Discord de los anuncios que encajan con dos perfiles de búsqueda. Corre gratis en GitHub Actions cada 15 minutos.

Diseño completo: `docs/superpowers/specs/2026-11-24-brno-pisos-design.md`.

## Qué lee

Bezrealitky, UlovDomov, eSpolubydleni, iDNES y Bazoš, respetando el `robots.txt` de cada uno. Sreality no se rastrea: solo se evalúan enlaces pegados a mano en `#revisar`.

## Uso local

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env              # rellenar valores
.venv/bin/python -m pisobot --dry-run
.venv/bin/pytest -q
```

`--dry-run` imprime los mensajes en vez de mandarlos y no guarda estado.

## Configuración

- Umbrales, perfiles y estimaciones de energía: `config.yaml`.
- Secretos (`.env` en local, GitHub Secrets en Actions): ver `.env.example`.

## Servicios externos

- Rutas: [Transitous](https://transitous.org) (open source, uso no comercial).
- Supermercados y geocodificación: OpenStreetMap (Overpass, Nominatim).
- Resúmenes: Gemini API, capa gratuita. Sin clave, el bot funciona solo con reglas.
````

- [ ] **Step 7: Anonimizar fixtures (repo público)**

Run:

```bash
.venv/bin/python -I - tests/fixtures <<'EOF'
import pathlib, re, sys
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?:\+420[  ]?)?\b\d{3}[  ]\d{3}[  ]\d{3}\b|\+420\d{9}")
for path in sorted(pathlib.Path(sys.argv[1]).iterdir()):
    text = path.read_text(encoding="utf-8")
    clean = PHONE.sub("000 000 000", EMAIL.sub("anon@example.com", text))
    if clean != text:
        path.write_text(clean, encoding="utf-8")
        print("anonimizado", path.name)
EOF
.venv/bin/pytest -q
```

Expected: la suite sigue en verde tras anonimizar.

- [ ] **Step 8: Primera ejecución real en seco (PEDIR PERMISO AL USUARIO: arranca la app y usa red real)**

Requisitos: `.env` con al menos `WORK_LATLON` (y `GEMINI_API_KEY` si la tiene).
Run: `.venv/bin/python -m pisobot --dry-run | tail -1`
Expected: JSON con `leidos_bezrealitky`, `leidos_ulovdomov`, `leidos_espolubydleni`, `leidos_idnes`, `leidos_bazos` todos > 0 y sin `portales_con_error`. Si un portal falla, corregir su recolector (y su fixture si la web cambió) antes de seguir.

- [ ] **Step 9: Entrega al usuario**

El controlador enseña el diff completo, propone el mensaje de commit (sin atribución a IA) y espera permiso. Después, el usuario crea el repo público en GitHub, carga los 6 secretos y lanza `run` a mano con `workflow_dispatch` para la primera ejecución.

---

## Spec coverage (auto-revisión)

| Requisito de la spec | Tarea |
|---|---|
| Perfiles `yo` / `grupo`, precio real, energías estimadas, veredictos | 2, 3 |
| Habitación propia (no 2L), lavadora, amueblado, průchozí, calefacción eléctrica | 2, 3 |
| Fuentes respetando robots.txt (Bezrealitky `/vypis/`, UlovDomov, eSpolubydleni, iDNES, Bazoš por PSČ) | 8-12 |
| Sreality y otras URLs solo vía `#revisar` | 13, 16 |
| Transitous (08:00 próximo laborable, regla del tranvía directo, caché, atribución, reintentos) | 4, 14, 16 |
| Supermercado OSM semanal, Nominatim, ubicación aproximada, Lužánky | 5, 14 |
| Dedupe por id y huella, purga a 60 días | 6, 16 |
| Gemini gratis con fallback a reglas y tope diario | 7, 16 |
| ✅ al momento, ⚠️ en resumen diario 19:00, `#revisar` con veredicto de ambos perfiles | 14, 15, 16 |
| Aviso tras 3 fallos de un portal y al recuperarse | 16 |
| GitHub Actions cada 15 min, commit de `data/`, secretos | 17 |
| Primera ejecución notifica lo que ya cumple | 16 (`seen.json` vacío ⇒ todo es nuevo) |
