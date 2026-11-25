import os
from datetime import date, timedelta

import pytest

from pisobot.models import Kind, Listing
from pisobot.store import Store, fingerprint


def make(**kw):
    base = dict(source="ulovdomov", source_id="1", url="u", title="t", kind=Kind.FLAT,
                rent=17500, area_m2=40, lat=49.20893, lon=16.60166)
    base.update(kw)
    return Listing(**base)


def test_fingerprint_matches_same_flat_on_other_portal():
    a = make()
    b = make(source="bezrealitky", source_id="9", rent=17490, lat=49.2091, lon=16.6017)
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(make(rent=None)) is None
    assert fingerprint(make(location_precision="postcode")) is None


def test_fingerprint_ignores_float_formatting():
    assert fingerprint(make(rent=17500.0, area_m2=40.0)) == fingerprint(make())


def test_fingerprint_treats_non_positive_values_as_missing():
    assert fingerprint(make(area_m2=0)) is None
    assert fingerprint(make(area_m2=-5)) is None
    assert fingerprint(make(rent=0, fees=0)) is None
    assert fingerprint(make(rent=1000, fees=-2000)) is None


def test_store_roundtrip_and_dedupe(tmp_path):
    store = Store(tmp_path)
    first = make()
    assert store.is_new(first)
    store.mark_seen(first, date(2026, 11, 24))
    assert not store.is_new(first)
    duplicate = make(source_id="2")  # mismo piso publicado dos veces (pasa en UlovDomov)
    assert not store.is_new(duplicate)
    store.state["revisar_last_id"] = "123"
    store.cache["transit"]["k"] = []
    store.save()

    reloaded = Store(tmp_path)
    assert not reloaded.is_new(first)
    assert reloaded.state["revisar_last_id"] == "123"
    assert reloaded.cache["transit"] == {"k": []}
    assert reloaded.state["llm"] == {"date": None, "count": 0}


def test_prune_forgets_old_entries(tmp_path):
    store = Store(tmp_path)
    old = make(source_id="old", area_m2=99)
    store.mark_seen(old, date(2026, 9, 1))
    store.mark_seen(make(), date(2026, 11, 20))
    store.prune(date(2026, 11, 24))
    assert store.is_new(old)
    assert not store.is_new(make())


def test_prune_boundary_keeps_60_days_and_drops_61(tmp_path):
    store = Store(tmp_path)
    today = date(2026, 11, 24)
    store.mark_seen(make(source_id="kept"), today - timedelta(days=60))
    store.mark_seen(make(source_id="dropped"), today - timedelta(days=61))
    store.prune(today)
    assert "ulovdomov:kept" in store.seen["ids"]
    assert "ulovdomov:dropped" not in store.seen["ids"]


def test_save_creates_missing_data_dir(tmp_path):
    data_dir = tmp_path / "nuevo" / "data"
    store = Store(data_dir)
    store.save()
    assert (data_dir / "state.json").exists()
    assert (data_dir / "seen.json").exists()
    assert (data_dir / "cache.json").exists()


def test_save_leaves_no_tmp_files_and_reloads_equal(tmp_path):
    store = Store(tmp_path)
    store.mark_seen(make(), date(2026, 11, 24))
    store.state["revisar_last_id"] = "7"
    store.save()
    assert list(tmp_path.glob("*.tmp")) == []
    reloaded = Store(tmp_path)
    assert reloaded.seen == store.seen
    assert reloaded.cache == store.cache
    assert reloaded.state == store.state


def test_interrupted_save_keeps_pending_notifications(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.mark_seen(make(), date(2026, 11, 24))
    store.state["pending"]["grupo"] = [{"title": "Anuncio dudoso"}]
    replace = os.replace

    def stop_at_seen(source, target):
        if target.name == "seen.json":
            raise OSError("escritura interrumpida")
        replace(source, target)

    monkeypatch.setattr("pisobot.store.os.replace", stop_at_seen)
    with pytest.raises(OSError, match="interrumpida"):
        store.save()
    restored = Store(tmp_path)
    assert restored.state["pending"] == {"grupo": [{"title": "Anuncio dudoso"}]}
    assert restored.is_new(make())


def test_listing_without_fingerprint_is_deduped_by_id(tmp_path):
    store = Store(tmp_path)
    listing = make(rent=None)
    assert fingerprint(listing) is None
    store.mark_seen(listing, date(2026, 11, 24))
    assert not store.is_new(listing)
    store.save()
    assert not Store(tmp_path).is_new(listing)


def test_partial_state_file_gets_default_keys(tmp_path):
    (tmp_path / "state.json").write_text('{"revisar_last_id": "5"}', encoding="utf-8")
    state = Store(tmp_path).state
    assert state["revisar_last_id"] == "5"
    assert state["pending"] == {}
    assert state["last_digest"] is None
    assert state["failures"] == {}
    assert state["transit_retries"] == {}
    assert state["llm"] == {"date": None, "count": 0}


def test_partial_nested_dict_gets_default_subkeys(tmp_path):
    (tmp_path / "state.json").write_text('{"llm": {"date": "2026-11-24"}}', encoding="utf-8")
    state = Store(tmp_path).state
    assert state["llm"] == {"date": "2026-11-24", "count": 0}


def test_corrupt_seen_file_raises_naming_the_file(tmp_path):
    (tmp_path / "seen.json").write_text('{"ids": {"a": ', encoding="utf-8")
    with pytest.raises(ValueError, match="seen.json"):
        Store(tmp_path)


def test_store_without_persistence_never_writes(tmp_path):
    store = Store(tmp_path / "data", persist=False)
    store.mark_seen(make(), date(2026, 11, 24))
    store.save()
    assert not (tmp_path / "data").exists()
