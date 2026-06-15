"""Tests for daemon socket-protocol dispatch."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mch.config import Config, GroupConfig, NodeConfig, NodeSecrets, SettingConfig, load
from mch.daemon import CONFIG_ERROR_EXIT, Daemon, _dispatch, _handle_advert, main
from mch.db import Database
from mch.models import (
    DesiredSetting,
    Node,
    NodeFact,
    NodeState,
    Scope,
    SettingPolicy,
    SettingRecord,
)
from mch.queue import NodeResult, OperationBusyError


@asynccontextmanager
async def _operation_ok(*args, **kwargs):
    yield


@asynccontextmanager
async def _operation_busy(*args, **kwargs):
    raise OperationBusyError("busy: scheduled sync aabbccdd")
    yield


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
    m.start_cycle = AsyncMock(return_value="sync started")
    m.operation = MagicMock(side_effect=_operation_ok)
    m.exec_command = AsyncMock(return_value="(ok)")
    m.unfiled_contacts = AsyncMock(return_value=[])
    m.management_path_status = MagicMock(return_value={"route": "stored"})
    m.discover_management_path = AsyncMock(return_value={"path": "e260", "path_hash_mode": 1})
    m.apply_management_path = AsyncMock(return_value={"route": "stored"})
    m.discover_nodes = AsyncMock(return_value={
        "radio": {"text": "915.800/250/SF10/CR5"},
        "dwell": 5.0,
        "nodes": [],
    })
    m.adopt_node = AsyncMock(return_value={"pubkey": "8e4a0d73" + "0" * 56})
    m.probe_node = AsyncMock(return_value={"ok": True, "response": "v1.14.1", "route": "direct"})
    return m


@pytest.fixture
def config():
    return Config()


def test_main_config_load_error_exits_with_config_status(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("nodes:\n  - pubkey_prefix: e2601d59\n    group: default\n")

    with patch("sys.argv", ["mchd", "--config", str(config_path)]), \
         pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == CONFIG_ERROR_EXIT


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
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="role", value="repeater"))
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
    assert data["facts"] == {"ver": "v1.14.1", "board": "TestBoard", "role": "repeater"}


async def test_node_dispatch_passwd_known_when_in_secrets(db, wq, config):
    """passwd_known is True when secrets has an entry matching the node's pubkey."""
    node = await _seed_node_with_facts(db)
    config.secrets = {node.pubkey[:8]: NodeSecrets(password="hunter2")}

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
    config.secrets = {"default": NodeSecrets(password="fleetpassword")}

    resp = await _dispatch({"cmd": "node", "pubkey": node.pubkey}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["passwd_known"] is True


async def test_fleet_node_dispatch_includes_management_path(db, wq, config):
    node = await _seed_node_with_facts(db)
    wq.management_path_status.return_value = {
        "pubkey": node.pubkey,
        "cached_path": "stored (out_path=e260; out_path_len=1; hash-mode=1)",
        "route": "stored",
        "path_repair_needed": False,
    }

    resp = await _dispatch({"cmd": "fleet_node", "query": "test"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["management_path"]["cached_path"].startswith("stored")
    wq.management_path_status.assert_called_once_with(node)


async def test_path_dispatch_resolves_node_and_returns_cached_path_status(db, wq, config):
    node = await _seed_node_with_facts(db)
    wq.management_path_status.return_value = {
        "pubkey": node.pubkey,
        "route": "flood",
        "path_repair_needed": True,
    }

    resp = await _dispatch({"cmd": "path", "query": "test"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["route"] == "flood"
    wq.management_path_status.assert_called_once_with(node)


async def test_path_discover_dispatch_resolves_node_and_returns_candidate(db, wq, config):
    node = await _seed_node_with_facts(db)

    resp = await _dispatch(
        {"cmd": "path_discover", "query": "test", "source": "advert"},
        wq,
        db,
        config,
    )

    assert resp["ok"] is True
    assert resp["data"]["path"] == "e260"
    wq.discover_management_path.assert_awaited_once_with(node, source="advert")


async def test_discover_dispatch_runs_bounded_radio_discovery(db, wq, config):
    resp = await _dispatch(
        {"cmd": "discover", "radio": [915.8, 250, 10, 5], "dwell": 4},
        wq,
        db,
        config,
    )

    assert resp["ok"] is True
    wq.discover_nodes.assert_awaited_once_with(
        (915.8, 250.0, 10, 5),
        dwell=4.0,
        progress=None,
    )


async def test_discover_dispatch_rejects_invalid_dwell(db, wq, config):
    resp = await _dispatch(
        {"cmd": "discover", "radio": [915.8, 250, 10, 5], "dwell": 31},
        wq,
        db,
        config,
    )

    assert resp["ok"] is False
    assert "dwell" in resp["error"]
    wq.discover_nodes.assert_not_awaited()


async def test_adopt_dispatch_adds_discovered_node(db, wq, config):
    pubkey = "8e4a0d73" + "0" * 56

    resp = await _dispatch(
        {
            "cmd": "adopt",
            "pubkey": pubkey,
            "radio": [915.8, 250, 11, 5],
            "label": "faulkland",
        },
        wq,
        db,
        config,
    )

    assert resp["ok"] is True
    wq.adopt_node.assert_awaited_once_with(
        pubkey,
        radio=(915.8, 250.0, 11, 5),
        label="faulkland",
        progress=None,
    )


async def test_probe_dispatch_resolves_node_and_probes(db, wq, config):
    node = await _seed_node_with_facts(db)

    resp = await _dispatch(
        {"cmd": "probe", "query": "test", "radio": [915.8, 250, 11, 5], "advert": True},
        wq,
        db,
        config,
    )

    assert resp["ok"] is True
    assert resp["data"]["route"] == "direct"
    wq.probe_node.assert_awaited_once_with(
        node, radio=(915.8, 250.0, 11, 5), advert=True, login=False, progress=None
    )


async def test_path_apply_dispatch_resolves_node_and_applies_path(db, wq, config):
    node = await _seed_node_with_facts(db)

    resp = await _dispatch(
        {"cmd": "path_apply", "query": "test", "path": "e260:1"},
        wq,
        db,
        config,
    )

    assert resp["ok"] is True
    assert resp["data"]["route"] == "stored"
    wq.apply_management_path.assert_awaited_once_with(node, "e260:1")


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
    config.secrets = {"default": NodeSecrets(password="fleetpassword")}

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["open_nodes"] == []


async def test_status_dispatch_no_open_nodes_when_default_password(db, wq, config):
    """status open_nodes is empty when a fleet-wide default password is configured."""
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Fleet Node",
        label="fleet",
        state=NodeState.CONFIGURED,
        auth_required=False,
        auth_cached=False,
    )
    await db.upsert_node(node)
    config.secrets = {"default": NodeSecrets(password="fleetpassword")}

    resp = await _dispatch({"cmd": "status"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["open_nodes"] == []


async def test_daemon_reload_updates_existing_config_object(tmp_path):
    config_path = tmp_path / "config.yaml"
    secrets_path = tmp_path / "secrets.yaml"
    config_path.write_text(
        "secrets_file: secrets.yaml\n"
        "nodes:\n"
        "  - pubkey_prefix: aabbccdd\n"
        "    label: old\n"
    )
    secrets_path.write_text("nodes:\n  aabbccdd: oldpass\n")
    secrets_path.chmod(0o600)

    config = load(config_path)
    daemon = Daemon(config)
    daemon._wq = MagicMock()
    daemon._wq.refresh_config_state = AsyncMock()
    daemon._messenger = MagicMock()

    config_path.write_text(
        "secrets_file: secrets.yaml\n"
        "messenger:\n"
        "  allowed_prefixes: [aabbccdd]\n"
        "nodes:\n"
        "  - pubkey_prefix: aabbccdd\n"
        "    label: new\n"
    )
    secrets_path.write_text("nodes:\n  aabbccdd: newpass\n")

    await daemon._reload_config("test")

    assert daemon._config is config
    assert config.nodes[0].label == "new"
    assert config.secrets["aabbccdd"].password == "newpass"
    daemon._wq.refresh_config_state.assert_awaited_once()
    daemon._messenger.update_config.assert_called_once_with(
        allowed_prefixes=["aabbccdd"],
        announce_channel=None,
    )


def test_daemon_config_snapshot_detects_equal_size_replacement_with_preserved_mtime(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("nodes:\n  - pubkey_prefix: aabbccdd\n    label: old\n")
    config = load(config_path, load_secrets=False)
    daemon = Daemon(config)
    before = daemon._snapshot_config_files()
    original_stat = config_path.stat()

    replacement = tmp_path / "replacement.yaml"
    replacement.write_text("nodes:\n  - pubkey_prefix: aabbccdd\n    label: new\n")
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    replacement.replace(config_path)

    assert config_path.stat().st_size == original_stat.st_size
    assert config_path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert daemon._snapshot_config_files() != before


async def test_daemon_shutdown_waits_for_work_queue_cancellation_before_closing_db():
    daemon = Daemon(Config())
    events: list[str] = []
    started = asyncio.Event()

    async def running_work() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("work cancelled")
            raise

    task = asyncio.create_task(running_work())
    daemon._work_queue_task = task
    await started.wait()

    async def stop_queue() -> None:
        events.append("stop")
        task.cancel()

    daemon._wq = MagicMock()
    daemon._wq.stop = AsyncMock(side_effect=stop_queue)
    daemon._db = MagicMock()

    async def close_db() -> None:
        events.append("db closed")

    daemon._db.close = AsyncMock(side_effect=close_db)

    await daemon._shutdown()

    assert events == ["stop", "work cancelled", "db closed"]
    assert daemon._shutdown_complete.is_set()


async def test_node_dispatch_full_pubkey_returns_facts(db, wq, config):
    """node command with full pubkey should also return facts."""
    node = await _seed_node_with_facts(db)

    resp = await _dispatch({"cmd": "node", "pubkey": node.pubkey}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["facts"] == {"ver": "v1.14.1", "board": "TestBoard", "role": "repeater"}


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
    node1 = Node(
        pubkey="aabbccdd" * 8, name="Wip Node A", label="wip 01", state=NodeState.CONFIGURED
    )
    node2 = Node(
        pubkey="11223344" * 8, name="Wip Node B", label="wip 02", state=NodeState.CONFIGURED
    )
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


async def test_suggest_dispatch_label_match(db, wq, config):
    """suggest command should match by label when pubkey prefix lookup fails."""
    node = Node(
        pubkey="aabbccdd" * 8, name="Tower Node", label="QH Test", state=NodeState.CONFIGURED
    )
    await db.upsert_node(node)
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="neighbors", value="5"))

    resp = await _dispatch({"cmd": "suggest", "pubkey": "QH Test"}, wq, db, config)
    assert resp["ok"] is True
    assert len(resp["data"]) == 1
    assert resp["data"][0]["label"] == "QH Test"


async def test_suggest_dispatch_all_nodes_when_no_pubkey(db, wq, config):
    """suggest with no pubkey returns all nodes."""
    node1 = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    node2 = Node(pubkey="11223344" * 8, name="Valley", label="site-b", state=NodeState.CONFIGURED)
    await db.upsert_node(node1)
    await db.upsert_node(node2)

    resp = await _dispatch({"cmd": "suggest"}, wq, db, config)
    assert resp["ok"] is True
    assert len(resp["data"]) == 2


async def test_suggest_dispatch_includes_profile_when_neighbors_known(db, wq, config):
    """suggest assigns a profile when neighbors fact is present."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="neighbors", value="25"))

    resp = await _dispatch({"cmd": "suggest"}, wq, db, config)
    assert resp["ok"] is True
    item = resp["data"][0]
    assert item["neighbour_count"] == 25
    assert item["profile"] is not None
    assert item["profile"]["name"] is not None  # e.g. "tower"


async def test_suggest_dispatch_profile_none_when_no_neighbors(db, wq, config):
    """suggest neighbour_count is None when fact not yet collected."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)

    resp = await _dispatch({"cmd": "suggest"}, wq, db, config)
    assert resp["ok"] is True
    assert resp["data"][0]["neighbour_count"] is None


async def test_prune_dispatch_lists_nodes_outside_config(db, wq, config):
    """prune dry-run lists DB nodes whose pubkeys are no longer in config."""
    kept = Node(pubkey="aabbccdd" * 8, name="Kept", label="kept", state=NodeState.CONFIGURED)
    retired = Node(
        pubkey="11223344" * 8,
        name="Retired",
        label="old-site",
        state=NodeState.CONFIGURED,
    )
    await db.upsert_node(kept)
    await db.upsert_node(retired)
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd", label="kept")]

    resp = await _dispatch({"cmd": "prune"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["deleted"] == 0
    assert [n["pubkey"] for n in resp["data"]["candidates"]] == [retired.pubkey]
    assert await db.get_node(retired.pubkey) is not None


async def test_prune_dispatch_deletes_nodes_outside_config(db, wq, config):
    """prune with dry_run false deletes retired local DB state only."""
    kept = Node(pubkey="aabbccdd" * 8, name="Kept", label="kept", state=NodeState.CONFIGURED)
    retired = Node(
        pubkey="11223344" * 8,
        name="Retired",
        label="old-site",
        state=NodeState.CONFIGURED,
    )
    await db.upsert_node(kept)
    await db.upsert_node(retired)
    await db.upsert_fact(NodeFact(node_pubkey=retired.pubkey, key="ver", value="v1.14.1"))
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd", label="kept")]

    resp = await _dispatch({"cmd": "prune", "dry_run": False}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["deleted"] == 1
    assert await db.get_node(kept.pubkey) is not None
    assert await db.get_node(retired.pubkey) is None
    assert await db.get_facts(retired.pubkey) == {}


# ------------------------------------------------------------------
# remove command
# ------------------------------------------------------------------

async def test_remove_dispatch_dry_run_resolves_one_node(db, wq, config):
    """remove dry-run resolves one DB node without deleting it."""
    node = Node(
        pubkey="11223344" * 8,
        name="Retired",
        label="old-site",
        state=NodeState.CONFIGURED,
    )
    await db.upsert_node(node)
    config.nodes = []

    resp = await _dispatch({"cmd": "remove", "query": "old-site"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["pubkey"] == node.pubkey
    assert resp["data"]["deleted"] == 0
    assert await db.get_node(node.pubkey) is not None


async def test_remove_dispatch_deletes_one_retired_node(db, wq, config):
    """remove with dry_run false deletes one node's local DB state."""
    kept = Node(pubkey="aabbccdd" * 8, name="Kept", label="kept", state=NodeState.CONFIGURED)
    retired = Node(
        pubkey="11223344" * 8,
        name="Retired",
        label="old-site",
        state=NodeState.CONFIGURED,
    )
    await db.upsert_node(kept)
    await db.upsert_node(retired)
    await db.upsert_fact(NodeFact(node_pubkey=retired.pubkey, key="ver", value="v1.14.1"))
    config.nodes = [NodeConfig(pubkey_prefix=kept.pubkey[:8], label="kept")]

    resp = await _dispatch(
        {"cmd": "remove", "query": "old-site", "dry_run": False},
        wq,
        db,
        config,
    )

    assert resp["ok"] is True
    assert resp["data"]["deleted"] == 1
    assert await db.get_node(kept.pubkey) is not None
    assert await db.get_node(retired.pubkey) is None
    assert await db.get_facts(retired.pubkey) == {}


async def test_remove_dispatch_refuses_configured_node_without_force(db, wq, config):
    """remove refuses nodes still present in config unless force is supplied."""
    node = Node(pubkey="aabbccdd" * 8, name="Kept", label="kept", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    config.nodes = [NodeConfig(pubkey_prefix=node.pubkey[:8], label="kept")]

    resp = await _dispatch(
        {"cmd": "remove", "query": "kept", "dry_run": False},
        wq,
        db,
        config,
    )

    assert resp["ok"] is False
    assert "still in config.yaml" in resp["error"]
    assert await db.get_node(node.pubkey) is not None


async def test_remove_dispatch_force_deletes_configured_node(db, wq, config):
    """remove --force allows deleting local state for a configured node."""
    node = Node(pubkey="aabbccdd" * 8, name="Kept", label="kept", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    config.nodes = [NodeConfig(pubkey_prefix=node.pubkey[:8], label="kept")]

    resp = await _dispatch(
        {"cmd": "remove", "query": "kept", "dry_run": False, "force": True},
        wq,
        db,
        config,
    )

    assert resp["ok"] is True
    assert resp["data"]["configured"] is True
    assert await db.get_node(node.pubkey) is None


# ------------------------------------------------------------------
# sync command
# ------------------------------------------------------------------

async def test_sync_dispatch_no_prefix_starts_background_cycle(db, wq, config):
    """sync without a prefix starts a background cycle and returns immediately."""
    resp = await _dispatch({"cmd": "sync"}, wq, db, config)
    assert resp["ok"] is True
    assert "sync started" in resp["data"]
    wq.start_cycle.assert_called_once()
    wq.run_cycle.assert_not_called()


async def test_sync_dispatch_no_prefix_busy_returns_error(db, wq, config):
    """sync without a prefix returns busy when another gateway operation owns the queue."""
    wq.start_cycle = AsyncMock(side_effect=OperationBusyError("busy: scheduled sync aabbccdd"))

    resp = await _dispatch({"cmd": "sync"}, wq, db, config)

    assert resp["ok"] is False
    assert "busy" in resp["error"]


async def test_sync_dispatch_with_prefix_runs_node(db, wq, config):
    """sync with a pubkey prefix resolves via DB and calls run_node with the full pubkey."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    wq.run_node = AsyncMock(return_value=NodeResult(pubkey=node.pubkey, name=node.name))

    resp = await _dispatch({"cmd": "sync", "prefix": "aabbcc"}, wq, db, config)

    assert resp["ok"] is True
    assert "Tower" in resp["data"]
    wq.run_node.assert_awaited_once_with("aabbccdd" * 8)


async def test_sync_dispatch_streams_node_progress_by_default(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", state=NodeState.CONFIGURED)
    await db.upsert_node(node)

    async def run_node(pubkey, *, progress=None):
        assert pubkey == node.pubkey
        assert progress is not None
        await progress("reading fact: ver")
        return NodeResult(pubkey=node.pubkey, name=node.name)

    wq.run_node = AsyncMock(side_effect=run_node)
    writer = MagicMock()
    writer.drain = AsyncMock()

    resp = await _dispatch(
        {"cmd": "sync", "prefix": "aabbcc"},
        wq,
        db,
        config,
        writer,
    )

    assert resp["ok"] is True
    writer.write.assert_called_once_with(
        b'{"type": "progress", "msg": "reading fact: ver"}\n'
    )


async def test_sync_dispatch_disconnect_cancels_blocked_node_sync(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run_node(pubkey, *, progress=None):
        assert progress is not None
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    wq.run_node = AsyncMock(side_effect=run_node)
    writer = MagicMock()
    writer.drain = AsyncMock()
    reader = MagicMock()

    async def disconnect_after_start():
        await started.wait()
        return b""

    reader.read = AsyncMock(side_effect=disconnect_after_start)

    resp = await _dispatch({"cmd": "sync", "prefix": "aabbcc"}, wq, db, config, writer, reader)

    assert resp == {"ok": False, "error": "sync client disconnected"}
    assert cancelled.is_set()


async def test_sync_dispatch_reports_partial_result(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", state=NodeState.PARTIAL)
    await db.upsert_node(node)
    wq.run_node = AsyncMock(return_value=NodeResult(
        pubkey=node.pubkey,
        name=node.name,
        enforced_settings_drifted=1,
        enforced_settings_failed=2,
        operations_failed=1,
    ))

    resp = await _dispatch({"cmd": "sync", "prefix": "aabbcc"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"] == (
        "sync completed for 'Tower' (aabbccdd) "
        "(partial: 1 enforced setting(s) still drifted; 2 enforced setting read(s) failed; "
        "1 operation(s) failed)"
    )


async def test_sync_dispatch_reports_failed_result(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", state=NodeState.OFFLINE)
    await db.upsert_node(node)
    wq.run_node = AsyncMock(return_value=NodeResult(
        pubkey=node.pubkey, name=node.name, error="login rejected"
    ))

    resp = await _dispatch({"cmd": "sync", "prefix": "aabbcc"}, wq, db, config)

    assert resp == {"ok": False, "error": "sync failed for 'Tower' (aabbccdd): login rejected"}


async def test_sync_dispatch_reports_skipped_result(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", state=NodeState.OFFLINE)
    await db.upsert_node(node)
    wq.run_node = AsyncMock(return_value=NodeResult(
        pubkey=node.pubkey, name=node.name, skipped_reason="unreachable over flood"
    ))

    resp = await _dispatch({"cmd": "sync", "prefix": "aabbcc"}, wq, db, config)

    assert resp == {
        "ok": True,
        "data": "sync skipped for 'Tower' (aabbccdd): unreachable over flood",
    }


async def test_sync_dispatch_with_prefix_busy_returns_error(db, wq, config):
    """forced node sync returns busy when another gateway operation is active."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    wq.run_node = AsyncMock(side_effect=OperationBusyError("busy: commission"))

    resp = await _dispatch({"cmd": "sync", "prefix": "aabbcc"}, wq, db, config)

    assert resp["ok"] is False
    assert "busy" in resp["error"]


async def test_sync_dispatch_unknown_query_returns_error(db, wq, config):
    """sync with a query that matches no node returns an error without calling run_node."""
    resp = await _dispatch({"cmd": "sync", "prefix": "zzzzzz"}, wq, db, config)

    assert resp["ok"] is False
    assert "zzzzzz" in resp["error"]
    wq.run_node.assert_not_called()


async def test_sync_dispatch_label_substring_resolves_to_full_pubkey(db, wq, config):
    """sync accepts a label substring (e.g. 'corb') and resolves it to the full pubkey."""
    node = Node(pubkey="e2601d59" * 8, name="Quakers Hill Corb", label="qh-corb",
                state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    wq.run_node = AsyncMock(return_value=NodeResult(pubkey=node.pubkey, name=node.name))

    resp = await _dispatch({"cmd": "sync", "prefix": "corb"}, wq, db, config)

    assert resp["ok"] is True
    wq.run_node.assert_awaited_once_with("e2601d59" * 8)


async def test_sync_dispatch_exact_label_resolves_to_full_pubkey(db, wq, config):
    """sync accepts an exact label (e.g. 'qh-corb') and resolves it to the full pubkey."""
    node = Node(pubkey="e2601d59" * 8, name="Quakers Hill Corb", label="qh-corb",
                state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    wq.run_node = AsyncMock(return_value=NodeResult(pubkey=node.pubkey, name=node.name))

    resp = await _dispatch({"cmd": "sync", "prefix": "qh-corb"}, wq, db, config)

    assert resp["ok"] is True
    wq.run_node.assert_awaited_once_with("e2601d59" * 8)


async def test_sync_dispatch_name_substring_resolves_to_full_pubkey(db, wq, config):
    """sync accepts a mesh name substring and resolves it to the full pubkey."""
    node = Node(pubkey="e2601d59" * 8, name="Quakers Hill Corb", label="qh-corb",
                state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    wq.run_node = AsyncMock(return_value=NodeResult(pubkey=node.pubkey, name=node.name))

    resp = await _dispatch({"cmd": "sync", "prefix": "quakers"}, wq, db, config)

    assert resp["ok"] is True
    wq.run_node.assert_awaited_once_with("e2601d59" * 8)


# ------------------------------------------------------------------
# contacts command
# ------------------------------------------------------------------

async def test_contacts_dispatch_returns_unfiled_rep_contacts(db, wq, config):
    """contacts returns REP nodes visible to the gateway but not in config."""
    rep_contact = {
        "public_key": "aabbccdd11223344" + "aa" * 24,
        "adv_name": "Millbrook",
        "type": 2,  # CONTACT_TYPE_REPEATER
    }
    wq.unfiled_contacts = AsyncMock(return_value=[rep_contact])

    resp = await _dispatch({"cmd": "contacts"}, wq, db, config)

    assert resp["ok"] is True
    assert len(resp["data"]) == 1
    assert resp["data"][0]["adv_name"] == "Millbrook"
    wq.unfiled_contacts.assert_awaited_once()


async def test_contacts_dispatch_excludes_already_managed(db, wq, config):
    """contacts excludes nodes whose pubkey_prefix is already in config."""
    wq.unfiled_contacts = AsyncMock(return_value=[])

    resp = await _dispatch({"cmd": "contacts"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"] == []
    wq.unfiled_contacts.assert_awaited_once()


# ------------------------------------------------------------------
# logs command — basic cases
# ------------------------------------------------------------------

async def test_logs_dispatch_all_logs_when_no_pubkey(db, wq, config):
    """logs with no pubkey returns all log entries."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.write_log(node.pubkey, "INFO", "synced ok")
    await db.write_log(None, "INFO", "daemon started")

    resp = await _dispatch({"cmd": "logs"}, wq, db, config)
    assert resp["ok"] is True
    assert len(resp["data"]) == 2
    node_entry = next(entry for entry in resp["data"] if entry.get("node_pubkey"))
    assert node_entry["label"] == "site-a"


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


async def test_clean_facts_dispatch_lists_stale_facts_without_deleting(db, wq, config):
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Tower",
        label="site-a",
        group="default",
        state=NodeState.CONFIGURED,
    )
    await db.upsert_node(node)
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="ver", value="v1.14.1"))
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="powersavings", value="on"))

    resp = await _dispatch({"cmd": "clean_facts", "query": "site-a"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["deleted"] == 0
    assert [item["key"] for item in resp["data"]["candidates"]] == ["powersavings"]
    assert "powersavings" in await db.get_facts(node.pubkey)


async def test_clean_facts_dispatch_deletes_scoped_fact(db, wq, config):
    node_a = Node(pubkey="aabbccdd" * 8, name="A", label="a", group="default")
    node_b = Node(pubkey="11223344" * 8, name="B", label="b", group="other")
    await db.upsert_node(node_a)
    await db.upsert_node(node_b)
    await db.upsert_fact(NodeFact(node_pubkey=node_a.pubkey, key="powersavings", value="on"))
    await db.upsert_fact(NodeFact(node_pubkey=node_b.pubkey, key="powersavings", value="on"))

    resp = await _dispatch(
        {
            "cmd": "clean_facts",
            "group": "default",
            "fact": "powersavings",
            "dry_run": False,
        },
        wq,
        db,
        config,
    )

    assert resp["ok"] is True
    assert resp["data"]["deleted"] == 1
    assert "powersavings" not in await db.get_facts(node_a.pubkey)
    assert "powersavings" in await db.get_facts(node_b.pubkey)


# ------------------------------------------------------------------
# status command
# ------------------------------------------------------------------

async def test_status_dispatch_returns_node_list_and_issue_arrays(db, wq, config):
    """status returns node list plus issue arrays."""
    node = Node(
        pubkey="aabbccdd" * 8, name="Tower", label="site-a",
        state=NodeState.CONFIGURED, auth_cached=True
    )
    await db.upsert_node(node)
    config.nodes = [NodeConfig(pubkey_prefix=node.pubkey[:8], label="site-a")]

    resp = await _dispatch({"cmd": "status"}, wq, db, config)

    assert resp["ok"] is True
    data = resp["data"]
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["pubkey"] == node.pubkey
    assert data["nodes"][0]["state"] == "configured"
    assert data["drifted"] == []
    assert data["bad_state"] == []
    assert data["ghosts"] == []
    assert data["unseen_nodes"] == []
    assert data["open_nodes"] == []


async def test_status_dispatch_returns_drifted_array(db, wq, config):
    """status drifted array includes settings that have drifted from desired."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    setting = SettingRecord(
        node_pubkey=node.pubkey,
        key="owner.info",
        desired="Managed by mch",
        observed="something else",
        policy=SettingPolicy.ENFORCE,
        update_attempts=4,
        drift=True,
    )
    await db.upsert_setting(setting)

    resp = await _dispatch({"cmd": "status"}, wq, db, config)

    assert len(resp["data"]["drifted"]) == 1
    assert resp["data"]["drifted"][0]["update_attempts"] == 4


async def test_node_dispatch_resets_displayed_attempts_for_new_desired_value(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_setting(SettingRecord(
        node_pubkey=node.pubkey,
        key="owner.info",
        desired="Old",
        observed="old observed",
        policy=SettingPolicy.ENFORCE,
        update_attempts=4,
    ))
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="owner.info", desired="New",
        policy=SettingPolicy.ENFORCE,
    ))
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd", label="site-a")]

    resp = await _dispatch({"cmd": "node", "pubkey": node.pubkey}, wq, db, config)

    assert resp["data"]["settings"][0]["update_attempts"] == 0


async def test_node_dispatch_hides_prior_attempts_when_policy_changes_to_audit(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_setting(SettingRecord(
        node_pubkey=node.pubkey,
        key="owner.info",
        desired="Managed",
        observed="wrong",
        policy=SettingPolicy.ENFORCE,
        update_attempts=4,
        drift=True,
    ))
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="owner.info", desired="Managed",
        policy=SettingPolicy.AUDIT,
    ))
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd", label="site-a")]

    resp = await _dispatch({"cmd": "node", "pubkey": node.pubkey}, wq, db, config)

    assert resp["data"]["settings"][0]["update_attempts"] == 0


async def test_plan_dispatch_lists_enforced_writes_from_known_state(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_setting(SettingRecord(
        node_pubkey=node.pubkey,
        key="name",
        desired="Old",
        observed="Old",
        policy=SettingPolicy.AUDIT,
    ))
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="name", desired="New",
        policy=SettingPolicy.ENFORCE,
    ))
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd", label="site-a")]

    resp = await _dispatch({"cmd": "plan"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["writes"] == [{
        "pubkey": node.pubkey,
        "label": "site-a",
        "mesh_name": "Tower",
        "key": "name",
        "desired": "New",
        "known": "Old",
        "policy": "enforce",
        "command": "set name New",
    }]
    assert resp["data"]["audits"] == []
    assert resp["data"]["unknown"] == []


async def test_plan_dispatch_uses_raw_meshcore_command_display(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_setting(SettingRecord(
        node_pubkey=node.pubkey,
        key="owner.info",
        desired="Old",
        observed="Old",
        policy=SettingPolicy.AUDIT,
    ))
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="owner.info", desired="🦷 rdm",
        policy=SettingPolicy.ENFORCE,
    ))
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd", label="site-a")]

    resp = await _dispatch({"cmd": "plan"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["writes"][0]["command"] == "set owner.info 🦷 rdm"


async def test_plan_dispatch_skips_close_enough_radio_roundoff(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_setting(SettingRecord(
        node_pubkey=node.pubkey,
        key="radio",
        desired="915.075,125,9,5",
        observed="915.0750122,125,9,5",
        policy=SettingPolicy.ENFORCE,
    ))
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="radio", desired="915.075,125,9,5",
        policy=SettingPolicy.ENFORCE,
    ))
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd", label="site-a")]

    resp = await _dispatch({"cmd": "plan"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["writes"] == []
    assert resp["data"]["audits"] == []
    assert resp["data"]["unknown"] == []


async def test_plan_dispatch_audit_default_does_not_write(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.upsert_setting(SettingRecord(
        node_pubkey=node.pubkey,
        key="owner.info",
        desired="Old",
        observed="Old",
        policy=SettingPolicy.AUDIT,
    ))
    # desired with no explicit policy -> inherits the default (audit)
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="owner.info", desired="New",
    ))
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd", label="site-a")]

    resp = await _dispatch({"cmd": "plan"}, wq, db, config)

    assert resp["data"]["writes"] == []
    assert resp["data"]["audits"][0]["key"] == "owner.info"
    assert resp["data"]["audits"][0]["policy"] == "audit"


async def test_plan_dispatch_unknown_when_no_known_setting(db, wq, config):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="name", desired="New",
        policy=SettingPolicy.ENFORCE,
    ))
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd", label="site-a")]

    resp = await _dispatch({"cmd": "plan"}, wq, db, config)

    assert resp["data"]["writes"] == []
    assert resp["data"]["unknown"][0]["key"] == "name"
    assert resp["data"]["unknown"][0]["reason"] == "not observed yet"


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
    node = Node(
        pubkey="aabbccdd" * 8, name="Tower", label="site-a",
        state=NodeState.CONFIGURED, auth_cached=True
    )
    await db.upsert_node(node)
    config.nodes = [NodeConfig(pubkey_prefix=node.pubkey[:8], label="site-a")]

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["drifted"] == []
    assert resp["data"]["bad_state"] == []
    assert resp["data"]["ghosts"] == []
    assert resp["data"]["unseen_nodes"] == []
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


async def test_issues_dispatch_returns_unseen_config_nodes(db, wq, config):
    """issues unseen_nodes section lists config entries with no matching DB node."""
    from mch.config import NodeConfig
    config.nodes = [NodeConfig(pubkey_prefix="deadbeef", label="wrong")]
    # DB is empty — no node with that prefix exists

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["data"]["ghosts"] == []
    assert len(resp["data"]["unseen_nodes"]) == 1
    assert resp["data"]["unseen_nodes"][0]["pubkey_prefix"] == "deadbeef"
    assert resp["data"]["unseen_nodes"][0]["label"] == "wrong"


async def test_issues_dispatch_returns_db_only_nodes_as_ghosts(db, wq, config):
    """issues ghosts section lists DB nodes no longer present in config."""
    node = Node(
        pubkey="11223344" * 8,
        name="Retired",
        label="old-site",
        state=NodeState.CONFIGURED,
    )
    await db.upsert_node(node)
    config.nodes = []

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["data"]["ghosts"] == [{
        "pubkey": node.pubkey,
        "label": "old-site",
        "name": "Retired",
        "state": "configured",
        "last_seen": None,
    }]
    assert resp["data"]["unseen_nodes"] == []


async def test_dispatch_unknown_command_returns_error(db, wq, config):
    resp = await _dispatch({"cmd": "nonexistent"}, wq, db, config)
    assert resp["ok"] is False
    assert "unknown command" in resp["error"]


# ------------------------------------------------------------------
# exec command
# ------------------------------------------------------------------

async def test_exec_dispatch_blocked_command_returns_error(db, wq, config):
    """exec with a blocked command (erase) returns an error without touching the gateway."""
    resp = await _dispatch(
        {"cmd": "exec", "pubkey": "aabbccdd", "command": "erase"}, wq, db, config
    )
    assert resp["ok"] is False
    assert "blocked" in resp["error"]


@pytest.mark.parametrize(
    "command",
    [
        "set prv.key deadbeef",
        "set guest.password guest",
        "start ota",
        "  SET   PRV.KEY deadbeef  ",
    ],
)
async def test_exec_dispatch_multi_word_blocked_command_returns_error(command, db, wq, config):
    """exec blocks dangerous multi-word commands before node/contact lookup."""
    resp = await _dispatch(
        {"cmd": "exec", "pubkey": "aabbccdd", "command": command}, wq, db, config
    )
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
    wq.exec_command = AsyncMock(side_effect=ValueError("no gateway contact for aabbccddaabb"))

    resp = await _dispatch({"cmd": "exec", "pubkey": "aabbccdd", "command": "ver"}, wq, db, config)

    assert resp["ok"] is False
    assert "contact" in resp["error"]
    wq.exec_command.assert_awaited_once_with(node.pubkey, "ver")


async def test_exec_dispatch_free_command_returns_text(db, wq, config):
    """exec with a free command returns the command's text response."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    wq.exec_command = AsyncMock(return_value="v1.14.1")

    resp = await _dispatch({"cmd": "exec", "pubkey": "aabbccdd", "command": "ver"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"] == "v1.14.1"
    wq.exec_command.assert_awaited_once_with(node.pubkey, "ver")


async def test_exec_dispatch_busy_returns_error(db, wq, config):
    """exec returns busy instead of touching the gateway during another operation."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    wq.exec_command = AsyncMock(side_effect=OperationBusyError("busy: scheduled sync aabbccdd"))

    resp = await _dispatch({"cmd": "exec", "pubkey": "aabbccdd", "command": "ver"}, wq, db, config)

    assert resp["ok"] is False
    assert "busy" in resp["error"]
    wq.exec_command.assert_awaited_once_with(node.pubkey, "ver")


async def test_exec_dispatch_fire_and_forget_timeout_returns_ok(db, wq, config):
    """exec reboot (fire-and-forget): timeout treated as success."""
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="site-a", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    wq.exec_command = AsyncMock(return_value="(sent - no response expected)")

    resp = await _dispatch(
        {"cmd": "exec", "pubkey": "aabbccdd", "command": "reboot"}, wq, db, config
    )

    assert resp["ok"] is True
    assert resp["data"] == "(sent - no response expected)"
    wq.exec_command.assert_awaited_once_with(node.pubkey, "reboot")


async def test_commission_dispatch_busy_returns_error(db, wq, config):
    """commission returns busy when another gateway operation is active."""
    wq.operation = MagicMock(side_effect=_operation_busy)

    resp = await _dispatch({"cmd": "commission", "mode": "scan"}, wq, db, config)

    assert resp["ok"] is False
    assert "busy" in resp["error"]


async def test_commission_dispatch_disconnected_progress_cancels(db, wq, config):
    """A broken progress stream cancels the commission RPC and releases operation ownership."""
    writer = MagicMock()
    writer.drain = AsyncMock(side_effect=BrokenPipeError)

    async def fake_handle(req, wq, db, config, progress=None):
        await progress("switching to factory frequency...")
        return {"ok": True, "data": {}}

    with patch("mch.commission.handle_commission", side_effect=fake_handle):
        resp = await _dispatch(
            {"cmd": "commission", "mode": "new"}, wq, db, config, writer=writer
        )

    assert resp["ok"] is False
    assert "client disconnected" in resp["error"]
    wq.operation.assert_called_once_with("commission", wait=False)
    writer.write.assert_called_once()


async def test_issues_dispatch_no_ghost_when_node_exists(db, wq, config):
    """A config node whose prefix matches a DB node is neither ghost nor unseen."""
    from mch.config import NodeConfig
    node = Node(pubkey="aabbccdd" * 8, name="N", label="n", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    config.nodes = [NodeConfig(pubkey_prefix="aabbccdd", label="n")]

    resp = await _dispatch({"cmd": "issues"}, wq, db, config)

    assert resp["data"]["ghosts"] == []
    assert resp["data"]["unseen_nodes"] == []


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
    config.secrets = {node.pubkey[:8]: NodeSecrets(password="hunter2")}

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


async def test_status_dispatch_open_nodes_in_array(db, wq, config):
    """status open_nodes array includes nodes with no ACL protection."""
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
    assert len(resp["data"]["open_nodes"]) == 1


# ------------------------------------------------------------------
# passive last_seen update from ADVERTISEMENT events
# ------------------------------------------------------------------

async def test_handle_advert_updates_last_seen_for_known_node(db):
    """Advert from a known node updates last_seen in the DB."""
    pubkey = "aabbccdd" * 8
    node = Node(pubkey=pubkey, name="Known Node", state=NodeState.CONFIGURED, last_seen=None)
    await db.upsert_node(node)

    await _handle_advert(db, pubkey)

    updated = await db.get_node(pubkey)
    assert updated is not None
    assert updated.last_seen is not None


async def test_handle_advert_ignores_unknown_node(db):
    """Advert from a node not in the DB does nothing — no insert."""
    await _handle_advert(db, "deadbeef" * 8)

    nodes = await db.get_all_nodes()
    assert nodes == []


# ------------------------------------------------------------------
# set_setting / unset_setting — DB-authored desired state
# ------------------------------------------------------------------

async def _seed_plain_node(db: Database) -> Node:
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Test Node",
        label="test",
        state=NodeState.CONFIGURED,
    )
    await db.upsert_node(node)
    return node


async def test_set_setting_writes_node_scope_row(db, wq, config):
    node = await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "txdelay", "value": "14"}, wq, db, config
    )

    assert resp["ok"] is True
    assert resp["data"]["pubkey"] == node.pubkey
    assert resp["data"]["value"] == "14"
    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert len(rows) == 1
    assert rows[0].key == "txdelay"
    assert rows[0].desired == "14"
    assert rows[0].policy is None


async def test_set_setting_resolves_pubkey_prefix(db, wq, config):
    node = await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": node.pubkey[:8], "key": "txdelay", "value": "20"},
        wq, db, config,
    )

    assert resp["ok"] is True
    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert rows[0].desired == "20"


async def test_set_setting_records_policy(db, wq, config):
    node = await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "txdelay", "value": "14", "policy": "audit"},
        wq, db, config,
    )

    assert resp["ok"] is True
    assert resp["data"]["policy"] == "audit"
    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert rows[0].policy is SettingPolicy.AUDIT


async def test_set_setting_policy_only_override(db, wq, config):
    node = await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "txdelay", "policy": "skip"},
        wq, db, config,
    )

    assert resp["ok"] is True
    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert rows[0].desired is None
    assert rows[0].policy is SettingPolicy.SKIP


async def test_set_setting_upserts_existing_key(db, wq, config):
    node = await _seed_plain_node(db)
    await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "txdelay", "value": "14"}, wq, db, config
    )

    await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "txdelay", "value": "22"}, wq, db, config
    )

    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert len(rows) == 1
    assert rows[0].desired == "22"


async def test_set_setting_rejects_unknown_key(db, wq, config):
    await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "nonsense", "value": "1"},
        wq, db, config,
    )

    assert resp["ok"] is False
    assert "not a writable setting" in resp["error"]


async def test_set_setting_rejects_invalid_value(db, wq, config):
    await _seed_plain_node(db)

    # tx has an int-range validator; a non-numeric value must be rejected
    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "txdelay", "value": "loud"},
        wq, db, config,
    )

    assert resp["ok"] is False
    assert "txdelay" in resp["error"]


async def test_set_setting_rejects_invalid_policy(db, wq, config):
    await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "txdelay", "value": "14", "policy": "maybe"},
        wq, db, config,
    )

    assert resp["ok"] is False
    assert "invalid policy" in resp["error"]


async def test_set_setting_unknown_node_errors(db, wq, config):
    resp = await _dispatch(
        {"cmd": "set_setting", "query": "ghost", "key": "txdelay", "value": "14"}, wq, db, config
    )

    assert resp["ok"] is False
    assert "not found" in resp["error"]


async def test_set_setting_requires_value_or_policy(db, wq, config):
    await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "txdelay"}, wq, db, config
    )

    assert resp["ok"] is False


async def test_set_setting_group_scope_writes_group_row(db, wq):
    config = Config(groups=[GroupConfig(name="solar")])

    resp = await _dispatch(
        {"cmd": "set_setting", "scope": "group", "group": "solar",
         "key": "txdelay", "value": "14"},
        wq, db, config,
    )

    assert resp["ok"] is True
    assert resp["data"]["scope"] == "group"
    assert resp["data"]["group"] == "solar"
    rows = await db.get_desired_settings(Scope.GROUP, "solar")
    assert rows[0].key == "txdelay"
    assert rows[0].desired == "14"
    # node-scope rows must be untouched
    assert await db.get_desired_settings(Scope.NODE, "solar") == []


async def test_set_setting_global_scope_writes_global_row(db, wq, config):
    resp = await _dispatch(
        {"cmd": "set_setting", "scope": "global", "key": "txdelay", "value": "14"},
        wq, db, config,
    )

    assert resp["ok"] is True
    assert resp["data"]["scope"] == "global"
    rows = await db.get_desired_settings(Scope.GLOBAL, "")
    assert rows[0].key == "txdelay"
    assert rows[0].desired == "14"


async def test_set_setting_accepts_name_prefix_directive(db, wq, config):
    # name_prefix is a directive (structured field), not a node CLI setting, but it is
    # operator-settable and stored as a desired_settings row like any other key.
    node = await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "name_prefix", "value": "\U0001f31e"},
        wq, db, config,
    )

    assert resp["ok"] is True
    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert rows[0].key == "name_prefix"
    assert rows[0].desired == "\U0001f31e"


async def test_set_setting_accepts_default_policy_directive(db, wq, config):
    node = await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "default_policy", "value": "enforce"},
        wq, db, config,
    )

    assert resp["ok"] is True
    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert rows[0].key == "default_policy"
    assert rows[0].desired == "enforce"


async def test_set_setting_rejects_invalid_default_policy(db, wq, config):
    await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "default_policy", "value": "loud"},
        wq, db, config,
    )

    assert resp["ok"] is False
    assert "default_policy" in resp["error"]


async def test_set_setting_accepts_via_radio_directive(db, wq, config):
    node = await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "via_radio", "value": "915.8,250,11,5"},
        wq, db, config,
    )

    assert resp["ok"] is True
    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert rows[0].key == "via_radio"
    assert rows[0].desired == "915.8,250,11,5"


async def test_set_setting_rejects_invalid_via_radio(db, wq, config):
    await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "via_radio", "value": "nonsense"},
        wq, db, config,
    )

    assert resp["ok"] is False
    assert "via_radio" in resp["error"]


async def test_set_setting_name_prefix_drops_leading_keeps_trailing_space(db, wq, config):
    node = await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "name_prefix", "value": " \U0001f31e "},
        wq, db, config,
    )

    assert resp["ok"] is True
    assert resp["data"]["value"] == "\U0001f31e "  # stored normalised, trailing kept
    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert rows[0].desired == "\U0001f31e "


async def test_set_setting_default_policy_strips_surrounding_space(db, wq, config):
    node = await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "default_policy", "value": " enforce "},
        wq, db, config,
    )

    assert resp["ok"] is True
    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert rows[0].desired == "enforce"


async def test_set_setting_name_prefix_all_spaces_stored_empty(db, wq, config):
    node = await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "name_prefix", "value": "   "},
        wq, db, config,
    )

    assert resp["ok"] is True
    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert rows[0].desired == ""  # all-spaces normalises to empty; overlay reads it as cleared


async def test_set_setting_group_scope_unknown_group_errors(db, wq, config):
    # config fixture has no groups; a typo'd group must error, not silently write a dead row
    resp = await _dispatch(
        {"cmd": "set_setting", "scope": "group", "group": "ghost",
         "key": "txdelay", "value": "14"},
        wq, db, config,
    )

    assert resp["ok"] is False
    assert "ghost" in resp["error"]


async def test_unset_setting_group_scope_removes_row(db, wq):
    config = Config(groups=[GroupConfig(name="solar")])
    await _dispatch(
        {"cmd": "set_setting", "scope": "group", "group": "solar",
         "key": "txdelay", "value": "14"}, wq, db, config,
    )

    resp = await _dispatch(
        {"cmd": "unset_setting", "scope": "group", "group": "solar", "key": "txdelay"},
        wq, db, config,
    )

    assert resp["ok"] is True
    assert await db.get_desired_settings(Scope.GROUP, "solar") == []


async def test_unset_setting_global_scope_removes_row(db, wq, config):
    await _dispatch(
        {"cmd": "set_setting", "scope": "global", "key": "txdelay", "value": "14"},
        wq, db, config,
    )

    resp = await _dispatch(
        {"cmd": "unset_setting", "scope": "global", "key": "txdelay"}, wq, db, config,
    )

    assert resp["ok"] is True
    assert await db.get_desired_settings(Scope.GLOBAL, "") == []


async def test_unset_setting_group_scope_missing_row_errors(db, wq):
    config = Config(groups=[GroupConfig(name="solar")])

    resp = await _dispatch(
        {"cmd": "unset_setting", "scope": "group", "group": "solar", "key": "txdelay"},
        wq, db, config,
    )

    assert resp["ok"] is False
    assert "txdelay" in resp["error"]


async def test_unset_setting_removes_row(db, wq, config):
    node = await _seed_plain_node(db)
    await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "txdelay", "value": "14"}, wq, db, config
    )

    resp = await _dispatch(
        {"cmd": "unset_setting", "query": "test", "key": "txdelay"}, wq, db, config
    )

    assert resp["ok"] is True
    assert await db.get_desired_settings(Scope.NODE, node.pubkey) == []


async def test_unset_setting_missing_key_errors(db, wq, config):
    await _seed_plain_node(db)

    resp = await _dispatch(
        {"cmd": "unset_setting", "query": "test", "key": "txdelay"}, wq, db, config
    )

    assert resp["ok"] is False
    assert "no DB-set" in resp["error"]


async def test_unset_setting_leaves_other_keys(db, wq, config):
    node = await _seed_plain_node(db)
    await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "txdelay", "value": "14"}, wq, db, config
    )
    await _dispatch(
        {"cmd": "set_setting", "query": "test", "key": "name", "value": "Tower"}, wq, db, config
    )

    await _dispatch({"cmd": "unset_setting", "query": "test", "key": "txdelay"}, wq, db, config)

    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert [r.key for r in rows] == ["name"]


# ------------------------------------------------------------------
# clean_settings — orphaned observed setting rows
# ------------------------------------------------------------------

async def _seed_observed_setting(db: Database, node: Node, key: str, observed: str = "x") -> None:
    await db.upsert_setting(SettingRecord(
        node_pubkey=node.pubkey,
        key=key,
        desired=None,
        policy=SettingPolicy.AUDIT,
        observed=observed,
    ))


async def test_clean_settings_lists_orphan_not_in_desired(db, wq):
    node = await _seed_plain_node(db)
    await _seed_observed_setting(db, node, "txdelay")
    config = Config(nodes=[NodeConfig(
        pubkey_prefix="aabbccdd", label="test",
        settings={"name": SettingConfig(desired="Test Node", policy=SettingPolicy.ENFORCE)},
    )])

    resp = await _dispatch({"cmd": "clean_settings", "query": "test"}, wq, db, config)

    assert resp["ok"] is True
    keys = [c["key"] for c in resp["data"]["candidates"]]
    assert keys == ["txdelay"]
    assert resp["data"]["deleted"] == 0  # dry run by default


async def test_clean_settings_keeps_db_set_key(db, wq, config):
    """A key set via `mch set` (DB overlay) must never be treated as an orphan."""
    node = await _seed_plain_node(db)
    await _seed_observed_setting(db, node, "txdelay", observed="14")
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="txdelay", desired="14",
    ))
    # node not in config.yaml at all - only the DB row keeps txdelay wanted
    resp = await _dispatch({"cmd": "clean_settings", "query": "test"}, wq, db, config)

    assert resp["data"]["candidates"] == []


async def test_clean_settings_keeps_db_global_set_key(db, wq, config):
    """A global-scope DB key keeps the observed row wanted, even for a node absent from config."""
    node = await _seed_plain_node(db)
    await _seed_observed_setting(db, node, "txdelay", observed="14")
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.GLOBAL, scope_id="", key="txdelay", desired="14",
    ))
    # config fixture is empty: only the global DB row keeps txdelay wanted
    resp = await _dispatch({"cmd": "clean_settings", "query": "test"}, wq, db, config)

    assert resp["data"]["candidates"] == []


async def test_clean_settings_keeps_db_group_set_key(db, wq, config):
    """A group-scope DB key keeps the observed row wanted for a node in that group.

    Group membership is read from the DB node row (sunset: desired state and group come
    from the DB, not config.yaml), so the group-scope key attaches to the node.
    """
    node = await _seed_plain_node(db)
    node.group = "g"
    await db.upsert_node(node)
    await _seed_observed_setting(db, node, "txdelay", observed="14")
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.GROUP, scope_id="g", key="txdelay", desired="14",
    ))
    resp = await _dispatch({"cmd": "clean_settings", "query": "test"}, wq, db, config)

    assert resp["data"]["candidates"] == []


async def test_clean_settings_yes_deletes_orphan(db, wq, config):
    node = await _seed_plain_node(db)
    await _seed_observed_setting(db, node, "txdelay")

    resp = await _dispatch(
        {"cmd": "clean_settings", "query": "test", "dry_run": False}, wq, db, config
    )

    assert resp["data"]["deleted"] == 1
    assert await db.get_settings(node.pubkey) == []


async def test_clean_settings_unconfigured_node_orphans_all(db, wq, config):
    """A node absent from config with no DB desired rows: every observed row is orphaned."""
    node = await _seed_plain_node(db)
    await _seed_observed_setting(db, node, "name")
    await _seed_observed_setting(db, node, "txdelay")

    resp = await _dispatch({"cmd": "clean_settings", "query": "test"}, wq, db, config)

    assert sorted(c["key"] for c in resp["data"]["candidates"]) == ["name", "txdelay"]


async def test_clean_settings_setting_key_filter(db, wq, config):
    node = await _seed_plain_node(db)
    await _seed_observed_setting(db, node, "name")
    await _seed_observed_setting(db, node, "txdelay")

    resp = await _dispatch(
        {"cmd": "clean_settings", "query": "test", "setting": "txdelay"}, wq, db, config
    )

    assert [c["key"] for c in resp["data"]["candidates"]] == ["txdelay"]


async def test_clean_settings_unknown_node_errors(db, wq, config):
    resp = await _dispatch({"cmd": "clean_settings", "query": "ghost"}, wq, db, config)

    assert resp["ok"] is False
    assert "not found" in resp["error"]


# ------ import ------

async def test_import_writes_desired_rows_with_yes(db, wq):
    node = await _seed_plain_node(db)  # pubkey aabbccdd*8
    config = Config(nodes=[NodeConfig(
        pubkey_prefix="aabbccdd", label="test",
        settings={"txdelay": SettingConfig(desired="14")},
    )])

    resp = await _dispatch({"cmd": "import", "dry_run": False}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["written"] == 1
    rows = await db.get_desired_settings(Scope.NODE, node.pubkey)
    assert rows[0].key == "txdelay"
    assert rows[0].desired == "14"


async def test_import_dry_run_does_not_write(db, wq):
    node = await _seed_plain_node(db)
    config = Config(nodes=[NodeConfig(
        pubkey_prefix="aabbccdd", label="test",
        settings={"txdelay": SettingConfig(desired="14")},
    )])

    resp = await _dispatch({"cmd": "import"}, wq, db, config)  # dry_run defaults True

    assert resp["ok"] is True
    assert resp["data"]["written"] == 0
    assert resp["data"]["total"] == 1
    assert resp["data"]["counts"]["node"] == 1
    assert await db.get_desired_settings(Scope.NODE, node.pubkey) == []


async def test_import_warns_unresolvable_node(db, wq):
    # node never seeded into the DB, so its prefix cannot resolve to a full pubkey
    config = Config(nodes=[NodeConfig(
        pubkey_prefix="deadbeef", label="ghost",
        settings={"txdelay": SettingConfig(desired="14")},
    )])

    resp = await _dispatch({"cmd": "import", "dry_run": False}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["written"] == 0
    assert any("ghost" in w or "deadbeef" in w for w in resp["data"]["warnings"])


# ------ export ------

async def test_export_returns_yaml_of_db_desired_state(db, wq, config):
    await db.set_desired_setting(DesiredSetting(Scope.GROUP, "solar", "txdelay", desired="20"))
    await db.set_desired_setting(
        DesiredSetting(Scope.GLOBAL, "", "owner.info", desired="rdm")
    )

    resp = await _dispatch({"cmd": "export"}, wq, db, config)

    assert resp["ok"] is True
    text = resp["data"]["yaml"]
    assert "solar" in text and "txdelay" in text and "owner.info" in text
    # the emitted YAML must parse back to the expected structure
    import yaml as _yaml
    parsed = _yaml.safe_load(text)
    assert parsed["groups"][0]["name"] == "solar"
    assert parsed["global"]["settings"]["owner.info"] == "rdm"
    assert resp["data"]["total"] == 2


async def test_export_empty_db_is_empty(db, wq, config):
    resp = await _dispatch({"cmd": "export"}, wq, db, config)

    assert resp["ok"] is True
    assert resp["data"]["total"] == 0
    assert resp["data"]["yaml"].strip() in ("", "{}")
