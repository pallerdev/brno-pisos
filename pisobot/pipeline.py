"""Una ejecución completa: portales → prefiltro → detalle → enriquecimiento → evaluación → Discord."""
from __future__ import annotations

import dataclasses
import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
from pisobot.review import ReplyRejected, split_url
from pisobot.store import Store, fingerprint

TRANSIT_ATTEMPTS = 3
# El job de Actions se corta a los 8 min: se deja margen para guardar el estado y publicarlo
DEFAULT_BUDGET_S = 420
# Fallos transitorios seguidos con el mismo mensaje de #revisar antes de darlo por perdido
REVIEW_ATTEMPTS = 3
# En la primera lectura de #revisar solo se contesta lo reciente: el historial viejo ya no interesa
REVIEW_FIRST_RUN_WINDOW = timedelta(hours=24)
_DISCORD_EPOCH_MS = 1420070400000
_ROOM_WORDS = re.compile(r"(?<![\w-])(?<!\d )pokoj\b|spolubydl|\broom\b|\bhabitacion\b")


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
    review_geocoder: object | None = None
    review_transit: object | None = None


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class _Budget:
    """Tiempo máximo de una ejecución; al agotarse no se empieza trabajo nuevo, pero se guarda lo hecho."""

    def __init__(self, seconds: float, clock: Callable[[], float]):
        self.seconds, self.clock = seconds, clock
        self.started = clock()
        self.hit = False

    def exceeded(self) -> bool:
        if self.clock() - self.started < self.seconds:
            return False
        if not self.hit:
            self.hit = True
            _log(f"presupuesto de {self.seconds:g} s agotado: se guarda y el resto queda para la próxima ejecución")
        return True


def _guarded(step: str, action: Callable, *args) -> None:
    """Un paso que falla no debe saltarse los demás ni el guardado del estado."""
    try:
        action(*args)
    except Exception as exc:  # noqa: BLE001
        _log(f"{step}: fallo inesperado ({type(exc).__name__})")


def run(cfg: Config, store: Store, deps: Deps, now: datetime, clock: Callable[[], float] = time.monotonic) -> dict:
    tz = cfg.raw["departure"]["tz"]
    today = now.astimezone(ZoneInfo(tz)).date()
    depart = next_weekday_departure(now, cfg.raw["departure"]["time"], tz)
    budget = _Budget(cfg.raw.get("run", {}).get("budget_seconds", DEFAULT_BUDGET_S), clock)
    report: Counter[str] = Counter()
    for collector in deps.collectors:
        if budget.exceeded():
            break
        _portal(collector, cfg, store, deps, depart, today, report, budget)
        # Cada portal deja guardado lo que ya se ha enviado: un corte posterior no repite avisos
        store.save()
    _guarded("revisar", _review, cfg, store, deps, depart, report, now, budget)
    store.save()
    _guarded("resumen", _digest, cfg, store, deps, now, report)
    store.save()
    _guarded("limpieza", store.prune, today)
    if budget.hit:
        report["presupuesto_agotado"] = 1
    return dict(report)


def _portal(collector, cfg: Config, store: Store, deps: Deps, depart: datetime, today: date, report: Counter,
            budget: _Budget) -> None:
    try:
        listings = collector.fetch_list()
    except Exception as exc:  # noqa: BLE001 - un portal roto no debe parar a los demás
        _collector_failed(cfg, store, deps, collector.name, exc)
        report["portales_con_error"] += 1
        return
    _collector_ok(cfg, store, deps, collector.name)
    report[f"leidos_{collector.name}"] += len(listings)
    for listing in listings:
        if budget.exceeded():
            return
        try:
            _process(listing, collector, cfg, store, deps, depart, today, report)
        except Exception as exc:  # Un anuncio defectuoso no debe impedir guardar el resto.
            _log(f"{collector.name}: anuncio no procesado ({type(exc).__name__})")
            report["anuncios_con_error"] += 1


def _process(listing, collector, cfg, store, deps, depart, today: date, report: Counter) -> None:
    if not store.is_new(listing):
        store.refresh_seen(listing, today)
        return
    facts = parse_text(f"{listing.title}\n{listing.description}")
    names = prefilter(listing, facts, cfg)
    if names and listing.needs_detail:
        try:
            listing = collector.fetch_detail(listing)
        except Exception as exc:  # noqa: BLE001 - se reintenta en la próxima ejecución
            _log(f"{collector.name}: detalle no disponible ({type(exc).__name__})")
            return
        if not store.is_new(listing):
            store.refresh_seen(listing, today)
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
    if not store.is_new(candidate.listing):
        store.refresh_seen(candidate.listing, today)
        return
    deliveries = store.state["deliveries"]
    delivery_key = fingerprint(listing) or fingerprint(candidate.listing) or listing.key
    previous = deliveries.get(delivery_key, {})
    sent_profiles = set(previous.get("sent", ()))
    pending_profiles = set(previous.get("pending", ()))
    for name in names:
        if name in sent_profiles:
            continue
        ev = evaluate(candidate, name, cfg)
        report[f"{name}_{ev.verdict.value}"] += 1
        if ev.verdict is Verdict.PASS:
            if _send(cfg, deps, name, [build_embed(candidate, ev, cfg)]):
                sent_profiles.add(name)
                pending_profiles.discard(name)
            else:
                report["envios_fallidos"] += 1
                pending_profiles.add(name)
        elif ev.verdict is Verdict.MAYBE:
            embed = build_embed(candidate, ev, cfg)
            pending = store.state["pending"].setdefault(name, [])
            if embed not in pending:
                pending.append(embed)
            sent_profiles.add(name)
            pending_profiles.discard(name)
        elif previous.get("owner") == listing.key:
            pending_profiles.discard(name)
    if pending_profiles:
        deliveries[delivery_key] = {
            "sent": sorted(sent_profiles),
            "pending": sorted(pending_profiles),
            "owner": previous.get("owner", listing.key),
        }
    else:
        store.mark_seen(candidate.listing, today)
        # La ficha puede completar la huella que aún no venía en el listado.
        store.mark_seen(listing, today)
        deliveries.pop(delivery_key, None)


def _send(cfg: Config, deps: Deps, name: str, embeds: list[dict], content: str | None = None) -> bool:
    url = cfg.secrets.get(cfg.profiles[name]["webhook_env"])
    return bool(url and deps.send(url, embeds, content))


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
    if listing.fees is None and llm.fees is not None:
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
            fetched = single_url.fetch_review_url(deps.http, url)
        except single_url.UnsupportedReviewURL:
            if len(text) < 30:
                return "No puedo abrir ese enlace. Pega el texto completo del anuncio.", []
            url, fetched = None, ""
        except ValueError:
            # El portal respondió, pero no con un anuncio legible (p. ej. Sreality sin JSON): sirve el texto pegado
            if len(text) < 30:
                return "No he podido leer el anuncio de ese enlace; pega el texto del anuncio.", []
            fetched = ""
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
    # Discord oculta los embeds que repiten URL dentro de un mismo mensaje.
    for embed in embeds[1:]:
        if embed.get("url") == embeds[0].get("url"):
            embed.pop("url", None)
    note = None
    if candidate.listing.point is None:
        note = "No encuentro la ubicación: vuelve a pegarlo añadiendo la calle."
    return note, embeds


def _review(cfg: Config, store: Store, deps: Deps, depart: datetime, report: Counter, now: datetime,
            budget: _Budget) -> None:
    channel = cfg.secrets.get("DISCORD_REVISAR_CHANNEL_ID")
    if deps.discord is None or not channel or budget.exceeded():
        return
    try:
        messages = deps.discord.new_messages(channel, store.state["revisar_last_id"])
    except Exception as exc:  # noqa: BLE001
        _log(f"#revisar no disponible ({type(exc).__name__})")
        return
    review_deps = dataclasses.replace(
        deps,
        geocoder=deps.review_geocoder if deps.review_geocoder is not None else deps.geocoder,
        transit=deps.review_transit if deps.review_transit is not None else deps.transit,
    )
    first_run = store.state["revisar_last_id"] is None
    for message in messages:
        if budget.exceeded():
            break
        message_id = message["id"]
        content = message.get("content")
        author = message.get("author")
        bot = isinstance(author, dict) and author.get("bot")
        if bot or message.get("webhook_id") or not isinstance(content, str) or not content.strip():
            store.state["revisar_last_id"] = message_id
            continue
        if first_run and now - _sent_at(message_id) > REVIEW_FIRST_RUN_WINDOW:
            store.state["revisar_last_id"] = message_id
            continue
        try:
            note, embeds = evaluate_pasted(content.strip(), message_id, cfg, review_deps, depart)
        except Exception as exc:  # noqa: BLE001 - un mensaje raro no debe tumbar la ejecución
            _log(f"revisar: {type(exc).__name__}")
            note, embeds = f"No he podido evaluarlo ({type(exc).__name__}).", []
        try:
            delivered = deps.discord.reply(channel, message_id, note, embeds)
        except ReplyRejected as exc:
            # Reintentar daría el mismo error y bloquearía los mensajes de detrás
            _log(f"revisar: HTTP {exc.status}")
            report["revisar_omitidos"] += 1
        else:
            if delivered:
                report["revisar_respondidos"] += 1
            else:
                report["revisar_fallidos"] += 1
                failures = store.state["revisar_retries"].get(message_id, 0) + 1
                if failures < REVIEW_ATTEMPTS:
                    store.state["revisar_retries"] = {message_id: failures}
                    break
                _log(f"revisar: mensaje omitido tras {failures} fallos seguidos")
                report["revisar_omitidos"] += 1
        store.state["revisar_retries"] = {}
        store.state["revisar_last_id"] = message_id


def _sent_at(message_id: str) -> datetime:
    """Los ids de Discord (snowflakes) llevan la fecha de envío en los bits altos."""
    return datetime.fromtimestamp(((int(message_id) >> 22) + _DISCORD_EPOCH_MS) / 1000, timezone.utc)


def _digest(cfg: Config, store: Store, deps: Deps, now: datetime, report: Counter) -> None:
    local = now.astimezone(ZoneInfo(cfg.raw["departure"]["tz"]))
    today = local.date().isoformat()
    if local.hour < cfg.raw["digest"]["hour"] or store.state["last_digest"] == today:
        return
    all_sent = True
    for name, embeds in store.state["pending"].items():
        if not embeds:
            continue
        delivered = 0
        for index, part in enumerate(chunk_embeds(embeds)):
            content = f"**Resumen diario ⚠️** · {len(embeds)} anuncios dudosos" if index == 0 else None
            if not _send(cfg, deps, name, part, content):
                all_sent = False
                break
            delivered += len(part)
            report["resumen_enviados"] += len(part)
        del embeds[:delivered]
    if all_sent:
        store.state["last_digest"] = today


def _alert(cfg: Config, deps: Deps, text: str) -> bool:
    channel = cfg.secrets.get("DISCORD_REVISAR_CHANNEL_ID")
    if deps.discord is not None and channel:
        return deps.discord.post(channel, text)
    _log(text)
    return True


def _collector_failed(cfg: Config, store: Store, deps: Deps, name: str, exc: Exception) -> None:
    failures = store.state["failures"]
    alerted = store.state["failure_alerted"]
    failures[name] = failures.get(name, 0) + 1
    reason = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
    _log(f"{name}: fallo {failures[name]} seguido ({reason})")
    if failures[name] >= cfg.raw["collectors"]["failure_alert_after"] and not alerted.get(name):
        if _alert(cfg, deps, f"⚠️ El portal **{name}** lleva {failures[name]} ejecuciones fallando ({reason})."):
            alerted[name] = True


def _collector_ok(cfg: Config, store: Store, deps: Deps, name: str) -> None:
    failures = store.state["failures"]
    alerted = store.state["failure_alerted"]
    if alerted.get(name) and _alert(cfg, deps, f"✅ El portal **{name}** vuelve a funcionar."):
        alerted.pop(name)
    failures.pop(name, None)
