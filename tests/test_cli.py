"""Tests for CLI helper functions."""

import pytest

from mch.cli import _filter_contacts, _filter_nodes, _filter_drift


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
