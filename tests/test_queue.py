"""Tests for the work queue — policy resolution and cycle logic."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcherd.config import (
    Config,
    GatewayConfig,
    GroupConfig,
    NodeConfig,
    OperationConfig,
    SchedulerConfig,
    SettingConfig,
)
from mcherd.db import Database
from mcherd.gateway import CommandResult, Gateway
from mcherd.models import (
    Node,
    NodeState,
    OperationRecord,
    OperationStatus,
    SettingPolicy,
)
from mcherd.queue import (
    WorkQueue,
    all_setting_keys,
    resolve_desired,
    resolve_policy,
)


# ---------------------------------------------------------------------------
# policy resolution
# ---------------------------------------------------------------------------

def _node(pubkey="aa", **kwargs) -> NodeConfig:
    return NodeConfig(pubkey_prefix=pubkey, **kwargs)


def _group(**kwargs) -> GroupConfig:
    return GroupConfig(name="g", **kwargs)


def test_resolve_policy_node_override_wins():
    node = _node(settings={"tx": SettingConfig(policy=SettingPolicy.AUDIT)})
    group = _group(default_policy=SettingPolicy.ENFORCE)
    assert resolve_policy("tx", node, group) == SettingPolicy.AUDIT


def test_resolve_policy_group_setting_override():
    node = _node()
    group = _group(setting_overrides={"tx": SettingConfig(policy=SettingPolicy.SKIP)})
    assert resolve_policy("tx", node, group) == SettingPolicy.SKIP


def test_resolve_policy_node_default():
    node = _node(default_policy=SettingPolicy.AUDIT)
    assert resolve_policy("tx", node, None) == SettingPolicy.AUDIT


def test_resolve_policy_group_default():
    node = _node()
    group = _group(default_policy=SettingPolicy.AUDIT)
    assert resolve_policy("tx", node, group) == SettingPolicy.AUDIT


def test_resolve_policy_global_default():
    assert resolve_policy("tx", _node(), None) == SettingPolicy.ENFORCE


def test_resolve_policy_specificity_order():
    # node setting > group override > node default > group default > global
    node = _node(
        default_policy=SettingPolicy.SKIP,
        settings={"tx": SettingConfig(policy=SettingPolicy.AUDIT)},
    )
    group = _group(
        default_policy=SettingPolicy.ENFORCE,
        setting_overrides={"tx": SettingConfig(policy=SettingPolicy.ENFORCE)},
    )
    assert resolve_policy("tx", node, group) == SettingPolicy.AUDIT


def test_resolve_desired_node_wins_over_group():
    node = _node(settings={"tx": SettingConfig(desired="high")})
    group = _group(settings={"tx": SettingConfig(desired="low")})
    assert resolve_desired("tx", node, group) == "high"


def test_resolve_desired_falls_back_to_group():
    node = _node()
    group = _group(settings={"tx": SettingConfig(desired="medium")})
    assert resolve_desired("tx", node, group) == "medium"


def test_resolve_desired_none_when_no_config():
    assert resolve_desired("tx", _node(), None) is None


def test_all_setting_keys_union():
    node = _node(settings={"a": SettingConfig(), "b": SettingConfig()})
    group = _group(
        settings={"b": SettingConfig(), "c": SettingConfig()},
        setting_overrides={"d": SettingConfig()},
    )
    assert all_setting_keys(node, group) == {"a", "b", "c", "d"}


def test_all_setting_keys_no_group():
    node = _node(settings={"x": SettingConfig()})
    assert all_setting_keys(node, None) == {"x"}


# ---------------------------------------------------------------------------
# WorkQueue cycle integration (mocked gateway + db)
# ---------------------------------------------------------------------------

def _make_config(node_cfgs=None, group_cfgs=None) -> Config:
    return Config(
        gateway=GatewayConfig(device="/dev/null"),
        scheduler=SchedulerConfig(inter_node_delay_seconds=0, sync_interval_minutes=1),
        nodes=node_cfgs or [],
        groups=group_cfgs or [],
        facts=["ver", "board"],
    )


def _make_gateway(contacts=None, cmd_text="ok") -> Gateway:
    gw = MagicMock(spec=Gateway)
    gw.get_contacts = AsyncMock(return_value=contacts or [])
    gw.login = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text=cmd_text))
    return gw


async def _make_db(tmp_path) -> Database:
    db = Database(tmp_path / "test.db")
    await db.open()
    return db


@pytest.fixture
async def db(tmp_path):
    d = await _make_db(tmp_path)
    yield d
    await d.close()


def _contact(pubkey="aabbccdd1122", name="TestNode"):
    return {"public_key": pubkey, "adv_name": name}


# ------------------------------------------------------------------
# node not in contacts → skipped
# ------------------------------------------------------------------

async def test_cycle_node_not_in_contacts(db):
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbcc", name="Missing")])
    gw = _make_gateway(contacts=[])  # no contacts
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert len(results) == 1
    assert results[0].reachable is False
    assert results[0].error == "not in gateway contacts"


# ------------------------------------------------------------------
# happy path: reachable node, facts collected
# ------------------------------------------------------------------

async def test_cycle_collects_facts(db):
    node_cfg = _node(pubkey="aabbcc", name="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg], )
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="v1.14.1"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert results[0].reachable is True
    assert results[0].facts_collected == 2  # ver + board


# ------------------------------------------------------------------
# drift detection and enforcement
# ------------------------------------------------------------------

async def test_cycle_enforces_drifted_setting(db):
    node_cfg = _node(
        pubkey="aabbcc",
        name="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []  # skip facts for this test

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    # get powersaving → returns "off" (drift); set → returns "ok"
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="off"),   # get powersaving
        CommandResult(success=True, text="ok"),    # set powersaving on
    ])
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert results[0].settings_drifted == 1
    assert results[0].settings_enforced == 1


async def test_cycle_audit_does_not_set(db):
    node_cfg = _node(
        pubkey="aabbcc",
        name="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.AUDIT)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="off"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert results[0].settings_drifted == 1
    assert results[0].settings_enforced == 0
    # set command must NOT have been called
    calls = [str(c) for c in gw.send_command.call_args_list]
    assert not any("set " in c for c in calls)


# ------------------------------------------------------------------
# login failure
# ------------------------------------------------------------------

async def test_cycle_login_failure_skips_node(db):
    node_cfg = _node(pubkey="aabbcc", name="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.secrets = {"aabbcc": "badpass"}

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.login = AsyncMock(return_value=False)
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert results[0].reachable is False
    assert results[0].error == "login failed"


# ------------------------------------------------------------------
# operations
# ------------------------------------------------------------------

async def test_cycle_runs_pending_operations(db):
    node_cfg = _node(pubkey="aabbcc", name="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    # insert a pending op directly
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))
    op = OperationRecord(node_pubkey="aabbccdd1122", command="clock sync")
    await db.upsert_operation(op)

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="synced"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert results[0].operations_run == 1
    assert results[0].operations_failed == 0
