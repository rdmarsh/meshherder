"""Tests for Messenger — DM bot and channel announcements."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mch.db import Database
from mch.gateway import Gateway
from mch.messenger import Messenger, _chunk, _find_node
from mch.models import Node, NodeState, SettingPolicy, SettingRecord


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


def _make_messenger(db, allowed=None, announce_channel=None):
    gw = MagicMock(spec=Gateway)
    gw.set_dm_callback = MagicMock()
    gw.send_dm = AsyncMock(return_value=True)
    gw.send_channel_msg = AsyncMock(return_value=True)
    m = Messenger(
        gateway=gw,
        db=db,
        allowed_prefixes=allowed or ["aabbcc"],
        announce_channel=announce_channel,
    )
    return m, gw


# ---------------------------------------------------------------------------
# _chunk
# ---------------------------------------------------------------------------

def test_chunk_short_message():
    assert _chunk("hello", 180) == ["hello"]


def test_chunk_splits_long_message():
    msg = "x" * 400
    chunks = _chunk(msg, 180)
    assert len(chunks) == 3
    assert all(len(c.encode()) <= 180 for c in chunks)
    assert "".join(chunks) == msg


def test_chunk_multibyte_safe():
    # "é" is 2 bytes in UTF-8; make sure we don't split in the middle
    msg = "é" * 100
    chunks = _chunk(msg, 10)
    for c in chunks:
        assert len(c.encode()) <= 10
    assert "".join(chunks) == msg


# ---------------------------------------------------------------------------
# _find_node
# ---------------------------------------------------------------------------

def _node(pubkey, name):
    return Node(pubkey=pubkey, name=name)


def test_find_node_exact_name():
    nodes = [_node("aabb", "Alpha"), _node("ccdd", "Beta")]
    assert _find_node(nodes, "Alpha").pubkey == "aabb"


def test_find_node_prefix_name():
    nodes = [_node("aabb", "Alpha Node"), _node("ccdd", "Beta")]
    assert _find_node(nodes, "Alpha").pubkey == "aabb"


def test_find_node_pubkey_prefix():
    nodes = [_node("aabbccdd", "Alpha"), _node("eeff0011", "Beta")]
    assert _find_node(nodes, "eeff").pubkey == "eeff0011"


def test_find_node_case_insensitive():
    nodes = [_node("aabb", "Alpha")]
    assert _find_node(nodes, "alpha").pubkey == "aabb"


def test_find_node_not_found():
    nodes = [_node("aabb", "Alpha")]
    assert _find_node(nodes, "zzz") is None


# ---------------------------------------------------------------------------
# authorisation
# ---------------------------------------------------------------------------

async def test_dm_from_unauthorised_ignored(db):
    m, gw = _make_messenger(db, allowed=["aabbcc"])
    m.start()
    # message from a different prefix
    await m._on_dm({"pubkey_prefix": "ffffff000000", "text": "status"})
    gw.send_dm.assert_not_called()


async def test_dm_from_authorised_gets_reply(db):
    m, gw = _make_messenger(db, allowed=["aabbcc"])
    m.start()
    m.register_contact("aabbcc112233", {"public_key": "aabbcc112233", "adv_name": "test"})
    await m._on_dm({"pubkey_prefix": "aabbcc112233", "text": "status"})
    gw.send_dm.assert_called_once()


async def test_prefix_partial_match_authorised(db):
    """A 12-char prefix in allowed_prefixes should match longer pubkeys."""
    m, gw = _make_messenger(db, allowed=["aabbcc"])
    m.start()
    m.register_contact("aabbcc112233", {"public_key": "aabbcc112233", "adv_name": "test"})
    await m._on_dm({"pubkey_prefix": "aabbcc112233", "text": "help"})
    gw.send_dm.assert_called()


# ---------------------------------------------------------------------------
# command responses
# ---------------------------------------------------------------------------

async def test_help_command(db):
    m, gw = _make_messenger(db)
    reply = await m._handle_command("help")
    assert "status" in reply
    assert "nodes" in reply


async def test_status_command_no_nodes(db):
    m, gw = _make_messenger(db)
    reply = await m._handle_command("status")
    assert "0 nodes" in reply


async def test_status_command_with_nodes(db):
    from mch.db import Database
    await db.upsert_node(Node(pubkey="aa", name="A", state=NodeState.CONFIGURED))
    await db.upsert_node(Node(pubkey="bb", name="B", state=NodeState.PARTIAL))
    m, gw = _make_messenger(db)
    reply = await m._handle_command("status")
    assert "2 nodes" in reply


async def test_nodes_command(db):
    await db.upsert_node(Node(pubkey="aa", name="NodeAlpha", state=NodeState.CONFIGURED))
    m, gw = _make_messenger(db)
    reply = await m._handle_command("nodes")
    assert "NodeAlpha" in reply


async def test_node_not_found(db):
    m, gw = _make_messenger(db)
    reply = await m._handle_command("node zzz")
    assert "no node" in reply.lower()


async def test_issues_command_no_issues(db):
    m, gw = _make_messenger(db)
    reply = await m._handle_command("issues")
    assert "no issues" in reply


async def test_issues_command_with_drift(db):
    await db.upsert_node(Node(pubkey="aabb", name="N"))
    await db.upsert_setting(SettingRecord(
        node_pubkey="aabb", key="powersaving",
        desired="on", policy=SettingPolicy.ENFORCE,
        observed="off", drift=True,
    ))
    m, gw = _make_messenger(db)
    reply = await m._handle_command("issues")
    assert "powersaving" in reply


async def test_unknown_command(db):
    m, gw = _make_messenger(db)
    reply = await m._handle_command("frobnicate")
    assert "unknown" in reply.lower()


async def test_cmd_command_lists_commands(db):
    """'cmd' returns a short command list fitting in a single DM (~150 chars)."""
    m, gw = _make_messenger(db)
    reply = await m._handle_command("cmd")
    assert "status" in reply
    assert "nodes" in reply
    assert len(reply.encode("utf-8")) <= 150


# ---------------------------------------------------------------------------
# channel announcements
# ---------------------------------------------------------------------------

async def test_announce_cycle_with_channel(db):
    m, gw = _make_messenger(db, announce_channel=1)
    await m.announce_cycle_complete(node_count=5, drift_count=0)
    gw.send_channel_msg.assert_called_once_with(1, "mchd: cycle done — 5 nodes")


async def test_announce_cycle_with_drift(db):
    m, gw = _make_messenger(db, announce_channel=1)
    await m.announce_cycle_complete(node_count=5, drift_count=2)
    call_text = gw.send_channel_msg.call_args[0][1]
    assert "2 drifted" in call_text


async def test_announce_no_channel(db):
    m, gw = _make_messenger(db, announce_channel=None)
    await m.announce_cycle_complete(node_count=5, drift_count=0)
    gw.send_channel_msg.assert_not_called()
