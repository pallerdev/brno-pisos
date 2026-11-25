"""Embeds de Discord y envío por webhook."""
from __future__ import annotations

import math
import re
import sys
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
_DESCRIPTION_LIMIT = 2600  # Caben los dos perfiles de #revisar en el mismo mensaje.
_MAX_EMBEDS = 10
_MAX_EMBED_CHARS = 6000
_MAX_RETRY_DELAY = 10
# El secreto del webhook lo pone quien configura el repo: no debe poder dirigir el envío a otro servidor
_WEBHOOK_HOSTS = frozenset({"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"})

_EMAIL = re.compile(r"[\w.+%-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(
    r"(?<!\w)(?:(?:\+420|00420)[ .\u00a0\u202f-]*)?"
    r"(?:[2-9]\d{2}|\([2-9]\d{2}\))(?:[ .\u00a0\u202f-]*\d{3}){2}(?!\w)"
)
_PRICE_BEFORE = re.compile(r"\b(?:CZK|Kč)\s+(?:\d{1,3}[ .\u00a0\u202f])*$", re.I)
_PRICE_AFTER = re.compile(r"\s*(?:CZK\b|Kč\b|korun\w*\b|,-)", re.I)
_HIDDEN_CONTACT = "[contacto omitido]"


def czk(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _yes_no(value: bool | None) -> str:
    return {True: "sí", False: "no", None: "?"}[value]


def _redact_contacts(text: str) -> str:
    # Los embeds dudosos quedan en la cola pública de resúmenes.
    text = _EMAIL.sub(_HIDDEN_CONTACT, text)

    def redact_phone(match: re.Match[str]) -> str:
        if _PRICE_BEFORE.search(text, 0, match.start()) or _PRICE_AFTER.match(text, match.end()):
            return match.group()
        return _HIDDEN_CONTACT

    return _PHONE.sub(redact_phone, text)


def _clip(text: str, limit: int) -> str:
    encoded = text.encode("utf-16-le", errors="replace")
    if len(encoded) <= limit * 2:
        return encoded.decode("utf-16-le")
    return encoded[:(limit - 1) * 2].decode("utf-16-le", errors="ignore").rstrip() + "…"


def _snippet(text: str, limit: int = 300) -> str:
    return _clip(" ".join(_redact_contacts(text).split()), limit)


def _price_line(ev: Evaluation) -> str:
    price = ev.price
    if price is None:
        return "💰 Precio no indicado"
    if ev.per_person:
        people = " · ".join(f"{n} pers. × {czk(v)}" for n, v in sorted(ev.per_person.items()))
        if price.fees_per_extra_person:
            head = f"💰 {people} CZK/mes"
        else:
            head = f"💰 {people} (total {czk(price.total)})"
    else:
        head = f"💰 {czk(price.total)} CZK/mes en total"
    parts = [f"{czk(price.rent)} alquiler"]
    if price.fees:
        parts.append(f"{czk(price.fees)} poplatky")
    if price.fees_per_extra_person:
        parts.append(f"{czk(price.fees_per_extra_person)} por cada persona adicional")
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
    segments = []
    for i, mode in enumerate(trip.modes):
        line = trip.lines[i] if i < len(trip.lines) else ""
        segments.append(f"{MODES.get(mode, mode.lower())} {line}".rstrip())
    route = " → ".join(segments)
    if trip.transfers == 0 and len(segments) == 1:
        route += " directo"
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
    commission = listing.commission if listing.commission is not None else c.facts.commission
    if commission is False:
        kind_line += " · sin comisión"
    elif commission:
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
    summary = _snippet(c.summary or listing.description)
    if summary:
        lines.append(f"📝 {summary}")

    star = " ⭐" if ev.star else ""
    tag = f" [{ev.profile}]" if show_profile else ""
    embed = {
        "title": _clip(f"{ICON[ev.verdict]}{star}{tag} {_redact_contacts(listing.title)}", 256),
        "description": _clip("\n".join(lines), _DESCRIPTION_LIMIT),
        "color": COLOR[ev.verdict],
        "footer": {"text": FOOTER},
    }
    if listing.url:
        embed["url"] = listing.url
    return embed


def _text_size(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _embed_size(embed: dict) -> int:
    fields = embed.get("fields", [])
    if len(fields) > 25:
        raise ValueError("demasiados campos en un embed de Discord")
    texts = [
        (embed.get("title", ""), 256),
        (embed.get("description", ""), 4096),
        (embed.get("footer", {}).get("text", ""), 2048),
        (embed.get("author", {}).get("name", ""), 256),
    ]
    for field in fields:
        texts.extend([(field.get("name", ""), 256), (field.get("value", ""), 1024)])
    sizes = [(_text_size(value), limit) for value, limit in texts]
    if any(size > limit for size, limit in sizes):
        raise ValueError("un campo del embed supera el límite de Discord")
    return sum(size for size, _ in sizes)


def chunk_embeds(embeds: list[dict], max_count: int = _MAX_EMBEDS, max_chars: int = 5500) -> list[list[dict]]:
    max_count, max_chars = min(max_count, _MAX_EMBEDS), min(max_chars, _MAX_EMBED_CHARS)
    if max_count < 1 or max_chars < 1:
        raise ValueError("los límites de embeds deben ser positivos")
    chunks, current, size = [], [], 0
    for embed in embeds:
        embed_size = _embed_size(embed)
        if embed_size > max_chars:
            raise ValueError("un embed supera el tamaño máximo del lote")
        if current and (len(current) == max_count or size + embed_size > max_chars):
            chunks.append(current)
            current, size = [], 0
        current.append(embed)
        size += embed_size
    if current:
        chunks.append(current)
    return chunks


def _retry_delay(response: httpx.Response) -> float:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    seconds = payload.get("retry_after") if isinstance(payload, dict) else None
    for value in (response.headers.get("Retry-After"), seconds):
        if isinstance(value, bool):
            continue
        try:
            delay = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(delay) and delay >= 0:
            return delay
    return 1.0


def _log(reason: str) -> None:
    # Solo estado o tipo de error: la URL del webhook es un secreto y Actions publica los logs
    print(f"discord: {reason}", file=sys.stderr, flush=True)


def send_webhook(http: httpx.Client, url: str, embeds: list[dict], content: str | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
    url = url.strip() if url else url
    if not url or not (content or embeds) or (content and _text_size(content) > 2000):
        return False
    try:
        if len(chunk_embeds(embeds, max_chars=_MAX_EMBED_CHARS)) > 1:
            return False
    except ValueError:
        return False
    try:
        target = httpx.URL(url).copy_merge_params({"wait": "true"})
    except (ValueError, httpx.InvalidURL):
        _log("URL de webhook no válida")
        return False
    if target.scheme != "https" or target.host not in _WEBHOOK_HOSTS:
        _log("URL de webhook no válida")
        return False

    body: dict = {"embeds": embeds, "allowed_mentions": {"parse": []}}
    if content:
        body["content"] = content
    for attempt in range(2):
        try:
            response = http.post(target, json=body, timeout=20, follow_redirects=False)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            _log(type(exc).__name__)
            return False
        if response.status_code != 429 or attempt == 1:
            if not response.is_success:
                _log(f"HTTP {response.status_code}")
            return response.is_success
        delay = _retry_delay(response)
        if delay > _MAX_RETRY_DELAY:
            _log(f"HTTP {response.status_code}")
            return False
        sleep(delay)
    return False
