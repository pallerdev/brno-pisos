import json
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest

from pisobot.enrich.supermarket import fetch_supermarkets, haversine_m, load_or_refresh, nearest
from pisobot.models import Point, Shop

BBOX = [0, 0, 1, 1]
TODAY = date(2026, 11, 24)
OLD_FILE = {"fetched": "2026-11-01", "shops": [{"name": "A", "lat": 1.0, "lon": 2.0}]}

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


def fixed_client(response_factory, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(request)
        return response_factory()
    return httpx.Client(transport=httpx.MockTransport(handler))


def write_old_file(path):
    path.write_text(json.dumps(OLD_FILE))


def data_without_attempt(path):
    data = json.loads(path.read_text())
    data.pop("attempted", None)
    return data


def test_haversine_known_distance():
    # 0,001 grados de latitud ≈ 111 m
    assert haversine_m(Point(49.2, 16.6), Point(49.201, 16.6)) == pytest.approx(111, abs=1)


def test_fetch_supermarkets_parses_nodes_and_ways():
    calls = []
    shops = fetch_supermarkets(overpass_client(calls), [49.10, 16.45, 49.30, 16.75])
    assert shops == [Shop("Albert", 49.2001, 16.6001), Shop("Lidl Lesná", 49.21, 16.61)]
    query = parse_qs(calls[0].content.decode())["data"][0]
    assert "(49.1,16.45,49.3,16.75)" in query
    assert "nwr" in query and "out center" in query


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"elements": [], "remark": "runtime error: Query timed out"}),
    httpx.Response(200, json={"elements": []}),
    httpx.Response(200, json=[{"type": "node"}]),
    httpx.Response(200, content=b"<html>504 Gateway Time-out</html>"),
])
def test_fetch_supermarkets_raises_on_invalid_body(response):
    with pytest.raises(ValueError):
        fetch_supermarkets(fixed_client(lambda: response), BBOX)


def test_nearest():
    shops = [Shop("A", 49.2, 16.6), Shop("B", 49.25, 16.6)]
    shop, distance = nearest(shops, Point(49.201, 16.6))
    assert shop.name == "A" and distance == pytest.approx(111, abs=1)
    assert nearest([], Point(49.2, 16.6)) is None


def test_load_or_refresh_uses_fresh_file(tmp_path):
    path = tmp_path / "supermarkets.json"
    path.write_text(json.dumps({"fetched": "2026-11-20", "shops": [{"name": "A", "lat": 1.0, "lon": 2.0}]}))
    calls = []
    shops = load_or_refresh(path, overpass_client(calls), BBOX, 7, TODAY)
    assert shops == [Shop("A", 1.0, 2.0)] and calls == []


def test_load_or_refresh_refetches_stale_file(tmp_path):
    path = tmp_path / "supermarkets.json"
    path.write_text(json.dumps({"fetched": "2026-11-01", "shops": []}))
    calls = []
    shops = load_or_refresh(path, overpass_client(calls), BBOX, 7, TODAY)
    assert len(shops) == 2 and len(calls) == 1
    assert json.loads(path.read_text())["fetched"] == "2026-11-24"


def test_load_or_refresh_keeps_old_file_on_error(tmp_path):
    path = tmp_path / "supermarkets.json"
    write_old_file(path)
    failing = fixed_client(lambda: httpx.Response(504))
    assert load_or_refresh(path, failing, BBOX, 7, TODAY) == [Shop("A", 1.0, 2.0)]
    assert data_without_attempt(path) == OLD_FILE


def test_load_or_refresh_keeps_old_data_on_overpass_remark(tmp_path):
    path = tmp_path / "supermarkets.json"
    write_old_file(path)
    timed_out = fixed_client(lambda: httpx.Response(
        200, json={"elements": [], "remark": "runtime error: Query timed out"}))
    assert load_or_refresh(path, timed_out, BBOX, 7, TODAY) == [Shop("A", 1.0, 2.0)]
    assert data_without_attempt(path) == OLD_FILE


def test_load_or_refresh_keeps_old_data_on_malformed_body(tmp_path):
    path = tmp_path / "supermarkets.json"
    write_old_file(path)
    malformed = fixed_client(lambda: httpx.Response(200, content=b"<html>504 Gateway Time-out</html>"))
    assert load_or_refresh(path, malformed, BBOX, 7, TODAY) == [Shop("A", 1.0, 2.0)]
    assert data_without_attempt(path) == OLD_FILE


def test_load_or_refresh_creates_missing_data_dir(tmp_path):
    path = tmp_path / "data" / "supermarkets.json"
    calls = []
    shops = load_or_refresh(path, overpass_client(calls), BBOX, 7, TODAY)
    assert len(shops) == 2
    assert json.loads(path.read_text())["fetched"] == "2026-11-24"


def test_load_or_refresh_without_file_and_failing_service(tmp_path):
    failing = fixed_client(lambda: httpx.Response(504))
    path = tmp_path / "supermarkets.json"
    assert load_or_refresh(path, failing, BBOX, 7, TODAY) == []
    assert not path.exists()


def test_overpass_failure_leaves_one_log_line(tmp_path, capsys):
    load_or_refresh(tmp_path / "a.json", fixed_client(lambda: httpx.Response(429)), BBOX, 7, TODAY)
    load_or_refresh(tmp_path / "b.json", fixed_client(lambda: httpx.Response(200, text="<html>")), BBOX, 7, TODAY)
    assert capsys.readouterr().err == "overpass: HTTP 429\noverpass: JSONDecodeError\n"


def test_failing_service_is_asked_once_per_day(tmp_path):
    path = tmp_path / "supermarkets.json"
    write_old_file(path)
    calls = []
    failing = fixed_client(lambda: httpx.Response(504), calls)
    assert load_or_refresh(path, failing, BBOX, 7, TODAY) == [Shop("A", 1.0, 2.0)]
    assert load_or_refresh(path, failing, BBOX, 7, TODAY) == [Shop("A", 1.0, 2.0)]
    assert len(calls) == 1


def test_failing_service_is_retried_next_day(tmp_path):
    path = tmp_path / "supermarkets.json"
    write_old_file(path)
    calls = []
    failing = fixed_client(lambda: httpx.Response(504), calls)
    load_or_refresh(path, failing, BBOX, 7, TODAY)
    load_or_refresh(path, failing, BBOX, 7, date(2026, 11, 25))
    assert len(calls) == 2


@pytest.mark.parametrize("content", [
    '{"fetched": "2026-11-2',
    json.dumps({"shops": []}),
    json.dumps({"fetched": "ayer", "shops": []}),
    json.dumps({"fetched": "2026-11-20"}),
])
def test_corrupt_file_is_treated_as_absent(tmp_path, content):
    path = tmp_path / "supermarkets.json"
    path.write_text(content)
    calls = []
    shops = load_or_refresh(path, overpass_client(calls), BBOX, 7, TODAY)
    assert len(shops) == 2 and len(calls) == 1
    assert json.loads(path.read_text())["fetched"] == "2026-11-24"
