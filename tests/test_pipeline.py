import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from pisobot.config import load_config
from pisobot.llm import LlmResult
from pisobot.models import Kind, Listing, Point, Shop, Trip
from pisobot.pipeline import Deps, evaluate_pasted, run
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


def test_active_listing_stays_seen_after_sixty_days(cfg, tmp_path):
    sent = Sent()
    store = Store(tmp_path)
    deps = deps_with([FakeCollector("fake", [room()])], sent=sent)
    run(cfg, store, deps, MORNING)
    later = MORNING + timedelta(days=61)
    run(cfg, store, deps, later)
    run(cfg, store, deps, later + timedelta(minutes=15))
    assert len(sent) == 1


def test_duplicate_detected_with_details_before_enrichment(cfg, tmp_path):
    class Collector(FakeCollector):
        def fetch_detail(self, listing):
            return dataclasses.replace(listing, lat=49.2, lon=16.6, area_m2=15, needs_detail=False)

    duplicate = room("2", lat=None, lon=None, needs_detail=True)
    sent, transit = Sent(), FakeTransit(FAST)
    deps = deps_with([Collector("fake", [room(), duplicate])], transit=transit, sent=sent)
    run(cfg, Store(tmp_path), deps, MORNING)
    assert len(sent) == transit.calls == 1


def test_partial_delivery_retries_only_the_failed_profile(cfg, tmp_path):
    sent = []

    def send(url, embeds, content):
        sent.append(url)
        return url != "https://hook/grupo" or sent.count(url) == 2

    listing = room(kind=Kind.FLAT, rent=7500, fees=0, fees_include_energy=True, disposition="4+1")
    deps = deps_with([FakeCollector("fake", [listing])], sent=send)
    store = Store(tmp_path)
    run(cfg, store, deps, MORNING)
    assert store.is_new(listing)
    store.save()
    store = Store(tmp_path)
    run(cfg, store, deps, MORNING)
    assert not store.is_new(listing)
    assert sent == ["https://hook/yo", "https://hook/grupo", "https://hook/grupo"]


def test_duplicate_with_pending_delivery_does_not_resend_to_successful_profile(cfg, tmp_path):
    sent = []

    def send(url, embeds, content):
        sent.append(url)
        return url.endswith("/yo")

    listing = room(kind=Kind.FLAT, rent=7500, fees=0, fees_include_energy=True, disposition="4+1")
    duplicate = dataclasses.replace(listing, source="otro")
    deps = deps_with([FakeCollector("fake", [listing]), FakeCollector("otro", [duplicate])], sent=send)
    run(cfg, Store(tmp_path), deps, MORNING)
    assert sent.count("https://hook/yo") == 1


def test_duplicate_with_fewer_eligible_profiles_does_not_cancel_retry(cfg, tmp_path):
    sent = []

    def send(url, embeds, content):
        sent.append(url)
        return url.endswith("/yo") or len(sent) >= 3

    listing = room(kind=Kind.FLAT, rent=7500, fees=0, fees_include_energy=True, disposition="4+1")
    duplicate = dataclasses.replace(listing, source="otro", disposition="2+1")
    deps = deps_with([FakeCollector("fake", [listing]), FakeCollector("otro", [duplicate])], sent=send)
    store = Store(tmp_path)
    run(cfg, store, deps, MORNING)
    assert store.is_new(listing)
    run(cfg, store, deps, MORNING)
    assert sent == ["https://hook/yo", "https://hook/grupo", "https://hook/grupo"]
    assert not store.is_new(listing)


def test_invalid_listing_does_not_stop_other_portals(cfg, tmp_path):
    invalid = room(lat="invalid")
    valid = dataclasses.replace(room(), source="otro")
    sent = Sent()
    deps = deps_with([FakeCollector("fake", [invalid]), FakeCollector("otro", [valid])], sent=sent)
    report = run(cfg, Store(tmp_path), deps, MORNING)
    assert report["anuncios_con_error"] == 1
    assert len(sent) == 1


def test_digest_retries_only_undelivered_batches(cfg, tmp_path):
    sent = []

    def send(url, embeds, content):
        sent.append([embed["url"] for embed in embeds])
        return len(sent) != 2

    store = Store(tmp_path)
    urls = [f"https://example.com/{i}" for i in range(11)]
    store.state["pending"]["yo"] = [{"url": url, "description": url} for url in urls]
    deps = deps_with([], sent=send)
    run(cfg, store, deps, EVENING)
    assert store.state["last_digest"] is None
    run(cfg, store, deps, EVENING)
    assert sent == [urls[:10], urls[10:], urls[10:]]
    assert store.state["pending"]["yo"] == []
    assert store.state["last_digest"] == "2026-11-24"


def test_review_cursor_advances_only_after_successful_reply(cfg, tmp_path):
    class Discord(FakeDiscord):
        def reply(self, channel, message_id, content, embeds):
            super().reply(channel, message_id, content, embeds)
            return len(self.replies) > 1

    discord = Discord([{"id": i, "content": "texto corto"} for i in ("20", "21")])
    deps = deps_with([], discord=discord)
    store = Store(tmp_path)
    run(cfg, store, deps, MORNING)
    assert store.state["revisar_last_id"] is None
    run(cfg, store, deps, MORNING)
    assert [reply[0] for reply in discord.replies] == ["20", "20", "21"]
    assert store.state["revisar_last_id"] == "21"


def test_review_keeps_two_profile_embeds_visible_and_uses_ephemeral_enrichment(cfg, tmp_path):
    url = "https://www.ulovdomov.cz/inzerat/1"
    page = "Pronajmu byt 4+1, 8 000 Kč vše v ceně, pračka. Pekarska."
    discord = FakeDiscord([{"id": "20", "content": url}])
    deps = deps_with([], llm=FakeLlm(LlmResult("Piso", street="Pekařská")), discord=discord)
    deps.http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=page)))
    deps.review_geocoder = FakeGeocoder()
    deps.review_transit = FakeTransit(FAST)
    run(cfg, Store(tmp_path), deps, MORNING)
    _, _, embeds = discord.replies[0]
    assert len(embeds) == 2
    assert embeds[0]["url"] == url and "url" not in embeds[1]
    assert deps.transit.calls == 0 and deps.review_transit.calls == 2


@pytest.mark.parametrize("url", ["https://example.com/listing", "https://reality.idnes.cz:9000/detail/1"])
def test_review_fetches_only_https_listings_from_supported_hosts(cfg, url):
    deps = deps_with([])
    deps.http = httpx.Client(transport=httpx.MockTransport(
        lambda request: pytest.fail(f"Petición inesperada a {request.url.host}")
    ))
    note, embeds = evaluate_pasted(url, "1", cfg, deps, MORNING)
    assert "texto completo" in note and not embeds


@pytest.mark.parametrize("target", ["https://example.com", "https://www.sreality.cz/detail/a/b/c/d/1"])
def test_review_redirect_outside_its_allowed_html_hosts_is_not_followed(cfg, target):
    def handler(request):
        assert request.url.host == "reality.idnes.cz"
        return httpx.Response(302, headers={"Location": target})

    deps = deps_with([])
    deps.http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    note, embeds = evaluate_pasted("https://reality.idnes.cz/detail/1", "1", cfg, deps, MORNING)
    assert "texto completo" in note and not embeds


def test_collector_alerts_do_not_copy_exception_content(cfg, tmp_path, capsys):
    class Collector(FakeCollector):
        def fetch_list(self):
            raise RuntimeError("texto-privado")

    discord = FakeDiscord()
    deps = deps_with([Collector("fake", [])], discord=discord)
    store = Store(tmp_path)
    for _ in range(3):
        run(cfg, store, deps, MORNING)
    assert discord.posts == ["⚠️ El portal **fake** lleva 3 ejecuciones fallando (RuntimeError)."]
    assert "texto-privado" not in capsys.readouterr().err
