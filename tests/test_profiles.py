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


def test_group_uses_monthly_fees_for_each_occupancy(cfg):
    listing = make(kind=Kind.FLAT, rent=40000, fees=2000, fees_per_extra_person=1000,
                   disposition="4+1", fees_include_energy=True)
    evaluation = evaluate(cand(listing), "grupo", cfg)
    assert evaluation.per_person == {3: 14667, 4: 11250}
    assert evaluation.verdict is Verdict.MAYBE
    listing.rent = 43500
    assert "grupo" not in prefilter(listing, GOOD, cfg)


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


@pytest.mark.parametrize("trip,fails", [
    (Trip(30, 0, 5, ["BUS"], ["44"]), []),
    (Trip(31, 0, 5, ["BUS"], ["44"]), ["transporte 31 min"]),
    (Trip(40, 0, 5, ["TRAM"], ["4"]), []),
    (Trip(41, 0, 5, ["TRAM"], ["4"]), ["transporte 41 min"]),
    (Trip(40, 0, 10, ["TRAM"], ["4"]), []),
    (Trip(40, 0, 11, ["TRAM"], ["4"]), ["transporte 40 min"]),
])
def test_transit_boundaries_in_evaluation(cfg, trip, fails):
    ev = evaluate(cand(make(), trip=trip), "yo", cfg)
    assert ev.fails == fails


# --- evaluación yo ---

def test_yo_pass_with_star(cfg):
    ev = evaluate(cand(make(rent=7000, fees=800)), "yo", cfg)
    assert ev.verdict is Verdict.PASS and ev.star


def test_yo_fail_price(cfg):
    ev = evaluate(cand(make(rent=9500, fees=1500)), "yo", cfg)
    assert ev.verdict is Verdict.FAIL
    assert "precio" in ev.fails[0]


@pytest.mark.parametrize("total,fails", [
    (10000, []),
    (10001, ["precio 10001 > 10000"]),
])
def test_yo_price_boundary(cfg, total, fails):
    assert evaluate(cand(make(rent=total)), "yo", cfg).fails == fails


@pytest.mark.parametrize("total,star", [(8000, True), (8001, False)])
def test_yo_star_boundary(cfg, total, star):
    ev = evaluate(cand(make(rent=total)), "yo", cfg)
    assert ev.verdict is Verdict.PASS and ev.star is star


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
    assert ev.fails == ["supermercado a 900 m"]


@pytest.mark.parametrize("distance,fails", [
    (640, []),
    (640.5, ["supermercado a 641 m"]),
    (641, ["supermercado a 641 m"]),
])
def test_supermarket_boundary(cfg, distance, fails):
    ev = evaluate(cand(make(), shop=(Shop("Albert", 49.2, 16.6), distance)), "yo", cfg)
    assert ev.fails == fails


def test_supermarket_fail_is_not_star(cfg):
    ev = evaluate(cand(make(rent=7500), shop=(Shop("Lidl", 49.2, 16.6), 641.0)), "yo", cfg)
    assert ev.verdict is Verdict.FAIL
    assert ev.star is False


def test_yo_maybe_unfurnished(cfg):
    ev = evaluate(cand(make(furnished=False)), "yo", cfg)
    assert "sin amueblar" in ev.questions


def test_yo_fail_shared_bedroom(cfg):
    ev = evaluate(cand(make(), facts=TextFacts(washing_machine=True, shared_bedroom=True)), "yo", cfg)
    assert ev.verdict is Verdict.FAIL
    assert "habitación compartida (2L)" in ev.fails


def test_room_without_price_is_maybe(cfg):
    ev = evaluate(cand(make(rent=None)), "yo", cfg)
    assert ev.verdict is Verdict.MAYBE
    assert ev.questions == ["precio no indicado"]


def test_empty_transit_list_is_maybe(cfg):
    c = Candidate(listing=make(), facts=GOOD, trips={"work": []}, shop=NEAR_SHOP)
    ev = evaluate(c, "yo", cfg)
    assert ev.verdict is Verdict.MAYBE
    assert ev.questions == ["sin ruta en transporte público"]


def test_no_shop_data_is_maybe(cfg):
    ev = evaluate(cand(make(), shop=None), "yo", cfg)
    assert ev.verdict is Verdict.MAYBE
    assert ev.questions == ["sin datos de supermercado"]


def test_unknown_location_is_maybe(cfg):
    ev = evaluate(cand(make(lat=None, lon=None)), "yo", cfg)
    assert ev.verdict is Verdict.MAYBE
    assert "ubicación desconocida: falta la calle" in ev.questions


def test_electric_heating_is_maybe(cfg):
    ev = evaluate(cand(make(), facts=TextFacts(washing_machine=True, electric_heating=True)), "yo", cfg)
    assert ev.verdict is Verdict.MAYBE
    assert "calefacción eléctrica: energías más altas" in ev.questions


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


@pytest.mark.parametrize("disposition,per_person", [("3+1", 3), ("4+1", 4)])
@pytest.mark.parametrize("per,verdict,fails,questions", [
    (11000, Verdict.PASS, [], []),
    (11001, Verdict.MAYBE, [], ["caro, solo si compensa"]),
    (12000, Verdict.MAYBE, [], ["caro, solo si compensa"]),
    (12001, Verdict.FAIL, ["12001 por persona > 12000"], []),
])
def test_grupo_per_person_boundaries(cfg, disposition, per_person, per, verdict, fails, questions):
    listing = make(kind=Kind.FLAT, rent=per * per_person, fees_include_energy=True, disposition=disposition)
    ev = evaluate(cand(listing), "grupo", cfg)
    assert ev.verdict is verdict
    assert ev.fails == fails
    assert ev.questions == questions


def test_grupo_too_few_bedrooms_fails(cfg):
    listing = make(kind=Kind.FLAT, rent=20000, fees_include_energy=True, disposition="3+kk")
    ev = evaluate(cand(listing), "grupo", cfg)
    assert ev.verdict is Verdict.FAIL
    assert ev.fails == ["solo 2 dormitorios"]


@pytest.mark.parametrize("disposition,fails", [
    ("2+1", ["solo 2 dormitorios"]),
    ("3+kk", ["solo 2 dormitorios"]),
    ("3+1", []),
    ("4+kk", []),
])
def test_grupo_bedroom_boundaries(cfg, disposition, fails):
    listing = make(kind=Kind.FLAT, rent=20000, fees_include_energy=True, disposition=disposition)
    assert evaluate(cand(listing), "grupo", cfg).fails == fails


def test_grupo_bedroom_fail_does_not_need_price(cfg):
    priced = evaluate(cand(make(kind=Kind.FLAT, rent=20000, fees_include_energy=True, disposition="2+1")), "grupo", cfg)
    unpriced = evaluate(cand(make(kind=Kind.FLAT, rent=None, disposition="2+1")), "grupo", cfg)
    assert priced.verdict is Verdict.FAIL and unpriced.verdict is Verdict.FAIL
    assert unpriced.fails == priced.fails == ["solo 2 dormitorios"]
    assert "precio no indicado" not in unpriced.questions


def test_grupo_rejects_rooms(cfg):
    assert evaluate(cand(make(kind=Kind.ROOM)), "grupo", cfg).verdict is Verdict.FAIL


def test_grupo_walkthrough_is_maybe(cfg):
    listing = make(kind=Kind.FLAT, rent=27000, fees=3000, fees_include_energy=True, disposition="3+1")
    ev = evaluate(cand(listing, facts=TextFacts(washing_machine=True, walkthrough_room=True)), "grupo", cfg)
    assert "habitación de paso (průchozí)" in ev.questions


def test_unnormalised_disposition_is_unknown_bedrooms(cfg):
    listing = make(kind=Kind.FLAT, rent=20000, fees_include_energy=True, disposition="garsonka")
    ev = evaluate(cand(listing), "grupo", cfg)
    assert "disposición desconocida" in ev.questions
    assert ev.per_person == {3: 6667, 4: 5000}
    assert prefilter(listing, TextFacts(washing_machine=True), cfg) == ["grupo"]


# --- ubicación aproximada (código postal) ---

@pytest.mark.parametrize("precision,fails,questions", [
    ("exact", ["supermercado a 900 m"], []),
    ("street", ["supermercado a 900 m"], []),
    ("postcode", [], ["supermercado a 900 m (ubicación aproximada)"]),
])
def test_supermarket_fail_depends_on_location_precision(cfg, precision, fails, questions):
    listing = make(location_precision=precision)
    ev = evaluate(cand(listing, shop=(Shop("Lidl", 49.2, 16.6), 900.0)), "yo", cfg)
    assert ev.fails == fails
    assert ev.questions == questions


@pytest.mark.parametrize("precision,fails,questions", [
    ("exact", ["transporte 35 min"], []),
    ("street", ["transporte 35 min"], []),
    ("postcode", [], ["transporte 35 min (ubicación aproximada)"]),
])
def test_transit_fail_depends_on_location_precision(cfg, precision, fails, questions):
    listing = make(location_precision=precision)
    ev = evaluate(cand(listing, trip=Trip(35, 0, 5, ["BUS"], ["44"])), "yo", cfg)
    assert ev.fails == fails
    assert ev.questions == questions


# --- prefiltro ---

def test_prefilter(cfg):
    assert prefilter(make(rent=12000), TextFacts(), cfg) == []
    assert prefilter(make(rent=9000), TextFacts(), cfg) == ["yo"]
    flat = make(kind=Kind.FLAT, rent=30000, fees=3000, disposition="4+kk")
    assert prefilter(flat, TextFacts(), cfg) == ["grupo"]
    assert prefilter(make(rent=7000), TextFacts(washing_machine=False), cfg) == []
    assert prefilter(make(rent=None), TextFacts(), cfg) == ["yo"]


def test_prefilter_group_bedrooms_do_not_depend_on_price(cfg):
    assert "grupo" not in prefilter(make(kind=Kind.FLAT, rent=None, disposition="2+1"), TextFacts(), cfg)
    assert "grupo" in prefilter(make(kind=Kind.FLAT, rent=None, disposition="3+1"), TextFacts(), cfg)
