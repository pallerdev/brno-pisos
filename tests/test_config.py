from pathlib import Path

import pytest

from pisobot.config import load_config, load_dotenv, parse_latlon
from pisobot.models import Kind, Listing, Point

ROOT = Path(__file__).resolve().parents[1]

def test_parse_latlon():
    assert parse_latlon(" 49.19, 16.61 ") == Point(49.19, 16.61)

def test_load_config_resolves_destinations_from_env():
    cfg = load_config(ROOT / "config.yaml", env={"WORK_LATLON": "49.18,16.60"})
    assert cfg.destinations["work"] == Point(49.18, 16.60)
    assert cfg.destinations["station"] == Point(49.1906, 16.6127)
    assert cfg.profiles["yo"]["max_total"] == 10000
    assert cfg.secrets["DISCORD_BOT_TOKEN"] == ""

def test_load_config_fails_without_work_coordinates():
    with pytest.raises(ValueError, match="WORK_LATLON"):
        load_config(ROOT / "config.yaml", env={})

def test_load_dotenv_does_not_override_existing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("A_KEY=from_file\n# comentario\nB_KEY=b\n\n", encoding="utf-8")
    env = {"A_KEY": "from_env"}
    load_dotenv(env_file, env)
    assert env == {"A_KEY": "from_env", "B_KEY": "b"}

def test_listing_key_and_point():
    listing = Listing(source="bezrealitky", source_id="42", url="u", title="t", kind=Kind.ROOM, rent=7000)
    assert listing.key == "bezrealitky:42"
    assert listing.point is None
    listing.lat, listing.lon = 49.2, 16.6
    assert listing.point == Point(49.2, 16.6)
