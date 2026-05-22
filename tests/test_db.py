"""Tests for the Database layer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mch.db import Database
from mch.models import (
    Node,
    NodeFact,
    NodeState,
    OperationRecord,
    OperationStatus,
    SettingPolicy,
    SettingRecord,
)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


# ------------------------------------------------------------------
# nodes
# ------------------------------------------------------------------

async def test_upsert_and_get_node(db):
    node = Node(pubkey="aabbcc", name="TestNode", group="test-group")
    await db.upsert_node(node)

    fetched = await db.get_node("aabbcc")
    assert fetched is not None
    assert fetched.name == "TestNode"
    assert fetched.group == "test-group"
    assert fetched.state == NodeState.UNKNOWN


async def test_upsert_node_updates_existing(db):
    node = Node(pubkey="aabbcc", name="OldName")
    await db.upsert_node(node)

    node.name = "NewName"
    node.state = NodeState.ONLINE
    await db.upsert_node(node)

    fetched = await db.get_node("aabbcc")
    assert fetched.name == "NewName"
    assert fetched.state == NodeState.ONLINE


async def test_get_node_missing_returns_none(db):
    assert await db.get_node("notexist") is None


async def test_get_all_nodes(db):
    await db.upsert_node(Node(pubkey="bb", name="B"))
    await db.upsert_node(Node(pubkey="aa", name="A"))

    nodes = await db.get_all_nodes()
    assert len(nodes) == 2
    assert nodes[0].name == "A"  # ordered by name


async def test_delete_node_removes_local_state(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    await db.upsert_setting(SettingRecord(
        node_pubkey="aabbcc",
        key="owner.info",
        desired="managed",
        policy=SettingPolicy.AUDIT,
    ))
    await db.upsert_fact(NodeFact(node_pubkey="aabbcc", key="ver", value="v1.14.1"))
    await db.upsert_operation(OperationRecord(node_pubkey="aabbcc", command="clock sync"))
    await db.write_log("aabbcc", "INFO", "test log")

    await db.delete_node("aabbcc")

    assert await db.get_node("aabbcc") is None
    assert await db.get_settings("aabbcc") == []
    assert await db.get_facts("aabbcc") == {}
    assert await db.get_pending_operations("aabbcc") == []
    assert await db.get_logs("aabbcc") == []


# ------------------------------------------------------------------
# settings
# ------------------------------------------------------------------

async def test_upsert_and_get_setting(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    setting = SettingRecord(
        node_pubkey="aabbcc",
        key="owner.info",
        desired="Alice",
        policy=SettingPolicy.AUDIT,
        poll_hours=24,
    )
    await db.upsert_setting(setting)

    settings = await db.get_settings("aabbcc")
    assert len(settings) == 1
    assert settings[0].key == "owner.info"
    assert settings[0].policy == SettingPolicy.AUDIT
    assert settings[0].desired == "Alice"


async def test_setting_drift_tracking(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    setting = SettingRecord(
        node_pubkey="aabbcc",
        key="powersaving",
        desired="on",
        policy=SettingPolicy.ENFORCE,
        observed="off",
        drift=True,
    )
    await db.upsert_setting(setting)

    drifted = await db.get_drifted_settings()
    assert len(drifted) == 1
    assert drifted[0].key == "powersaving"
    assert drifted[0].observed == "off"
    assert drifted[0].desired == "on"


# ------------------------------------------------------------------
# facts
# ------------------------------------------------------------------

async def test_upsert_and_get_fact(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    fact = NodeFact(node_pubkey="aabbcc", key="ver", value="v1.14.1")
    await db.upsert_fact(fact)

    facts = await db.get_facts("aabbcc")
    assert facts["ver"] == "v1.14.1"


async def test_fact_upsert_updates_value(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    await db.upsert_fact(NodeFact(node_pubkey="aabbcc", key="ver", value="v1.13.0"))
    await db.upsert_fact(NodeFact(node_pubkey="aabbcc", key="ver", value="v1.14.1"))

    facts = await db.get_facts("aabbcc")
    assert facts["ver"] == "v1.14.1"


# ------------------------------------------------------------------
# operations
# ------------------------------------------------------------------

async def test_upsert_and_get_pending_operations(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    op = OperationRecord(node_pubkey="aabbcc", command="clock sync", every_hours=6)
    await db.upsert_operation(op)

    pending = await db.get_pending_operations("aabbcc")
    assert len(pending) == 1
    assert pending[0].command == "clock sync"
    assert pending[0].status == OperationStatus.PENDING


# ------------------------------------------------------------------
# logs
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# next_sync_at scheduling
# ------------------------------------------------------------------

async def test_next_sync_at_persisted(db):
    t = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    node = Node(pubkey="aa", name="N", next_sync_at=t)
    await db.upsert_node(node)
    fetched = await db.get_node("aa")
    assert fetched.next_sync_at == t


async def test_set_next_sync(db):
    await db.upsert_node(Node(pubkey="aa", name="N"))
    t = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    await db.set_next_sync("aa", t)
    fetched = await db.get_node("aa")
    assert fetched.next_sync_at == t


async def test_get_due_node_returns_overdue(db):
    past   = datetime(2020, 1, 1, tzinfo=UTC)
    future = datetime(2099, 1, 1, tzinfo=UTC)
    await db.upsert_node(Node(pubkey="aa", name="A", next_sync_at=future))
    await db.upsert_node(Node(pubkey="bb", name="B", next_sync_at=past))
    node = await db.get_due_node()
    assert node is not None
    assert node.pubkey == "bb"


async def test_get_due_node_returns_none_when_all_future(db):
    future = datetime(2099, 1, 1, tzinfo=UTC)
    await db.upsert_node(Node(pubkey="aa", name="A", next_sync_at=future))
    assert await db.get_due_node() is None


async def test_get_due_node_treats_null_as_overdue(db):
    # next_sync_at=None means never scheduled — treat as immediately due
    await db.upsert_node(Node(pubkey="aa", name="A"))
    node = await db.get_due_node()
    assert node is not None
    assert node.pubkey == "aa"


# ------------------------------------------------------------------
# logs
# ------------------------------------------------------------------

async def test_write_and_get_logs(db):
    await db.write_log("aabbcc", "INFO", "connected")
    await db.write_log("aabbcc", "ERROR", "timeout")
    await db.write_log(None, "INFO", "daemon started")

    node_logs = await db.get_logs("aabbcc")
    assert len(node_logs) == 2
    assert node_logs[0]["message"] == "timeout"   # most recent first

    all_logs = await db.get_logs()
    assert len(all_logs) == 3
