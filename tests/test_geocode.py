import httpx
import pytest

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
    assert sleeps == [pytest.approx(0.8)]


def test_geocode_miss_and_error():
    cache, sleeps = {}, []
    assert make(lambda r: httpx.Response(200, json=[]), cache, sleeps).geocode("Nowhere") is None
    assert cache == {"nowhere": None}
    error_cache = {}
    assert make(lambda r: httpx.Response(500), error_cache, []).geocode("X") is None
    assert error_cache == {}


def test_geocode_malformed_response_returns_none_without_caching():
    cache = {}
    geocoder = make(lambda r: httpx.Response(200, json=[{"display_name": "sin coordenadas"}]), cache, [])
    assert geocoder.geocode("Sin coords") is None
    assert cache == {}


def test_geocode_connect_timeout_still_throttles_next_request():
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("timeout")
        return httpx.Response(200, json=[{"lat": "49.2071", "lon": "16.5963"}])

    sleeps = []
    geocoder = make(handler, {}, sleeps)
    assert geocoder.geocode("Primera, Brno") is None
    assert geocoder.geocode("Segunda, Brno") == Point(49.2071, 16.5963)
    assert len(attempts) == 2
    assert sleeps == [pytest.approx(0.8)]


def test_geocode_cached_miss_makes_no_request():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(200, json=[])

    cache = {"nowhere": None}
    assert make(handler, cache, []).geocode("Nowhere") is None
    assert attempts == []


def test_cache_for_review_shares_pacing_but_does_not_store_private_queries():
    cache, sleeps = {}, []
    clock = iter([0.0, 0.2, 15.0]).__next__
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    geocoder = Geocoder(httpx.Client(transport=transport), cache, sleep=sleeps.append, clock=clock, min_interval=15)
    review = geocoder.with_cache({})

    assert geocoder.geocode("Calle pública, Brno") is None
    assert review.geocode("Calle de Discord, Brno") is None
    assert review.geocode("Calle de Discord, Brno") is None
    assert sleeps == [pytest.approx(14.8)]
    assert cache == {"calle pública, brno": None}
