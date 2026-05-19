"""Tests for CLI helper functions and command output."""

import pytest
from unittest.mock import patch

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


# ------ status ------

def test_status_no_nodes():
    result = _run(["status"], {"ok": True, "data": {"nodes": [], "drifted_settings": 0}})
    assert result.exit_code == 0
    assert "nodes: 0" in result.output


def test_status_lists_nodes():
    result = _run(["status"], {"ok": True, "data": {
        "nodes": [{"pubkey": "aabbccdd" * 8, "name": "Tower", "label": "site-a",
                   "state": "configured", "last_seen": None}],
        "drifted_settings": 0,
    }})
    assert result.exit_code == 0
    assert "configured" in result.output
    assert "Tower" in result.output


def test_status_shows_drifted_count():
    result = _run(["status"], {"ok": True, "data": {
        "nodes": [],
        "drifted_settings": 3,
    }})
    assert "drifted settings: 3" in result.output


def test_status_filtered():
    # _rpc returns all 3 nodes; client-side filter reduces to 1; "filtered from 3" appears
    result = _run(["status", "tower"], {"ok": True, "data": {
        "nodes": [
            {"pubkey": "aabbccdd" * 8, "name": "Tower", "label": "tower",
             "state": "configured", "last_seen": None},
            {"pubkey": "11223344" * 8, "name": "Valley", "label": "valley",
             "state": "configured", "last_seen": None},
            {"pubkey": "55667788" * 8, "name": "Ridge", "label": "ridge",
             "state": "configured", "last_seen": None},
        ],
        "drifted_settings": 0,
    }})
    assert "filtered from 3" in result.output


# ------ nodes ------

def test_nodes_no_nodes():
    result = _run(["nodes"], {"ok": True, "data": []})
    assert result.exit_code == 0
    assert "no nodes" in result.output


def test_nodes_lists_node():
    result = _run(["nodes"], {"ok": True, "data": [
        {"pubkey": "aabbccdd" * 8, "name": "Tower", "label": "site-a",
         "group": "repeaters", "state": "configured", "last_seen": None,
         "last_connected": None, "neighbors": "5", "ver": "v1.14.1"},
    ]})
    assert result.exit_code == 0
    assert "Tower" in result.output
    assert "site-a" in result.output
    assert "v1.14.1" in result.output


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
        "facts": {"ver": "v1.14.1", "board": "T-Beam"},
        "settings": [],
        "logs": [],
    }})
    assert result.exit_code == 0
    assert "Tower" in result.output
    assert "site-a" in result.output
    assert "v1.14.1" in result.output


def test_node_not_found():
    result = _run(["node", "zzzz"], {"ok": False, "error": "node 'zzzz' not found"})
    assert result.exit_code == 1
    assert "not found" in result.output


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


# ------ drift ------

def test_drift_no_drift():
    result = _run(["drift"], {"ok": True, "data": []})
    assert result.exit_code == 0
    assert "no drift detected" in result.output


def test_drift_shows_drifted_row():
    result = _run(["drift"], {"ok": True, "data": [
        {"node_pubkey": "aabbccdd" * 8, "label": "site-a",
         "key": "owner.info", "desired": "Managed by mch",
         "observed": "wrong", "policy": "enforce"},
    ]})
    assert result.exit_code == 0
    assert "owner.info" in result.output
    assert "site-a" in result.output


def test_drift_no_match_for_query():
    result = _run(["drift", "zzz"], {"ok": True, "data": [
        {"node_pubkey": "aabbccdd" * 8, "label": "site-a",
         "key": "owner.info", "desired": "Managed by mch",
         "observed": "wrong", "policy": "enforce"},
    ]})
    assert "no drift for" in result.output


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
