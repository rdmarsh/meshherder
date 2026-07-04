"""Tests for the vendored firmware command-compatibility catalogue (advisory only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mch.command_catalog import (
    SUPPORTED,
    UNKNOWN,
    UNSUPPORTED,
    command_flags,
    command_support,
    load_command_catalog,
)


def test_bundled_catalogue_loads_and_has_core_commands() -> None:
    cat = load_command_catalog()
    assert cat.version_order  # non-empty, oldest -> newest
    assert cat.current_version in cat.version_order
    # A few commands mch actually sends must be present.
    assert "reboot" in cat.commands
    assert "get radio" in cat.commands
    assert "set name" in cat.commands


def test_bundled_json_is_committed_and_current(tmp_path: Path) -> None:
    # Guard: the vendored file exists and is the shape the loader expects.
    raw = json.loads((Path("src/mch/data/repeater_commands.json")).read_text())
    assert raw["version_order"][0] == raw["base_version"]
    assert isinstance(raw["commands"], dict)


# command_support(command, node_ver) -> supported | unsupported | unknown

def test_command_newer_than_node_is_unsupported() -> None:
    # poweroff was added in v1.14.1; a v1.13.0 node cannot run it.
    assert command_support("poweroff", "v1.13.0") == UNSUPPORTED


def test_command_available_on_node_is_supported() -> None:
    assert command_support("poweroff", "v1.15.0") == SUPPORTED
    # Baseline commands (no addedIn) are supported on any tracked version.
    assert command_support("reboot", "v1.13.0") == SUPPORTED


def test_ver_build_suffix_is_stripped() -> None:
    # The `ver` fact carries a build suffix; it must parse to the release.
    assert command_support("poweroff", "v1.14.1-467959c (Build: Jun 6 2026)") == SUPPORTED
    assert command_support("poweroff", "v1.14.0-abc123 (Build: x)") == UNSUPPORTED


def test_ver_without_v_prefix_is_normalised() -> None:
    assert command_support("get acl", "1.9.0") == SUPPORTED  # get acl added v1.9.0
    assert command_support("get acl", "1.8.1") == UNSUPPORTED


def test_get_set_forms_resolve_to_setting_entry() -> None:
    # `get role` / `set name` addedIn metadata must be honoured.
    assert command_support("get role", "v1.4.1") == UNSUPPORTED  # role added v1.4.2
    assert command_support("get role", "v1.4.2") == SUPPORTED
    # Arguments after the command must not defeat the match.
    assert command_support("set name faulkland-01", "v1.16.0") == SUPPORTED


def test_longest_prefix_wins_over_shorter_command() -> None:
    # Both "clock" and "clock sync" are catalogue keys; "clock sync" must win.
    assert command_support("clock sync", "v1.16.0") == SUPPORTED


def test_unknown_command_is_unknown() -> None:
    assert command_support("frobnicate the widget", "v1.16.0") == UNKNOWN
    # A near-miss that is not a real prefix must not false-match.
    assert command_support("getacl", "v1.16.0") == UNKNOWN


def test_unknown_node_version_is_unknown() -> None:
    # No version, unparseable version, or a release we don't track -> silent (unknown).
    assert command_support("poweroff", None) == UNKNOWN
    assert command_support("poweroff", "") == UNKNOWN
    assert command_support("poweroff", "???") == UNKNOWN
    assert command_support("poweroff", "v9.9.9") == UNKNOWN


def test_empty_command_is_unknown() -> None:
    assert command_support("", "v1.16.0") == UNKNOWN
    assert command_support("   ", "v1.16.0") == UNKNOWN


# command_flags(command) -> set of upstream flags (serial / reboot / build / destructive)

def test_serial_only_command_carries_serial_flag() -> None:
    assert "serial" in command_flags("get acl")
    assert "serial" in command_flags("stats-core")


def test_reboot_command_carries_reboot_flag() -> None:
    # set radio only takes effect after a reboot.
    assert "reboot" in command_flags("set radio 915.8,250,11,5")


def test_command_with_no_flags_returns_empty() -> None:
    assert command_flags("reboot") == set()
    assert command_flags("set name Tower") == set()


def test_flags_of_unknown_command_is_empty() -> None:
    assert command_flags("frobnicate") == set()


def test_load_from_explicit_path(tmp_path: Path) -> None:
    doc = {
        "current_version": "v2.0",
        "base_version": "v1.0",
        "version_order": ["v1.0", "v1.5", "v2.0"],
        "commands": {"widget": {"added_in": "v1.5", "deprecated_at": None}},
    }
    p = tmp_path / "cat.json"
    p.write_text(json.dumps(doc))
    cat = load_command_catalog(p)
    assert cat.support("widget", "v1.0") == UNSUPPORTED
    assert cat.support("widget", "v2.0") == SUPPORTED


def test_missing_file_raises() -> None:
    with pytest.raises(RuntimeError):
        load_command_catalog(Path("/nonexistent/repeater_commands.json"))
