"""Tests for daemon socket-protocol dispatch."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from mch.config import Config
from mch.daemon import _dispatch
from mch.db import Database
from mch.models import Node, NodeFact, NodeState


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


@pytest.fixture
def wq():
    m = MagicMock()
    m.run_node = AsyncMock()
    m.run_cycle = AsyncMock()
    return m


@pytest.fixture
def config():
    return Config()


async def _seed_node_with_facts(db: Database) -> Node:
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Test Node",
        label="test",
        state=NodeState.CONFIGURED,
    )
    await db.upsert_node(node)
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="ver", value="v1.14.1"))
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="board", value="TestBoard"))
    return node


# ------------------------------------------------------------------
# node command — prefix lookup
# ------------------------------------------------------------------

async def test_node_dispatch_prefix_returns_facts(db, wq, config):
    """node command with a prefix should return facts (not an empty dict)."""
    node = await _seed_node_with_facts(db)
    prefix = node.pubkey[:8]

    resp = await _dispatch({"cmd": "node", "pubkey": prefix}, wq, db, config)

    assert resp["ok"] is True
    data = resp["data"]
    assert data["facts"] == {"ver": "v1.14.1", "board": "TestBoard"}


async def test_node_dispatch_full_pubkey_returns_facts(db, wq, config):
    """node command with full pubkey should also return facts."""
    node = await _seed_node_with_facts(db)

    resp = await _dispatch({"cmd": "node", "pubkey": node.pubkey}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["facts"] == {"ver": "v1.14.1", "board": "TestBoard"}


async def test_node_dispatch_unknown_pubkey_returns_error(db, wq, config):
    resp = await _dispatch({"cmd": "node", "pubkey": "deadbeef"}, wq, db, config)
    assert resp["ok"] is False
    assert "not found" in resp["error"]


async def test_node_dispatch_label_match(db, wq, config):
    """node command should match by label when pubkey prefix lookup fails."""
    node = await _seed_node_with_facts(db)
    # node.label == "test"
    resp = await _dispatch({"cmd": "node", "pubkey": "test"}, wq, db, config)
    assert resp["ok"] is True
    assert resp["data"]["pubkey"] == node.pubkey


async def test_node_dispatch_label_match_case_insensitive(db, wq, config):
    node = await _seed_node_with_facts(db)
    resp = await _dispatch({"cmd": "node", "pubkey": "TEST"}, wq, db, config)
    assert resp["ok"] is True
    assert resp["data"]["pubkey"] == node.pubkey


async def test_node_dispatch_label_substring_match(db, wq, config):
    """node command matches a label by substring, not just exact match."""
    node = await _seed_node_with_facts(db)
    # node.label == "test"; query "es" should match
    resp = await _dispatch({"cmd": "node", "pubkey": "es"}, wq, db, config)
    assert resp["ok"] is True
    assert resp["data"]["pubkey"] == node.pubkey


async def test_node_dispatch_name_substring_match(db, wq, config):
    """node command falls through to name substring when label doesn't match."""
    node = await _seed_node_with_facts(db)
    # node.name == "Test Node"; query "ode" should match
    resp = await _dispatch({"cmd": "node", "pubkey": "ode"}, wq, db, config)
    assert resp["ok"] is True
    assert resp["data"]["pubkey"] == node.pubkey


async def test_node_dispatch_ambiguous_returns_error(db, wq, config):
    """node command returns an error when the query matches more than one node."""
    node1 = Node(pubkey="aabbccdd" * 8, name="Wip Node A", label="wip 01", state=NodeState.CONFIGURED)
    node2 = Node(pubkey="11223344" * 8, name="Wip Node B", label="wip 02", state=NodeState.CONFIGURED)
    await db.upsert_node(node1)
    await db.upsert_node(node2)

    resp = await _dispatch({"cmd": "node", "pubkey": "wip"}, wq, db, config)
    assert resp["ok"] is False
    assert "ambiguous" in resp["error"]


# ------------------------------------------------------------------
# logs command — label/name lookup
# ------------------------------------------------------------------

async def test_logs_dispatch_label_substring_match(db, wq, config):
    """logs command resolves a label substring to the node's logs."""
    node = Node(pubkey="aabbccdd" * 8, name="Wip Node", label="wip 07", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.write_log(node.pubkey, "INFO", "test message")

    resp = await _dispatch({"cmd": "logs", "pubkey": "wip"}, wq, db, config)
    assert resp["ok"] is True
    assert len(resp["data"]) == 1
    assert resp["data"][0]["message"] == "test message"


async def test_logs_dispatch_ambiguous_returns_error(db, wq, config):
    """logs command returns an error when the query matches more than one node."""
    node1 = Node(pubkey="aabbccdd" * 8, name="Wip A", label="wip 01", state=NodeState.CONFIGURED)
    node2 = Node(pubkey="11223344" * 8, name="Wip B", label="wip 02", state=NodeState.CONFIGURED)
    await db.upsert_node(node1)
    await db.upsert_node(node2)

    resp = await _dispatch({"cmd": "logs", "pubkey": "wip"}, wq, db, config)
    assert resp["ok"] is False
    assert "ambiguous" in resp["error"]


async def test_hint_dispatch_label_match(db, wq, config):
    """hint command should match by label when pubkey prefix lookup fails."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower Node", label="QH Test", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="neighbors", value="5"))

    resp = await _dispatch({"cmd": "hint", "pubkey": "QH Test"}, wq, db, config)
    assert resp["ok"] is True
    assert len(resp["data"]) == 1
    assert resp["data"][0]["label"] == "QH Test"


# ------------------------------------------------------------------
# nodes command — includes neighbors fact
# ------------------------------------------------------------------

async def test_nodes_dispatch_includes_neighbors(db, wq, config):
    """nodes command should include the neighbors fact for each node."""
    node = Node(pubkey="aabbccdd" * 8, name="Test", label="test", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="neighbors", value="7"))

    resp = await _dispatch({"cmd": "nodes"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"][0]["neighbors"] == "7"


async def test_nodes_dispatch_includes_ver(db, wq, config):
    """nodes command should include the ver fact for each node."""
    node = Node(pubkey="aabbccdd" * 8, name="Test", label="test", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="ver", value="v1.15.0-dee3e26 (Build: 19-Apr-2026)"))

    resp = await _dispatch({"cmd": "nodes"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"][0]["ver"] == "v1.15.0-dee3e26 (Build: 19-Apr-2026)"


async def test_nodes_dispatch_ver_absent_when_not_collected(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Test", label="test", state=NodeState.CONFIGURED)
    await db.upsert_node(node)

    resp = await _dispatch({"cmd": "nodes"}, wq, db, config)

    assert resp["data"][0]["ver"] is None


async def test_nodes_dispatch_neighbors_absent_when_not_collected(db, wq, config):
    """nodes command returns None for neighbors when fact not yet collected."""
    node = Node(pubkey="aabbccdd" * 8, name="Test", label="test", state=NodeState.CONFIGURED)
    await db.upsert_node(node)

    resp = await _dispatch({"cmd": "nodes"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"][0]["neighbors"] is None
