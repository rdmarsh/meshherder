"""Tests for CLI helper functions and command output."""

import asyncio
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from mch import __version__
from mch.cli import (
    _ago,
    _default_commission_label,
    _filter_contacts,
    _filter_drift,
    _filter_nodes,
    _local_ts,
    _log_node_label,
    _log_node_pubkey,
    _next_sync_text,
    _node_detail_view,
    cli,
)
from mch.meshkey import derive_public_key


def _contact(pubkey: str, name: str) -> dict:
    return {"public_key": pubkey, "adv_name": name}


CONTACTS = [
    _contact("aabbccdd11223344", "Millbrook"),
    _contact("4e5f6a7b88990011", "Fernvale"),
    _contact("1234567890abcdef", "Hilltop"),
]


def test_ago_formats_relative_times():
    now = datetime.now(UTC)
    assert _ago(None) == "never"
    assert _ago((now - timedelta(seconds=42)).isoformat()) == " 42s ago"
    assert _ago((now - timedelta(minutes=5)).isoformat()) == "  5m ago"
    assert _ago((now - timedelta(hours=3)).isoformat()) == "  3h ago"
    assert _ago((now - timedelta(days=2)).isoformat()) == "  2d ago"


def test_ago_treats_future_times_as_just_now():
    when = datetime.now(UTC) + timedelta(minutes=5)
    assert _ago(when.isoformat()) == "just now"


def test_status_lists_relative_last_seen():
    seen = (datetime.now(UTC) - timedelta(minutes=7)).isoformat()
    result = _run(["fleet", "status"], _status_resp(nodes=[{**_summary(), "last_seen": seen}]))
    assert result.exit_code == 0
    assert "  7m ago" in result.output


def test_ago_with_timestamp_is_not_padded_in_prose_output():
    seen = (datetime.now(UTC) - timedelta(minutes=7)).isoformat()
    detail = _detail()
    detail["data"]["last_seen"] = seen
    result = _run(["node", "show", "site-a"], detail)

    assert "(7m ago)" in result.output
    assert "(  7m ago)" not in result.output


def test_next_sync_text_formats_future_as_approximate():
    when = datetime.now(UTC) + timedelta(hours=19, minutes=30)
    text = _next_sync_text(when.isoformat())

    assert "(in ~19h 29m)" in text or "(in ~19h 30m)" in text


def test_next_sync_text_formats_overdue_as_due_now():
    when = datetime.now(UTC) - timedelta(minutes=5)

    assert _next_sync_text(when.isoformat()).endswith("(due now)")


def test_next_sync_text_formats_missing_as_unscheduled():
    assert _next_sync_text(None) == "unscheduled"


def test_node_detail_view_preserves_next_sync_at():
    detail = _node_detail_view({
        "pubkey": "aabbccdd" * 8,
        "name": "Tower",
        "label": "site-a",
        "group": None,
        "state": "configured",
        "last_seen": None,
        "last_connected": None,
        "next_sync_at": "2026-06-02T15:10:07+00:00",
        "auth": {"cached": True, "password_known": True},
        "fact_keys": [],
        "facts": {},
        "facts_collected_at": {},
        "settings": [],
        "logs": [],
    })

    assert detail["next_sync_at"] == "2026-06-02T15:10:07+00:00"


def test_node_detail_view_preserves_management_path():
    detail = _node_detail_view({
        "pubkey": "aabbccdd" * 8,
        "name": "Tower",
        "label": "site-a",
        "group": None,
        "state": "configured",
        "last_seen": None,
        "last_connected": None,
        "next_sync_at": None,
        "auth": {"cached": True, "password_known": True},
        "fact_keys": [],
        "facts": {},
        "facts_collected_at": {},
        "settings": [],
        "logs": [],
        "management_path": {
            "cached_path": "stored (out_path=e260; out_path_len=1; hash-mode=1)",
            "path_repair_needed": False,
        },
    })

    assert detail["management_path"]["cached_path"].startswith("stored")


def test_local_ts_displays_utc_timestamp_in_local_timezone(monkeypatch):
    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Australia/Sydney")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        assert _local_ts("2026-06-01T05:03:49+00:00") == "2026-06-01 15:03:49"
    finally:
        if old_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old_tz)
        if hasattr(time, "tzset"):
            time.tzset()


def test_log_node_identity_uses_separate_pubkey_and_label():
    entry = {"node_pubkey": "aabbccdd11223344", "label": "site-a"}
    assert _log_node_pubkey(entry) == "aabbccdd1122"
    assert _log_node_label(entry) == "site-a"


def test_log_node_identity_can_use_display_identity_without_db_node():
    entry = {
        "node_pubkey": None,
        "display_pubkey": "deadbeef",
        "display_label": "wrong",
    }
    assert _log_node_pubkey(entry) == "deadbeef"
    assert _log_node_label(entry) == "wrong"


def test_filter_contacts_no_query_returns_all():
    assert _filter_contacts(CONTACTS, None) == CONTACTS


def test_filter_contacts_pubkey_prefix_match():
    result = _filter_contacts(CONTACTS, "4e5f")
    assert len(result) == 1
    assert result[0]["adv_name"] == "Fernvale"


def test_filter_contacts_pubkey_prefix_short():
    # single char prefix — should match only keys starting with that char
    result = _filter_contacts(CONTACTS, "4")
    assert len(result) == 1
    assert result[0]["adv_name"] == "Fernvale"


def test_filter_contacts_pubkey_no_match():
    result = _filter_contacts(CONTACTS, "ffff")
    assert result == []


def test_filter_contacts_name_substring_match():
    result = _filter_contacts(CONTACTS, "mill")
    assert len(result) == 1
    assert result[0]["adv_name"] == "Millbrook"


def test_filter_contacts_name_case_insensitive():
    result = _filter_contacts(CONTACTS, "MILLBROOK")
    assert len(result) == 1
    assert result[0]["adv_name"] == "Millbrook"


def test_filter_contacts_lowercase_prefix_matches_mixed_case_name():
    contacts = [_contact("4d0385b9d942f20c", "Acacia Gardens")]
    result = _filter_contacts(contacts, "aca")
    assert len(result) == 1
    assert result[0]["adv_name"] == "Acacia Gardens"


def test_filter_contacts_name_partial_match():
    result = _filter_contacts(CONTACTS, "hill")
    assert len(result) == 1
    assert result[0]["adv_name"] == "Hilltop"


def test_filter_contacts_no_match_returns_empty():
    result = _filter_contacts(CONTACTS, "zzznomatch")
    assert result == []


def test_filter_contacts_empty_query_returns_all():
    # empty string is falsy — treat same as None
    assert _filter_contacts(CONTACTS, "") == CONTACTS


def test_filter_contacts_missing_adv_name():
    # adv_name may be None or absent — should not raise
    contacts = [{"public_key": "aabbccdd11223344", "adv_name": None}]
    result = _filter_contacts(contacts, "nomatch")
    assert result == []


def test_filter_contacts_missing_public_key():
    # public_key absent — should not raise
    contacts = [{"adv_name": "Orphan"}]
    result = _filter_contacts(contacts, "aa")
    assert result == []


# ---------------------------------------------------------------------------
# _filter_nodes
# ---------------------------------------------------------------------------

def _node(pubkey="aabbccdd11223344", label="My Node", name="📡 My Node") -> dict:
    return {"pubkey": pubkey, "label": label, "name": name}


NODES = [
    _node("aabbccdd11223344", "QH Test",  "📡 Quakers Hill Test"),
    _node("2fd9c7bb88997766", "QH Wold",  "📡 Quakers Hill Wold"),
    _node("3134bf1055443322", "wip 07",   "📡 wip 07"),
]


def test_filter_nodes_no_query_returns_all():
    assert _filter_nodes(NODES, None) == NODES


def test_filter_nodes_pubkey_prefix_match():
    result = _filter_nodes(NODES, "3134")
    assert len(result) == 1
    assert result[0]["label"] == "wip 07"


def test_filter_nodes_label_substring_match():
    result = _filter_nodes(NODES, "wold")
    assert len(result) == 1
    assert result[0]["label"] == "QH Wold"


def test_filter_nodes_label_case_insensitive():
    result = _filter_nodes(NODES, "QH")
    assert len(result) == 2


def test_filter_nodes_name_substring_match():
    result = _filter_nodes(NODES, "quakers")
    assert len(result) == 2


def test_filter_nodes_no_match_returns_empty():
    assert _filter_nodes(NODES, "zzznomatch") == []


def test_filter_nodes_empty_query_returns_all():
    assert _filter_nodes(NODES, "") == NODES


# ---------------------------------------------------------------------------
# _filter_drift
# ---------------------------------------------------------------------------

def _drift(pubkey="aabbccdd11223344", label="My Node", key="txdelay") -> dict:
    return {"node_pubkey": pubkey, "label": label, "key": key,
            "desired": "1.0", "observed": "2.0", "policy": "enforce"}


DRIFTED = [
    _drift("aabbccdd11223344", "QH Test",  "txdelay"),
    _drift("2fd9c7bb88997766", "QH Wold",  "path.hash.mode"),
    _drift("aabbccdd11223344", "QH Test",  "multi.acks"),
]


def test_filter_drift_no_query_returns_all():
    assert _filter_drift(DRIFTED, None) == DRIFTED


def test_filter_drift_pubkey_prefix_match():
    result = _filter_drift(DRIFTED, "2fd9")
    assert len(result) == 1
    assert result[0]["key"] == "path.hash.mode"


def test_filter_drift_label_substring_match():
    result = _filter_drift(DRIFTED, "test")
    assert len(result) == 2
    assert all(s["label"] == "QH Test" for s in result)


def test_filter_drift_label_case_insensitive():
    result = _filter_drift(DRIFTED, "QH WOLD")
    assert len(result) == 1


def test_filter_drift_no_match_returns_empty():
    assert _filter_drift(DRIFTED, "zzznomatch") == []


def test_filter_drift_empty_query_returns_all():
    assert _filter_drift(DRIFTED, "") == DRIFTED


# ---------------------------------------------------------------------------
# CLI command rendering — CliRunner + mocked _rpc
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_version_options_report_source_version(flag: str):
    result = CliRunner().invoke(cli, [flag])

    assert result.exit_code == 0
    assert result.output.endswith(f"version {__version__}\n")


def test_current_release_version_is_0_22_0():
    assert __version__ == "0.22.0"


def _run(args, rpc_response):
    """Invoke a CLI command with _rpc mocked to return rpc_response."""
    runner = CliRunner()
    with patch("mch.cli._rpc", return_value=rpc_response):
        return runner.invoke(cli, ["--socket", "/fake.sock"] + args)


def test_rpc_timeout_shows_clean_error():
    """A timeout waiting for the daemon produces a clean error, not a traceback."""
    runner = CliRunner()
    mock_writer = MagicMock()
    mock_writer.drain = AsyncMock()
    mock_writer.close = MagicMock()
    mock_reader = MagicMock()

    async def fake_open_conn(*a, **kw):
        return mock_reader, mock_writer

    with patch("asyncio.open_unix_connection", fake_open_conn), \
         patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "fleet", "status"])

    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "timed out" in combined.lower()
    assert "Traceback" not in combined


def test_rpc_eof_shows_clean_error():
    """A daemon restart during a foreground command reports EOF cleanly."""
    runner = CliRunner()
    mock_writer = MagicMock()
    mock_writer.drain = AsyncMock()
    mock_writer.close = MagicMock()
    mock_reader = MagicMock()
    mock_reader.readline = AsyncMock(return_value=b"")

    async def fake_open_conn(*a, **kw):
        return mock_reader, mock_writer

    with patch("asyncio.open_unix_connection", fake_open_conn):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "fleet", "status"])

    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "daemon connection closed" in combined.lower()
    assert "Traceback" not in combined


def test_daemon_connection_failure_checks_default_config(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("typo: true\n")

    async def fake_open_conn(*a, **kw):
        raise FileNotFoundError

    runner = CliRunner()
    with patch("asyncio.open_unix_connection", fake_open_conn), \
         patch("mch.cli.DEFAULT_CONFIG", config):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "fleet", "status"])

    assert result.exit_code != 0
    assert "cannot connect to daemon" in result.output
    assert f"checking config: {config}" in result.output
    assert "config check failed:" in result.output
    assert "unknown config key 'typo'" in result.output
    assert "run: mch config validate" in result.output


def test_daemon_connection_failure_reports_config_ok(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("nodes: []\n")

    async def fake_open_conn(*a, **kw):
        raise ConnectionRefusedError

    runner = CliRunner()
    with patch("asyncio.open_unix_connection", fake_open_conn), \
         patch("mch.cli.DEFAULT_CONFIG", config):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "fleet", "status"])

    assert result.exit_code != 0
    assert "cannot connect to daemon" in result.output
    assert f"checking config: {config}" in result.output
    assert "config ok:" in result.output


def test_daemon_down_hint_points_at_daemon_commands(tmp_path):
    """The daemon-down hint suggests the real diagnostics, not the removed `mchd --help` (S1.7)."""
    config = tmp_path / "config.yaml"
    config.write_text("nodes: []\n")

    async def fake_open_conn(*a, **kw):
        raise ConnectionRefusedError

    runner = CliRunner()
    with patch("asyncio.open_unix_connection", fake_open_conn), \
         patch("mch.cli.DEFAULT_CONFIG", config):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "fleet", "status"])

    assert "mch daemon status" in result.output
    assert "mch daemon logs" in result.output
    assert "mchd --help" not in result.output


def test_path_command_shows_stored_route_metadata():
    result = _run(["node", "path", "site-a"], {"ok": True, "data": {
        "pubkey": "aabbccdd11223344",
        "label": "site-a",
        "name": "Tower",
        "contact_present": True,
        "cached_path": "stored (out_path=e260,5f6c; out_path_len=2; hash-mode=1)",
        "route": "stored",
        "path_repair_needed": False,
        "contact_name": "Tower",
        "contact_type": 2,
        "contact_flags": 1,
        "out_path": "e260,5f6c",
        "out_path_len": 2,
        "out_path_hash_mode": 1,
    }})

    assert result.exit_code == 0
    assert "aabbccdd1122  site-a  Tower" in result.output
    assert "cached path: stored (out_path=e260,5f6c; out_path_len=2; hash-mode=1)" in result.output
    assert "route: stored" in result.output
    assert "path repair needed" not in result.output
    assert "out_path_hash_mode: 1" in result.output


def test_path_command_marks_flood_route_for_repair():
    result = _run(["node", "path", "site-a"], {"ok": True, "data": {
        "pubkey": "aabbccdd11223344",
        "label": "site-a",
        "name": "Tower",
        "contact_present": True,
        "cached_path": "flood",
        "route": "flood",
        "path_repair_needed": True,
        "out_path": "",
        "out_path_len": -1,
        "out_path_hash_mode": 0,
    }})

    assert result.exit_code == 0
    assert "cached path: flood" in result.output
    assert "route: flood (path repair needed)" in result.output


def test_path_discover_command_shows_candidate():
    result = _run(["node", "path", "site-a", "--discover"], {"ok": True, "data": {
        "pubkey": "aabbccdd11223344",
        "label": "site-a",
        "name": "Tower",
        "source": "discovery",
        "path": "e260,5f6c",
        "path_len": 2,
        "path_hash_mode": 1,
        "apply_value": "e260,5f6c:1",
    }})

    assert result.exit_code == 0
    assert "discovery path candidate:" in result.output
    assert "path: e260,5f6c" in result.output
    assert "apply with: mch node path aabbccdd1122 --apply e260,5f6c:1" in result.output


def test_path_apply_command_confirms_and_renders_updated_status():
    rpc_response = {"ok": True, "data": {
        "pubkey": "aabbccdd11223344",
        "label": "site-a",
        "name": "Tower",
        "contact_present": True,
        "cached_path": "stored (out_path=e260; out_path_len=1; hash-mode=1)",
        "route": "stored",
        "path_repair_needed": False,
        "out_path": "e260",
        "out_path_len": 1,
        "out_path_hash_mode": 1,
    }}
    runner = CliRunner()
    with patch("mch.cli._rpc", return_value=rpc_response):
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "path", "site-a", "--apply", "e260:1", "--yes"],
        )

    assert result.exit_code == 0
    assert "updated cached management path" in result.output
    assert "route: stored" in result.output


def test_path_apply_previews_without_yes_and_does_not_call_daemon():
    """--apply previews by default and only mutates with --yes (RECOMMENDATIONS-2 S2.2)."""
    runner = CliRunner()
    with patch("mch.cli._rpc") as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "path", "site-a", "--apply", "e260:1"],
        )

    assert result.exit_code == 0
    mock_rpc.assert_not_called()
    assert "would set management path" in result.output
    assert "--yes" in result.output


def test_path_command_rejects_multiple_mutating_options():
    result = _run(
        ["node", "path", "site-a", "--discover", "--apply", "e260:1"],
        {"ok": True, "data": {}},
    )

    assert result.exit_code != 0
    assert "choose only one" in result.output


def test_discover_command_renders_active_discovery_results():
    result = _run(["node", "discover", "--radio", "915.800,250,10,5", "--dwell", "3"], {
        "ok": True,
        "data": {
            "radio": {"text": "915.800/250/SF10/CR5"},
            "dwell": 3.0,
            "nodes": [{
                "pubkey": "8e4a0d73" + "0" * 56,
                "node_type": 2,
                "SNR_in": 12.5,
                "tag": "81ca8751",
                "status": "config-only",
                "label": "faulkland",
            }],
        },
    })

    assert result.exit_code == 0
    assert "discovery radio: 915.800/250/SF10/CR5" in result.output
    assert "8e4a0d73" in result.output
    assert "repeater" in result.output
    assert "12.5" in result.output
    assert "config-only" in result.output
    assert "faulkland" in result.output
    assert "TAG" not in result.output
    assert "81ca8751" not in result.output


def test_discover_command_unknown_only_hides_managed_nodes():
    result = _run(["node", "discover", "--radio", "915.800,250,10,5", "--unknown-only"], {
        "ok": True,
        "data": {
            "radio": {"text": "915.800/250/SF10/CR5"},
            "dwell": 5.0,
            "nodes": [
                {
                    "pubkey": "8e4a0d73" + "0" * 56,
                    "node_type": 2,
                    "SNR_in": 12.5,
                    "status": "config-only",
                    "label": "faulkland",
                },
                {
                    "pubkey": "75ab8517" + "0" * 56,
                    "node_type": 2,
                    "SNR_in": 10.0,
                    "status": "managed",
                    "label": "qh-test",
                },
            ],
        },
    })

    assert result.exit_code == 0
    assert "faulkland" in result.output
    assert "qh-test" not in result.output
    assert "75ab8517" not in result.output


def test_discover_command_rejects_invalid_radio_before_rpc():
    runner = CliRunner()
    with patch("mch.cli._rpc") as rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "discover", "--radio", "915.800,250,99,5"],
        )

    assert result.exit_code != 0
    assert "radio spreading factor" in result.output
    rpc.assert_not_called()


def test_adopt_command_confirms_and_renders_result():
    pubkey = "8e4a0d73" + "0" * 56
    runner = CliRunner()
    with patch("mch.cli._rpc", return_value={
            "ok": True,
            "data": {
                "pubkey": pubkey,
                "label": "faulkland",
                "group": None,
                "radio": {"text": "915.800/250/SF11/CR5"},
                "matched_config": True,
                "contact_added": True,
                "favourited": True,
                "route": "flood",
                "path_repair_needed": True,
            },
        }):
        result = runner.invoke(
            cli,
            [
                            "--socket",
                            "/fake.sock",
                            "node",
                            "adopt",
                            pubkey,
                            "--radio",
                            "915.800,250,11,5",
                            "--label",
                            "faulkland",
                        ],
            input="y\n",
        )

    assert result.exit_code == 0
    assert "about to adopt discovered node" in result.output
    assert "adopted node" in result.output
    assert "faulkland" in result.output
    assert "route: flood (path repair needed)" in result.output


def test_adopt_command_rejects_short_pubkey_before_rpc():
    runner = CliRunner()
    with patch("mch.cli._rpc") as rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "adopt", "8e4a0d73", "--radio", "915.800,250,11,5"],
        )

    assert result.exit_code != 0
    assert "full 64-character" in result.output
    rpc.assert_not_called()


# ------ validate ------

def test_validate_valid_config(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("""\
nodes:
  - pubkey_prefix: aabbccdd
    label: site-a
""")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  aabbccdd: nodepass\n")
    secrets.chmod(0o600)

    runner = CliRunner()
    with patch("mch.cli._rpc") as mock_rpc:
        result = runner.invoke(cli, ["daemon", "validate", "--config", str(config)])

    mock_rpc.assert_not_called()
    assert result.exit_code == 0
    assert f"checking config: {config}" in result.output
    assert "config ok:" in result.output
    assert "nodes: 1" in result.output
    assert "secrets ok:" in result.output


def test_validate_without_config_option_uses_default_config_not_cwd(tmp_path):
    default_config = tmp_path / "etc" / "meshherder" / "config.yaml"
    default_config.parent.mkdir(parents=True)
    default_config.write_text("""\
nodes:
  - pubkey_prefix: aabbccdd
    label: site-a
""")
    default_secrets = default_config.parent / "secrets.yaml"
    default_secrets.write_text("nodes: {}\n")
    default_secrets.chmod(0o600)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("config.yaml").write_text("typo: true\n")

        with patch("mch.cli._rpc") as mock_rpc, \
             patch("mch.cli.DEFAULT_CONFIG", default_config):
            result = runner.invoke(cli, ["daemon", "validate"])

    mock_rpc.assert_not_called()
    assert result.exit_code == 0
    assert f"checking config: {default_config}" in result.output
    assert f"config ok: {default_config.resolve()}" in result.output
    assert "nodes: 1" in result.output


def test_validate_invalid_config_exits_nonzero(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("typo: true\n")

    runner = CliRunner()
    with patch("mch.cli._rpc") as mock_rpc:
        result = runner.invoke(cli, ["daemon", "validate", "--config", str(config)])

    mock_rpc.assert_not_called()
    assert result.exit_code != 0
    assert "validation failed" in result.output
    assert "unknown config key" in result.output


def test_validate_tab_indentation_explains_fix(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("nodes:\n\t- pubkey_prefix: aabbccdd\n")

    result = CliRunner().invoke(cli, ["daemon", "validate", "--config", str(config)])

    assert result.exit_code != 0
    assert "tab character in YAML indentation" in result.output
    assert "replace tabs with spaces" in result.output


def test_validate_duplicate_key_reports_lines(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("""\
groups:
  - name: dave_test
    settings:
      owner.info: Managed by rdm
    settings:
      radio: 915.800,250,10,5
""")

    result = CliRunner().invoke(cli, ["daemon", "validate", "--config", str(config)])

    assert result.exit_code != 0
    assert "duplicate YAML key 'settings' at line 5" in result.output
    assert "first defined at line 3" in result.output


def test_validate_reports_all_unknown_group_references_with_hint(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("""\
groups:
  - name: dave_test
nodes:
  - pubkey_prefix: 3134bf10
    group: test
  - pubkey_prefix: c041a906
    group: test
""")

    result = CliRunner().invoke(cli, ["daemon", "validate", "--config", str(config)])

    assert result.exit_code != 0
    assert "node '3134bf10' references unknown group 'test'" in result.output
    assert "node 'c041a906' references unknown group 'test'" in result.output
    assert "did you mean 'dave_test'?" in result.output
    assert "defined groups: ['dave_test']" in result.output


def test_validate_explicit_missing_secrets_exits_nonzero(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("gateway: {}\n")

    runner = CliRunner()
    with patch("mch.cli._rpc") as mock_rpc:
        result = runner.invoke(
            cli,
            [
                            "daemon",
                            "validate",
                            "--config",
                            str(config),
                            "--secrets",
                            str(tmp_path / "missing.yaml"),
                        ],
        )

    mock_rpc.assert_not_called()
    assert result.exit_code != 0
    assert "secrets file not found" in result.output


def test_validate_permission_denied_suggests_sudo(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("gateway: {}\nfacts:\n  - powersavings\n")
    denied = PermissionError(13, "Permission denied", "/etc/meshherder/secrets.yaml")
    config_only = SimpleNamespace(
        config_path=str(config.resolve()),
        nodes=[],
        groups=[],
        facts=["powersavings"],
        secrets={},
        secrets_path="/etc/meshherder/secrets.yaml",
    )

    runner = CliRunner()
    with patch("mch.cli._rpc") as mock_rpc, \
         patch("mch.cli.load_config", side_effect=[denied, config_only]):
        result = runner.invoke(cli, ["daemon", "validate", "--config", str(config)])

    mock_rpc.assert_not_called()
    assert result.exit_code != 0
    assert "config ok:" in result.output
    assert "secrets: not checked" in result.output
    assert "did you mean 'powersaving'?" in result.output
    assert "cannot read secrets file" in result.output
    assert "sudo mch config validate" in result.output


def test_validate_warns_when_prv_key_does_not_match_config_prefix(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("""\
nodes:
  - pubkey_prefix: aabbccdd
    label: site-a
""")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  aabbccdd:\n    prv_key: " + ("10" * 64) + "\n")
    secrets.chmod(0o600)

    runner = CliRunner()
    with patch("mch.cli._rpc") as mock_rpc:
        result = runner.invoke(
            cli, ["daemon", "validate", "--config", str(config), "--secrets", str(secrets)]
        )

    mock_rpc.assert_not_called()
    assert result.exit_code == 0
    assert "warning:" in result.output
    assert "does not match config pubkey_prefix" in result.output


def test_validate_warns_for_unknown_fact_and_suggests_typo(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("facts:\n  - powersavings\n")

    result = CliRunner().invoke(cli, ["daemon", "validate", "--config", str(config)])

    assert result.exit_code == 0
    assert "warning: facts contains unrecognised key 'powersavings'" in result.output
    assert "did you mean 'powersaving'?" in result.output
    assert "Unknown keys remain permitted" in result.output


# ------ status ------

def _status_resp(
    nodes=None, drifted=None, bad_state=None, open_nodes=None
):
    """Build a status RPC response with empty defaults for all issue arrays."""
    return {"ok": True, "data": {
        "nodes": nodes or [],
        "drifted": drifted or [],
        "bad_state": bad_state or [],
        "open_nodes": open_nodes or [],
    }}


def test_moved_commands_available_under_new_nouns():
    """Relocated commands live under their new noun; old paths are gone (alpha, no aliases)."""
    runner = CliRunner()
    for path in (["gateway", "admin", "--help"], ["gateway", "contacts", "--help"],
                 ["fleet", "suggest", "--help"], ["daemon", "validate", "--help"]):
        assert runner.invoke(cli, path).exit_code == 0
    assert "gateway" in runner.invoke(cli, ["--help"]).output
    # Old paths no longer resolve.
    for path in (["node", "admin"], ["node", "contacts"],
                 ["config", "suggest"], ["config", "validate"]):
        assert runner.invoke(cli, [*path, "--help"]).exit_code != 0


def test_fleet_status_with_issues_still_exits_zero():
    """`fleet status` is a viewing command: it stays exit 0 even when issues are present.

    The cron/alerting surface is `fleet issues` (exit 2), not `fleet status` (S1.5).
    """
    result = _run(["fleet", "status"], _status_resp(bad_state=[{
        "pubkey": "aabbccdd" * 8, "label": "wip 07",
        "name": "wip 07", "state": "configuring", "last_seen": None,
    }]))
    assert result.exit_code == 0


def test_top_level_status_shortcut_forwards_to_fleet_status():
    """`mch status` is a bare alias for `mch fleet status` (RECOMMENDATIONS-2 S1.4)."""
    fleet = _run(["fleet", "status"], _status_resp())
    shortcut = _run(["status"], _status_resp())
    assert shortcut.exit_code == 0
    assert shortcut.output == fleet.output


def test_top_level_sync_shortcut_forwards_to_fleet_sync():
    shortcut = _run(["sync"], {"ok": True, "data": "sync started"})
    assert shortcut.exit_code == 0
    assert "sync started" in shortcut.output


def test_top_level_shortcuts_listed_in_help():
    result = CliRunner().invoke(cli, ["--help"])
    assert "Shortcut for `mch fleet status`" in result.output
    assert "Shortcut for `mch fleet sync`" in result.output


def test_help_topics_teach_vocabulary():
    """`mch help selectors` / `mch help concepts` exist and cover the core vocabulary (S1.2)."""
    runner = CliRunner()
    selectors = runner.invoke(cli, ["help", "selectors"])
    assert selectors.exit_code == 0
    assert "pubkey prefix" in selectors.output and "mesh name" in selectors.output
    concepts = runner.invoke(cli, ["help", "concepts"])
    assert concepts.exit_code == 0
    for word in ("enforce", "audit", "fact", "skip", "Exit codes"):
        assert word in concepts.output


def test_help_states_topic_explains_the_state_column():
    """`mch help states` teaches the STATE words, including the honest unreachable line (S2.3)."""
    import re as _re
    runner = CliRunner()
    result = runner.invoke(cli, ["help", "states"])
    assert result.exit_code == 0
    for word in ("unreachable", "partial", "configured", "retired", "TRIES"):
        assert word in result.output
    # Same banned-jargon rule as --help output (RECOMMENDATIONS-2 S1.1).
    assert not _re.search(
        r"\bDB\b|source of truth|sunset|eviction|policy-only override|projection",
        result.output, _re.IGNORECASE,
    )


# ------ --json scripting surface (RECOMMENDATIONS-2 S3.2) ------

def _report_resp(nodes=None, issues=None, plan=None):
    """A fleet response in the report-model shape the daemon's `fleet` command returns."""
    return {"ok": True, "data": {
        "schema_version": 2,
        "generated_at": "2026-07-04T00:00:00Z",
        "summary": {"known_nodes": len(nodes or [])},
        "nodes": nodes or [],
        "issues": issues or {"drifted": [], "bad_state": [], "open_nodes": []},
        "plan": plan or {"writes": [], "audits": [], "unknown": [], "blocked": []},
    }}


def _report_node(pubkey="aabbccdd" * 8, label="tower", name="Tower"):
    return {"pubkey": pubkey, "label": label, "name": name, "group": None,
            "state": "configured", "last_seen": None, "last_connected": None,
            "next_sync_at": None, "facts": {}, "settings": [],
            "issues": [], "health": "healthy",
            "auth": {"required": False, "cached": True, "password_known": True, "open": False}}


def test_fleet_status_json_emits_report_verbatim():
    resp = _report_resp(nodes=[_report_node()])
    result = _run(["fleet", "status", "--json"], resp)
    assert result.exit_code == 0
    assert json.loads(result.output) == resp["data"]


def test_fleet_status_json_filters_nodes_by_query():
    resp = _report_resp(nodes=[_report_node(label="tower"),
                               _report_node(pubkey="11" * 32, label="ridge", name="Ridge")])
    result = _run(["fleet", "status", "ridge", "--json"], resp)
    assert result.exit_code == 0
    nodes = json.loads(result.output)["nodes"]
    assert [n["label"] for n in nodes] == ["ridge"]


def test_fleet_issues_json_exit_two_when_present():
    issues = {"drifted": [], "bad_state": [
        {"pubkey": "aa" * 32, "label": "x", "name": "x", "state": "configuring", "last_seen": None}
    ], "open_nodes": []}
    result = _run(["fleet", "issues", "--json"], _report_resp(issues=issues))
    assert result.exit_code == 2
    assert json.loads(result.output)["bad_state"][0]["label"] == "x"


def test_fleet_issues_json_exit_zero_when_clean():
    result = _run(["fleet", "issues", "--json"], _report_resp())
    assert result.exit_code == 0
    assert json.loads(result.output) == {"drifted": [], "bad_state": [], "open_nodes": []}


def test_fleet_plan_json_emits_plan():
    plan = {"writes": [{"key": "name", "command": "set name New"}],
            "audits": [], "unknown": [], "blocked": []}
    result = _run(["fleet", "plan", "--json"], _report_resp(plan=plan))
    assert result.exit_code == 0
    assert json.loads(result.output)["writes"][0]["command"] == "set name New"


def test_fleet_logs_json_emits_list():
    logs = [{"timestamp": "2026-07-04T00:00:00Z", "level": "info", "message": "hi"}]
    result = _run(["fleet", "logs", "--json"], {"ok": True, "data": logs})
    assert result.exit_code == 0
    assert json.loads(result.output) == logs


def test_node_show_json_emits_report_verbatim():
    node = _report_node()
    node["logs"] = []
    result = _run(["node", "show", "tower", "--json"], {"ok": True, "data": node})
    assert result.exit_code == 0
    assert json.loads(result.output) == node


def test_config_show_json_emits_resolution_verbatim():
    data = {"pubkey": "aabbccdd" * 8, "label": "tower", "name": "Tower",
            "configured": True, "resolved": {"policy": "enforce", "desired": "New",
            "desired_source": "node", "observed": "Old", "observed_at": None}, "scopes": []}
    result = _run(["config", "show", "tower", "name", "--json"], {"ok": True, "data": data})
    assert result.exit_code == 0
    assert json.loads(result.output) == data


def test_top_level_status_json_shortcut():
    resp = _report_resp(nodes=[_report_node()])
    result = _run(["status", "--json"], resp)
    assert result.exit_code == 0
    assert json.loads(result.output) == resp["data"]


# ------ fleet-wide firmware-currency surfaces (RECOMMENDATIONS-2 S3.3) ------

def _report_resp_with_firmware(ver, outdated):
    """Report response where one node reports `ver` and firmware carries the advisory."""
    node = _report_node()
    node["facts"] = {"ver": ver}
    firmware = {"latest": "v1.16.0", "outdated": outdated}
    resp = _report_resp(nodes=[node])
    resp["data"]["firmware"] = firmware
    return resp


def test_status_marks_outdated_firmware_in_ver_column():
    outdated = [{"pubkey": "aabbccdd" * 8, "label": "tower", "name": "Tower",
                 "ver": "v1.14.1", "releases_behind": 2}]
    result = _run(["fleet", "status"], _report_resp_with_firmware("v1.14.1", outdated))
    assert result.exit_code == 0
    assert "v1.14.1!" in result.output  # trailing marker flags the outdated node
    assert "behind latest v1.16.0" in result.output  # advisory footer


def test_status_no_firmware_marker_when_current():
    result = _run(["fleet", "status"], _report_resp_with_firmware("v1.16.0", []))
    assert result.exit_code == 0
    assert "!" not in result.output
    assert "behind latest" not in result.output


def test_issues_shows_firmware_advisory_without_exit_two():
    """Firmware age is advisory: it prints but never trips the exit-2 alert code (S3.3)."""
    outdated = [{"pubkey": "aabbccdd" * 8, "label": "tower", "name": "Tower",
                 "ver": "v1.14.1", "releases_behind": 2}]
    result = _run(["fleet", "issues"], _report_resp_with_firmware("v1.14.1", outdated))
    assert result.exit_code == 0  # no real issues → clean exit despite outdated firmware
    assert "no issues" in result.output
    assert "behind latest v1.16.0" in result.output


def test_fleet_issues_clean_exits_zero():
    result = _run(["fleet", "issues"], _status_resp())
    assert result.exit_code == 0
    assert "no issues" in result.output


def test_fleet_issues_present_exits_two():
    result = _run(["fleet", "issues"], _status_resp(bad_state=[{
        "pubkey": "aabbccdd" * 8, "label": "wip 07",
        "name": "wip 07", "state": "configuring", "last_seen": None,
    }]))
    assert result.exit_code == 2
    assert "problem states" in result.output
    assert "wip 07" in result.output


def test_fleet_issues_filters_by_query():
    result = _run(["fleet", "issues", "nomatch"], _status_resp(open_nodes=[{
        "pubkey": "aabbccdd" * 8, "label": "site-a", "name": "site-a",
    }]))
    # The one open node doesn't match the query, so nothing is reported → clean exit.
    assert result.exit_code == 0
    assert "no issues" in result.output


def _summary(pubkey="aabbccdd" * 8, name="Tower", label="site-a",
             group=None, state="configured", ver=None, neighbors=None):
    return {"pubkey": pubkey, "name": name, "label": label, "group": group,
            "state": state, "last_seen": None, "last_connected": None,
            "next_sync_at": None, "neighbors": neighbors, "ver": ver}


def _detail(pubkey="aabbccdd" * 8, name="Tower", label="site-a", group=None,
            facts=None, fact_times=None, settings=None, logs=None):
    return {"ok": True, "data": {
        "pubkey": pubkey, "name": name, "label": label, "group": group,
        "state": "configured", "last_seen": None, "last_connected": None,
        "next_sync_at": None,
        "auth_required": False, "auth_cached": True, "passwd_known": True,
        "facts": facts or {}, "facts_collected_at": fact_times or {},
        "settings": settings or [], "logs": logs or [],
    }}


def _fleet_resp(nodes=None, plan=None):
    return {"ok": True, "data": {
        "schema_version": 2,
        "nodes": nodes or [],
        "issues": {
            "drifted": [],
            "bad_state": [],
            "open_nodes": [],
        },
        "plan": plan or {"writes": [], "audits": [], "unknown": []},
    }}


def _run_multi(args, *responses):
    """Invoke a CLI command with _rpc mocked to return responses in sequence."""
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=list(responses)):
        return runner.invoke(cli, ["--socket", "/fake.sock"] + args)


def test_status_no_nodes():
    result = _run(["fleet", "status"], _status_resp())
    assert result.exit_code == 0
    assert "0 nodes" in result.output


def test_status_renders_canonical_fleet_api_response():
    result = _run(["fleet", "status"], _fleet_resp(nodes=[{
        "pubkey": "aabbccdd" * 8,
        "name": "Tower",
        "label": "site-a",
        "group": "repeaters",
        "state": "configured",
        "last_seen": None,
        "last_connected": None,
        "facts": {"ver": "v1.15.0", "neighbors": "4"},
    }]))

    assert result.exit_code == 0
    assert "site-a" in result.output
    assert "v1.15.0" in result.output
    assert "4" in result.output


def test_node_show_blank_mesh_name_renders_dash():
    # S2.7: a never-contacted node has no mesh name; render '-' not emptiness.
    result = _run(["node", "show", "9635"], _detail(name=""))
    assert "mesh name:   -" in result.output


def test_config_set_usage_shows_positional_metavar():
    # S2.7: usage reads with the real positionals, not the nargs `ARGS...` hack.
    runner = CliRunner()
    result = runner.invoke(cli, ["config", "set"])
    assert "[NODE] KEY [VALUE]" in result.output
    assert "ARGS..." not in result.output


def test_node_show_renders_known_path_from_detail_response():
    detail = _detail()
    detail["data"]["management_path"] = {
        "cached_path": "stored (out_path=e260; out_path_len=1; hash-mode=1)",
        "path_repair_needed": False,
    }
    result = _run(["node", "show", "site-a"], detail)

    assert result.exit_code == 0
    assert "known path:  stored (out_path=e260; out_path_len=1; hash-mode=1)" in result.output


def test_node_show_marks_known_path_repair_needed():
    detail = _detail()
    detail["data"]["management_path"] = {
        "cached_path": "flood",
        "path_repair_needed": True,
    }
    result = _run(["node", "show", "site-a"], detail)

    assert result.exit_code == 0
    assert "known path:  flood (path repair needed)" in result.output


def test_node_show_renders_canonical_node_api_unknown_fact():
    detail = {"ok": True, "data": {
        "pubkey": "aabbccdd" * 8,
        "name": "Tower",
        "label": "site-a",
        "group": None,
        "state": "configured",
        "last_seen": None,
        "last_connected": None,
        "auth": {"cached": True, "password_known": True},
        "fact_keys": ["ver"],
        "facts": {},
        "settings": [],
        "logs": [],
    }}

    result = _run(["node", "show", "site-a"], detail)

    assert result.exit_code == 0
    assert "ver" in result.output
    assert "???" in result.output


def test_status_lists_nodes():
    result = _run(["fleet", "status"], _status_resp(nodes=[_summary()]))
    assert result.exit_code == 0
    assert "configured" in result.output
    assert "Tower" in result.output


def test_status_colors_state_when_color_enabled():
    runner = CliRunner()
    with patch("mch.cli._rpc", return_value=_status_resp(nodes=[
        _summary(state="configured"),
        _summary("11223344" * 8, name="Partial", label="partial", state="partial"),
        _summary("55667788" * 8, name="Offline", label="offline", state="offline"),
    ])):
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "--color", "fleet", "status"],
            color=True,
        )

    assert result.exit_code == 0
    assert "\x1b[32mconfigured" in result.output
    assert "\x1b[33mpartial" in result.output
    assert "\x1b[31moffline" in result.output


def test_status_no_color_flag_disables_state_color():
    runner = CliRunner()
    with patch("mch.cli._rpc", return_value=_status_resp(nodes=[_summary()])):
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "--no-color", "fleet", "status"],
            color=True,
        )

    assert result.exit_code == 0
    assert "\x1b[" not in result.output
    assert "configured" in result.output


def test_status_honors_no_color_environment_variable():
    runner = CliRunner()
    with patch("mch.cli._rpc", return_value=_status_resp(nodes=[_summary()])):
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "fleet", "status"],
            color=True,
            env={"NO_COLOR": "1"},
        )

    assert result.exit_code == 0
    assert "\x1b[" not in result.output


def test_status_shows_label_and_group_columns():
    result = _run(["fleet", "status"], _status_resp(nodes=[
        _summary(group="repeaters", ver="v1.14.1", neighbors="5")
    ]))
    assert result.output.lstrip().startswith("PUBKEY")
    assert "LABEL" in result.output
    assert "GROUP" in result.output
    assert "site-a" in result.output
    assert "repeaters" in result.output


def test_status_no_issues_no_sections():
    result = _run(["fleet", "status"], _status_resp())
    assert "drifted" not in result.output
    assert "problem states" not in result.output
    assert "ghost" not in result.output
    assert "unseen" not in result.output


def test_status_summarizes_drift_without_the_table():
    # S2.1: `status` summarises; the full drift table moved to `fleet issues`.
    result = _run(["fleet", "status"], _status_resp(drifted=[{
        "node_pubkey": "aabbccdd" * 8, "label": "site-a",
        "key": "owner.info", "desired": "Managed by mch",
        "observed": "wrong", "policy": "enforce", "update_attempts": 20,
    }]))
    assert "issues: 1 drifted setting on 1 node" in result.output
    assert "(`mch fleet issues` for detail)" in result.output
    assert "owner.info" not in result.output  # the table itself is not repeated here


def test_status_summarizes_bad_state_without_the_table():
    result = _run(["fleet", "status"], _status_resp(bad_state=[{
        "pubkey": "aabbccdd" * 8, "label": "wip 07",
        "name": "wip 07", "state": "configuring", "last_seen": None,
    }]))
    assert "1 node in problem states" in result.output
    assert "problem states:" not in result.output  # section heading only in `fleet issues`


def test_status_summarizes_open_nodes_as_one_line():
    result = _run(["fleet", "status"], _status_resp(open_nodes=[{
        "pubkey": "aabbccdd" * 8, "label": "wip 06", "name": "wip 06",
    }]))
    assert "open nodes: 1 with no ACL" in result.output


def test_issues_shows_full_drift_table():
    result = _run(["fleet", "issues"], _status_resp(drifted=[{
        "node_pubkey": "aabbccdd" * 8, "label": "site-a",
        "key": "owner.info", "desired": "Managed by mch",
        "observed": "wrong", "policy": "enforce", "update_attempts": 20,
    }]))
    assert result.exit_code == 2
    assert "drifted settings" in result.output
    assert "owner.info" in result.output
    assert "TRIES" in result.output
    assert "20" in result.output


def test_status_outage_advisory_when_most_nodes_down():
    # S2.5: ≥3 and ≥half of non-retired nodes down → gateway advisory.
    nodes = [_summary(pubkey=f"{i:02d}" + "aa" * 31, label=f"n{i}", state="unreachable")
             for i in range(3)] + [_summary(pubkey="ff" * 32, label="up", state="configured")]
    result = _run(["fleet", "status"], _status_resp(nodes=nodes))
    assert "note: 3 of 4 nodes unreachable" in result.output
    assert "check the gateway" in result.output


def test_status_no_outage_advisory_when_fleet_healthy():
    nodes = [_summary(pubkey=f"{i:02d}" + "aa" * 31, label=f"n{i}", state="configured")
             for i in range(4)]
    result = _run(["fleet", "status"], _status_resp(nodes=nodes))
    assert "check the gateway" not in result.output


def test_status_multi_match_shows_table():
    # query "site" matches 2 of 3 nodes → filtered table (not detail view)
    result = _run(["fleet", "status", "site"], _status_resp(nodes=[
        _summary("aabbccdd" * 8, name="Site A", label="site-a"),
        _summary("11223344" * 8, name="Site B", label="site-b"),
        _summary("55667788" * 8, name="Ridge", label="ridge"),
    ]))
    assert "2 of 3 nodes shown" in result.output


def test_status_multiple_matches_filters_issues():
    # query "qh" matches 2 of 3 nodes; ridge's issue is filtered out
    result = _run(["fleet", "status", "qh"], _status_resp(
        nodes=[_summary("aabbccdd" * 8, name="QH Tower", label="qh-tower"),
               _summary("11223344" * 8, name="QH Valley", label="qh-valley"),
               _summary("55667788" * 8, name="Ridge", label="ridge")],
        drifted=[{"node_pubkey": "aabbccdd" * 8, "label": "qh-tower",
                  "key": "owner.info", "desired": "x", "observed": "y", "policy": "enforce"},
                 {"node_pubkey": "55667788" * 8, "label": "ridge",
                  "key": "owner.info", "desired": "x", "observed": "y", "policy": "enforce"}],
    ))
    assert "qh-tower" in result.output
    assert "ridge" not in result.output


def test_node_show_full_detail_fields():
    # The detail view (facts, identity, auth) is `mch node show` — the `fleet status` magic switch
    # that used to show it on a single match was dropped after 0.21.0.
    detail = _detail(facts={"ver": "v1.14.1", "board": "T-Beam"})
    result = _run(["node", "show", "aabb"], detail)
    assert result.exit_code == 0
    assert "Tower" in result.output
    assert "site-a" in result.output
    assert "pubkey:" in result.output
    assert "pubkey full:" in result.output
    assert "v1.14.1" in result.output
    assert "authed:" in result.output
    assert "passwd:" in result.output


def test_node_show_renders_detail():
    detail = _detail(facts={"ver": "v1.14.1", "board": "T-Beam"})
    result = _run(["node", "show", "aabb"], detail)
    assert result.exit_code == 0
    assert "pubkey:" in result.output
    assert "pubkey full:" in result.output
    assert "v1.14.1" in result.output


def test_node_show_flags_outdated_firmware():
    """The ver fact row notes how far the node is behind the latest known release."""
    detail = _detail(
        facts={"ver": "v1.10.0"},
        fact_times={"ver": "2026-06-01T23:12:00+00:00"},
    )
    result = _run(["node", "show", "aabb"], detail)
    assert result.exit_code == 0
    assert "behind" in result.output and "latest" in result.output


def test_node_show_marks_current_firmware_up_to_date():
    from mch.command_catalog import load_command_catalog

    latest = load_command_catalog().current_version
    detail = _detail(
        facts={"ver": latest},
        fact_times={"ver": "2026-06-01T23:12:00+00:00"},
    )
    result = _run(["node", "show", "aabb"], detail)
    assert result.exit_code == 0
    assert "up to date" in result.output


def test_node_show_unknown_firmware_has_no_currency_note():
    """A version newer than the catalogue (or unparseable) must not read as a problem."""
    detail = _detail(
        facts={"ver": "v9.9.9"},
        fact_times={"ver": "2026-06-01T23:12:00+00:00"},
    )
    result = _run(["node", "show", "aabb"], detail)
    assert result.exit_code == 0
    assert "behind" not in result.output and "up to date" not in result.output


def test_status_single_match_shows_table_and_hints_node_show():
    """A single match now renders the (1-row) table and points at `mch node show`, not detail.

    The magic detail switch was dropped after 0.21.0 shipped its deprecation hint (S1.3). Only one
    RPC (the fleet report) is made — no second detail call.
    """
    status = _status_resp(nodes=[_summary()])
    result = _run(["fleet", "status", "aabb"], status)
    assert result.exit_code == 0
    assert "PUBKEY" in result.output  # the table header, not the detail view
    assert "pubkey full:" not in result.output  # detail-only field
    assert "mch node show aabb" in result.output


def test_node_show_shows_next_sync_estimate():
    detail = _detail()
    detail["data"]["next_sync_at"] = (datetime.now(UTC) + timedelta(hours=19)).isoformat()

    result = _run(["node", "show", "aabb"], detail)

    assert result.exit_code == 0
    assert "next sync:" in result.output
    assert "(in ~" in result.output


def test_node_show_facts_show_observed_time_and_clock_drift():
    detail = _detail(
        facts={"clock": "12:08 - 25/5/2026 UTC", "ver": "v1.14.1"},
        fact_times={
            "clock": "2026-06-01T23:11:00+00:00",
            "ver": "2026-06-01T23:12:00+00:00",
        },
    )

    result = _run(["node", "show", "aabb"], detail)

    assert result.exit_code == 0
    assert "KEY" in result.output
    assert "OBSERVED" in result.output
    assert "clock" in result.output
    assert "2026-06-02" in result.output
    assert "slow by 7d 11h" in result.output


def test_node_show_clock_within_one_minute_is_not_called_drift():
    detail = _detail(
        facts={"clock": "12:08 - 25/5/2026 UTC"},
        fact_times={"clock": "2026-05-25T12:08:30+00:00"},
    )

    result = _run(["node", "show", "aabb"], detail)

    assert result.exit_code == 0
    assert "within ±1m" in result.output
    assert "drift <1m" not in result.output


def test_node_show_clock_ahead_is_reported_as_fast():
    detail = _detail(
        facts={"clock": "12:08 - 25/5/2026 UTC"},
        fact_times={"clock": "2026-05-25T12:04:00+00:00"},
    )

    result = _run(["node", "show", "aabb"], detail)

    assert result.exit_code == 0
    assert "fast by 4m" in result.output


def test_node_show_activity_newest_at_bottom():
    """Detail view: oldest activity entry first, newest last."""
    detail = _detail(logs=[
        {"level": "INFO", "timestamp": "2026-05-19T12:00:00", "message": "second event"},
        {"level": "INFO", "timestamp": "2026-05-19T10:00:00", "message": "first event"},
    ])
    result = _run(["node", "show", "aabb"], detail)
    assert result.exit_code == 0
    assert result.output.index("first event") < result.output.index("second event")


def test_node_show_shows_settings_drift_status():
    detail = _detail(settings=[{
        "key": "owner.info", "desired": "Managed by mch",
        "observed": "wrong", "policy": "enforce", "drift": True,
        "update_attempts": 20,
        "last_checked": None, "last_set": None,
    }])
    result = _run(["node", "show", "aabb"], detail)
    assert "STATUS" in result.output
    assert "drifted" in result.output
    assert "owner.info" in result.output
    assert "TRIES" in result.output
    assert "20" in result.output


def test_node_show_shows_unobserved_settings_in_yellow():
    runner = CliRunner()
    detail = _detail(settings=[{
        "key": "owner.info", "desired": "Managed by mch",
        "observed": None, "policy": "enforce", "drift": True,
        "update_attempts": 0,
    }])
    with patch("mch.cli._rpc", side_effect=[detail]):
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "--color", "node", "show", "aabb"],
            color=True,
        )

    assert result.exit_code == 0
    assert "unknown = not yet observed" not in result.output
    assert re.search(
        r"owner\.info\s+Managed by mch\s+-\s+0\s+\x1b\[33mnot observed\x1b\[0m",
        result.output,
    )


def test_node_show_shows_tries_as_not_applicable_for_audit():
    detail = _detail(settings=[{
        "key": "owner.info", "desired": "Managed by mch",
        "observed": "wrong", "policy": "audit", "drift": True,
        "update_attempts": 0,
    }])
    result = _run(["node", "show", "aabb"], detail)
    assert re.search(
        r"audit\s+owner\.info\s+Managed by mch\s+wrong\s+-\s+drifted", result.output
    )


def test_node_show_shows_approximate_settings_status():
    detail = _detail(settings=[{
        "key": "radio", "desired": "915.075,125,9,5",
        "observed": "915.0750122,125,9,5", "policy": "enforce",
        "drift": False, "approximate": True,
        "last_checked": None, "last_set": None,
    }])
    result = _run(["node", "show", "aabb"], detail)
    assert "* = nominal (within expected tolerance)" not in result.output
    assert "radio" in result.output
    assert re.search(
        r"radio\s+915\.075,125,9,5\s+915\.0750122,125,9,5\s+0\s+nominal",
        result.output,
    )


def test_node_show_shows_ok_settings_in_green():
    runner = CliRunner()
    detail = _detail(settings=[{
        "key": "owner.info", "desired": "Managed by mch",
        "observed": "Managed by mch", "policy": "enforce",
        "drift": False, "approximate": False,
        "update_attempts": 0,
    }])
    with patch("mch.cli._rpc", side_effect=[detail]):
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "--color", "node", "show", "aabb"],
            color=True,
        )

    assert result.exit_code == 0
    assert re.search(
        r"owner\.info\s+Managed by mch\s+Managed by mch\s+0\s+\x1b\[32mok\x1b\[0m",
        result.output,
    )


def test_status_no_match():
    # S2.7: a zero-match filter fails loudly (stderr + exit 1), like `node show`.
    result = _run(["fleet", "status", "zzzz"], _status_resp())
    assert result.exit_code == 1
    assert "no nodes matching" in result.output


def test_status_filtered_footer_says_shown():
    # 3 nodes, query "site" matches 2 → "2 of 3 nodes shown"
    result = _run(["fleet", "status", "site"], _status_resp(nodes=[
        _summary("aabbccdd" * 8, label="site-a"),
        _summary("11223344" * 8, label="site-b"),
        _summary("55667788" * 8, label="ridge"),
    ]))
    assert "2 of 3 nodes shown" in result.output


# ------ prune ------

def _prune_resp(candidates=None, deleted=0):
    return {"ok": True, "data": {"candidates": candidates or [], "deleted": deleted}}


def test_prune_no_candidates():
    result = _run(["node", "prune"], _prune_resp())
    assert result.exit_code == 0
    assert "no retired nodes to prune" in result.output
    assert "mch node retire" in result.output


def test_prune_lists_candidates_without_deleting():
    result = _run(["node", "prune"], _prune_resp(candidates=[{
        "pubkey": "aabbccdd" * 8,
        "label": "old-site",
        "name": "Old Site",
        "state": "retired",
        "last_seen": None,
    }]))
    assert result.exit_code == 0
    assert "retired nodes" in result.output
    assert "old-site" in result.output
    assert "run 'mch node prune --yes'" in result.output


def test_prune_yes_reports_deleted_count():
    result = _run(["node", "prune", "--yes"], _prune_resp(candidates=[{
        "pubkey": "aabbccdd" * 8,
        "label": "old-site",
        "name": "Old Site",
        "state": "retired",
        "last_seen": None,
    }], deleted=1))
    assert result.exit_code == 0
    assert "deleted 1 retired node(s)" in result.output


def test_prune_single_node_previews_without_yes():
    """`node prune NODE` previews the one retired node and doesn't delete without --yes (S2.3)."""
    result = _run(["node", "prune", "old-site"], _remove_resp())
    assert result.exit_code == 0
    assert "retired node to delete" in result.output
    assert "run again with --yes" in result.output


def test_prune_single_node_yes_deletes():
    result = _run_multi(
        ["node", "prune", "old-site", "--yes"], _remove_resp(), _remove_resp(deleted=1)
    )
    assert result.exit_code == 0
    assert "deleted 1 node(s)" in result.output


def test_prune_single_node_refuses_still_managed():
    """A still-managed node is refused with a retire-first hint, not deleted (S2.3 guard)."""
    refusal = {"ok": False, "error": "'site-a' is still managed - retire it first, or use --force"}
    result = _run(["node", "prune", "site-a"], refusal)
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "retire it first" in combined
    assert "mch node retire site-a" in combined


def test_node_remove_is_hidden_but_still_works():
    """`node remove` stays as a hidden deprecated alias until a release ships `prune NODE`."""
    runner = CliRunner()
    assert ("node", "remove") not in {
        ("node", name) for name in cli.commands["node"].commands
    } or cli.commands["node"].commands["remove"].hidden
    result = runner.invoke(cli, ["node", "remove", "--help"])
    assert result.exit_code == 0
    assert "deprecated" in result.output.lower()


# ------ clean facts ------

def _clean_facts_resp(candidates=None, deleted=0):
    return {"ok": True, "data": {"candidates": candidates or [], "deleted": deleted}}


def test_clean_facts_requires_scope():
    result = CliRunner().invoke(cli, ["node", "clean", "facts"])

    assert result.exit_code != 0
    assert "choose exactly one scope" in result.output


def test_clean_facts_lists_candidates_without_deleting():
    result = _run(["node", "clean", "facts", "--all", "--fact", "powersavings"], _clean_facts_resp(
        candidates=[{
            "pubkey": "aabbccdd" * 8,
            "label": "site-a",
            "name": "Tower",
            "group": "default",
            "key": "powersavings",
            "value": "on",
            "collected_at": "2026-06-01T23:12:00+00:00",
        }]
    ))

    assert result.exit_code == 0
    assert "stale facts:" in result.output
    assert "powersavings" in result.output
    assert "site-a" in result.output
    assert "run again with --yes" in result.output


def test_clean_facts_yes_reports_deleted_count():
    result = _run(["node", "clean", "facts", "site-a", "--yes"], _clean_facts_resp(
        candidates=[{
            "pubkey": "aabbccdd" * 8,
            "label": "site-a",
            "name": "Tower",
            "group": "default",
            "key": "powersavings",
            "value": "on",
            "collected_at": "2026-06-01T23:12:00+00:00",
        }],
        deleted=1,
    ))

    assert result.exit_code == 0
    assert "deleted 1 stale fact(s)" in result.output


# ------ remove ------

def _remove_resp(deleted=0, retired=True):
    return {"ok": True, "data": {
        "pubkey": "aabbccdd" * 8,
        "label": "old-site",
        "name": "Old Site",
        "state": "retired" if retired else "configured",
        "last_seen": None,
        "retired": retired,
        "deleted": deleted,
    }}


def test_remove_prompt_can_decline_delete():
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[_remove_resp()]) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "remove", "old-site"],
            input="n\n",
        )
    assert result.exit_code == 0
    mock_rpc.assert_called_once()
    assert "Delete this node?" in result.output
    assert "not deleted" in result.output


def test_remove_yes_deletes_without_prompt():
    result = _run_multi(
        ["node", "remove", "old-site", "--yes"], _remove_resp(), _remove_resp(deleted=1)
    )
    assert result.exit_code == 0
    assert "node to delete" in result.output
    assert "deleted 1 node(s)" in result.output
    assert "secrets.yaml is left unchanged" in result.output


def test_remove_active_node_notes_force_when_used():
    result = _run_multi(
        ["node", "remove", "site-a", "--force", "--yes"],
        _remove_resp(retired=False),
        _remove_resp(deleted=1, retired=False),
    )
    assert result.exit_code == 0
    assert "still managed (not retired)" in result.output


def _retire_resp(state="retired"):
    return {"ok": True, "data": {
        "pubkey": "aabbccdd" * 8, "label": "site", "name": "Site", "state": state,
    }}


def test_retire_command_sends_query_and_reports_state():
    rpc = MagicMock(return_value=_retire_resp())
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(cli, ["--socket", "/fake.sock", "node", "retire", "site"])

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {"cmd": "retire", "query": "site"}
    assert "retired site" in result.output


def test_unretire_command_sends_query_and_reports_state():
    rpc = MagicMock(return_value=_retire_resp(state="unknown"))
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(cli, ["--socket", "/fake.sock", "node", "unretire", "site"])

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {"cmd": "unretire", "query": "site"}
    assert "unretired site" in result.output


# ------ plan ------

def _plan_resp(writes=None, audits=None, unknown=None):
    return {"ok": True, "data": {
        "writes": writes or [],
        "audits": audits or [],
        "unknown": unknown or [],
    }}


def test_plan_shows_enforced_writes():
    result = _run(["fleet", "plan"], _plan_resp(writes=[{
        "pubkey": "aabbccdd" * 8,
        "label": "site-a",
        "mesh_name": "Tower",
        "key": "name",
        "known": "Old",
        "desired": "New",
        "policy": "enforce",
        "command": "set name New",
    }]))

    assert result.exit_code == 0
    assert "planned writes" in result.output
    assert "set name New" in result.output
    assert "site-a" in result.output


def test_plan_renders_canonical_fleet_api_response():
    result = _run(["fleet", "plan"], _fleet_resp(plan={"writes": [{
        "pubkey": "aabbccdd" * 8,
        "label": "site-a",
        "mesh_name": "Tower",
        "key": "name",
        "known": "Old",
        "desired": "New",
        "policy": "enforce",
        "command": "set name New",
    }], "audits": [], "unknown": []}))

    assert result.exit_code == 0
    assert "set name New" in result.output


def test_plan_shows_audit_drift_without_write():
    result = _run(["fleet", "plan"], _plan_resp(audits=[{
        "pubkey": "aabbccdd" * 8,
        "label": "site-a",
        "mesh_name": "Tower",
        "key": "owner.info",
        "known": "old",
        "desired": "new",
        "policy": "audit",
    }]))

    assert result.exit_code == 0
    assert "audit drift" in result.output
    assert "owner.info" in result.output
    assert "set owner.info" not in result.output


def test_plan_shows_unknown_values():
    result = _run(["fleet", "plan"], _plan_resp(unknown=[{
        "pubkey": "aabbccdd" * 8,
        "label": "site-a",
        "mesh_name": "Tower",
        "key": "name",
        "known": None,
        "desired": "New",
        "policy": "enforce",
        "reason": "not observed yet",
    }]))

    assert result.exit_code == 0
    assert "needs observation" in result.output
    assert "not observed yet" in result.output


def test_plan_filters_by_query():
    result = _run(["fleet", "plan", "site-b"], _plan_resp(writes=[
        {
            "pubkey": "aabbccdd" * 8,
            "label": "site-a",
            "mesh_name": "Tower A",
            "key": "name",
            "known": "Old",
            "desired": "New",
            "policy": "enforce",
            "command": "set name New",
        },
        {
            "pubkey": "11223344" * 8,
            "label": "site-b",
            "mesh_name": "Tower B",
            "key": "name",
            "known": "Old B",
            "desired": "New B",
            "policy": "enforce",
            "command": "set name New B",
        },
    ]))

    assert "site-b" in result.output
    assert "site-a" not in result.output


def test_plan_uses_display_width_for_emoji_values():
    result = _run(["fleet", "plan"], _plan_resp(writes=[{
        "pubkey": "aabbccdd" * 8,
        "label": "qh-corb",
        "mesh_name": "Tower",
        "key": "owner.info",
        "known": "🦷 dave",
        "desired": "🦷 rdm",
        "policy": "enforce",
        "command": "set owner.info 🦷 rdm",
    }]))

    assert result.exit_code == 0
    assert "🦷 dave" in result.output
    assert "🦷 rdm" in result.output
    assert "set owner.info 🦷 rdm" in result.output


# ------ logs ------

def test_logs_no_entries():
    result = _run(["fleet", "logs"], {"ok": True, "data": []})
    assert result.exit_code == 0
    assert "no logs" in result.output


def test_logs_shows_entries():
    result = _run(["fleet", "logs"], {"ok": True, "data": [
        {"node_pubkey": "aabbccdd" * 8, "label": "site-a", "level": "INFO",
         "timestamp": "2026-05-19T10:00:00", "message": "synced ok"},
    ]})
    assert result.exit_code == 0
    assert "synced ok" in result.output
    assert "INFO" in result.output
    assert "[aabbccddaabb] [site-a]" in result.output
    assert "ago)" in result.output
    assert "( " not in result.output


def test_logs_align_level_and_node_columns():
    result = _run(["fleet", "logs"], {"ok": True, "data": [
        {"node_pubkey": "11223344" * 8, "label": "long-label", "level": "WARNING",
         "timestamp": "2026-05-19T12:00:00", "message": "second event"},
        {"node_pubkey": "aabbccdd" * 8, "label": "a", "level": "INFO",
         "timestamp": "2026-05-19T10:00:00", "message": "first event"},
    ]})

    assert result.exit_code == 0
    assert "[INFO   ] [aabbccddaabb] [a         ] first event" in result.output
    assert "[WARNING] [112233441122] [long-label] second event" in result.output


def test_logs_align_relative_age_column():
    with patch("mch.cli._ago", side_effect=["6h ago", "26m ago", "26m ago", "6h ago"]):
        result = _run(["fleet", "logs"], {"ok": True, "data": [
            {"node_pubkey": "11223344" * 8, "label": "long-label", "level": "WARNING",
             "timestamp": "2026-05-19T12:00:00", "message": "second event"},
            {"node_pubkey": "aabbccdd" * 8, "label": "a", "level": "INFO",
             "timestamp": "2026-05-19T10:00:00", "message": "first event"},
        ]})

    assert result.exit_code == 0
    assert "(26m ago) [INFO" in result.output
    assert "( 6h ago) [WARNING" in result.output


def test_logs_do_not_pad_global_entries_with_blank_node_columns():
    result = _run(["fleet", "logs"], {"ok": True, "data": [
        {"node_pubkey": "aabbccdd" * 8, "label": "a", "level": "WARNING",
         "timestamp": "2026-05-19T12:00:00", "message": "node event"},
        {"node_pubkey": None, "level": "WARNING",
         "timestamp": "2026-05-19T10:00:00", "message": "global event"},
    ]})

    assert result.exit_code == 0
    assert "[WARNING] global event" in result.output
    assert "[WARNING]                            global event" not in result.output


def test_logs_show_config_only_display_identity():
    result = _run(["fleet", "logs"], {"ok": True, "data": [
        {"node_pubkey": None, "display_pubkey": "deadbeef", "display_label": "wrong",
         "level": "WARNING", "timestamp": "2026-05-19T10:00:00",
         "message": "config node not visible to gateway - cannot manage"},
    ]})

    assert result.exit_code == 0
    assert (
        "[WARNING] [deadbeef] [wrong] config node not visible to gateway - cannot manage"
        in result.output
    )


def test_logs_newest_at_bottom():
    """Daemon returns newest-first; CLI must reverse so newest appears last."""
    result = _run(["fleet", "logs"], {"ok": True, "data": [
        {"node_pubkey": "aabbccdd" * 8, "level": "INFO",
         "timestamp": "2026-05-19T12:00:00", "message": "second event"},
        {"node_pubkey": "aabbccdd" * 8, "level": "INFO",
         "timestamp": "2026-05-19T10:00:00", "message": "first event"},
    ]})
    assert result.exit_code == 0
    assert result.output.index("first event") < result.output.index("second event")


def test_node_show_activity_includes_relative_age():
    detail = _detail(logs=[
        {"level": "INFO", "timestamp": "2026-05-19T10:00:00", "message": "event"},
    ])

    result = _run(["node", "show", "aabb"], detail)

    assert "recent activity:" in result.output
    assert "ago)" in result.output


def test_node_show_activity_uses_logs_shape_without_node_columns():
    detail = _detail(logs=[
        {"node_pubkey": "aabbccdd" * 8, "label": "site-a", "level": "WARNING",
         "timestamp": "2026-05-19T12:00:00", "message": "second event"},
        {"node_pubkey": "aabbccdd" * 8, "label": "site-a", "level": "INFO",
         "timestamp": "2026-05-19T10:00:00", "message": "first event"},
    ])

    with patch("mch.cli._ago", side_effect=["6h ago", "26m ago", "26m ago", "6h ago"]):
        result = _run(["node", "show", "aabb"], detail)

    assert result.exit_code == 0
    assert "(26m ago) [INFO   ] first event" in result.output
    assert "( 6h ago) [WARNING] second event" in result.output
    assert "[aabbccddaabb]" not in result.output


# ------ node group / label ------

def test_node_group_set():
    resp = {"ok": True, "data": {
        "pubkey": "aabbccdd" * 8, "label": "site-a", "name": "Tower", "group": "solar",
    }}
    result = _run(["node", "group", "site-a", "solar"], resp)
    assert result.exit_code == 0
    assert "group set to 'solar'" in result.output


def test_node_group_clear():
    resp = {"ok": True, "data": {
        "pubkey": "aabbccdd" * 8, "label": "site-a", "name": "Tower", "group": None,
    }}
    result = _run(["node", "group", "site-a"], resp)
    assert result.exit_code == 0
    assert "group cleared" in result.output


def test_node_label_set():
    resp = {"ok": True, "data": {
        "pubkey": "aabbccdd" * 8, "label": "east-hill", "name": "Tower", "group": None,
    }}
    result = _run(["node", "label", "aabbccdd", "east-hill"], resp)
    assert result.exit_code == 0
    assert "label set to 'east-hill'" in result.output


# ------ contacts ------

def test_contacts_no_contacts():
    result = _run(["gateway", "contacts"], {"ok": True, "data": []})
    assert result.exit_code == 0
    assert "no unmanaged repeater contacts" in result.output


def test_contacts_shows_adopt_command():
    result = _run(["gateway", "contacts"], {"ok": True, "data": [
        {"public_key": "aabbccdd" * 8, "adv_name": "Millbrook", "type": 2},
    ]})
    assert result.exit_code == 0
    # Post-sunset: print the runnable adopt command with the full pubkey, not a config.yaml block.
    assert "mch node adopt" in result.output
    assert "aabbccdd" * 8 in result.output
    assert "Millbrook" in result.output
    assert "config.yaml" not in result.output


# ------ sync ------

def test_sync_all_nodes():
    result = _run(["fleet", "sync"], {"ok": True, "data": "sync started"})
    assert result.exit_code == 0
    assert "sync started" in result.output


def test_sync_single_node():
    result = _run(["fleet", "sync", "aabbcc"], {"ok": True, "data": "synced 'Tower' (aabbccdd)"})
    assert result.exit_code == 0
    assert "synced" in result.output


def test_sync_single_node_always_prints_daemon_progress():
    def fake_rpc(socket, payload, timeout=30, on_progress=None):
        assert payload == {"cmd": "sync", "prefix": "aabbcc"}
        assert on_progress is not None
        on_progress("reading fact: ver")
        on_progress("fact ver failed: timeout")
        return {"ok": True, "data": "synced 'Tower' (aabbccdd)"}

    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=fake_rpc):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "fleet", "sync", "aabbcc"])

    assert result.exit_code == 0
    assert "reading fact: ver" in result.output
    assert "fact ver failed: timeout" in result.output
    assert "synced" in result.output


def test_sync_verbose_option_has_been_removed():
    result = CliRunner().invoke(cli, ["fleet", "sync", "--verbose", "aabbcc"])

    assert result.exit_code != 0
    assert "No such option '--verbose'" in result.output


@pytest.mark.parametrize(("command", "complete"), [("stop", "stopped"), ("restart", "restarted")])
def test_lifecycle_stop_paths_warn_while_current_sync_is_finishing(command: str, complete: str):
    runner = CliRunner()
    result_obj = SimpleNamespace(returncode=0, stderr="")

    with patch("mch.cli.subprocess.run", return_value=result_obj) as systemctl:
        result = runner.invoke(cli, ["daemon", command])

    assert result.exit_code == 0
    assert "stopping current sync, this may take some time" in result.output
    assert f"mchd {complete}" in result.output
    systemctl.assert_called_once_with(
        ["systemctl", command, "mchd"], capture_output=True, text=True
    )


def test_daemon_status_shows_systemctl_status_output():
    runner = CliRunner()
    result_obj = SimpleNamespace(
        returncode=0,
        stdout="● mchd.service - Mesh Herder\n   Active: active (running)\n",
        stderr="",
    )

    with patch("mch.cli.subprocess.run", return_value=result_obj) as run:
        result = runner.invoke(cli, ["daemon", "status"])

    assert result.exit_code == 0
    assert "Active: active (running)" in result.output
    run.assert_called_once_with(
        ["systemctl", "status", "mchd", "--no-pager"], capture_output=True, text=True
    )


def test_daemon_status_passes_through_nonzero_code_without_failure_message():
    """systemctl status exits non-zero when the unit is stopped; that is not an error."""
    runner = CliRunner()
    result_obj = SimpleNamespace(
        returncode=3,
        stdout="● mchd.service - Mesh Herder\n   Active: inactive (dead)\n",
        stderr="",
    )

    with patch("mch.cli.subprocess.run", return_value=result_obj):
        result = runner.invoke(cli, ["daemon", "status"])

    assert result.exit_code == 3
    assert "Active: inactive (dead)" in result.output
    assert "failed" not in result.output.lower()


def test_daemon_logs_shows_journal_lines():
    runner = CliRunner()
    result_obj = SimpleNamespace(
        returncode=0, stdout="Jul 03 23:29:54 luna mchd[123]: started\n", stderr=""
    )

    with patch("mch.cli.subprocess.run", return_value=result_obj) as run:
        result = runner.invoke(cli, ["daemon", "logs"])

    assert result.exit_code == 0
    assert "mchd[123]: started" in result.output
    run.assert_called_once_with(
        ["journalctl", "-u", "mchd", "--no-pager", "-n", "50"],
        capture_output=True,
        text=True,
    )


def test_daemon_logs_respects_line_count():
    runner = CliRunner()
    result_obj = SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("mch.cli.subprocess.run", return_value=result_obj) as run:
        result = runner.invoke(cli, ["daemon", "logs", "-n", "200"])

    assert result.exit_code == 0
    run.assert_called_once_with(
        ["journalctl", "-u", "mchd", "--no-pager", "-n", "200"],
        capture_output=True,
        text=True,
    )


def test_daemon_logs_follow_streams_without_capture():
    runner = CliRunner()
    result_obj = SimpleNamespace(returncode=0)

    with patch("mch.cli.subprocess.run", return_value=result_obj) as run:
        result = runner.invoke(cli, ["daemon", "logs", "-f"])

    assert result.exit_code == 0
    # Follow mode must inherit stdio (no capture_output) so lines stream live
    # and Ctrl-C terminates cleanly instead of buffering indefinitely.
    run.assert_called_once_with(["journalctl", "-u", "mchd", "--no-pager", "-n", "50", "-f"])


def test_admin_stops_mchd_runs_meshcore_cli_and_restarts(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("""\
gateway:
  device: /dev/ttyUSB-admin
  baud: 57600
nodes: []
""")
    runner = CliRunner()
    ok = SimpleNamespace(returncode=0, stderr="")

    with patch("mch.cli.subprocess.run", return_value=ok) as run:
        result = runner.invoke(
            cli,
            ["gateway", "admin", "--config", str(config), "--", "-r", "ver"],
        )

    assert result.exit_code == 0
    assert "stopping mchd" in result.output
    assert "meshcore-cli -s /dev/ttyUSB-admin -b 57600 -r ver" in result.output
    assert "mchd restarted" in result.output
    assert run.call_args_list[0].args[0] == ["systemctl", "stop", "mchd"]
    assert run.call_args_list[1].args[0] == [
        "meshcore-cli",
        "-s",
        "/dev/ttyUSB-admin",
        "-b",
        "57600",
        "-r",
        "ver",
    ]
    assert run.call_args_list[2].args[0] == ["systemctl", "start", "mchd"]


def test_admin_can_leave_mchd_stopped(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("""\
gateway:
  device: /dev/ttyUSB-admin
  baud: 115200
nodes: []
""")
    runner = CliRunner()
    ok = SimpleNamespace(returncode=0, stderr="")

    with patch("mch.cli.subprocess.run", return_value=ok) as run:
        result = runner.invoke(
            cli,
            ["gateway", "admin", "--config", str(config), "--no-restart"],
        )

    assert result.exit_code == 0
    assert "mchd left stopped" in result.output
    assert [call.args[0] for call in run.call_args_list] == [
        ["systemctl", "stop", "mchd"],
        ["meshcore-cli", "-s", "/dev/ttyUSB-admin", "-b", "115200"],
    ]


def test_admin_drops_meshcore_cli_back_to_sudo_user(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("""\
gateway:
  device: /dev/ttyUSB-admin
  baud: 115200
nodes: []
""")
    runner = CliRunner()
    ok = SimpleNamespace(returncode=0, stderr="")
    pwent = SimpleNamespace(pw_dir="/home/rdm")
    dialout = SimpleNamespace(gr_gid=20, gr_mem=["rdm"])

    with patch("mch.cli.subprocess.run", return_value=ok) as run, \
         patch("mch.cli.os.geteuid", return_value=0), \
         patch.dict(
             "mch.cli.os.environ",
             {"SUDO_UID": "1000", "SUDO_GID": "1000", "SUDO_USER": "rdm"},
             clear=False,
         ), \
         patch("mch.cli.pwd.getpwuid", return_value=pwent), \
         patch("mch.cli.grp.getgrall", return_value=[dialout]):
        result = runner.invoke(cli, ["gateway", "admin", "--config", str(config)])

    assert result.exit_code == 0
    assert run.call_args_list[1].args[0] == [
        "meshcore-cli",
        "-s",
        "/dev/ttyUSB-admin",
        "-b",
        "115200",
    ]
    assert run.call_args_list[1].kwargs["user"] == 1000
    assert run.call_args_list[1].kwargs["group"] == 1000
    assert run.call_args_list[1].kwargs["extra_groups"] == [1000, 20]
    assert run.call_args_list[1].kwargs["env"]["USER"] == "rdm"
    assert run.call_args_list[1].kwargs["env"]["HOME"] == "/home/rdm"
    assert run.call_args_list[0].kwargs == {"capture_output": True, "text": True}
    assert run.call_args_list[2].kwargs == {"capture_output": True, "text": True}


# ------ suggest ------

def _suggest_node(label="site-a", name="Tower", count=5):
    return {
        "pubkey": "aabbccdd" * 8,
        "label": label,
        "name": name,
        "neighbour_count": count,
        "profile": {"name": "rooftop", "txdelay": "1.0", "direct_txdelay": "0.5",
                    "description": "few neighbours"},
        "universal": [{"key": "radio", "value": "915.800,250,10,5",
                       "description": "home freq"}],
    }


def test_suggest_table_multiple_nodes():
    result = _run(
        ["fleet", "suggest"],
        {"ok": True, "data": [_suggest_node("site-a"), _suggest_node("site-b")]},
    )
    assert result.exit_code == 0
    assert result.output.lstrip().startswith("PUBKEY")
    assert "TXDELAY" in result.output
    assert "aabbccdd" in result.output
    assert "site-a" in result.output


def test_suggest_detail_single_node():
    result = _run(["fleet", "suggest", "site-a"], {"ok": True, "data": [_suggest_node("site-a")]})
    assert result.exit_code == 0
    assert "neighbours:" in result.output
    assert "rooftop" in result.output
    # Suggest emits runnable `mch config set` commands (desired state lives in the DB now).
    assert "mch config set site-a" in result.output
    assert "--policy enforce" in result.output


def test_suggest_no_neighbour_data():
    node = _suggest_node(count=None)
    node["profile"] = None
    result = _run(["fleet", "suggest"], {"ok": True, "data": [node]})
    assert result.exit_code == 0
    assert "-" in result.output  # NBRS column shows dash


def test_suggest_no_nodes():
    result = _run(["fleet", "suggest"], {"ok": True, "data": []})
    assert result.exit_code == 0
    assert "no nodes" in result.output


# ------ exec ------

_NODE_RESP = {"ok": True, "data": {
    "pubkey": "aabbccdd" * 8,
    "name": "Tower",
    "label": "site-a",
    "state": "configured",
    "authed": True,
    "auth_required": False,
    "passwd_known": False,
    "facts": {},
    "settings": [],
    "logs": [],
    "last_seen": None,
    "last_connected": None,
    "group": None,
}}


def test_exec_blocked_command_refuses():
    """exec with a blocked command (erase) exits non-zero without calling daemon."""
    runner = CliRunner()
    with patch("mch.cli._rpc") as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "exec", "site-a", "erase"])
    mock_rpc.assert_not_called()
    assert result.exit_code != 0
    assert "blocked" in result.output.lower() or "blocked" in (result.stderr or "").lower()


@pytest.mark.parametrize(
    "command",
    [
        ("set", "prv.key", "deadbeef"),
        ("set", "guest.password", "guest"),
        ("start", "ota"),
        ("SET", "PRV.KEY", "deadbeef"),
    ],
)
def test_exec_multi_word_blocked_command_refuses(command):
    """exec blocks dangerous multi-word commands before contacting the daemon."""
    runner = CliRunner()
    with patch("mch.cli._rpc") as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "exec", "site-a", *command])
    mock_rpc.assert_not_called()
    assert result.exit_code != 0
    assert "blocked" in result.output.lower() or "blocked" in (result.stderr or "").lower()


def test_exec_free_command_shows_output():
    """exec with a free command (ver) calls daemon and prints the response."""
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[_NODE_RESP, {"ok": True, "data": "v1.14.1"}]):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "exec", "site-a", "ver"])
    assert result.exit_code == 0
    assert "v1.14.1" in result.output


def _node_resp_with_ver(ver):
    data = dict(_NODE_RESP["data"])
    data["facts"] = {"ver": ver}
    return {"ok": True, "data": data}


def test_exec_warns_when_command_newer_than_node_firmware():
    """exec advises (but does not block) when a command predates the node's firmware."""
    runner = CliRunner()
    # poweroff was added in v1.14.1; a v1.13.0 node is too old.
    with patch(
        "mch.cli._rpc",
        side_effect=[_node_resp_with_ver("v1.13.0"), {"ok": True, "data": "(sent)"}],
    ):
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "node", "exec", "site-a", "poweroff", "--yes"]
        )
    assert result.exit_code == 0  # advisory only — still runs
    combined = result.output.lower() + (result.stderr or "").lower()
    assert "warning" in combined and "firmware" in combined


def test_exec_no_warning_when_command_supported():
    """A command available on the node's firmware produces no compatibility warning."""
    runner = CliRunner()
    with patch(
        "mch.cli._rpc",
        side_effect=[_node_resp_with_ver("v1.15.0"), {"ok": True, "data": "(sent)"}],
    ):
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "node", "exec", "site-a", "poweroff", "--yes"]
        )
    assert result.exit_code == 0
    assert "warning" not in (result.output.lower() + (result.stderr or "").lower())


def test_exec_no_warning_when_node_version_unknown():
    """No firmware fact -> stay silent (never warn on a guess)."""
    runner = CliRunner()
    with patch(
        "mch.cli._rpc",
        side_effect=[_NODE_RESP, {"ok": True, "data": "(sent)"}],  # facts: {} -> no ver
    ):
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "node", "exec", "site-a", "poweroff", "--yes"]
        )
    assert result.exit_code == 0
    assert "warning" not in (result.output.lower() + (result.stderr or "").lower())


def test_exec_warns_serial_only_command():
    """exec advises (does not block) when a command is USB/serial-only."""
    runner = CliRunner()
    with patch(
        "mch.cli._rpc",
        side_effect=[_node_resp_with_ver("v1.16.0"), {"ok": True, "data": "(sent)"}],
    ):
        # get acl is serial-only; --yes not needed (not a confirm command).
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "node", "exec", "site-a", "get", "acl"]
        )
    assert result.exit_code == 0  # advisory only — still runs
    combined = result.output.lower() + (result.stderr or "").lower()
    assert "warning" in combined and "serial" in combined


def test_exec_warns_reboot_required_command():
    """exec advises when a command only takes effect after a reboot (e.g. set radio)."""
    runner = CliRunner()
    with patch(
        "mch.cli._rpc",
        side_effect=[_node_resp_with_ver("v1.16.0"), {"ok": True, "data": "(sent)"}],
    ):
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "exec", "site-a", "set", "radio", "915.8,250,11,5"],
        )
    assert result.exit_code == 0
    combined = result.output.lower() + (result.stderr or "").lower()
    assert "warning" in combined and "reboot" in combined


def test_exec_confirm_with_yes_skips_prompt():
    """exec with --yes on a confirm command (reboot) runs without prompting."""
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[
        _NODE_RESP,
        {"ok": True, "data": "(sent - no response expected)"},
    ]):
        result = runner.invoke(cli, [
                            "--socket",
                            "/fake.sock",
                            "node",
                            "exec",
                            "site-a",
                            "reboot",
                            "--yes",
                        ])
    assert result.exit_code == 0


def test_exec_confirm_prompts_and_accepts_correct_prefix():
    """exec on a confirm command prompts for pubkey prefix; correct prefix runs the command."""
    pubkey = "aabbccdd" * 8
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[
        _NODE_RESP,
        {"ok": True, "data": "(sent - no response expected)"},
    ]):
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "exec", "site-a", "reboot"],
            input=pubkey[:8] + "\n",
        )
    assert result.exit_code == 0


def test_exec_multi_word_confirm_prompts_and_accepts_correct_prefix():
    """exec prompts for dangerous multi-word commands like clear stats."""
    pubkey = "aabbccdd" * 8
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[
        _NODE_RESP,
        {"ok": True, "data": "ok"},
    ]) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "exec", "site-a", "clear", "stats"],
            input=pubkey[:8] + "\n",
        )
    assert result.exit_code == 0
    assert mock_rpc.call_count == 2


def test_exec_confirm_wrong_prefix_cancels():
    """exec on a confirm command with wrong pubkey prefix cancels without running."""
    runner = CliRunner()
    with patch("mch.cli._rpc", return_value=_NODE_RESP) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "exec", "site-a", "reboot"],
            input="wrongprefix\n",
        )
    # exec RPC should NOT have been called (only node lookup was)
    assert mock_rpc.call_count == 1
    assert result.exit_code != 0


def test_exec_daemon_error_exits_nonzero():
    """exec exits non-zero when the daemon returns an error."""
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[
        _NODE_RESP,
        {"ok": False, "error": "no gateway contact for aabbccdd"},
    ]):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "exec", "site-a", "ver"])
    assert result.exit_code != 0


# ------ commission ------

# scan returns exactly one unmanaged node + gateway_radio
_SCAN_RESP_ONE = {"ok": True, "data": {
    "nodes": [{"pubkey": "a695e2be" + "0" * 52, "node_type": 2, "SNR_in": 5.0}],
    "gateway_radio": "915.800,250,10,5",
    "radio_options": [{"label": "gateway/home radio", "value": "915.800,250,10,5"}],
    "group_options": ["default"],
}}
_SCAN_RESP_NONE = {"ok": True, "data": {
    "nodes": [],
    "gateway_radio": "915.800,250,10,5",
    "radio_options": [{"label": "gateway/home radio", "value": "915.800,250,10,5"}],
    "group_options": ["default"],
}}


def _commission_input(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_new_command_is_visible_and_commission_command_is_removed():
    # `new` now lives under the `node` group after the noun-nesting refactor.
    result = CliRunner().invoke(cli, ["node", "--help"])

    assert result.exit_code == 0
    assert "new" in result.output
    assert "commission" not in result.output

    old_result = CliRunner().invoke(cli, ["node", "commission"])
    assert old_result.exit_code != 0
    assert "No such command 'commission'" in old_result.output


def test_new_command_runs_commissioning_wizard():
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE]) as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input="q\n")

    assert mock_rpc.call_count == 1
    assert result.exit_code == 0
    assert "Add a MeshCore Node" in result.output


def test_commission_new_happy_path():
    """Happy path: scan finds node, 'new' mode, confirms, daemon returns success."""
    runner = CliRunner()
    success_resp = {"ok": True, "data": {
        "pubkey_prefix": "a695e2be",
        "label": "Test Site",
        "name": "Test Site",
        "group": None,
        "target_freq": "915.800,250,10,5",
    }}
    inp = _commission_input(
        "n",          # mode: new (after scan)
        "Test Site",  # label
        "",           # name (default to label)
        "",           # group (skip)
        "",           # admin password (accept default 'password')
        "",           # guest password (skip)
        "",           # radio (accept gateway defaults)
        "y",          # confirm
    )
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE, success_resp]) as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input=inp)
    assert result.exit_code == 0, result.output
    assert "a695e2be" in result.output  # pubkey_prefix shown
    assert "mch config import" in result.output
    assert "Reload mchd" in result.output
    assert "mch fleet status a695e2be" in result.output
    assert "Optional: mch fleet sync a695e2be" in result.output
    req = mock_rpc.call_args_list[1][0][1]
    assert req["label"] == "Test-Site"
    assert req["name"] == "Test Site"
    assert "local label must be config-safe; using 'Test-Site'" in result.output


def test_commission_new_cancel_aborts():
    """Answering 'q' at the confirm prompt aborts without calling the commission RPC."""
    runner = CliRunner()
    inp = _commission_input(
        "n",          # mode: new
        "Test Site",  # label
        "",           # name (default to label)
        "",           # group
        "",           # admin password (default 'password')
        "",           # guest password
        "",           # radio (accept gateway defaults)
        "q",          # cancel at confirm
    )
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE]) as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input=inp)
    assert mock_rpc.call_count == 1  # only the scan call, no commission call
    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()


def test_commission_new_no_at_confirm_aborts():
    """Answering 'n' at the confirm prompt aborts instead of proceeding as yes."""
    runner = CliRunner()
    inp = _commission_input(
        "n",          # mode: new
        "Test Site",  # label
        "",           # name (default to label)
        "",           # group
        "",           # admin password
        "",           # guest password
        "",           # radio
        "n",          # no at confirm
    )
    with patch("mch.cli._profile_radio_options", return_value=[]), \
         patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE]) as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input=inp)

    assert mock_rpc.call_count == 1
    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()


def test_commission_new_quit_at_radio_prompt_aborts():
    """Radio menu accepts q/quit-style cancellation."""
    runner = CliRunner()
    inp = _commission_input(
        "n",          # mode: new
        "Test Site",  # label
        "",           # name
        "",           # group
        "",           # admin password
        "",           # guest password
        "q",          # cancel at radio menu
    )
    with patch("mch.cli._profile_radio_options", return_value=[]), \
         patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE]) as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input=inp)

    assert mock_rpc.call_count == 1
    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()


def test_commission_new_admin_password_defaults_to_password():
    """New node wizard has useful defaults when user presses Enter."""
    runner = CliRunner()
    success_resp = {"ok": True, "data": {
        "pubkey_prefix": "a695e2be",
        "label": "Test Site",
        "name": "Test Site",
        "group": None,
        "target_freq": "915.800,250,10,5",
    }}
    presets = [("Australia Mid", "915.075,125,9,5")]
    with patch("mch.cli._profile_radio_options", return_value=presets), \
         patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE, success_resp]) as mock_rpc:
        runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input=_commission_input(
            "n", "Test Site", "", "", "", "", "", "y",
            # label, name, group, admin_pw, guest_pw, radio, confirm
        ))
    # Second call is the commission RPC — admin_password should be 'password'
    commission_call = mock_rpc.call_args_list[1]
    req = commission_call[0][1]
    assert req["admin_password"] == "password"
    assert req["group"] == "default"
    assert req["guest_password"] == "guest"


def test_commission_new_can_choose_config_group():
    """The new-node wizard offers groups from config.yaml."""
    runner = CliRunner()
    scan_resp = {"ok": True, "data": {
        "nodes": [{"pubkey": "a695e2be" + "0" * 52, "node_type": 2, "SNR_in": 5.0}],
        "gateway_radio": "915.800,250,10,5",
        "radio_options": [{"label": "gateway/home radio", "value": "915.800,250,10,5"}],
        "group_options": ["QH", "test"],
    }}
    success_resp = {"ok": True, "data": {
        "pubkey_prefix": "a695e2be",
        "label": "Test Site",
        "name": "Test Site",
        "group": "test",
        "target_freq": "915.800,250,10,5",
    }}
    with patch("mch.cli._profile_radio_options", return_value=[]), \
         patch("mch.cli._rpc", side_effect=[scan_resp, success_resp]) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "new"],
            input=_commission_input(
                "n", "Test Site", "", "2", "", "", "", "y",
                # mode, label, name, group choice, admin_pw, guest_pw, radio, confirm
            ),
        )

    assert result.exit_code == 0, result.output
    assert "Groups:" in result.output
    assert "1. QH" in result.output
    assert "2. test" in result.output
    req = mock_rpc.call_args_list[1][0][1]
    assert req["group"] == "test"


def test_commission_new_leaves_group_unset_when_no_config_groups():
    """When config has no groups, Enter cannot emit an undefined default group."""
    runner = CliRunner()
    scan_resp = {"ok": True, "data": {
        "nodes": [{"pubkey": "a695e2be" + "0" * 52, "node_type": 2, "SNR_in": 5.0}],
        "gateway_radio": "915.800,250,10,5",
        "radio_options": [{"label": "gateway/home radio", "value": "915.800,250,10,5"}],
        "group_options": [],
    }}
    success_resp = {"ok": True, "data": {
        "pubkey_prefix": "a695e2be",
        "label": "Test Site",
        "name": "Test Site",
        "group": None,
        "target_freq": "915.800,250,10,5",
    }}
    with patch("mch.cli._profile_radio_options", return_value=[]), \
         patch("mch.cli._rpc", side_effect=[scan_resp, success_resp]) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "new"],
            input=_commission_input(
                "n", "Test Site", "", "", "", "", "", "y",
                # mode, label, name, group default, admin_pw, guest_pw, radio, confirm
            ),
        )

    assert result.exit_code == 0, result.output
    req = mock_rpc.call_args_list[1][0][1]
    assert req["group"] is None


def test_commission_new_group_defaults_to_first_config_group():
    """When no default group exists, Enter selects the first configured group."""
    runner = CliRunner()
    scan_resp = {"ok": True, "data": {
        "nodes": [{"pubkey": "a695e2be" + "0" * 52, "node_type": 2, "SNR_in": 5.0}],
        "gateway_radio": "915.800,250,10,5",
        "radio_options": [{"label": "gateway/home radio", "value": "915.800,250,10,5"}],
        "group_options": ["QH"],
    }}
    success_resp = {"ok": True, "data": {
        "pubkey_prefix": "a695e2be",
        "label": "Test Site",
        "name": "Test Site",
        "group": "QH",
        "target_freq": "915.800,250,10,5",
    }}
    with patch("mch.cli._profile_radio_options", return_value=[]), \
         patch("mch.cli._rpc", side_effect=[scan_resp, success_resp]) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "new"],
            input=_commission_input(
                "n", "Test Site", "", "", "", "", "", "y",
                # mode, label, name, group default, admin_pw, guest_pw, radio, confirm
            ),
        )

    assert result.exit_code == 0, result.output
    req = mock_rpc.call_args_list[1][0][1]
    assert req["group"] == "QH"


def test_commission_new_can_choose_profile_radio_option():
    """The new-node wizard offers radio choices beyond the gateway default."""
    runner = CliRunner()
    success_resp = {"ok": True, "data": {
        "pubkey_prefix": "a695e2be",
        "label": "Test Site",
        "name": "Test Site",
        "group": None,
        "target_freq": "915.075,125,9,5",
    }}
    presets = [("Australia Mid", "915.075,125,9,5")]
    with patch("mch.cli._profile_radio_options", return_value=presets), \
         patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE, success_resp]) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "new"],
            input=_commission_input(
                "n", "Test Site", "", "", "", "", "2", "y",
                # mode, label, name, group, admin_pw, guest_pw, radio choice, confirm
            ),
        )

    assert result.exit_code == 0, result.output
    assert "gateway/home radio" in result.output
    assert "Australia Mid" in result.output
    req = mock_rpc.call_args_list[1][0][1]
    assert req["target_freq"] == "915.075,125,9,5"


def test_commission_new_can_choose_config_radio_option():
    """The new-node wizard offers radio choices from config.yaml."""
    runner = CliRunner()
    scan_resp = {"ok": True, "data": {
        "nodes": [{"pubkey": "a695e2be" + "0" * 52, "node_type": 2, "SNR_in": 5.0}],
        "gateway_radio": "915.800,250,10,5",
        "radio_options": [
            {"label": "gateway/home radio", "value": "915.800,250,10,5"},
            {"label": "group repeaters", "value": "916.575,62.5,7,8"},
        ],
    }}
    success_resp = {"ok": True, "data": {
        "pubkey_prefix": "a695e2be",
        "label": "Test Site",
        "name": "Test Site",
        "group": None,
        "target_freq": "916.575,62.5,7,8",
    }}
    with patch("mch.cli._profile_radio_options", return_value=[]), \
         patch("mch.cli._rpc", side_effect=[scan_resp, success_resp]) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "new"],
            input=_commission_input(
                "n", "Test Site", "", "", "", "", "2", "y",
                # mode, label, name, group, admin_pw, guest_pw, radio choice, confirm
            ),
        )

    assert result.exit_code == 0, result.output
    assert "group repeaters" in result.output
    req = mock_rpc.call_args_list[1][0][1]
    assert req["target_freq"] == "916.575,62.5,7,8"


def test_commission_new_can_paste_radio_tuple():
    """The radio menu accepts a pasted freq,bw,sf,cr tuple."""
    runner = CliRunner()
    success_resp = {"ok": True, "data": {
        "pubkey_prefix": "a695e2be",
        "label": "Test Site",
        "name": "Test Site",
        "group": None,
        "target_freq": "916.575,62.5,7,8",
    }}
    presets = [("Australia Mid", "915.075,125,9,5")]
    with patch("mch.cli._profile_radio_options", return_value=presets), \
         patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE, success_resp]) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "new"],
            input=_commission_input(
                "n", "Test Site", "", "", "", "", "p", "916.575,62.5,7,8", "y",
                # mode, label, name, group, admin_pw, guest_pw, paste, tuple, confirm
            ),
        )

    assert result.exit_code == 0, result.output
    req = mock_rpc.call_args_list[1][0][1]
    assert req["target_freq"] == "916.575,62.5,7,8"


def test_commission_new_blank_paste_returns_to_radio_menu():
    """Blank input in paste mode returns to the radio menu instead of trapping the user."""
    runner = CliRunner()
    success_resp = {"ok": True, "data": {
        "pubkey_prefix": "a695e2be",
        "label": "Test Site",
        "name": "Test Site",
        "group": None,
        "target_freq": "915.800,250,10,5",
    }}
    with patch("mch.cli._profile_radio_options", return_value=[]), \
         patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE, success_resp]) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "new"],
            input=_commission_input(
                "n", "Test Site", "", "", "", "", "p", "", "1", "y",
                # mode, label, name, group, admin_pw, guest_pw, paste, blank, radio, confirm
            ),
        )

    assert result.exit_code == 0, result.output
    req = mock_rpc.call_args_list[1][0][1]
    assert req["target_freq"] == "915.800,250,10,5"


def test_default_commission_label_uses_managed_pubkey_prefix():
    assert _default_commission_label("fa5a4515607e") == "fa5a4515607e"
    assert _default_commission_label(None) == "node01"


def test_commission_new_defaults_label_from_generated_identity():
    """Pressing Enter for label uses the generated managed pubkey."""
    runner = CliRunner()
    prv_key = "10" * 64
    pubkey = derive_public_key(prv_key)
    label = _default_commission_label(pubkey)
    success_resp = {"ok": True, "data": {
        "pubkey_prefix": pubkey[:8],
        "label": label,
        "name": label,
        "group": "default",
        "target_freq": "915.800,250,10,5",
    }}
    with patch("mch.cli.generate_private_key", return_value=prv_key), \
         patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE, success_resp]) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "node", "new"],
            input=_commission_input(
                "n", "", "", "", "", "", "", "y",
                # mode, label, name, group, admin_pw, guest_pw, radio, confirm
            ),
        )
    assert result.exit_code == 0, result.output
    req = mock_rpc.call_args_list[1][0][1]
    assert req["label"] == label
    assert req["name"] == label
    assert req["prv_key"] == prv_key


def test_commission_new_daemon_error_shows_retry_prompt():
    """When commission RPC fails, error is shown and [e]dit/[q]cancel options are offered."""
    runner = CliRunner()
    error_resp = {"ok": False, "error": "commission failed at 'password x': ERR_CODE_NOT_FOUND"}
    inp = _commission_input(
        "n",          # mode
        "Test Site",  # label
        "",           # name (default to label)
        "",           # group
        "",           # admin password (default)
        "",           # guest password
        "",           # radio (accept gateway defaults)
        "y",          # confirm
        "q",          # quit at retry prompt
    )
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE, error_resp]):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input=inp)
    assert "ERR_CODE_NOT_FOUND" in result.output
    assert result.exit_code == 0  # graceful exit via retry prompt


def test_commission_new_edit_at_summary_re_enters_values():
    """Entering 'e' at the summary confirm re-enters the wizard keeping prior values as defaults."""
    runner = CliRunner()
    success_resp = {"ok": True, "data": {
        "pubkey_prefix": "a695e2be",
        "label": "Test Site",
        "name": "Test Site",
        "group": None,
        "target_freq": "915.800,250,10,5",
    }}
    inp = _commission_input(
        "n",          # mode: new
        "Test Site",  # label (first pass)
        "",           # name (default to label)
        "",           # group
        "",           # admin password
        "",           # guest password
        "",           # radio (accept gateway defaults)
        "e",          # edit — re-enter values
        "",           # label (second pass — accept default "Test Site")
        "",           # name
        "",           # group
        "",           # admin password
        "",           # guest password
        "",           # radio (accept gateway defaults)
        "y",          # confirm (second pass)
    )
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE, success_resp]) as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input=inp)
    assert result.exit_code == 0, result.output
    assert mock_rpc.call_count == 2  # scan + commission (only one commission attempt)


def test_commission_new_edit_shows_selected_group_and_radio_first():
    """After edit, previously chosen group/radio are item 1 and visibly selected."""
    runner = CliRunner()
    scan_resp = {"ok": True, "data": {
        "nodes": [{"pubkey": "a695e2be" + "0" * 52, "node_type": 2, "SNR_in": 5.0}],
        "gateway_radio": "915.075,125,9,5",
        "radio_options": [
            {"label": "gateway/home radio", "value": "915.075,125,9,5"},
            {"label": "group dave_test", "value": "915.800,250,10,5"},
        ],
        "group_options": ["default", "narrow", "wide", "dave_test"],
    }}
    inp = _commission_input(
        "n",              # mode: new
        "Test Site",      # label
        "",               # name
        "4",              # group dave_test
        "",               # admin password
        "",               # guest password
        "2",              # radio group dave_test
        "e",              # edit
        "",               # label
        "",               # name
        "",               # group default is now selected dave_test
        "",               # admin password
        "",               # guest password
        "q",              # cancel at radio menu
    )
    with patch("mch.cli._profile_radio_options", return_value=[]), \
         patch("mch.cli._rpc", side_effect=[scan_resp]) as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input=inp)

    assert result.exit_code == 0, result.output
    assert mock_rpc.call_count == 1
    assert "  1. dave_test (selected)" in result.output
    assert "  1. 915.800,250,10,5   group dave_test (selected)" in result.output


def test_commission_quit_at_mode_prompt():
    """Entering 'q' at the mode prompt (after scan) exits cleanly."""
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE]) as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input="q\n")
    assert mock_rpc.call_count == 1  # scan was called, but nothing else
    assert result.exit_code == 0


def test_commission_scan_no_nodes_retry_then_quit():
    """When no unmanaged nodes found, retry prompt is offered; user can quit."""
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_NONE]):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input="q\n")
    assert "no unmanaged nodes" in result.output.lower()
    assert result.exit_code == 0


def test_commission_scan_busy_explains_wait_and_recovery():
    """A busy commission scan tells the operator what is happening."""
    runner = CliRunner()
    busy = {"ok": False, "error": "busy: commission"}
    with patch("mch.cli._rpc", side_effect=[busy]):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input="q\n")
    assert result.exit_code == 0
    assert "still finishing or cancelling" in result.output
    assert "Wait about 10-30 seconds" in result.output
    assert "restart mchd" in result.output


def test_commission_replace_no_replaceable_nodes():
    """'replace' mode with no config nodes having prv_key exits with informative message."""
    runner = CliRunner()
    list_resp = {"ok": True, "data": {"replaceable": []}}
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE, list_resp]):
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "node", "new"], input=_commission_input("r")
        )
    assert result.exit_code != 0
    assert "prv_key" in result.output or "no node" in result.output.lower()


def test_commission_replace_happy_path():
    """Happy path: 'replace' mode lists nodes, operator selects one, daemon confirms."""
    runner = CliRunner()
    list_resp = {"ok": True, "data": {"replaceable": [
        {"pubkey_prefix": "aabbccdd", "label": "Site A"},
        {"pubkey_prefix": "11223344", "label": "Site B"},
    ]}}
    commission_resp = {"ok": True, "data": {
        "pubkey_prefix": "aabbccdd",
        "label": "Site A",
    }}
    inp = _commission_input("r", "1", "y")
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE, list_resp, commission_resp]):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "node", "new"], input=inp)
    assert result.exit_code == 0
    assert "Site A" in result.output or "aabbccdd" in result.output


# ------ set / unset (DB-authored desired state) ------

def _set_resp(value="14", policy=None):
    return {"ok": True, "data": {
        "pubkey": "aabbccdd" * 8,
        "label": "site-a",
        "name": "Tower",
        "key": "txdelay",
        "value": value,
        "policy": policy,
    }}


def test_set_sends_node_key_value_and_reports():
    runner = CliRunner()
    rpc = MagicMock(return_value=_set_resp())
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, [
                            "--socket",
                            "/fake.sock",
                            "config",
                            "set",
                            "site-a",
                            "txdelay",
                            "14",
                        ])

    assert result.exit_code == 0
    sent = rpc.call_args.args[1]
    assert sent == {
        "cmd": "set_setting",
        "query": "site-a",
        "key": "txdelay",
        "value": "14",
        "policy": None,
    }
    assert "set txdelay = '14'" in result.output
    assert "mch fleet sync aabbccdd" in result.output


def test_set_passes_policy():
    runner = CliRunner()
    rpc = MagicMock(return_value=_set_resp(policy="audit"))
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli,
            [
                            "--socket",
                            "/fake.sock",
                            "config",
                            "set",
                            "site-a",
                            "txdelay",
                            "14",
                            "--policy",
                            "audit",
                        ],
        )

    assert result.exit_code == 0
    assert rpc.call_args.args[1]["policy"] == "audit"
    assert "policy=audit" in result.output


def test_set_policy_only_override():
    runner = CliRunner()
    rpc = MagicMock(return_value=_set_resp(value=None, policy="skip"))
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli, [
                            "--socket",
                            "/fake.sock",
                            "config",
                            "set",
                            "site-a",
                            "txdelay",
                            "--policy",
                            "skip",
                        ]
        )

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {
        "cmd": "set_setting",
        "query": "site-a",
        "key": "txdelay",
        "value": None,
        "policy": "skip",
    }


def test_set_rejects_unknown_key_before_rpc():
    runner = CliRunner()
    rpc = MagicMock(return_value=_set_resp())
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "config", "set", "site-a", "nonsense", "1"]
        )

    assert result.exit_code != 0
    assert "not a writable setting" in result.output
    rpc.assert_not_called()


def test_set_requires_value_or_policy():
    runner = CliRunner()
    rpc = MagicMock(return_value=_set_resp())
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, [
                            "--socket",
                            "/fake.sock",
                            "config",
                            "set",
                            "site-a",
                            "txdelay",
                        ])

    assert result.exit_code != 0
    assert "provide a VALUE" in result.output
    rpc.assert_not_called()


def test_set_invalid_policy_choice_rejected():
    runner = CliRunner()
    result = runner.invoke(
        cli, [
                        "--socket",
                        "/fake.sock",
                        "config",
                        "set",
                        "site-a",
                        "txdelay",
                        "14",
                        "--policy",
                        "maybe",
                    ]
    )
    assert result.exit_code != 0


def test_set_fact_policy_accepted_in_choice():
    # 'fact' is a valid --policy choice and Click must not reject it.
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--socket", "/fake.sock", "config", "set", "site-a", "txdelay", "--policy", "fact"]
    )
    assert "'fact'" not in result.output or "invalid choice" not in result.output.lower()


def test_set_readonly_fact_key_passes_gate_to_daemon():
    # A read-only fact key (ver) is not a writable setting, but `--policy fact`
    # must pass the client gate and reach the daemon (which validates the rest).
    runner = CliRunner()
    rpc = MagicMock(return_value=_set_resp(value=None, policy="fact"))
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "config", "set", "site-a", "ver", "--policy", "fact"]
        )

    assert result.exit_code == 0
    rpc.assert_called_once()
    assert rpc.call_args.args[1]["key"] == "ver"
    assert rpc.call_args.args[1]["policy"] == "fact"


def test_unset_sends_query_and_key():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {
        "pubkey": "aabbccdd" * 8, "label": "site-a", "name": "Tower", "key": "txdelay",
    }})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, [
                            "--socket",
                            "/fake.sock",
                            "config",
                            "unset",
                            "site-a",
                            "txdelay",
                        ])

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {
        "cmd": "unset_setting", "query": "site-a", "key": "txdelay",
    }
    assert "unset txdelay" in result.output


def test_unset_surfaces_daemon_error():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": False, "error": "no DB-set 'txdelay' on 'site-a'"})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, [
                            "--socket",
                            "/fake.sock",
                            "config",
                            "unset",
                            "site-a",
                            "txdelay",
                        ])

    assert result.exit_code != 0
    assert "no DB-set" in result.output


def test_show_sends_query_and_renders_resolution():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {
        "key": "txdelay",
        "pubkey": "aabbccdd" * 8,
        "label": "site-a",
        "name": "Tower",
        "group": "solar",
        "configured": True,
        "resolved": {
            "policy": "enforce", "desired": "14", "desired_source": "node",
            "observed": "14", "observed_at": "2026-06-24T00:00:00+00:00",
        },
        "scopes": [
            {"scope": "global", "scope_id": "", "desired": "10", "policy": "enforce"},
            {"scope": "node", "scope_id": "aabbccdd" * 8, "desired": "14", "policy": "enforce"},
        ],
    }})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, [
                            "--socket",
                            "/fake.sock",
                            "config",
                            "show",
                            "site-a",
                            "txdelay",
                        ])

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {"cmd": "show_setting", "query": "site-a", "key": "txdelay"}
    assert "policy=enforce" in result.output
    assert "desired='14'" in result.output
    assert "from node" in result.output
    assert "global" in result.output
    assert "where it's set:" in result.output


def test_show_reports_unconfigured_key():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {
        "key": "txdelay", "pubkey": "aabbccdd" * 8, "label": "site-a", "name": "Tower",
        "group": None, "configured": False,
        "resolved": {"policy": "audit", "desired": None, "desired_source": None,
                     "observed": None, "observed_at": None},
        "scopes": [],
    }})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, [
                            "--socket",
                            "/fake.sock",
                            "config",
                            "show",
                            "site-a",
                            "txdelay",
                        ])

    assert result.exit_code == 0
    assert "nothing is set for this key" in result.output
    assert "policy=audit (default)" in result.output
    assert "never read" in result.output


def test_show_no_key_renders_all_keys_table():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {
        "pubkey": "e2601d591517" + "00" * 26, "label": "corbin", "name": "Corb",
        "group": "deployed", "all_keys": True,
        "settings": [
            {"key": "advert.interval", "policy": "enforce", "desired": "238",
             "desired_source": "global", "observed": "238",
             "observed_at": "2026-07-03T00:00:00+00:00", "directive": None},
            {"key": "name", "policy": "enforce", "desired": "\U0001f9b7 Corb",
             "desired_source": "node", "observed": "\U0001f9b7 Corb",
             "observed_at": "2026-07-03T00:00:00+00:00",
             "directive": "node value + name_prefix '\U0001f9b7 ' from group 'deployed'"},
        ],
    }})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, [
            "--socket", "/fake.sock", "config", "show", "corbin",
        ])

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {"cmd": "show_setting", "query": "corbin"}
    assert "settings for corbin" in result.output
    assert "advert.interval" in result.output
    assert "KEY" in result.output and "FROM" in result.output
    # directive-transformed row is starred and footnoted
    assert "\U0001f9b7 Corb *" in result.output
    assert "* name:" in result.output
    assert "name_prefix" in result.output


def test_show_no_key_json_emits_settings_list():
    runner = CliRunner()
    payload = {"ok": True, "data": {
        "pubkey": "e2601d591517" + "00" * 26, "label": "corbin", "name": "Corb",
        "group": "deployed", "all_keys": True,
        "settings": [{"key": "name", "policy": "enforce", "desired": "Corb",
                      "desired_source": "node", "observed": "Corb",
                      "observed_at": None, "directive": None}],
    }}
    with patch("mch.cli._rpc", MagicMock(return_value=payload)):
        result = runner.invoke(cli, [
            "--socket", "/fake.sock", "config", "show", "corbin", "--json",
        ])
    assert result.exit_code == 0
    assert '"all_keys": true' in result.output


def test_show_global_scope_lists_rows():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {
        "scope": "global",
        "settings": [
            {"key": "owner.info", "desired": "rdm", "policy": "enforce"},
        ],
    }})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, [
            "--socket", "/fake.sock", "config", "show", "--global",
        ])
    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {"cmd": "show_setting", "scope": "global"}
    assert "global settings" in result.output
    assert "owner.info" in result.output


def test_show_group_scope_sends_group_request():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {
        "scope": "group", "group": "deployed",
        "settings": [{"key": "txdelay", "desired": "10", "policy": "audit"}],
    }})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, [
            "--socket", "/fake.sock", "config", "show", "--group", "deployed",
        ])
    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {"cmd": "show_setting", "scope": "group", "group": "deployed"}
    assert "group 'deployed'" in result.output


def test_show_node_with_scope_flag_is_usage_error():
    runner = CliRunner()
    with patch("mch.cli._rpc", MagicMock()) as rpc:
        result = runner.invoke(cli, [
            "--socket", "/fake.sock", "config", "show", "corbin", "--global",
        ])
    assert result.exit_code == 2
    rpc.assert_not_called()


def test_show_no_args_is_usage_error():
    runner = CliRunner()
    with patch("mch.cli._rpc", MagicMock()) as rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "config", "show"])
    assert result.exit_code == 2
    rpc.assert_not_called()


def test_set_group_scope_sends_group_request():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {
        "scope": "group", "group": "solar", "key": "txdelay", "value": "14", "policy": None,
    }})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "config", "set", "--group", "solar", "txdelay", "14"]
        )

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {
        "cmd": "set_setting", "scope": "group", "group": "solar",
        "key": "txdelay", "value": "14", "policy": None,
    }
    assert "group 'solar'" in result.output


def test_set_global_scope_sends_global_request():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {
        "scope": "global", "key": "txdelay", "value": "14", "policy": None,
    }})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "config", "set", "--global", "txdelay", "14"]
        )

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {
        "cmd": "set_setting", "scope": "global",
        "key": "txdelay", "value": "14", "policy": None,
    }
    assert "all managed nodes" in result.output


def test_set_accepts_name_prefix_directive():
    # name_prefix is a directive but still operator-settable; it must not be rejected
    # as "not a writable setting" before the RPC.
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {
        "scope": "global", "key": "name_prefix", "value": "\U0001f31e", "policy": None,
    }})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli, [
                            "--socket",
                            "/fake.sock",
                            "config",
                            "set",
                            "--global",
                            "name_prefix",
                            "\U0001f31e",
                        ]
        )

    assert result.exit_code == 0
    assert rpc.call_args.args[1]["key"] == "name_prefix"
    assert rpc.call_args.args[1]["scope"] == "global"


def test_set_rejects_group_and_global_together():
    runner = CliRunner()
    rpc = MagicMock()
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli,
            [
                            "--socket",
                            "/fake.sock",
                            "config",
                            "set",
                            "--group",
                            "g",
                            "--global",
                            "txdelay",
                            "14",
                        ],
        )

    assert result.exit_code != 0
    rpc.assert_not_called()


def test_set_group_scope_validates_key_before_rpc():
    runner = CliRunner()
    rpc = MagicMock(return_value=_set_resp())
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "config", "set", "--group", "g", "nonsense", "1"]
        )

    assert result.exit_code != 0
    assert "not a writable setting" in result.output
    rpc.assert_not_called()


def test_unset_group_scope_sends_group_request():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {
        "scope": "group", "group": "solar", "key": "txdelay",
    }})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "config", "unset", "--group", "solar", "txdelay"]
        )

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {
        "cmd": "unset_setting", "scope": "group", "group": "solar", "key": "txdelay",
    }
    assert "group 'solar'" in result.output


def test_unset_global_scope_sends_global_request():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {"scope": "global", "key": "txdelay"}})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, [
                            "--socket",
                            "/fake.sock",
                            "config",
                            "unset",
                            "--global",
                            "txdelay",
                        ])

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {
        "cmd": "unset_setting", "scope": "global", "key": "txdelay",
    }
    assert "all managed nodes" in result.output


def test_node_show_settings_table_has_no_source_column():
    # The DB is the only desired-state source, so the SOURCE column is dropped.
    detail = _detail(settings=[
        {
            "key": "txdelay", "desired": "14", "observed": "14",
            "policy": "enforce", "source": "db", "drift": False,
            "approximate": False, "update_attempts": 0,
        },
    ])
    result = _run(["node", "show", "aabb"], detail)

    assert result.exit_code == 0
    assert "SOURCE" not in result.output
    # DESIRED comes before OBSERVED, matching the fleet drift table.
    assert re.search(r"DESIRED\s+OBSERVED", result.output)
    # No source column between OBSERVED and TRIES now.
    assert re.search(r"txdelay\s+14\s+14\s+0\s+ok", result.output)


def test_node_show_settings_show_desired_before_observed():
    """The detail settings table must read DESIRED then OBSERVED (matches drift table)."""
    detail = _detail(settings=[{
        "key": "txdelay", "desired": "14", "observed": "20",
        "policy": "enforce", "source": "db", "drift": True,
        "approximate": False, "update_attempts": 1,
    }])
    result = _run(["node", "show", "aabb"], detail)

    assert result.exit_code == 0
    # desired (14) appears before observed (20) on the row
    assert re.search(r"txdelay\s+14\s+20\s+1\s+drifted", result.output)


# ------ clean settings ------

def _clean_settings_resp(candidates=None, deleted=0):
    return {"ok": True, "data": {"candidates": candidates or [], "deleted": deleted}}


def test_clean_settings_requires_scope():
    result = CliRunner().invoke(cli, ["node", "clean", "settings"])

    assert result.exit_code != 0
    assert "choose exactly one scope" in result.output


# ------ import ------

def _import_resp(counts=None, total=0, warnings=None, written=0, dry_run=True,
                 retired_in_backup=0, retired_mode=None):
    return {"ok": True, "data": {
        "counts": counts or {"global": 0, "group": 0, "node": 0},
        "total": total,
        "warnings": warnings or [],
        "written": written,
        "dry_run": dry_run,
        "retired_in_backup": retired_in_backup,
        "retired_mode": retired_mode,
    }}


def test_import_retired_flag_passes_mode():
    rpc = MagicMock(return_value=_import_resp(written=1, dry_run=False, retired_mode="keep"))
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(
            cli, ["--socket", "/fake.sock", "config", "import", "--retired", "keep", "--yes"]
        )

    assert result.exit_code == 0
    assert rpc.call_args.args[1]["retired"] == "keep"


def test_import_dry_run_warns_about_retired_nodes():
    rpc = MagicMock(return_value=_import_resp(total=2, retired_in_backup=2))
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(cli, ["--socket", "/fake.sock", "config", "import"])

    assert result.exit_code == 0
    assert "--retired keep" in result.output
    assert "--retired skip" in result.output


def test_import_rejects_invalid_retired_choice():
    result = CliRunner().invoke(
        cli, ["--socket", "/fake.sock", "config", "import", "--retired", "bogus"]
    )
    assert result.exit_code != 0


def test_import_dry_run_previews_counts():
    rpc = MagicMock(return_value=_import_resp(
        counts={"global": 1, "group": 0, "node": 2}, total=3,
    ))
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(cli, ["--socket", "/fake.sock", "config", "import"])

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {"cmd": "import", "dry_run": True}
    assert "3" in result.output
    assert "--yes" in result.output


def test_import_yes_applies_and_reports_written():
    rpc = MagicMock(return_value=_import_resp(
        counts={"global": 1, "group": 0, "node": 2}, total=3, written=3, dry_run=False,
    ))
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(cli, ["--socket", "/fake.sock", "config", "import", "--yes"])

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {"cmd": "import", "dry_run": False}
    assert "imported 3" in result.output


def test_import_with_explicit_path_sends_absolute_path(tmp_path):
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("nodes: []\n")
    rpc = MagicMock(return_value=_import_resp())
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(
            cli, ["--socket", "/fake.sock", "config", "import", str(legacy), "--yes"]
        )

    assert result.exit_code == 0
    sent = rpc.call_args.args[1]
    assert sent["cmd"] == "import"
    assert sent["dry_run"] is False
    assert sent["path"] == str(legacy.resolve())


def test_import_with_missing_path_errors_before_rpc():
    rpc = MagicMock(return_value=_import_resp())
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(
            cli, ["--socket", "/fake.sock", "config", "import", "/no/such/file.yaml"]
        )

    assert result.exit_code != 0
    rpc.assert_not_called()


def test_import_surfaces_warnings():
    rpc = MagicMock(return_value=_import_resp(
        warnings=["node 'ghost' (deadbeef) is not known to the DB yet - skipped"],
    ))
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(cli, ["--socket", "/fake.sock", "config", "import"])

    assert result.exit_code == 0
    assert "ghost" in result.output


# ------ export ------

def test_export_prints_yaml():
    yaml_text = "global:\n  settings:\n    owner.info: rdm\n"
    rpc = MagicMock(return_value={"ok": True, "data": {"yaml": yaml_text, "total": 1}})
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(cli, ["--socket", "/fake.sock", "config", "export"])

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {"cmd": "export"}
    assert "owner.info: rdm" in result.output


def test_export_empty_reports_nothing_to_export():
    rpc = MagicMock(return_value={"ok": True, "data": {"yaml": "", "total": 0}})
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(cli, ["--socket", "/fake.sock", "config", "export"])

    assert result.exit_code == 0
    assert "nothing to export" in result.output.lower()


def test_clean_settings_lists_candidates_without_deleting():
    rpc = MagicMock(return_value=_clean_settings_resp(candidates=[{
        "pubkey": "aabbccdd" * 8,
        "label": "site-a",
        "name": "Tower",
        "group": "default",
        "key": "txdelay",
        "value": "14",
        "collected_at": "2026-06-01T23:12:00+00:00",
    }]))
    with patch("mch.cli._rpc", rpc):
        result = CliRunner().invoke(
            cli, ["--socket", "/fake.sock", "node", "clean", "settings", "--all"]
        )

    assert result.exit_code == 0
    assert "stale settings:" in result.output
    assert "SETTING" in result.output
    assert "txdelay" in result.output
    assert "site-a" in result.output
    assert "run again with --yes" in result.output
    # dry-run default: the daemon request must not delete
    assert rpc.call_args.args[1]["dry_run"] is True
    assert rpc.call_args.args[1]["cmd"] == "clean_settings"


def test_clean_settings_yes_reports_deleted_count():
    result = _run(["node", "clean", "settings", "site-a", "--yes"], _clean_settings_resp(
        candidates=[{
            "pubkey": "aabbccdd" * 8,
            "label": "site-a",
            "name": "Tower",
            "group": "default",
            "key": "txdelay",
            "value": "14",
            "collected_at": "2026-06-01T23:12:00+00:00",
        }],
        deleted=1,
    ))

    assert result.exit_code == 0
    assert "deleted 1 stale setting(s)" in result.output


def test_clean_settings_none_reports_clean():
    result = _run(["node", "clean", "settings", "--all"], _clean_settings_resp())

    assert result.exit_code == 0
    assert "no stale settings" in result.output
