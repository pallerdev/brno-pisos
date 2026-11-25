"""Punto de entrada: python -m pisobot [--dry-run]."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import httpx

from pisobot.collectors import build_collectors
from pisobot.config import Config, load_config, load_dotenv
from pisobot.enrich.geocode import NOMINATIM_URL, Geocoder
from pisobot.enrich.supermarket import load_or_refresh
from pisobot.enrich.transit import TransitClient
from pisobot.http import make_client
from pisobot.llm import Gemini
from pisobot.models import Shop
from pisobot.notify import send_webhook
from pisobot.pipeline import Deps, run
from pisobot.review import DiscordBot
from pisobot.store import Store

ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pisobot")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="imprime los avisos sin enviarlos a Discord ni guardar el estado",
    )
    return parser


def _load_shops(cfg: Config, http: httpx.Client, path: Path, today: date, dry_run: bool) -> list[Shop]:
    options = cfg.raw["supermarket"]
    if not dry_run:
        return load_or_refresh(path, http, options["bbox"], options["refresh_days"], today)

    # Conserva el caché existente para la simulación y aísla cualquier actualización de Overpass.
    with TemporaryDirectory(prefix="pisobot-") as tmp:
        preview = Path(tmp) / path.name
        if path.is_file():
            shutil.copyfile(path, preview)
        return load_or_refresh(preview, http, options["bbox"], options["refresh_days"], today)


def _print_webhook(_url: str, embeds: list[dict], content: str | None) -> bool:
    print(json.dumps({"content": content, "embeds": embeds}, ensure_ascii=False, indent=1))
    return True


def _run(dry_run: bool) -> int:
    env = os.environ.copy()
    load_dotenv(ROOT / ".env", env)
    try:
        cfg = load_config(ROOT / "config.yaml", env)
    except ValueError:
        print("pisobot: comprueba config.yaml y WORK_LATLON (latitud,longitud)", file=sys.stderr)
        return 1

    if dry_run:
        for profile, options in cfg.profiles.items():
            cfg.secrets[options["webhook_env"]] = f"dry-run:{profile}"
        cfg.secrets["DISCORD_BOT_TOKEN"] = ""
        cfg.secrets["DISCORD_REVISAR_CHANNEL_ID"] = ""

    store = Store(ROOT / "data", persist=not dry_run)
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(cfg.raw["departure"]["tz"])).date()
    labels = {point: name for name, point in cfg.destinations.items()}
    llm_cfg = cfg.raw["llm"]

    try:
        with make_client(cfg.raw["user_agent"]) as http:
            shops = _load_shops(cfg, http, store.dir / "supermarkets.json", today, dry_run)
            # Nominatim limita las ejecuciones periódicas a cuatro peticiones por minuto.
            geocoder = Geocoder(http, store.cache["geo"], min_interval=15, endpoint=env.get("NOMINATIM_URL") or NOMINATIM_URL)
            token = cfg.secrets["DISCORD_BOT_TOKEN"]
            discord = DiscordBot(http, token) if token else None
            deps = Deps(
                http=http,
                collectors=build_collectors(http, cfg),
                transit=TransitClient(http, store.cache["transit"], dest_labels=labels),
                geocoder=geocoder,
                llm=Gemini(
                    http, cfg.secrets["GEMINI_API_KEY"], llm_cfg["model"],
                    llm_cfg["daily_limit"], store.state["llm"], today,
                ),
                shops=shops,
                send=_print_webhook if dry_run else partial(send_webhook, http),
                discord=discord,
                review_geocoder=geocoder.with_cache({}),
                review_transit=TransitClient(http, {}, dest_labels=labels),
            )
            report = run(cfg, store, deps, now)
    finally:
        # Un fallo a mitad no puede perder los avisos ya enviados: se volverían a enviar en la próxima ejecución
        store.save()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args.dry_run)
    except Exception as exc:
        # Los errores HTTP pueden incluir URLs con claves, webhooks o coordenadas privadas.
        print(f"pisobot: ejecución interrumpida ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
