import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from pisobot.enrich.transit import TransitClient, next_weekday_departure, parse_plan
from pisobot.models import Point, Trip

FIXTURES = Path(__file__).parent / "fixtures"
PLAN = json.loads((FIXTURES / "transitous_plan.json").read_text(encoding="utf-8"))


def test_parse_plan_real_fixture():
    trips = parse_plan(PLAN)
    assert len(trips) == 4
    first = trips[0]
    assert (first.minutes, first.transfers, first.first_walk_min) == (24, 0, 4)
    assert first.modes == ["TRAM"] and first.lines == ["12"]


def test_parse_plan_marks_trolleybus():
    payload = {"itineraries": [
        {"duration": 900, "transfers": 0, "legs": [
            {"mode": "WALK", "duration": 120},
            {"mode": "BUS", "routeType": 800, "routeShortName": "25", "duration": 600},
        ]},
    ]}
    (trolley,) = parse_plan(payload)
    assert trolley.modes == ["TROLLEYBUS"] and trolley.lines == ["25"]


def test_parse_plan_reads_walk_only_from_direct():
    payload = {"itineraries": [], "direct": [
        {"duration": 600, "transfers": 0, "legs": [{"mode": "WALK", "duration": 600}]},
    ]}
    assert parse_plan(payload) == [Trip(minutes=10, transfers=0, first_walk_min=10, modes=[], lines=[])]


def test_parse_plan_skips_malformed_itinerary():
    good = {"duration": 600, "transfers": 0, "legs": [{"mode": "WALK", "duration": 60}]}
    payload = {"itineraries": [good, {"legs": []}, None, good]}
    assert len(parse_plan(payload)) == 2


def test_parse_plan_first_leg_not_walk_has_no_first_walk():
    payload = {"itineraries": [
        {"duration": 600, "transfers": 0, "legs": [
            {"mode": "TRAM", "routeShortName": "12", "duration": 600},
        ]},
    ]}
    (trip,) = parse_plan(payload)
    assert trip.first_walk_min == 0 and trip.modes == ["TRAM"]


def test_parse_plan_empty_legs():
    payload = {"itineraries": [{"duration": 60, "transfers": 0, "legs": []}]}
    assert parse_plan(payload) == [Trip(minutes=1, transfers=0, first_walk_min=0, modes=[], lines=[])]


def test_next_weekday_departure():
    tuesday = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    assert next_weekday_departure(tuesday, "08:00", "Europe/Prague") == datetime(2026, 11, 25, 7, 0, tzinfo=timezone.utc)
    friday = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)
    assert next_weekday_departure(friday, "08:00", "Europe/Prague") == datetime(2026, 11, 30, 7, 0, tzinfo=timezone.utc)


def test_next_weekday_departure_summer_time():
    # En verano Praga es UTC+2: 08:00 locales son 06:00 UTC
    tuesday = datetime(2027, 6, 22, 10, 0, tzinfo=timezone.utc)
    assert next_weekday_departure(tuesday, "08:00", "Europe/Prague") == datetime(2027, 6, 23, 6, 0, tzinfo=timezone.utc)


def test_next_weekday_departure_across_dst_jump():
    # El cambio a horario de verano cae el domingo 28 de marzo de 2027
    friday_night = datetime(2027, 3, 26, 23, 30, tzinfo=timezone.utc)
    assert next_weekday_departure(friday_night, "08:00", "Europe/Prague") == datetime(2027, 3, 29, 6, 0, tzinfo=timezone.utc)


def _client(handler, cache, dest_labels=None):
    return TransitClient(httpx.Client(transport=httpx.MockTransport(handler)), cache, dest_labels)


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


def test_client_cache_hit_returns_same_trips():
    client = _client(lambda r: httpx.Response(200, json=PLAN), {})
    depart = datetime(2026, 11, 25, 7, 0, tzinfo=timezone.utc)
    first = client.trips(Point(49.21, 16.593), Point(49.1906, 16.6127), depart)
    second = client.trips(Point(49.21, 16.593), Point(49.1906, 16.6127), depart)
    assert second == first


def test_client_different_destination_misses_cache():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=PLAN)

    client = _client(handler, {})
    depart = datetime(2026, 11, 25, 7, 0, tzinfo=timezone.utc)
    client.trips(Point(49.21, 16.593), Point(49.1906, 16.6127), depart)
    client.trips(Point(49.21, 16.593), Point(49.2, 16.6), depart)
    assert len(calls) == 2


def test_client_cache_key_hides_labelled_destination():
    cache = {}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=PLAN)

    work = Point(49.1906, 16.6127)
    client = _client(handler, cache, dest_labels={work: "work"})
    depart = datetime(2026, 11, 25, 7, 0, tzinfo=timezone.utc)
    client.trips(Point(49.21, 16.593), work, depart)
    assert all("49.1906" not in key and "16.6127" not in key for key in cache)
    assert "49.2100,16.5930>work" in cache
    # ~9 m de diferencia en el origen: mismo celda de 50 m, debe salir del caché
    client.trips(Point(49.21008, 16.59308), work, depart)
    assert len(calls) == 1


def test_client_returns_none_on_error_and_does_not_cache():
    cache = {}
    client = _client(lambda r: httpx.Response(503), cache)
    depart = datetime(2026, 11, 25, 7, 0, tzinfo=timezone.utc)
    assert client.trips(Point(49.2, 16.6), Point(49.19, 16.61), depart) is None
    assert cache == {}


def test_client_returns_none_on_malformed_response_and_does_not_cache():
    depart = datetime(2026, 11, 25, 7, 0, tzinfo=timezone.utc)
    for body in ({"itineraries": None}, []):
        cache = {}
        client = _client(lambda r, body=body: httpx.Response(200, json=body), cache)
        assert client.trips(Point(49.2, 16.6), Point(49.19, 16.61), depart) is None
        assert cache == {}


def test_client_caches_empty_result():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"itineraries": [], "direct": []})

    cache = {}
    client = _client(handler, cache)
    depart = datetime(2026, 11, 25, 7, 0, tzinfo=timezone.utc)
    assert client.trips(Point(49.2, 16.6), Point(49.19, 16.61), depart) == []
    assert client.trips(Point(49.2, 16.6), Point(49.19, 16.61), depart) == []
    assert len(calls) == 1 and len(cache) == 1
