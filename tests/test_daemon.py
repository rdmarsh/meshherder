"""Tests for daemon socket-protocol dispatch."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from mch.config import Config
from mch.daemon import _dispatch
from mch.db import Database
from mch.models import Node, NodeFact, NodeState, SettingPolicy, SettingRecord


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


async def test_node_dispatch_passwd_known_when_in_secrets(db, wq, config):
    """passwd_known is True when secrets has an entry matching the node's pubkey."""
    node = await _seed_node_with_facts(db)
    config.secrets = {node.pubkey[:8]: "hunter2"}

    resp = await _dispatch({"cmd": "node", "pubkey": node.pubkey}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["passwd_known"] is True


async def test_node_dispatch_passwd_known_false_when_no_secret(db, wq, config):
    """passwd_known is False when no secrets entry matches the node's pubkey."""
    node = await _seed_node_with_facts(db)
    config.secrets = {}

    resp = await _dispatch({"cmd": "node", "pubkey": node.pubkey}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["passwd_known"] is False


async def test_node_dispatch_passwd_known_true_when_default_secret(db, wq, config):
    """passwd_known is True when a fleet-wide default password is configured."""
    node = await _seed_node_with_facts(db)
    config.secrets = {"default": "fleetpassword"}

    resp = await _dispatch({"cmd": "node", "pubkey": node.pubkey}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["passwd_known"] is True


async def test_issues_dispatch_no_open_node_when_default_password(db, wq, config):
    """issues open_nodes excludes nodes when a fleet-wide default password is configured."""
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Fleet Node",
        label="fleet",
        state=NodeState.CONFIGURED,
        auth_required=False,
        auth_cached=False,
    )
    await db.upsert_node(node)
    config.secrets = {"default": "fleetpassword"}

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["open_nodes"] == []


async def test_status_dispatch_no_open_node_count_when_default_password(db, wq, config):
    """status issues count does not include nodes covered by the default password."""
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Fleet Node",
        label="fleet",
        state=NodeState.CONFIGURED,
        auth_required=False,
        auth_cached=False,
    )
    await db.upsert_node(node)
    config.secrets = {"default": "fleetpassword"}

    resp = await _dispatch({"cmd": "status"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["issues"] == 0


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


async def test_hint_dispatch_all_nodes_when_no_pubkey(db, wq, config):
    """hint with no pubkey returns all nodes."""
    node1 = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    node2 = Node(pubkey="11223344" * 8, name="Valley", label="site-b", state=NodeState.CONFIGURED)
    await db.upsert_node(node1)
    await db.upsert_node(node2)

    resp = await _dispatch({"cmd": "hint"}, wq, db, config)
    assert resp["ok"] is True
    assert len(resp["data"]) == 2


async def test_hint_dispatch_includes_profile_when_neighbors_known(db, wq, config):
    """hint assigns a profile when neighbors fact is present."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="neighbors", value="25"))

    resp = await _dispatch({"cmd": "hint"}, wq, db, config)
    assert resp["ok"] is True
    item = resp["data"][0]
    assert item["neighbour_count"] == 25
    assert item["profile"] is not None
    assert item["profile"]["name"] is not None  # e.g. "tower"


async def test_hint_dispatch_profile_none_when_no_neighbors(db, wq, config):
    """hint neighbour_count is None when fact not yet collected."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)

    resp = await _dispatch({"cmd": "hint"}, wq, db, config)
    assert resp["ok"] is True
    assert resp["data"][0]["neighbour_count"] is None


# ------------------------------------------------------------------
# sync command
# ------------------------------------------------------------------

async def test_sync_dispatch_no_prefix_starts_background_cycle(db, wq, config):
    """sync without a prefix starts a background cycle and returns immediately."""
    resp = await _dispatch({"cmd": "sync"}, wq, db, config)
    assert resp["ok"] is True
    assert "sync started" in resp["data"]
    wq.run_cycle.assert_called_once()


async def test_sync_dispatch_with_prefix_runs_node(db, wq, config):
    """sync with a prefix runs a single node inline and returns the result."""
    from unittest.mock import AsyncMock
    from mch.models import Node as _Node
    node = Node(pubkey="aabbccdd" * 8, name="Tower", state=NodeState.CONFIGURED)
    wq.run_node = AsyncMock(return_value=node)

    resp = await _dispatch({"cmd": "sync", "prefix": "aabbcc"}, wq, db, config)

    assert resp["ok"] is True
    assert "Tower" in resp["data"]
    wq.run_node.assert_awaited_once_with("aabbcc")


async def test_sync_dispatch_bad_prefix_returns_error(db, wq, config):
    """sync with a prefix that the queue rejects returns an error."""
    from unittest.mock import AsyncMock
    wq.run_node = AsyncMock(side_effect=ValueError("no node matching prefix 'zzzzzz'"))

    resp = await _dispatch({"cmd": "sync", "prefix": "zzzzzz"}, wq, db, config)

    assert resp["ok"] is False
    assert "zzzzzz" in resp["error"]


# ------------------------------------------------------------------
# contacts command
# ------------------------------------------------------------------

async def test_contacts_dispatch_returns_unfiled_rep_contacts(db, wq, config):
    """contacts returns REP nodes visible to the gateway but not in config."""
    from unittest.mock import AsyncMock, patch
    rep_contact = {
        "public_key": "aabbccdd11223344" + "aa" * 24,
        "adv_name": "Millbrook",
        "type": 2,  # CONTACT_TYPE_REPEATER
    }
    wq._gateway = MagicMock()
    wq._gateway.get_contacts = AsyncMock(return_value=[rep_contact])

    resp = await _dispatch({"cmd": "contacts"}, wq, db, config)

    assert resp["ok"] is True
    assert len(resp["data"]) == 1
    assert resp["data"][0]["adv_name"] == "Millbrook"


async def test_contacts_dispatch_excludes_already_managed(db, wq, config):
    """contacts excludes nodes whose pubkey_prefix is already in config."""
    from mch.config import NodeConfig
    from unittest.mock import AsyncMock
    rep_contact = {
        "public_key": "aabbccdd11223344" + "aa" * 24,
        "adv_name": "Millbrook",
        "type": 2,
    }
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd")]
    wq._gateway = MagicMock()
    wq._gateway.get_contacts = AsyncMock(return_value=[rep_contact])

    resp = await _dispatch({"cmd": "contacts"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"] == []


# ------------------------------------------------------------------
# logs command — basic cases
# ------------------------------------------------------------------

async def test_logs_dispatch_all_logs_when_no_pubkey(db, wq, config):
    """logs with no pubkey returns all log entries."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.write_log(node.pubkey, "INFO", "synced ok")
    await db.write_log(None, "INFO", "daemon started")

    resp = await _dispatch({"cmd": "logs"}, wq, db, config)
    assert resp["ok"] is True
    assert len(resp["data"]) == 2


async def test_logs_dispatch_filtered_by_pubkey_prefix(db, wq, config):
    """logs with a pubkey prefix returns only that node's logs."""
    node_a = Node(pubkey="aabbccdd" * 8, name="A", state=NodeState.CONFIGURED)
    node_b = Node(pubkey="11223344" * 8, name="B", state=NodeState.CONFIGURED)
    await db.upsert_node(node_a)
    await db.upsert_node(node_b)
    await db.write_log(node_a.pubkey, "INFO", "from A")
    await db.write_log(node_b.pubkey, "INFO", "from B")

    resp = await _dispatch({"cmd": "logs", "pubkey": "aabbccdd"}, wq, db, config)
    assert resp["ok"] is True
    assert all(e["message"] == "from A" for e in resp["data"])


# ------------------------------------------------------------------
# status command
# ------------------------------------------------------------------

async def test_status_dispatch_returns_node_list_and_issue_count(db, wq, config):
    """status command returns node list and a count of issues."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED, auth_cached=True)
    await db.upsert_node(node)

    resp = await _dispatch({"cmd": "status"}, wq, db, config)

    assert resp["ok"] is True
    data = resp["data"]
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["pubkey"] == node.pubkey
    assert data["nodes"][0]["state"] == "configured"
    assert data["issues"] == 0


async def test_status_dispatch_counts_issues(db, wq, config):
    """status issues count aggregates drifted settings, bad-state nodes, and ghosts."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    setting = SettingRecord(
        node_pubkey=node.pubkey,
        key="owner.info",
        desired="Managed by mch",
        observed="something else",
        policy=SettingPolicy.ENFORCE,
        drift=True,
    )
    await db.upsert_setting(setting)

    resp = await _dispatch({"cmd": "status"}, wq, db, config)

    assert "issues" in resp["data"]
    assert resp["data"]["issues"] >= 1


async def test_status_dispatch_includes_label(db, wq, config):
    """status nodes list includes the label field."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)

    resp = await _dispatch({"cmd": "status"}, wq, db, config)

    assert resp["data"]["nodes"][0]["label"] == "site-a"


async def test_status_dispatch_includes_group_and_facts(db, wq, config):
    """status response includes group, ver, and neighbors for each node."""
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Tower",
        label="site-a",
        group="repeaters",
        state=NodeState.CONFIGURED,
    )
    await db.upsert_node(node)
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="ver", value="v1.14.1"))
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="neighbors", value="5"))

    resp = await _dispatch({"cmd": "status"}, wq, db, config)

    assert resp["ok"] is True
    row = resp["data"]["nodes"][0]
    assert row["group"] == "repeaters"
    assert row["ver"] == "v1.14.1"
    assert row["neighbors"] == "5"


# ------------------------------------------------------------------
# issues command
# ------------------------------------------------------------------

async def test_issues_dispatch_no_issues(db, wq, config):
    """issues returns empty sections when everything is clean."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED, auth_cached=True)
    await db.upsert_node(node)

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["drifted"] == []
    assert resp["data"]["bad_state"] == []
    assert resp["data"]["ghosts"] == []
    assert resp["data"]["open_nodes"] == []


async def test_issues_dispatch_returns_drifted_settings(db, wq, config):
    """issues drifted section contains settings where drift=True."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_setting(SettingRecord(
        node_pubkey=node.pubkey, key="owner.info",
        desired="Managed by mch", observed="wrong",
        policy=SettingPolicy.ENFORCE, drift=True,
    ))

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert len(resp["data"]["drifted"]) == 1
    assert resp["data"]["drifted"][0]["key"] == "owner.info"
    assert resp["data"]["drifted"][0]["label"] == "site-a"


async def test_issues_dispatch_returns_bad_state_nodes(db, wq, config):
    """issues bad_state section contains nodes not in configured/online state."""
    good = Node(pubkey="aabbccdd" * 8, name="Good", label="g", state=NodeState.CONFIGURED)
    bad = Node(pubkey="11223344" * 8, name="Bad", label="b", state=NodeState.CONFIGURING)
    await db.upsert_node(good)
    await db.upsert_node(bad)

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["data"]["bad_state"] == [{"pubkey": bad.pubkey, "label": "b",
                                           "name": "Bad", "state": "configuring",
                                           "last_seen": None}]


async def test_issues_dispatch_online_not_in_bad_state(db, wq, config):
    """online state (actively syncing) is not treated as a problem."""
    node = Node(pubkey="aabbccdd" * 8, name="N", label="n", state=NodeState.ONLINE)
    await db.upsert_node(node)

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["data"]["bad_state"] == []


async def test_issues_dispatch_returns_ghosts(db, wq, config):
    """issues ghosts section lists config entries with no matching DB node."""
    from mch.config import NodeConfig
    config.nodes = [NodeConfig(pubkey_prefix="deadbeef", label="wrong")]
    # DB is empty — no node with that prefix exists

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert len(resp["data"]["ghosts"]) == 1
    assert resp["data"]["ghosts"][0]["pubkey_prefix"] == "deadbeef"
    assert resp["data"]["ghosts"][0]["label"] == "wrong"


async def test_dispatch_unknown_command_returns_error(db, wq, config):
    resp = await _dispatch({"cmd": "nonexistent"}, wq, db, config)
    assert resp["ok"] is False
    assert "unknown command" in resp["error"]


# ------------------------------------------------------------------
# exec command
# ------------------------------------------------------------------

async def test_exec_dispatch_blocked_command_returns_error(db, wq, config):
    """exec with a blocked command (erase) returns an error without touching the gateway."""
    resp = await _dispatch({"cmd": "exec", "pubkey": "aabbccdd", "command": "erase"}, wq, db, config)
    assert resp["ok"] is False
    assert "blocked" in resp["error"]


async def test_exec_dispatch_unknown_node_returns_error(db, wq, config):
    """exec with an unknown node returns an error."""
    resp = await _dispatch({"cmd": "exec", "pubkey": "deadbeef", "command": "ver"}, wq, db, config)
    assert resp["ok"] is False
    assert "not found" in resp["error"]


async def test_exec_dispatch_no_contact_returns_error(db, wq, config):
    """exec returns error when node is in DB but not in gateway contacts."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    wq._contacts = []  # no contacts

    resp = await _dispatch({"cmd": "exec", "pubkey": "aabbccdd", "command": "ver"}, wq, db, config)

    assert resp["ok"] is False
    assert "contact" in resp["error"]


async def test_exec_dispatch_free_command_returns_text(db, wq, config):
    """exec with a free command returns the command's text response."""
    from mch.gateway import CommandResult
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    contact = {"public_key": node.pubkey, "adv_name": "Tower", "type": 2}
    wq._contacts = [contact]
    wq._gateway = MagicMock()
    wq._gateway.send_command = AsyncMock(return_value=CommandResult(success=True, text="v1.14.1"))

    resp = await _dispatch({"cmd": "exec", "pubkey": "aabbccdd", "command": "ver"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"] == "v1.14.1"


async def test_exec_dispatch_fire_and_forget_timeout_returns_ok(db, wq, config):
    """exec reboot (fire-and-forget): timeout treated as success."""
    from mch.gateway import CommandResult
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    contact = {"public_key": node.pubkey, "adv_name": "Tower", "type": 2}
    wq._contacts = [contact]
    wq._gateway = MagicMock()
    wq._gateway.send_command = AsyncMock(return_value=CommandResult(success=False, error="timeout"))

    resp = await _dispatch({"cmd": "exec", "pubkey": "aabbccdd", "command": "reboot"}, wq, db, config)

    assert resp["ok"] is True


async def test_issues_dispatch_no_ghost_when_node_exists(db, wq, config):
    """A config node whose prefix matches a DB node is not a ghost."""
    from mch.config import NodeConfig
    node = Node(pubkey="aabbccdd" * 8, name="N", label="n", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd", label="n")]

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["data"]["ghosts"] == []


# ------------------------------------------------------------------
# open-node detection
# ------------------------------------------------------------------

async def test_issues_dispatch_open_nodes_when_no_acl(db, wq, config):
    """issues open_nodes section lists nodes that synced with no ACL protection."""
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Open Node",
        label="open",
        state=NodeState.CONFIGURED,
        auth_required=False,
        auth_cached=False,
    )
    await db.upsert_node(node)
    config.secrets = {}

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["ok"] is True
    assert len(resp["data"]["open_nodes"]) == 1
    assert resp["data"]["open_nodes"][0]["pubkey"] == node.pubkey


async def test_issues_dispatch_no_open_node_when_passwd_configured(db, wq, config):
    """issues open_nodes excludes nodes that have a password in secrets."""
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Secured Node",
        label="secured",
        state=NodeState.CONFIGURED,
        auth_required=False,
        auth_cached=False,
    )
    await db.upsert_node(node)
    config.secrets = {node.pubkey[:8]: "hunter2"}

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["open_nodes"] == []


async def test_issues_dispatch_no_open_node_when_auth_cached(db, wq, config):
    """issues open_nodes excludes nodes where auth_cached=True (ACL login worked)."""
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Protected",
        label="prot",
        state=NodeState.CONFIGURED,
        auth_required=False,
        auth_cached=True,
    )
    await db.upsert_node(node)
    config.secrets = {}

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["open_nodes"] == []


async def test_issues_dispatch_no_open_node_when_auth_required(db, wq, config):
    """issues open_nodes excludes nodes where auth_required=True."""
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Protected",
        label="prot",
        state=NodeState.CONFIGURED,
        auth_required=True,
        auth_cached=False,
    )
    await db.upsert_node(node)
    config.secrets = {}

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["open_nodes"] == []


async def test_status_dispatch_counts_open_nodes_in_issues(db, wq, config):
    """status issues count includes open nodes (no ACL protection)."""
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Open Node",
        label="open",
        state=NodeState.CONFIGURED,
        auth_required=False,
        auth_cached=False,
    )
    await db.upsert_node(node)
    config.secrets = {}

    resp = await _dispatch({"cmd": "status"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["issues"] >= 1
