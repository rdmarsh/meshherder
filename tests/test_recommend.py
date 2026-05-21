"""Tests for mch.recommend — profile loading and neighbour-count lookup."""

from __future__ import annotations

import pytest

from mch.recommend import (
    Profile,
    UniversalSetting,
    get_profile,
    load_profiles,
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
