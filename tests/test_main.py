import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from pisobot import __main__ as cli
from pisobot.config import SECRET_KEYS
from pisobot.models import Point
from pisobot.store import Store


ROOT = Path(__file__).resolve().parents[1]
WEBHOOK = "https://discord.com/api/webhooks/123/local-test-token"


def test_parser_dry_run_flag():
    assert cli.build_parser().parse_args(["--dry-run"]).dry_run is True
    assert cli.build_parser().parse_args([]).dry_run is False


@pytest.fixture
def local_project(monkeypatch, tmp_path):
    for name in SECRET_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    (tmp_path / "config.yaml").write_bytes((ROOT / "config.yaml").read_bytes())
    (tmp_path / ".env").write_text(
        "WORK_LATLON=49.1234,16.5432\n"
        f"DISCORD_WEBHOOK_YO={WEBHOOK}\n"
        "DISCORD_BOT_TOKEN=local-test-bot-token\n"
        "DISCORD_REVISAR_CHANNEL_ID=456\n",
        encoding="utf-8",
    )
    return tmp_path


def test_dry_run_previews_without_sending_or_saving(local_project, monkeypatch, capsys):
    data = local_project / "data"
    data.mkdir()
    super_cache = data / "supermarkets.json"
    saved = '{"fetched":"2020-01-01","shops":[{"name":"Viejo","lat":49.2,"lon":16.6}]}'
    super_cache.write_text(saved, encoding="utf-8")
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "overpass-api.de"
        return httpx.Response(200, json={"elements": [{
            "lat": 49.2, "lon": 16.6, "tags": {"name": "Nuevo"},
        }]})

    def run(cfg, store, deps, now):
        assert deps.discord is None
        assert [shop.name for shop in deps.shops] == ["Nuevo"]
        assert deps.send(cfg.secrets["DISCORD_WEBHOOK_YO"], [], "Un piso")
        assert deps.send(cfg.secrets["DISCORD_WEBHOOK_GRUPO"], [], "Otro piso")
        store.state["last_digest"] = "2026-11-24"
        return {"leidos_bazos": 1}

    monkeypatch.setattr(cli, "make_client", lambda agent: httpx.Client(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(cli, "run", run)
    assert cli.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Un piso" in out and "Otro piso" in out
    assert "local-test" not in out and WEBHOOK not in out
    assert json.loads(out.splitlines()[-1]) == {"leidos_bazos": 1}
    assert len(requests) == 1
    assert list(data.iterdir()) == [super_cache]
    assert super_cache.read_text(encoding="utf-8") == saved


def test_live_run_persists_only_scan_cache_with_label_for_work(local_project, monkeypatch, capsys):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "overpass-api.de":
            return httpx.Response(200, json={"elements": [{"lat": 49.2, "lon": 16.6}]})
        if request.url.host == "api.transitous.org":
            return httpx.Response(200, json={"itineraries": []})
        if request.url.host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json=[])
        assert str(request.url).split("?")[0] == WEBHOOK
        return httpx.Response(204)

    def run(cfg, store, deps, now):
        assert deps.discord is not None
        depart = datetime(2026, 11, 25, 8, tzinfo=timezone.utc)
        assert deps.transit.trips(Point(49.2, 16.6), cfg.destinations["work"], depart) == []
        assert deps.review_transit.trips(Point(49.21, 16.61), cfg.destinations["work"], depart) == []
        assert deps.review_geocoder.geocode("Calle pegada en Discord") is None
        assert deps.send(cfg.secrets["DISCORD_WEBHOOK_YO"], [], "Un piso")
        store.state["last_digest"] = "2026-11-24"
        return {"leidos_bazos": 1}

    monkeypatch.setattr(cli, "make_client", lambda agent: httpx.Client(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(cli, "run", run)
    assert cli.main([]) == 0
    saved = Store(local_project / "data")
    assert saved.cache == {"transit": {"49.2000,16.6000>work": []}, "geo": {}}
    assert saved.state["last_digest"] == "2026-11-24"
    assert [request.url.host for request in requests] == [
        "overpass-api.de", "api.transitous.org", "api.transitous.org", "nominatim.openstreetmap.org", "discord.com",
    ]
    assert json.loads(capsys.readouterr().out) == {"leidos_bazos": 1}


def test_unexpected_errors_do_not_log_secret_values(local_project, monkeypatch, capsys):
    def respond(request):
        assert request.url.host == "overpass-api.de"
        return httpx.Response(200, json={"elements": [{"lat": 49.2, "lon": 16.6}]})

    def run(cfg, store, deps, now):
        raise RuntimeError(f"Fallo en {cfg.secrets['DISCORD_WEBHOOK_YO']}")

    monkeypatch.setattr(cli, "make_client", lambda agent: httpx.Client(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(cli, "run", run)
    assert cli.main([]) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert WEBHOOK not in err
