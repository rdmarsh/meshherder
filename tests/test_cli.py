"""Tests for CLI helper functions and command output."""

import asyncio
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
    result = _run(["status"], _status_resp(nodes=[{**_summary(), "last_seen": seen}]))
    assert result.exit_code == 0
    assert "  7m ago" in result.output


def test_ago_with_timestamp_is_not_padded_in_prose_output():
    seen = (datetime.now(UTC) - timedelta(minutes=7)).isoformat()
    detail = _detail()
    detail["data"]["last_seen"] = seen
    result = _run_multi(["status", "site-a"], _status_resp(nodes=[_summary()]), detail)

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


def test_current_release_version_is_0_9_35():
    assert __version__ == "0.9.35"


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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "status"])

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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "status"])

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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "status"])

    assert result.exit_code != 0
    assert "cannot connect to daemon" in result.output
    assert f"checking config: {config}" in result.output
    assert "config check failed:" in result.output
    assert "unknown config key 'typo'" in result.output
    assert "run: mch validate" in result.output


def test_daemon_connection_failure_reports_config_ok(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("nodes: []\n")

    async def fake_open_conn(*a, **kw):
        raise ConnectionRefusedError

    runner = CliRunner()
    with patch("asyncio.open_unix_connection", fake_open_conn), \
         patch("mch.cli.DEFAULT_CONFIG", config):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "status"])

    assert result.exit_code != 0
    assert "cannot connect to daemon" in result.output
    assert f"checking config: {config}" in result.output
    assert "config ok:" in result.output


def test_path_command_shows_stored_route_metadata():
    result = _run(["path", "site-a"], {"ok": True, "data": {
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
    result = _run(["path", "site-a"], {"ok": True, "data": {
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
    result = _run(["path", "site-a", "--discover"], {"ok": True, "data": {
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
    assert "apply with: mch path aabbccdd1122 --apply e260,5f6c:1" in result.output


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
            ["--socket", "/fake.sock", "path", "site-a", "--apply", "e260:1"],
            input="y\n",
        )

    assert result.exit_code == 0
    assert "updated cached management path" in result.output
    assert "route: stored" in result.output


def test_path_command_rejects_multiple_mutating_options():
    result = _run(
        ["path", "site-a", "--discover", "--apply", "e260:1"],
        {"ok": True, "data": {}},
    )

    assert result.exit_code != 0
    assert "choose only one" in result.output


def test_discover_command_renders_active_discovery_results():
    result = _run(["discover", "--radio", "915.800,250,10,5", "--dwell", "3"], {
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
    result = _run(["discover", "--radio", "915.800,250,10,5", "--unknown-only"], {
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
            ["--socket", "/fake.sock", "discover", "--radio", "915.800,250,99,5"],
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
                "--socket", "/fake.sock", "adopt", pubkey,
                "--radio", "915.800,250,11,5", "--label", "faulkland",
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
            ["--socket", "/fake.sock", "adopt", "8e4a0d73", "--radio", "915.800,250,11,5"],
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
        result = runner.invoke(cli, ["validate", "--config", str(config)])

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
            result = runner.invoke(cli, ["validate"])

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
        result = runner.invoke(cli, ["validate", "--config", str(config)])

    mock_rpc.assert_not_called()
    assert result.exit_code != 0
    assert "validation failed" in result.output
    assert "unknown config key" in result.output


def test_validate_tab_indentation_explains_fix(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("nodes:\n\t- pubkey_prefix: aabbccdd\n")

    result = CliRunner().invoke(cli, ["validate", "--config", str(config)])

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

    result = CliRunner().invoke(cli, ["validate", "--config", str(config)])

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

    result = CliRunner().invoke(cli, ["validate", "--config", str(config)])

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
            ["validate", "--config", str(config), "--secrets", str(tmp_path / "missing.yaml")],
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
        result = runner.invoke(cli, ["validate", "--config", str(config)])

    mock_rpc.assert_not_called()
    assert result.exit_code != 0
    assert "config ok:" in result.output
    assert "secrets: not checked" in result.output
    assert "did you mean 'powersaving'?" in result.output
    assert "cannot read secrets file" in result.output
    assert "sudo mch validate" in result.output


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
            cli, ["validate", "--config", str(config), "--secrets", str(secrets)]
        )

    mock_rpc.assert_not_called()
    assert result.exit_code == 0
    assert "warning:" in result.output
    assert "does not match config pubkey_prefix" in result.output


def test_validate_warns_for_unknown_fact_and_suggests_typo(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("facts:\n  - powersavings\n")

    result = CliRunner().invoke(cli, ["validate", "--config", str(config)])

    assert result.exit_code == 0
    assert "warning: facts contains unrecognised key 'powersavings'" in result.output
    assert "did you mean 'powersaving'?" in result.output
    assert "Unknown keys remain permitted" in result.output


# ------ status ------

def _status_resp(
    nodes=None, drifted=None, bad_state=None, ghosts=None, unseen_nodes=None, open_nodes=None
):
    """Build a status RPC response with empty defaults for all issue arrays."""
    return {"ok": True, "data": {
        "nodes": nodes or [],
        "drifted": drifted or [],
        "bad_state": bad_state or [],
        "ghosts": ghosts or [],
        "unseen_nodes": unseen_nodes or [],
        "open_nodes": open_nodes or [],
    }}


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
            "ghosts": [],
            "unseen_nodes": [],
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
    result = _run(["status"], _status_resp())
    assert result.exit_code == 0
    assert "0 nodes" in result.output


def test_status_renders_canonical_fleet_api_response():
    result = _run(["status"], _fleet_resp(nodes=[{
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


def test_status_node_renders_known_path_from_detail_response():
    detail = _detail()
    detail["data"]["management_path"] = {
        "cached_path": "stored (out_path=e260; out_path_len=1; hash-mode=1)",
        "path_repair_needed": False,
    }
    result = _run_multi(["status", "site-a"], _status_resp(nodes=[_summary()]), detail)

    assert result.exit_code == 0
    assert "known path:  stored (out_path=e260; out_path_len=1; hash-mode=1)" in result.output


def test_status_node_marks_known_path_repair_needed():
    detail = _detail()
    detail["data"]["management_path"] = {
        "cached_path": "flood",
        "path_repair_needed": True,
    }
    result = _run_multi(["status", "site-a"], _status_resp(nodes=[_summary()]), detail)

    assert result.exit_code == 0
    assert "known path:  flood (path repair needed)" in result.output


def test_status_detail_renders_canonical_node_api_unknown_fact():
    fleet = _fleet_resp(nodes=[{
        "pubkey": "aabbccdd" * 8,
        "name": "Tower",
        "label": "site-a",
        "group": None,
        "state": "configured",
        "last_seen": None,
        "last_connected": None,
        "facts": {},
    }])
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

    result = _run_multi(["status", "site-a"], fleet, detail)

    assert result.exit_code == 0
    assert "ver" in result.output
    assert "???" in result.output


def test_status_lists_nodes():
    result = _run(["status"], _status_resp(nodes=[_summary()]))
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
            ["--socket", "/fake.sock", "--color", "status"],
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
            ["--socket", "/fake.sock", "--no-color", "status"],
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
            ["--socket", "/fake.sock", "status"],
            color=True,
            env={"NO_COLOR": "1"},
        )

    assert result.exit_code == 0
    assert "\x1b[" not in result.output


def test_status_shows_label_and_group_columns():
    result = _run(["status"], _status_resp(nodes=[
        _summary(group="repeaters", ver="v1.14.1", neighbors="5")
    ]))
    assert result.output.lstrip().startswith("PUBKEY")
    assert "LABEL" in result.output
    assert "GROUP" in result.output
    assert "site-a" in result.output
    assert "repeaters" in result.output


def test_status_no_issues_no_sections():
    result = _run(["status"], _status_resp())
    assert "drifted" not in result.output
    assert "problem states" not in result.output
    assert "ghost" not in result.output
    assert "unseen" not in result.output


def test_status_shows_drifted_section():
    result = _run(["status"], _status_resp(drifted=[{
        "node_pubkey": "aabbccdd" * 8, "label": "site-a",
        "key": "owner.info", "desired": "Managed by mch",
        "observed": "wrong", "policy": "enforce", "update_attempts": 20,
    }]))
    assert "drifted settings" in result.output
    assert re.search(r"PUBKEY\s+LABEL", result.output)
    assert "owner.info" in result.output
    assert "site-a" in result.output
    assert "TRIES" in result.output
    assert "20" in result.output


def test_status_shows_bad_state_section():
    result = _run(["status"], _status_resp(bad_state=[{
        "pubkey": "aabbccdd" * 8, "label": "wip 07",
        "name": "wip 07", "state": "configuring", "last_seen": None,
    }]))
    assert "problem states" in result.output
    assert re.search(r"PUBKEY\s+LABEL", result.output)
    assert "configuring" in result.output
    assert "wip 07" in result.output


def test_status_shows_ghosts_section():
    result = _run(["status"], _status_resp(ghosts=[
        {
            "pubkey": "deadbeef" * 8,
            "label": "old-site",
            "name": "Old Site",
            "last_seen": None,
        }
    ]))
    assert "ghost nodes" in result.output
    assert "deadbeef" in result.output
    assert "old-site" in result.output
    assert "Old Site" in result.output


def test_status_shows_unseen_config_nodes_section():
    result = _run(["status"], _status_resp(unseen_nodes=[
        {"pubkey_prefix": "deadbeef", "label": "wrong"}
    ]))
    assert "unseen config nodes" in result.output
    assert "deadbeef" in result.output
    assert "wrong" in result.output


def test_status_shows_open_nodes_section():
    result = _run(["status"], _status_resp(open_nodes=[{
        "pubkey": "aabbccdd" * 8, "label": "wip 06", "name": "wip 06",
    }]))
    assert "open node" in result.output
    assert re.search(r"PUBKEY\s+LABEL", result.output)
    assert "wip 06" in result.output


def test_status_multi_match_shows_table():
    # query "site" matches 2 of 3 nodes → filtered table (not detail view)
    result = _run(["status", "site"], _status_resp(nodes=[
        _summary("aabbccdd" * 8, name="Site A", label="site-a"),
        _summary("11223344" * 8, name="Site B", label="site-b"),
        _summary("55667788" * 8, name="Ridge", label="ridge"),
    ]))
    assert "2 of 3 nodes shown" in result.output


def test_status_multiple_matches_filters_issues():
    # query "qh" matches 2 of 3 nodes; ridge's issue is filtered out
    result = _run(["status", "qh"], _status_resp(
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


def test_status_single_match_shows_detail():
    # query matching 1 of 2 nodes → node detail view (two RPC calls)
    status = _status_resp(nodes=[
        _summary(),  # pubkey "aabbccdd"*8, matches query "aabb"
        _summary("11223344" * 8, name="Other", label="other"),
    ])
    detail = _detail(facts={"ver": "v1.14.1", "board": "T-Beam"})
    result = _run_multi(["status", "aabb"], status, detail)
    assert result.exit_code == 0
    assert "Tower" in result.output
    assert "site-a" in result.output
    assert "pubkey:" in result.output
    assert "pubkey full:" in result.output
    assert "v1.14.1" in result.output
    assert "authed:" in result.output
    assert "passwd:" in result.output


def test_status_single_match_shows_next_sync_estimate():
    status = _status_resp(nodes=[_summary()])
    detail = _detail()
    detail["data"]["next_sync_at"] = (datetime.now(UTC) + timedelta(hours=19)).isoformat()

    result = _run_multi(["status", "aabb"], status, detail)

    assert result.exit_code == 0
    assert "next sync:" in result.output
    assert "(in ~" in result.output


def test_status_single_match_facts_show_observed_time_and_clock_drift():
    status = _status_resp(nodes=[_summary()])
    detail = _detail(
        facts={"clock": "12:08 - 25/5/2026 UTC", "ver": "v1.14.1"},
        fact_times={
            "clock": "2026-06-01T23:11:00+00:00",
            "ver": "2026-06-01T23:12:00+00:00",
        },
    )

    result = _run_multi(["status", "aabb"], status, detail)

    assert result.exit_code == 0
    assert "KEY" in result.output
    assert "OBSERVED" in result.output
    assert "clock" in result.output
    assert "2026-06-02" in result.output
    assert "slow by 7d 11h" in result.output


def test_status_single_match_clock_within_one_minute_is_not_called_drift():
    status = _status_resp(nodes=[_summary()])
    detail = _detail(
        facts={"clock": "12:08 - 25/5/2026 UTC"},
        fact_times={"clock": "2026-05-25T12:08:30+00:00"},
    )

    result = _run_multi(["status", "aabb"], status, detail)

    assert result.exit_code == 0
    assert "within ±1m" in result.output
    assert "drift <1m" not in result.output


def test_status_single_match_clock_ahead_is_reported_as_fast():
    status = _status_resp(nodes=[_summary()])
    detail = _detail(
        facts={"clock": "12:08 - 25/5/2026 UTC"},
        fact_times={"clock": "2026-05-25T12:04:00+00:00"},
    )

    result = _run_multi(["status", "aabb"], status, detail)

    assert result.exit_code == 0
    assert "fast by 4m" in result.output


def test_status_single_match_activity_newest_at_bottom():
    """Single-match detail: oldest activity entry first, newest last."""
    status = _status_resp(nodes=[_summary()])
    detail = _detail(logs=[
        {"level": "INFO", "timestamp": "2026-05-19T12:00:00", "message": "second event"},
        {"level": "INFO", "timestamp": "2026-05-19T10:00:00", "message": "first event"},
    ])
    result = _run_multi(["status", "aabb"], status, detail)
    assert result.exit_code == 0
    assert result.output.index("first event") < result.output.index("second event")


def test_status_single_match_shows_settings_drift_status():
    status = _status_resp(nodes=[_summary()])
    detail = _detail(settings=[{
        "key": "owner.info", "desired": "Managed by mch",
        "observed": "wrong", "policy": "enforce", "drift": True,
        "update_attempts": 20,
        "last_checked": None, "last_set": None,
    }])
    result = _run_multi(["status", "aabb"], status, detail)
    assert "STATUS" in result.output
    assert "drifted" in result.output
    assert "owner.info" in result.output
    assert "TRIES" in result.output
    assert "20" in result.output


def test_status_single_match_shows_unobserved_settings_in_yellow():
    runner = CliRunner()
    status = _status_resp(nodes=[_summary()])
    detail = _detail(settings=[{
        "key": "owner.info", "desired": "Managed by mch",
        "observed": None, "policy": "enforce", "drift": True,
        "update_attempts": 0,
    }])
    responses = [status, detail]
    with patch("mch.cli._rpc", side_effect=responses):
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "--color", "status", "aabb"],
            color=True,
        )

    assert result.exit_code == 0
    assert "unknown = not yet observed" not in result.output
    assert re.search(
        r"owner\.info\s+-\s+Managed by mch\s+0\s+\x1b\[33mnot observed\x1b\[0m",
        result.output,
    )


def test_status_single_match_shows_tries_as_not_applicable_for_audit():
    status = _status_resp(nodes=[_summary()])
    detail = _detail(settings=[{
        "key": "owner.info", "desired": "Managed by mch",
        "observed": "wrong", "policy": "audit", "drift": True,
        "update_attempts": 0,
    }])
    result = _run_multi(["status", "aabb"], status, detail)
    assert re.search(r"audit\s+owner\.info\s+wrong\s+Managed by mch\s+-\s+drifted", result.output)


def test_status_single_match_shows_approximate_settings_status():
    status = _status_resp(nodes=[_summary()])
    detail = _detail(settings=[{
        "key": "radio", "desired": "915.075,125,9,5",
        "observed": "915.0750122,125,9,5", "policy": "enforce",
        "drift": False, "approximate": True,
        "last_checked": None, "last_set": None,
    }])
    result = _run_multi(["status", "aabb"], status, detail)
    assert "* = nominal (within expected tolerance)" not in result.output
    assert "radio" in result.output
    assert re.search(
        r"radio\s+915\.0750122,125,9,5\s+915\.075,125,9,5\s+0\s+nominal",
        result.output,
    )


def test_status_single_match_shows_ok_settings_in_green():
    runner = CliRunner()
    status = _status_resp(nodes=[_summary()])
    detail = _detail(settings=[{
        "key": "owner.info", "desired": "Managed by mch",
        "observed": "Managed by mch", "policy": "enforce",
        "drift": False, "approximate": False,
        "update_attempts": 0,
    }])
    responses = [status, detail]
    with patch("mch.cli._rpc", side_effect=responses):
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "--color", "status", "aabb"],
            color=True,
        )

    assert result.exit_code == 0
    assert re.search(
        r"owner\.info\s+Managed by mch\s+Managed by mch\s+0\s+\x1b\[32mok\x1b\[0m",
        result.output,
    )


def test_status_no_match():
    result = _run(["status", "zzzz"], _status_resp())
    assert result.exit_code == 0
    assert "no nodes matching" in result.output


def test_status_filtered_footer_says_shown():
    # 3 nodes, query "site" matches 2 → "2 of 3 nodes shown"
    result = _run(["status", "site"], _status_resp(nodes=[
        _summary("aabbccdd" * 8, label="site-a"),
        _summary("11223344" * 8, label="site-b"),
        _summary("55667788" * 8, label="ridge"),
    ]))
    assert "2 of 3 nodes shown" in result.output


# ------ prune ------

def _prune_resp(candidates=None, deleted=0):
    return {"ok": True, "data": {"candidates": candidates or [], "deleted": deleted}}


def test_prune_no_candidates():
    result = _run(["prune"], _prune_resp())
    assert result.exit_code == 0
    assert "no DB nodes outside config.yaml" in result.output
    assert "secrets.yaml entries and gateway favourites are left unchanged" in result.output


def test_prune_lists_candidates_without_deleting():
    result = _run(["prune"], _prune_resp(candidates=[{
        "pubkey": "aabbccdd" * 8,
        "label": "old-site",
        "name": "Old Site",
        "state": "configured",
        "last_seen": None,
    }]))
    assert result.exit_code == 0
    assert "DB nodes outside config.yaml" in result.output
    assert "old-site" in result.output
    assert "run 'mch prune --yes'" in result.output
    assert "secrets.yaml entries and gateway favourites are left unchanged" in result.output


def test_prune_yes_reports_deleted_count():
    result = _run(["prune", "--yes"], _prune_resp(candidates=[{
        "pubkey": "aabbccdd" * 8,
        "label": "old-site",
        "name": "Old Site",
        "state": "configured",
        "last_seen": None,
    }], deleted=1))
    assert result.exit_code == 0
    assert "deleted 1 node(s)" in result.output
    assert "secrets.yaml entries and gateway favourites are left unchanged" in result.output


# ------ clean facts ------

def _clean_facts_resp(candidates=None, deleted=0):
    return {"ok": True, "data": {"candidates": candidates or [], "deleted": deleted}}


def test_clean_facts_requires_scope():
    result = CliRunner().invoke(cli, ["clean", "facts"])

    assert result.exit_code != 0
    assert "choose exactly one scope" in result.output


def test_clean_facts_lists_candidates_without_deleting():
    result = _run(["clean", "facts", "--all", "--fact", "powersavings"], _clean_facts_resp(
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
    result = _run(["clean", "facts", "site-a", "--yes"], _clean_facts_resp(
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

def _remove_resp(deleted=0, configured=False):
    return {"ok": True, "data": {
        "pubkey": "aabbccdd" * 8,
        "label": "old-site",
        "name": "Old Site",
        "state": "configured",
        "last_seen": None,
        "configured": configured,
        "deleted": deleted,
    }}


def test_remove_prompt_can_decline_delete():
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[_remove_resp()]) as mock_rpc:
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "remove", "old-site"],
            input="n\n",
        )
    assert result.exit_code == 0
    mock_rpc.assert_called_once()
    assert "Delete this local DB state?" in result.output
    assert "not deleted" in result.output


def test_remove_yes_deletes_without_prompt():
    result = _run_multi(["remove", "old-site", "--yes"], _remove_resp(), _remove_resp(deleted=1))
    assert result.exit_code == 0
    assert "local DB node state" in result.output
    assert "deleted 1 node(s)" in result.output
    assert "config.yaml, secrets.yaml, and gateway favourites are left unchanged" in result.output


def test_remove_configured_node_mentions_force_when_used():
    result = _run_multi(
        ["remove", "site-a", "--force", "--yes"],
        _remove_resp(configured=True),
        _remove_resp(deleted=1, configured=True),
    )
    assert result.exit_code == 0
    assert "still present in config.yaml" in result.output


# ------ plan ------

def _plan_resp(writes=None, audits=None, unknown=None):
    return {"ok": True, "data": {
        "writes": writes or [],
        "audits": audits or [],
        "unknown": unknown or [],
    }}


def test_plan_shows_enforced_writes():
    result = _run(["plan"], _plan_resp(writes=[{
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
    result = _run(["plan"], _fleet_resp(plan={"writes": [{
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
    result = _run(["plan"], _plan_resp(audits=[{
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
    result = _run(["plan"], _plan_resp(unknown=[{
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
    result = _run(["plan", "site-b"], _plan_resp(writes=[
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
    result = _run(["plan"], _plan_resp(writes=[{
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
    result = _run(["logs"], {"ok": True, "data": []})
    assert result.exit_code == 0
    assert "no logs" in result.output


def test_logs_shows_entries():
    result = _run(["logs"], {"ok": True, "data": [
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
    result = _run(["logs"], {"ok": True, "data": [
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
        result = _run(["logs"], {"ok": True, "data": [
            {"node_pubkey": "11223344" * 8, "label": "long-label", "level": "WARNING",
             "timestamp": "2026-05-19T12:00:00", "message": "second event"},
            {"node_pubkey": "aabbccdd" * 8, "label": "a", "level": "INFO",
             "timestamp": "2026-05-19T10:00:00", "message": "first event"},
        ]})

    assert result.exit_code == 0
    assert "(26m ago) [INFO" in result.output
    assert "( 6h ago) [WARNING" in result.output


def test_logs_do_not_pad_global_entries_with_blank_node_columns():
    result = _run(["logs"], {"ok": True, "data": [
        {"node_pubkey": "aabbccdd" * 8, "label": "a", "level": "WARNING",
         "timestamp": "2026-05-19T12:00:00", "message": "node event"},
        {"node_pubkey": None, "level": "WARNING",
         "timestamp": "2026-05-19T10:00:00", "message": "global event"},
    ]})

    assert result.exit_code == 0
    assert "[WARNING] global event" in result.output
    assert "[WARNING]                            global event" not in result.output


def test_logs_show_config_only_display_identity():
    result = _run(["logs"], {"ok": True, "data": [
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
    result = _run(["logs"], {"ok": True, "data": [
        {"node_pubkey": "aabbccdd" * 8, "level": "INFO",
         "timestamp": "2026-05-19T12:00:00", "message": "second event"},
        {"node_pubkey": "aabbccdd" * 8, "level": "INFO",
         "timestamp": "2026-05-19T10:00:00", "message": "first event"},
    ]})
    assert result.exit_code == 0
    assert result.output.index("first event") < result.output.index("second event")


def test_status_single_match_activity_includes_relative_age():
    status = _status_resp(nodes=[_summary()])
    detail = _detail(logs=[
        {"level": "INFO", "timestamp": "2026-05-19T10:00:00", "message": "event"},
    ])

    result = _run_multi(["status", "aabb"], status, detail)

    assert "recent activity:" in result.output
    assert "ago)" in result.output


def test_status_single_match_activity_uses_logs_shape_without_node_columns():
    status = _status_resp(nodes=[_summary()])
    detail = _detail(logs=[
        {"node_pubkey": "aabbccdd" * 8, "label": "site-a", "level": "WARNING",
         "timestamp": "2026-05-19T12:00:00", "message": "second event"},
        {"node_pubkey": "aabbccdd" * 8, "label": "site-a", "level": "INFO",
         "timestamp": "2026-05-19T10:00:00", "message": "first event"},
    ])

    with patch("mch.cli._ago", side_effect=["6h ago", "26m ago", "26m ago", "6h ago"]):
        result = _run_multi(["status", "aabb"], status, detail)

    assert result.exit_code == 0
    assert "(26m ago) [INFO   ] first event" in result.output
    assert "( 6h ago) [WARNING] second event" in result.output
    assert "[aabbccddaabb]" not in result.output


# ------ contacts ------

def test_contacts_no_contacts():
    result = _run(["contacts"], {"ok": True, "data": []})
    assert result.exit_code == 0
    assert "no repeater contacts" in result.output


def test_contacts_shows_yaml_snippet():
    result = _run(["contacts"], {"ok": True, "data": [
        {"public_key": "aabbccdd" * 8, "adv_name": "Millbrook", "type": 2},
    ]})
    assert result.exit_code == 0
    assert "pubkey_prefix" in result.output
    assert "aabbccdd" in result.output
    assert "Millbrook" in result.output


# ------ sync ------

def test_sync_all_nodes():
    result = _run(["sync"], {"ok": True, "data": "sync started"})
    assert result.exit_code == 0
    assert "sync started" in result.output


def test_sync_single_node():
    result = _run(["sync", "aabbcc"], {"ok": True, "data": "synced 'Tower' (aabbccdd)"})
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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "sync", "aabbcc"])

    assert result.exit_code == 0
    assert "reading fact: ver" in result.output
    assert "fact ver failed: timeout" in result.output
    assert "synced" in result.output


def test_sync_verbose_option_has_been_removed():
    result = CliRunner().invoke(cli, ["sync", "--verbose", "aabbcc"])

    assert result.exit_code != 0
    assert "No such option '--verbose'" in result.output


@pytest.mark.parametrize(("command", "complete"), [("stop", "stopped"), ("restart", "restarted")])
def test_lifecycle_stop_paths_warn_while_current_sync_is_finishing(command: str, complete: str):
    runner = CliRunner()
    result_obj = SimpleNamespace(returncode=0, stderr="")

    with patch("mch.cli.subprocess.run", return_value=result_obj) as systemctl:
        result = runner.invoke(cli, [command])

    assert result.exit_code == 0
    assert "stopping current sync, this may take some time" in result.output
    assert f"mchd {complete}" in result.output
    systemctl.assert_called_once_with(
        ["systemctl", command, "mchd"], capture_output=True, text=True
    )


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
            ["admin", "--config", str(config), "--", "-r", "ver"],
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
            ["admin", "--config", str(config), "--no-restart"],
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
        result = runner.invoke(cli, ["admin", "--config", str(config)])

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
        ["suggest"],
        {"ok": True, "data": [_suggest_node("site-a"), _suggest_node("site-b")]},
    )
    assert result.exit_code == 0
    assert result.output.lstrip().startswith("PUBKEY")
    assert "TXDELAY" in result.output
    assert "aabbccdd" in result.output
    assert "site-a" in result.output


def test_suggest_detail_single_node():
    result = _run(["suggest", "site-a"], {"ok": True, "data": [_suggest_node("site-a")]})
    assert result.exit_code == 0
    assert "neighbours:" in result.output
    assert "rooftop" in result.output
    assert "config.yaml" in result.output


def test_suggest_no_neighbour_data():
    node = _suggest_node(count=None)
    node["profile"] = None
    result = _run(["suggest"], {"ok": True, "data": [node]})
    assert result.exit_code == 0
    assert "-" in result.output  # NBRS column shows dash


def test_suggest_no_nodes():
    result = _run(["suggest"], {"ok": True, "data": []})
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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "exec", "site-a", "erase"])
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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "exec", "site-a", *command])
    mock_rpc.assert_not_called()
    assert result.exit_code != 0
    assert "blocked" in result.output.lower() or "blocked" in (result.stderr or "").lower()


def test_exec_free_command_shows_output():
    """exec with a free command (ver) calls daemon and prints the response."""
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[_NODE_RESP, {"ok": True, "data": "v1.14.1"}]):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "exec", "site-a", "ver"])
    assert result.exit_code == 0
    assert "v1.14.1" in result.output


def test_exec_confirm_with_yes_skips_prompt():
    """exec with --yes on a confirm command (reboot) runs without prompting."""
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[
        _NODE_RESP,
        {"ok": True, "data": "(sent - no response expected)"},
    ]):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "exec", "site-a", "reboot", "--yes"])
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
            ["--socket", "/fake.sock", "exec", "site-a", "reboot"],
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
            ["--socket", "/fake.sock", "exec", "site-a", "clear", "stats"],
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
            ["--socket", "/fake.sock", "exec", "site-a", "reboot"],
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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "exec", "site-a", "ver"])
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
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "new" in result.output
    assert "commission" not in result.output

    old_result = CliRunner().invoke(cli, ["commission"])
    assert old_result.exit_code != 0
    assert "No such command 'commission'" in old_result.output


def test_new_command_runs_commissioning_wizard():
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE]) as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input="q\n")

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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input=inp)
    assert result.exit_code == 0, result.output
    assert "a695e2be" in result.output  # pubkey_prefix shown
    assert "Restart or reload mchd" in result.output
    assert "mch status a695e2be" in result.output
    assert "Optional: mch sync a695e2be" in result.output
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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input=inp)
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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input=inp)

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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input=inp)

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
        runner.invoke(cli, ["--socket", "/fake.sock", "new"], input=_commission_input(
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
            ["--socket", "/fake.sock", "new"],
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
            ["--socket", "/fake.sock", "new"],
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
            ["--socket", "/fake.sock", "new"],
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
            ["--socket", "/fake.sock", "new"],
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
            ["--socket", "/fake.sock", "new"],
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
            ["--socket", "/fake.sock", "new"],
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
            ["--socket", "/fake.sock", "new"],
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
            ["--socket", "/fake.sock", "new"],
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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input=inp)
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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input=inp)
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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input=inp)

    assert result.exit_code == 0, result.output
    assert mock_rpc.call_count == 1
    assert "  1. dave_test (selected)" in result.output
    assert "  1. 915.800,250,10,5   group dave_test (selected)" in result.output


def test_commission_quit_at_mode_prompt():
    """Entering 'q' at the mode prompt (after scan) exits cleanly."""
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_ONE]) as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input="q\n")
    assert mock_rpc.call_count == 1  # scan was called, but nothing else
    assert result.exit_code == 0


def test_commission_scan_no_nodes_retry_then_quit():
    """When no unmanaged nodes found, retry prompt is offered; user can quit."""
    runner = CliRunner()
    with patch("mch.cli._rpc", side_effect=[_SCAN_RESP_NONE]):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input="q\n")
    assert "no unmanaged nodes" in result.output.lower()
    assert result.exit_code == 0


def test_commission_scan_busy_explains_wait_and_recovery():
    """A busy commission scan tells the operator what is happening."""
    runner = CliRunner()
    busy = {"ok": False, "error": "busy: commission"}
    with patch("mch.cli._rpc", side_effect=[busy]):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input="q\n")
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
            cli, ["--socket", "/fake.sock", "new"], input=_commission_input("r")
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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "new"], input=inp)
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
        result = runner.invoke(cli, ["--socket", "/fake.sock", "set", "site-a", "txdelay", "14"])

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
    assert "mch sync aabbccdd" in result.output


def test_set_passes_policy():
    runner = CliRunner()
    rpc = MagicMock(return_value=_set_resp(policy="audit"))
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli,
            ["--socket", "/fake.sock", "set", "site-a", "txdelay", "14", "--policy", "audit"],
        )

    assert result.exit_code == 0
    assert rpc.call_args.args[1]["policy"] == "audit"
    assert "policy=audit" in result.output


def test_set_policy_only_override():
    runner = CliRunner()
    rpc = MagicMock(return_value=_set_resp(value=None, policy="skip"))
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(
            cli, ["--socket", "/fake.sock", "set", "site-a", "txdelay", "--policy", "skip"]
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
            cli, ["--socket", "/fake.sock", "set", "site-a", "nonsense", "1"]
        )

    assert result.exit_code != 0
    assert "not a writable setting" in result.output
    rpc.assert_not_called()


def test_set_requires_value_or_policy():
    runner = CliRunner()
    rpc = MagicMock(return_value=_set_resp())
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "set", "site-a", "txdelay"])

    assert result.exit_code != 0
    assert "provide a VALUE" in result.output
    rpc.assert_not_called()


def test_set_invalid_policy_choice_rejected():
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--socket", "/fake.sock", "set", "site-a", "txdelay", "14", "--policy", "maybe"]
    )
    assert result.exit_code != 0


def test_unset_sends_query_and_key():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": True, "data": {
        "pubkey": "aabbccdd" * 8, "label": "site-a", "name": "Tower", "key": "txdelay",
    }})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "unset", "site-a", "txdelay"])

    assert result.exit_code == 0
    assert rpc.call_args.args[1] == {
        "cmd": "unset_setting", "query": "site-a", "key": "txdelay",
    }
    assert "unset txdelay" in result.output


def test_unset_surfaces_daemon_error():
    runner = CliRunner()
    rpc = MagicMock(return_value={"ok": False, "error": "no DB-set 'txdelay' on 'site-a'"})
    with patch("mch.cli._rpc", rpc):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "unset", "site-a", "txdelay"])

    assert result.exit_code != 0
    assert "no DB-set" in result.output
