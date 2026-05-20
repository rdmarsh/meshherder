"""Tests for CLI helper functions and command output."""

import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from click.testing import CliRunner

from mch.cli import _filter_contacts, _filter_nodes, _filter_drift, cli


def _contact(pubkey: str, name: str) -> dict:
    return {"public_key": pubkey, "adv_name": name}


CONTACTS = [
    _contact("aabbccdd11223344", "Millbrook"),
    _contact("4e5f6a7b88990011", "Fernvale"),
    _contact("1234567890abcdef", "Hilltop"),
]


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


# ------ status ------

def test_status_no_nodes():
    result = _run(["status"], {"ok": True, "data": {"nodes": [], "issues": 0}})
    assert result.exit_code == 0
    assert "0 nodes" in result.output


def test_status_lists_nodes():
    result = _run(["status"], {"ok": True, "data": {
        "nodes": [{"pubkey": "aabbccdd" * 8, "name": "Tower", "label": "site-a",
                   "group": None, "state": "configured", "last_seen": None,
                   "last_connected": None, "neighbors": None, "ver": None}],
        "issues": 0,
    }})
    assert result.exit_code == 0
    assert "configured" in result.output
    assert "Tower" in result.output


def test_status_shows_issues_count():
    result = _run(["status"], {"ok": True, "data": {
        "nodes": [],
        "issues": 3,
    }})
    assert "3 issues" in result.output


def test_status_filtered():
    # _rpc returns all 3 nodes; client-side filter reduces to 1; footer shows "1 of 3 nodes"
    result = _run(["status", "tower"], {"ok": True, "data": {
        "nodes": [
            {"pubkey": "aabbccdd" * 8, "name": "Tower", "label": "tower",
             "group": None, "state": "configured", "last_seen": None,
             "last_connected": None, "neighbors": None, "ver": None},
            {"pubkey": "11223344" * 8, "name": "Valley", "label": "valley",
             "group": None, "state": "configured", "last_seen": None,
             "last_connected": None, "neighbors": None, "ver": None},
            {"pubkey": "55667788" * 8, "name": "Ridge", "label": "ridge",
             "group": None, "state": "configured", "last_seen": None,
             "last_connected": None, "neighbors": None, "ver": None},
        ],
        "issues": 0,
    }})
    assert "1 of 3 nodes shown" in result.output


def test_status_shows_label_and_group_columns():
    result = _run(["status"], {"ok": True, "data": {
        "nodes": [{"pubkey": "aabbccdd" * 8, "name": "Tower", "label": "site-a",
                   "group": "repeaters", "state": "configured", "last_seen": None,
                   "last_connected": None, "neighbors": "5", "ver": "v1.14.1"}],
        "issues": 0,
    }})
    assert "LABEL" in result.output
    assert "GROUP" in result.output
    assert "site-a" in result.output
    assert "repeaters" in result.output


def test_status_shows_issue_footer_when_issues_nonzero():
    result = _run(["status"], {"ok": True, "data": {
        "nodes": [],
        "issues": 2,
    }})
    assert "mch issues" in result.output


def test_status_no_footer_when_no_issues():
    result = _run(["status"], {"ok": True, "data": {
        "nodes": [],
        "issues": 0,
    }})
    assert "mch issues" not in result.output


def test_status_singular_issue_grammar():
    result = _run(["status"], {"ok": True, "data": {"nodes": [], "issues": 1}})
    assert "1 issue " in result.output
    assert "1 issues" not in result.output


def test_status_filtered_footer_says_shown():
    result = _run(["status", "tower"], {"ok": True, "data": {
        "nodes": [
            {"pubkey": "aabbccdd" * 8, "name": "Tower", "label": "tower",
             "group": None, "state": "configured", "last_seen": None,
             "last_connected": None, "neighbors": None, "ver": None},
            {"pubkey": "11223344" * 8, "name": "Valley", "label": "valley",
             "group": None, "state": "configured", "last_seen": None,
             "last_connected": None, "neighbors": None, "ver": None},
        ],
        "issues": 0,
    }})
    assert "1 of 2 nodes shown" in result.output


# ------ node ------

def test_node_detail_view():
    result = _run(["node", "aabb"], {"ok": True, "data": {
        "pubkey": "aabbccdd" * 8,
        "name": "Tower",
        "label": "site-a",
        "group": "repeaters",
        "state": "configured",
        "last_seen": "2026-05-19T10:00:00",
        "last_connected": None,
        "auth_required": False,
        "auth_cached": True,
        "passwd_known": True,
        "facts": {"ver": "v1.14.1", "board": "T-Beam"},
        "settings": [],
        "logs": [],
    }})
    assert result.exit_code == 0
    assert "Tower" in result.output
    assert "site-a" in result.output
    assert "v1.14.1" in result.output
    assert "authed:" in result.output
    assert "passwd:" in result.output


def test_node_not_found():
    result = _run(["node", "zzzz"], {"ok": False, "error": "node 'zzzz' not found"})
    assert result.exit_code == 1
    assert "not found" in result.output


def test_node_activity_newest_at_bottom():
    """Node recent activity must show oldest entry first, newest last."""
    result = _run(["node", "aabb"], {"ok": True, "data": {
        "pubkey": "aabbccdd" * 8,
        "name": "Tower", "label": "site-a", "group": None,
        "state": "configured", "last_seen": None, "last_connected": None,
        "auth_required": False, "auth_cached": False,
        "facts": {}, "settings": [],
        "logs": [
            {"level": "INFO", "timestamp": "2026-05-19T12:00:00", "message": "second event"},
            {"level": "INFO", "timestamp": "2026-05-19T10:00:00", "message": "first event"},
        ],
    }})
    assert result.exit_code == 0
    assert result.output.index("first event") < result.output.index("second event")


def test_node_shows_settings_drift_flag():
    result = _run(["node", "aabb"], {"ok": True, "data": {
        "pubkey": "aabbccdd" * 8,
        "name": "Tower",
        "label": "site-a",
        "group": None,
        "state": "configured",
        "last_seen": None,
        "last_connected": None,
        "auth_required": False,
        "auth_cached": False,
        "facts": {},
        "settings": [
            {"key": "owner.info", "desired": "Managed by mch",
             "observed": "wrong", "policy": "enforce", "drift": True,
             "last_checked": None, "last_set": None},
        ],
        "logs": [],
    }})
    assert "~" in result.output
    assert "owner.info" in result.output


# ------ issues ------

_ISSUES_EMPTY = {"ok": True, "data": {"drifted": [], "bad_state": [], "ghosts": [], "open_nodes": []}}


def test_issues_no_issues():
    result = _run(["issues"], _ISSUES_EMPTY)
    assert result.exit_code == 0
    assert "no issues detected" in result.output


def test_issues_shows_drifted_section():
    data = {"drifted": [{"node_pubkey": "aabbccdd" * 8, "label": "site-a",
                          "key": "owner.info", "desired": "Managed by mch",
                          "observed": "wrong", "policy": "enforce"}],
            "bad_state": [], "ghosts": [], "open_nodes": []}
    result = _run(["issues"], {"ok": True, "data": data})
    assert result.exit_code == 0
    assert "drifted settings" in result.output
    assert "owner.info" in result.output
    assert "site-a" in result.output


def test_issues_shows_bad_state_section():
    data = {"drifted": [],
            "bad_state": [{"pubkey": "aabbccdd" * 8, "label": "wip 07",
                            "name": "wip 07", "state": "configuring", "last_seen": None}],
            "ghosts": [], "open_nodes": []}
    result = _run(["issues"], {"ok": True, "data": data})
    assert result.exit_code == 0
    assert "problem states" in result.output
    assert "configuring" in result.output
    assert "wip 07" in result.output
    assert "aabbccdd" in result.output


def test_issues_shows_ghosts_section():
    data = {"drifted": [], "bad_state": [],
            "ghosts": [{"pubkey_prefix": "deadbeef", "label": "wrong"}], "open_nodes": []}
    result = _run(["issues"], {"ok": True, "data": data})
    assert result.exit_code == 0
    assert "ghost" in result.output
    assert "deadbeef" in result.output
    assert "wrong" in result.output


def test_issues_shows_open_nodes_section():
    data = {"drifted": [], "bad_state": [], "ghosts": [],
            "open_nodes": [{"pubkey": "aabbccdd" * 8, "label": "wip 06", "name": "wip 06"}]}
    result = _run(["issues"], {"ok": True, "data": data})
    assert result.exit_code == 0
    assert "open node" in result.output
    assert "wip 06" in result.output
    assert "aabbccdd" in result.output


def test_issues_filtered_by_query():
    data = {"drifted": [{"node_pubkey": "aabbccdd" * 8, "label": "site-a",
                          "key": "owner.info", "desired": "x", "observed": "y",
                          "policy": "enforce"}],
            "bad_state": [{"pubkey": "11223344" * 8, "label": "site-b",
                            "name": "B", "state": "offline", "last_seen": None}],
            "ghosts": [{"pubkey_prefix": "deadbeef", "label": "ghost-node"}],
            "open_nodes": [{"pubkey": "99aabbcc" * 8, "label": "site-c", "name": "C"}]}
    result = _run(["issues", "site-a"], {"ok": True, "data": data})
    assert "site-a" in result.output
    assert "site-b" not in result.output
    assert "deadbeef" not in result.output
    assert "site-c" not in result.output


# ------ logs ------

def test_logs_no_entries():
    result = _run(["logs"], {"ok": True, "data": []})
    assert result.exit_code == 0
    assert "no logs" in result.output


def test_logs_shows_entries():
    result = _run(["logs"], {"ok": True, "data": [
        {"node_pubkey": "aabbccdd" * 8, "level": "INFO",
         "timestamp": "2026-05-19T10:00:00", "message": "synced ok"},
    ]})
    assert result.exit_code == 0
    assert "synced ok" in result.output
    assert "INFO" in result.output


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


# ------ hint ------

def _hint_node(label="site-a", name="Tower", count=5):
    return {
        "pubkey": "aabbccdd" * 8,
        "label": label,
        "name": name,
        "neighbour_count": count,
        "profile": {"name": "rooftop", "txdelay": "1.0", "direct_txdelay": "0.5",
                    "description": "few neighbours"},
        "universal": [{"key": "radio", "value": "915.075,125,9,5",
                       "description": "home freq"}],
    }


def test_hint_table_multiple_nodes():
    result = _run(["hint"], {"ok": True, "data": [_hint_node("site-a"), _hint_node("site-b")]})
    assert result.exit_code == 0
    assert "TXDELAY" in result.output
    assert "site-a" in result.output


def test_hint_detail_single_node():
    result = _run(["hint", "site-a"], {"ok": True, "data": [_hint_node("site-a")]})
    assert result.exit_code == 0
    assert "neighbours:" in result.output
    assert "rooftop" in result.output
    assert "config.yaml" in result.output


def test_hint_no_neighbour_data():
    node = _hint_node(count=None)
    node["profile"] = None
    result = _run(["hint"], {"ok": True, "data": [node]})
    assert result.exit_code == 0
    assert "-" in result.output  # NBRS column shows dash


def test_hint_no_nodes():
    result = _run(["hint"], {"ok": True, "data": []})
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

_COMMISSION_NODES_RESP = {"ok": True, "data": {
    "nodes": [
        {"pubkey_prefix": "aabbccdd", "label": "Site A", "prv_key": True},
        {"pubkey_prefix": "11223344", "label": "Site B", "prv_key": True},
    ],
}}


def _commission_input(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_commission_new_happy_path():
    """Happy path: 'new' mode collects inputs, confirms, calls daemon, shows prv_key."""
    runner = CliRunner()
    success_resp = {"ok": True, "data": {
        "factory_prefix": "a695e2be",
        "prv_key": "deadbeef" * 8,
        "label": "Test Site",
        "name": "Test Site",
        "group": None,
        "target_freq": "915.075,125,9,5",
    }}
    with patch("mch.cli._rpc", return_value=success_resp):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "commission"], input=_commission_input(
            "n",               # mode: new
            "Test Site",       # label
            "",                # group (skip)
            "",                # name (default to label)
            "915.075,125,9,5", # target freq
            "adminpass",       # admin password
            "",                # guest password (skip)
            "y",               # confirm
        ))
    assert result.exit_code == 0
    assert "deadbeef" in result.output  # prv_key shown
    assert "a695e2be" in result.output  # factory prefix shown


def test_commission_new_cancel_aborts():
    """Answering 'n' at the confirm prompt aborts without calling the daemon."""
    runner = CliRunner()
    with patch("mch.cli._rpc") as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "commission"], input=_commission_input(
            "n",               # mode: new
            "Test Site",       # label
            "",                # group
            "",                # name
            "915.075,125,9,5", # freq
            "adminpass",       # password
            "",                # guest password
            "n",               # cancel
        ))
    mock_rpc.assert_not_called()
    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()


def test_commission_new_daemon_error_shown():
    """When daemon returns an error, it is displayed."""
    runner = CliRunner()
    with patch("mch.cli._rpc", return_value={"ok": False, "error": "no unmanaged nodes found"}):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "commission"], input=_commission_input(
            "n",
            "Test Site",
            "",
            "",
            "915.075,125,9,5",
            "adminpass",
            "",
            "y",
        ))
    assert result.exit_code != 0
    assert "no unmanaged nodes found" in result.output


def test_commission_quit_at_mode_prompt():
    """Entering 'q' at the mode prompt exits cleanly."""
    runner = CliRunner()
    with patch("mch.cli._rpc") as mock_rpc:
        result = runner.invoke(cli, ["--socket", "/fake.sock", "commission"], input="q\n")
    mock_rpc.assert_not_called()
    assert result.exit_code == 0


def test_commission_replace_no_replaceable_nodes():
    """'replace' mode with no config nodes having prv_key exits with informative message."""
    runner = CliRunner()
    list_resp = {"ok": True, "data": {"replaceable": []}}
    with patch("mch.cli._rpc", return_value=list_resp):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "commission"], input=_commission_input(
            "r",
        ))
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
    with patch("mch.cli._rpc", side_effect=[list_resp, commission_resp]):
        result = runner.invoke(cli, ["--socket", "/fake.sock", "commission"], input=_commission_input(
            "r",    # replace
            "1",    # select node 1
            "y",    # confirm
        ))
    assert result.exit_code == 0
    assert "Site A" in result.output or "aabbccdd" in result.output
