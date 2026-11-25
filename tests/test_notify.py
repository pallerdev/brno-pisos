import json
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


def test_group_embed_explains_the_extra_charge_per_person(cfg):
    c = candidate(kind=Kind.FLAT, rent=37000, fees=2000, fees_include_energy=True,
                  disposition="4+1", fees_per_extra_person=600)
    embed = build_embed(c, evaluate(c, "grupo", cfg), cfg)
    assert "3 pers. × 13.400 · 4 pers. × 10.200 CZK/mes" in embed["description"]
    assert "2.000 poplatky + 600 por cada persona adicional" in embed["description"]


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


def test_chunk_embeds_counts_fields_and_author():
    embeds = [{"fields": [{"name": "Alquiler", "value": "a" * 900}], "author": {"name": "Brno"}}] * 3
    assert [len(part) for part in chunk_embeds(embeds, max_chars=2000)] == [2, 1]


def test_chunk_embeds_rejects_one_embed_larger_than_the_budget():
    with pytest.raises(ValueError, match="embed"):
        chunk_embeds([{"description": "a" * 50}], max_chars=40)


def test_review_pair_fits_discord_limits_with_long_listing_information(cfg):
    c = candidate(kind=Kind.FLAT, title="🏠" * 300)
    c.shop = (Shop("Albert" * 1000, 49.2, 16.59), 300.0)
    embeds = [build_embed(c, evaluate(c, profile, cfg), cfg, show_profile=True) for profile in ("yo", "grupo")]

    for embed in embeds:
        assert len(embed["title"].encode("utf-16-le")) // 2 <= 256
        assert len(embed["description"].encode("utf-16-le")) // 2 <= 4096
    assert chunk_embeds(embeds, max_chars=6000) == [embeds]


@pytest.mark.parametrize("trip,expected", [
    (Trip(20, 0, 0), "20 min andando"),
    (Trip(20, 0, 4, ["TRAM"], [""]), "20 min · tranvía directo"),
    (Trip(20, 1, 4, ["TRAM", "BUS"], ["12", "67"]), "20 min · tranvía 12 → bus 67"),
])
def test_embed_transit_details(cfg, trip, expected):
    c = candidate()
    c.trips["work"] = [trip]
    assert f"🚋 oficina: {expected}" in build_embed(c, evaluate(c, "yo", cfg), cfg)["description"]


def test_embed_uses_text_commission_if_collector_does_not_know(cfg):
    c = candidate(commission=None)
    c.facts.commission = False
    assert "sin comisión" in build_embed(c, evaluate(c, "yo", cfg), cfg)["description"]


@pytest.mark.parametrize("use_summary", [False, True])
def test_embed_redacts_ad_contact_details_but_keeps_prices(cfg, use_summary):
    text = (
        "Kontakt: +420 (777) 000 000 / 777000000 / 777000000@example.invalid. "
        "Cena 8 000 Kč, CZK 200 000 000, 200 000 000 Kč."
    )
    c = candidate(title="Pokoj 777.000.000", description=text if not use_summary else "")
    c.summary = text if use_summary else ""

    embed = build_embed(c, evaluate(c, "yo", cfg), cfg)

    assert "777.000.000" not in embed["title"]
    assert "+420" not in embed["description"]
    assert "777000000" not in embed["description"]
    assert "@example.invalid" not in embed["description"]
    assert "Cena 8 000 Kč, CZK 200 000 000, 200 000 000 Kč" in embed["description"]


def test_send_webhook_success_and_retry_after_429():
    responses = iter([httpx.Response(429, json={"retry_after": 0.5}), httpx.Response(200, json={})])
    calls, sleeps = [], []

    def handler(request):
        calls.append(request)
        return next(responses)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    url = "https://discord.com/api/webhooks/1/abc?thread_id=42"
    assert send_webhook(http, url, [{"title": "t"}], "hola", sleep=sleeps.append)
    assert sleeps == [0.5] and len(calls) == 2
    assert calls[0].url.params["wait"] == "true"
    assert calls[0].url.params["thread_id"] == "42"
    assert json.loads(calls[0].content) == {
        "embeds": [{"title": "t"}], "content": "hola", "allowed_mentions": {"parse": []},
    }


@pytest.mark.parametrize("response,expected_sleeps", [
    (httpx.Response(429, headers={"Retry-After": "1.25"}, json=[]), [1.25]),
    (httpx.Response(429, json={"retry_after": None}), [1.0]),
    (httpx.Response(429, json={"retry_after": -1}), [1.0]),
    (httpx.Response(429, json={"retry_after": 120}), []),
])
def test_send_webhook_rate_limit_retries_at_most_once(response, expected_sleeps):
    calls, sleeps = [], []

    def handler(request):
        calls.append(request)
        return response

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert send_webhook(http, "https://discord.com/api/webhooks/1/abc", [{"title": "t"}], sleep=sleeps.append) is False
    assert sleeps == expected_sleeps
    assert len(calls) == 1 + len(expected_sleeps)


def test_send_webhook_returns_failure_on_http_error_without_logging_url(capsys):
    def handler(request):
        raise httpx.ReadTimeout(f"timeout for {request.url}", request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert send_webhook(http, "https://discord.com/api/webhooks/1/secret", [{"title": "t"}]) is False
    assert "secret" not in capsys.readouterr().err


@pytest.mark.parametrize("embeds", [
    [{"description": "a" * 3001}] * 2,
    [{"description": "a" * 4097}],
    [{"title": "t"}] * 11,
])
def test_send_webhook_rejects_a_batch_over_discord_limits_before_sending(embeds):
    def never(request):
        pytest.fail("Discord rechaza un mensaje con más de 6000 caracteres en embeds")

    http = httpx.Client(transport=httpx.MockTransport(never))
    assert send_webhook(http, "https://discord.com/api/webhooks/1/abc", embeds) is False


def test_send_webhook_returns_failure_for_redirect_without_forwarding():
    targets = []

    def handler(request):
        targets.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://example.invalid"})

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert send_webhook(http, "https://discord.com/api/webhooks/1/secret", [{"title": "t"}]) is False
    assert targets == ["discord.com"]


def test_send_webhook_failures():
    failing = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert send_webhook(failing, "https://discord.com/api/webhooks/1/abc", [{"title": "t"}]) is False

    def never(request):
        raise AssertionError("sin URL no debe haber petición")

    assert send_webhook(httpx.Client(transport=httpx.MockTransport(never)), "", [{"title": "t"}]) is False


def _recording_client(status=204):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


@pytest.mark.parametrize("url", [
    "discord.com/api/webhooks/1/token-secreto",
    "http://discord.com/api/webhooks/1/token-secreto",
    "https://example.com/api/webhooks/1/token-secreto",
    "https://discord.com.example.com/api/webhooks/1/token-secreto",
    "https://example.com/discord.com/api/webhooks/1/token-secreto",
    "https://discord.com@example.com/api/webhooks/1/token-secreto",
    '"https://discord.com/api/webhooks/1/token-secreto"',
])
def test_send_webhook_refuses_urls_outside_discord_without_a_request(url, capsys):
    http, calls = _recording_client()
    assert send_webhook(http, url, [{"title": "t"}]) is False
    assert calls == []
    captured = capsys.readouterr()
    assert "token-secreto" not in captured.err and "token-secreto" not in captured.out
    assert "discord: URL de webhook no válida" in captured.err


@pytest.mark.parametrize("host", ["discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"])
def test_send_webhook_accepts_the_discord_hosts(host):
    http, calls = _recording_client()
    assert send_webhook(http, f"https://{host}/api/webhooks/1/abc", [{"title": "t"}])
    assert [call.url.host for call in calls] == [host]


def test_send_webhook_strips_whitespace_around_the_url():
    http, calls = _recording_client()
    assert send_webhook(http, "  https://discord.com/api/webhooks/1/abc\n", [{"title": "t"}])
    assert len(calls) == 1


@pytest.mark.parametrize("status", [400, 404, 429, 500])
def test_send_webhook_logs_only_the_status_of_a_failure(status, capsys):
    http, _ = _recording_client(status)
    assert send_webhook(http, "https://discord.com/api/webhooks/1/token-secreto", [{"title": "t"}],
                        sleep=lambda seconds: None) is False
    err = capsys.readouterr().err
    assert err.splitlines() == [f"discord: HTTP {status}"]


def test_send_webhook_logs_only_the_exception_type_of_a_transport_error(capsys):
    def handler(request):
        raise httpx.ReadTimeout(f"timeout for {request.url}", request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert send_webhook(http, "https://discord.com/api/webhooks/1/token-secreto", [{"title": "t"}]) is False
    assert capsys.readouterr().err.splitlines() == ["discord: ReadTimeout"]


def test_send_webhook_logs_nothing_on_success(capsys):
    http, _ = _recording_client()
    assert send_webhook(http, "https://discord.com/api/webhooks/1/abc", [{"title": "t"}])
    assert capsys.readouterr().err == ""
