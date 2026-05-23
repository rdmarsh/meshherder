"""Tests for mch.recommend — profile loading and neighbour-count lookup."""

from __future__ import annotations

import pytest

from mch.recommend import (
    Profile,
    RadioPreset,
    UniversalSetting,
    get_profile,
    load_profiles,
    load_radio_presets,
    parse_neighbour_count,
)

# ---------------------------------------------------------------------------
# parse_neighbour_count
# ---------------------------------------------------------------------------

def test_parse_neighbour_count_empty():
    assert parse_neighbour_count("") == 0

def test_parse_neighbour_count_whitespace_only():
    assert parse_neighbour_count("   \n  \n") == 0

def test_parse_neighbour_count_prompt_only():
    # bare ">" lines are not neighbours
    assert parse_neighbour_count(">") == 0

def test_parse_neighbour_count_single():
    assert parse_neighbour_count("> NodeA abc123") == 1

def test_parse_neighbour_count_multiple():
    raw = "> NodeA abc\n> NodeB def\n> NodeC ghi\n"
    assert parse_neighbour_count(raw) == 3

def test_parse_neighbour_count_eight():
    raw = "\n".join(f"> Node{i} key{i}" for i in range(8))
    assert parse_neighbour_count(raw) == 8

def test_parse_neighbour_count_strips_leading_trailing():
    assert parse_neighbour_count("\n> NodeA abc\n\n") == 1


# ---------------------------------------------------------------------------
# get_profile — boundary values
# ---------------------------------------------------------------------------

@pytest.fixture
def profiles():
    return load_profiles()[0]

def test_get_profile_zero_neighbours(profiles):
    assert get_profile(0, profiles).name == "mobile"

def test_get_profile_one_neighbour(profiles):
    assert get_profile(1, profiles).name == "indoor"

def test_get_profile_four_neighbours(profiles):
    assert get_profile(4, profiles).name == "indoor"

def test_get_profile_five_neighbours(profiles):
    assert get_profile(5, profiles).name == "rooftop"

def test_get_profile_fourteen_neighbours(profiles):
    assert get_profile(14, profiles).name == "rooftop"

def test_get_profile_fifteen_neighbours(profiles):
    assert get_profile(15, profiles).name == "elevated"

def test_get_profile_nineteen_neighbours(profiles):
    assert get_profile(19, profiles).name == "elevated"

def test_get_profile_twenty_neighbours(profiles):
    assert get_profile(20, profiles).name == "tower"

def test_get_profile_many_neighbours(profiles):
    assert get_profile(50, profiles).name == "tower"


# ---------------------------------------------------------------------------
# load_profiles — bundled file
# ---------------------------------------------------------------------------

def test_load_profiles_bundled_returns_profiles():
    profiles, universal = load_profiles()
    assert len(profiles) > 0
    assert all(isinstance(p, Profile) for p in profiles)

def test_load_profiles_bundled_returns_universal():
    _, universal = load_profiles()
    assert len(universal) > 0
    assert all(isinstance(u, UniversalSetting) for u in universal)

def test_load_profiles_expected_names():
    profiles, _ = load_profiles()
    names = {p.name for p in profiles}
    assert {"tower", "elevated", "rooftop", "indoor", "mobile"} <= names

def test_load_profiles_have_txdelay():
    profiles, _ = load_profiles()
    for p in profiles:
        assert p.txdelay
        assert p.direct_txdelay

def test_load_profiles_universal_has_path_hash_mode():
    _, universal = load_profiles()
    keys = {u.key for u in universal}
    assert "path.hash.mode" in keys


def test_load_profiles_universal_has_advert_settings():
    _, universal = load_profiles()
    values = {u.key: u.value for u in universal}
    assert values["radio"] == "915.075,125,9,5"
    assert values["flood.advert.interval"] == "47"
    assert values["advert.interval"] == "238"
    assert values["multi.acks"] == "1"
    assert values["path.hash.mode"] == "1"


def test_load_radio_presets_bundled_returns_versioned_presets():
    presets = load_radio_presets()
    assert presets
    assert all(isinstance(p, RadioPreset) for p in presets)
    assert any(p.name == "Australia Mid" and p.value == "915.075,125,9,5" for p in presets)


def test_load_profiles_from_custom_file(tmp_path):
    custom = tmp_path / "profiles.yaml"
    custom.write_text(
        "profiles:\n"
        "  - name: test\n"
        "    min_neighbours: 0\n"
        "    txdelay: '1.0'\n"
        "    direct_txdelay: '0.5'\n"
        "    description: Test profile\n"
        "universal: []\n"
    )
    profiles, universal = load_profiles(profiles_file=custom)
    assert len(profiles) == 1
    assert profiles[0].name == "test"
    assert universal == []


def test_load_radio_presets_from_custom_file(tmp_path):
    custom = tmp_path / "profiles.yaml"
    custom.write_text(
        "version: 1\n"
        "profiles: []\n"
        "universal: []\n"
        "radio_presets:\n"
        "  - name: QH WIP\n"
        "    value: '915.800,250,10,5'\n"
        "    description: Test mesh\n"
    )
    presets = load_radio_presets(profiles_file=custom)
    assert presets == [RadioPreset("QH WIP", "915.800,250,10,5", "Test mesh")]


def test_load_radio_presets_falls_back_to_universal_radio(tmp_path):
    custom = tmp_path / "profiles.yaml"
    custom.write_text(
        "profiles: []\n"
        "universal:\n"
        "  - key: radio\n"
        "    value: '915.075,125,9,5'\n"
        "    description: Old schema radio\n"
    )
    presets = load_radio_presets(profiles_file=custom)
    assert presets == [RadioPreset("Profile radio", "915.075,125,9,5", "Old schema radio")]
