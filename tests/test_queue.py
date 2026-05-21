"""Tests for the work queue — policy resolution and cycle logic."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from mch.config import (
    Config,
    GatewayConfig,
    GroupConfig,
    NodeConfig,
    NodeSecrets,
    OperationConfig,
    SchedulerConfig,
    SettingConfig,
)
from mch.db import Database
from mch.gateway import CommandResult, Gateway
from mch.models import (
    Node,
    NodeState,
    OperationRecord,
    OperationStatus,
    SettingPolicy,
)
from mch.queue import (
    WorkQueue,
    all_setting_keys,
    get_unfiled_contacts,
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
        scheduler=SchedulerConfig(inter_node_delay_seconds=0, sync_interval_days=1),
        nodes=node_cfgs or [],
        groups=group_cfgs or [],
        facts=["ver", "board"],
    )


def _make_gateway(contacts=None, cmd_text="ok") -> Gateway:
    gw = MagicMock(spec=Gateway)
    gw.get_contacts = AsyncMock(return_value=contacts or [])
    gw.login = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text=cmd_text))
    gw.hop_radio = AsyncMock(return_value=(915.800, 250, 10, 5))
    gw.restore_radio = AsyncMock()
    gw.info = {"radio_freq": 915.800, "radio_bw": 250, "radio_sf": 10, "radio_cr": 5}
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


def _contact(pubkey="aabbccdd1122", name="TestNode", type_=2, flags=0):
    # type_ 2=REP, 1=CLI
    return {"public_key": pubkey, "adv_name": name, "type": type_, "flags": flags}


# ------------------------------------------------------------------
# node not in contacts → skipped
# ------------------------------------------------------------------

async def test_cycle_node_not_in_contacts(db):
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbcc", label="Missing")])
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
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
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
        label="TestNode",
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
    assert results[0].settings_enforced == 1
    assert results[0].settings_drifted == 0  # drift was fixed — not counted as residual


async def test_cycle_audit_does_not_set(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
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
# login / auth flow
# ------------------------------------------------------------------

async def test_permission_error_login_failure_marks_offline(db):
    """When a command returns 'no permission' and re-login fails, node is marked offline."""
    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        settings={"name": SettingConfig(desired="TestNode", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {"aabbcc": NodeSecrets(password="badpass")}

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.login = AsyncMock(return_value=False)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="no permission"))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.OFFLINE


async def test_no_permission_error_means_no_login(db):
    """When commands succeed normally, login is never called regardless of secrets config."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {"aabbcc": NodeSecrets(password="hunter2")}

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()
    gw.login.assert_not_awaited()



async def test_permission_error_triggers_relogin_and_retry(db):
    """A permission-denied response on a command clears auth_cached, re-logins, and retries."""
    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        settings={"name": SettingConfig(desired="TestNode", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {"aabbcc": NodeSecrets(password="hunter2")}

    # pre-seed with auth_cached=True so initial login is skipped
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode", auth_cached=True))

    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.login = AsyncMock(return_value=True)
    # first get name → permission error; after re-login, get name → ok; set name → ok
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="no permission"),  # get name — permission denied
        CommandResult(success=True, text="TestNode"),       # get name — retry after re-login
        CommandResult(success=True, text="ok"),             # set name (if needed)
    ])
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    # re-login must have been called
    gw.login.assert_awaited_once()
    # node should be reachable and processed
    assert results[0].reachable is True


async def test_permission_error_relogin_fails_marks_node_offline(db):
    """If re-login after a permission error also fails, the node is marked offline."""
    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        settings={"name": SettingConfig(desired="TestNode", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {"aabbcc": NodeSecrets(password="hunter2")}

    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode", auth_cached=True))

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.login = AsyncMock(return_value=False)  # re-login fails
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="no permission"))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()
    gw.login.assert_awaited_once()
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.OFFLINE
    assert node.auth_cached is False  # cleared so next cycle will try again


# ------------------------------------------------------------------
# operations
# ------------------------------------------------------------------

async def test_config_operations_seeded_on_first_run(db):
    """Operations in config are queued as pending when they have never run."""
    op_cfg = OperationConfig(command="clock sync", every_hours=24)
    node_cfg = _node(pubkey="aabbcc", label="TestNode", operations=[op_cfg])
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert results[0].operations_run == 1


async def test_config_operations_not_reseeded_if_recent(db):
    """Operations that ran within every_hours are not re-queued."""
    op_cfg = OperationConfig(command="clock sync", every_hours=24)
    node_cfg = _node(pubkey="aabbcc", label="TestNode", operations=[op_cfg])
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))
    # insert a successful run 1 hour ago — well within the 24h window
    op = OperationRecord(
        node_pubkey="aabbccdd1122", command="clock sync", every_hours=24,
        status=OperationStatus.SUCCESS, run_at=datetime.now(UTC) - timedelta(hours=1),
    )
    await db.upsert_operation(op)
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert results[0].operations_run == 0  # not due yet


async def test_config_operations_reseeded_when_due(db):
    """Operations that ran longer than every_hours ago are re-queued."""
    op_cfg = OperationConfig(command="clock sync", every_hours=24)
    node_cfg = _node(pubkey="aabbcc", label="TestNode", operations=[op_cfg])
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))
    # insert a successful run 25 hours ago — past the 24h window
    op = OperationRecord(
        node_pubkey="aabbccdd1122", command="clock sync", every_hours=24,
        status=OperationStatus.SUCCESS, run_at=datetime.now(UTC) - timedelta(hours=25),
    )
    await db.upsert_operation(op)
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert results[0].operations_run == 1


async def test_operation_row_updated_not_duplicated(db):
    """Running a pending op updates the original row in-place — no duplicate INSERT."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))

    # Insert a PENDING operation directly (simulates what _seed_operations does)
    op = OperationRecord(node_pubkey="aabbccdd1122", command="clock sync")
    await db.upsert_operation(op)

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="synced"))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()

    # The original PENDING row must have been updated to SUCCESS — no new row inserted
    pending = await db.get_pending_operations("aabbccdd1122")
    assert pending == [], f"original PENDING row was never updated — still in queue: {pending}"

    last = await db.get_last_operation("aabbccdd1122", "clock sync")
    assert last is not None
    assert last.status == OperationStatus.SUCCESS


async def test_fire_and_forget_op_marked_success_on_timeout(db):
    """An op with fire_and_forget=True is marked SUCCESS even when send_command fails."""
    op_cfg = OperationConfig(command="reboot", every_hours=168, fire_and_forget=True)
    node_cfg = _node(pubkey="aabbcc", label="TestNode", operations=[op_cfg])
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    # node reboots before responding — gateway returns a failure/timeout
    gw.send_command = AsyncMock(return_value=CommandResult(success=False, error="timeout"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert results[0].operations_run == 1
    assert results[0].operations_failed == 0

    last = await db.get_last_operation("aabbccdd1122", "reboot")
    assert last is not None
    assert last.status == OperationStatus.SUCCESS


async def test_fire_and_forget_false_still_fails_on_timeout(db):
    """A normal op (fire_and_forget=False) is still marked FAILED when send_command fails."""
    op_cfg = OperationConfig(command="clock sync", every_hours=24, fire_and_forget=False)
    node_cfg = _node(pubkey="aabbcc", label="TestNode", operations=[op_cfg])
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=False, error="timeout"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert results[0].operations_run == 0
    assert results[0].operations_failed == 1


async def test_cycle_runs_pending_operations(db):
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
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


# ------------------------------------------------------------------
# repeater-only filtering
# ------------------------------------------------------------------

async def test_cycle_skips_non_repeater_contacts(db):
    """CLI contacts (type=1) are not processed even if pubkey matches config."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122", type_=1)])  # CLI, not REP
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()
    assert results[0].reachable is False
    assert "not a repeater" in (results[0].error or "")


# ------------------------------------------------------------------
# favourite flag sync
# ------------------------------------------------------------------

async def test_cycle_sets_favourite_flag_on_unfavourited_contact(db):
    """A managed contact that is not yet favourited (flags=0) gets flagged."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    contact = _contact("aabbccdd1122", flags=0)
    gw = _make_gateway(contacts=[contact])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()
    gw.set_contact_favourite.assert_awaited_once()


async def test_cycle_skips_favourite_if_already_set(db):
    """set_contact_favourite is not called when contact already has flags=1."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    contact = _contact("aabbccdd1122", flags=1)  # already favourited
    gw = _make_gateway(contacts=[contact])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()
    gw.set_contact_favourite.assert_not_awaited()


# ------------------------------------------------------------------
# run_node — forced single-node sync
# ------------------------------------------------------------------

async def test_run_node_forces_immediate_sync(db):
    """run_node processes a node regardless of its next_sync_at."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    # node is scheduled far in the future — run_node should bypass that
    node = Node(pubkey="aabbccdd1122", name="TestNode",
                next_sync_at=datetime(2099, 1, 1, tzinfo=UTC))
    await db.upsert_node(node)
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [_contact("aabbccdd1122")]  # prime cache as run() would at startup

    result = await wq.run_node("aabbcc")
    assert result is not None
    assert result.reachable is True


async def test_run_node_odd_prefix_rejected(db):
    """Prefix with an odd number of hex chars is rejected (convention: must be even)."""
    cfg = _make_config()
    gw = _make_gateway()
    wq = WorkQueue(cfg, gw, db)
    with pytest.raises(ValueError, match="even"):
        await wq.run_node("abc")  # 3 chars — odd


async def test_run_node_ambiguous_prefix_raises(db):
    """A prefix matching multiple contacts raises ValueError."""
    node_cfg1 = _node(pubkey="aa11", label="Node1")
    node_cfg2 = _node(pubkey="aa22", label="Node2")
    cfg = _make_config(node_cfgs=[node_cfg1, node_cfg2])
    contacts = [
        _contact("aa11bbcc1122", name="Node1"),
        _contact("aa22bbcc1122", name="Node2"),
    ]
    gw = _make_gateway(contacts=contacts)
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = contacts  # prime cache as run() would at startup
    with pytest.raises(ValueError, match="ambiguous"):
        await wq.run_node("aa")  # 2 chars — matches both


async def test_run_node_unknown_prefix_raises(db):
    """A prefix matching no contacts raises ValueError."""
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbcc")])
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    wq = WorkQueue(cfg, gw, db)
    with pytest.raises(ValueError, match="no node"):
        await wq.run_node("ff00")  # not in contacts


# ------------------------------------------------------------------
# overdue rescheduling on startup
# ------------------------------------------------------------------

async def test_reschedule_overdue_spreads_nodes(db):
    """Nodes with past next_sync_at are spread randomly across the sync interval."""
    from datetime import timedelta
    past = datetime(2020, 1, 1, tzinfo=UTC)
    await db.upsert_node(Node(pubkey="aa", name="A", next_sync_at=past))
    await db.upsert_node(Node(pubkey="bb", name="B", next_sync_at=past))

    cfg = _make_config()
    gw = _make_gateway()
    wq = WorkQueue(cfg, gw, db)

    before = datetime.now(UTC)
    await wq._reschedule_overdue()
    interval = timedelta(days=cfg.scheduler.sync_interval_days)

    a = await db.get_node("aa")
    b = await db.get_node("bb")
    assert a.next_sync_at >= before
    assert a.next_sync_at <= before + interval
    assert b.next_sync_at >= before
    assert b.next_sync_at <= before + interval


async def test_reschedule_overdue_leaves_future_nodes_alone(db):
    """Nodes already scheduled in the future are not rescheduled."""
    future = datetime(2099, 1, 1, tzinfo=UTC)
    await db.upsert_node(Node(pubkey="aa", name="A", next_sync_at=future))

    cfg = _make_config()
    gw = _make_gateway()
    wq = WorkQueue(cfg, gw, db)

    await wq._reschedule_overdue()
    a = await db.get_node("aa")
    assert a.next_sync_at == future


# ------------------------------------------------------------------
# unfiled contacts (mch contacts)
# ------------------------------------------------------------------

async def test_unfiled_contacts_excludes_managed_nodes(db):
    """get_unfiled_contacts omits contacts whose pubkey is already in config."""
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbcc")])
    gw = _make_gateway(contacts=[
        _contact("aabbccdd1122", name="Managed"),
        _contact("ff001122aabb", name="NewREP"),
    ])
    result = await get_unfiled_contacts(cfg, gw)
    names = [c["adv_name"] for c in result]
    assert "Managed" not in names
    assert "NewREP" in names


async def test_cycle_state_configured_after_successful_enforcement(db):
    """State is CONFIGURED after drift is detected and successfully enforced."""
    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="off"),   # get powersaving — drift
        CommandResult(success=True, text="ok"),    # set powersaving on — success
    ])
    wq = WorkQueue(cfg, gw, db)
    await wq.run_cycle()
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.CONFIGURED


async def test_cycle_observed_updated_after_set(db):
    """After a successful set, the observed value in the DB reflects the new value."""
    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="off"),   # get powersaving — drift
        CommandResult(success=True, text="ok"),    # set powersaving on — success
    ])
    wq = WorkQueue(cfg, gw, db)
    await wq.run_cycle()
    settings = await db.get_settings("aabbccdd1122")
    ps = next(s for s in settings if s.key == "powersaving")
    assert ps.observed == "on"
    assert ps.drift is False


async def test_unfiled_contacts_excludes_non_repeaters(db):
    """get_unfiled_contacts only returns REP contacts, not CLI."""
    cfg = _make_config()
    gw = _make_gateway(contacts=[
        _contact("ff001122aabb", name="NewREP", type_=2),
        _contact("ee991122aabb", name="Companion", type_=1),
    ])
    result = await get_unfiled_contacts(cfg, gw)
    names = [c["adv_name"] for c in result]
    assert "NewREP" in names
    assert "Companion" not in names


# ------------------------------------------------------------------
# frequency hopping
# ------------------------------------------------------------------

async def test_node_on_different_freq_hops_radio(db):
    """A node with a radio config causes the gateway to hop before processing and restore after."""
    from mch.config import RadioConfig
    node_cfg = _node(pubkey="aabbcc", label="TestNode",
                     via=RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8))
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.info = {"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 5}
    gw.hop_radio = AsyncMock(return_value=(869.618, 62.5, 8, 5))
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()
    gw.hop_radio.assert_awaited_once_with(916.575, 62.5, 7, 8)
    gw.restore_radio.assert_awaited_once_with(869.618, 62.5, 8, 5)


async def test_node_no_radio_config_no_hop(db):
    """A node without a radio config never causes a frequency hop."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()
    gw.hop_radio.assert_not_awaited()
    gw.restore_radio.assert_not_awaited()


async def test_radio_restored_on_relogin_failure(db):
    """Radio is restored via finally even when processing raises _ReloginFailed."""
    from mch.config import RadioConfig
    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        via=RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8),
        settings={"name": SettingConfig(desired="X", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {"aabbcc": NodeSecrets(password="pw")}

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.info = {"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 5}
    gw.hop_radio = AsyncMock(return_value=(869.618, 62.5, 8, 5))
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.login = AsyncMock(return_value=False)  # re-login fails → _ReloginFailed
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="no permission"))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()
    gw.hop_radio.assert_awaited_once()
    gw.restore_radio.assert_awaited_once()  # restored even on failure


# ------------------------------------------------------------------
# label stored in DB and seeded on startup
# ------------------------------------------------------------------

async def test_label_written_to_db_on_sync(db):
    """Label from NodeConfig is persisted to the nodes table during a sync."""
    node_cfg = _node(pubkey="aabbcc", label="My Site")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()

    node = await db.get_node("aabbccdd1122")
    assert node is not None
    assert node.label == "My Site"


async def test_seed_node_labels_updates_existing_db_entry(db):
    """_seed_node_labels() writes label into an already-existing DB node on startup."""
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="OldName", label=""))
    node_cfg = _node(pubkey="aabbcc", label="New Label")
    cfg = _make_config(node_cfgs=[node_cfg])
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    wq = WorkQueue(cfg, gw, db)

    await wq._seed_node_labels()

    node = await db.get_node("aabbccdd1122")
    assert node.label == "New Label"


async def test_seed_node_labels_updates_group_field(db):
    """_seed_node_labels() refreshes the group field when it differs from config."""
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="N", label="L", group="OldGroup"))
    node_cfg = _node(pubkey="aabbcc", label="L", group="NewGroup")
    cfg = _make_config(node_cfgs=[node_cfg])
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    wq = WorkQueue(cfg, gw, db)

    await wq._seed_node_labels()

    node = await db.get_node("aabbccdd1122")
    assert node.group == "NewGroup"


async def test_seed_node_labels_skips_unknown_nodes(db):
    """_seed_node_labels() silently skips config entries with no matching DB row."""
    node_cfg = _node(pubkey="aabbcc", label="Ghost")
    cfg = _make_config(node_cfgs=[node_cfg])
    gw = _make_gateway(contacts=[])
    wq = WorkQueue(cfg, gw, db)

    await wq._seed_node_labels()  # should not raise

    assert await db.get_node("aabbccdd1122") is None


# ---------------------------------------------------------------------------
# neighbors fact collection
# ---------------------------------------------------------------------------

async def test_collect_facts_neighbors_sends_bare_command(db):
    """'neighbors' in facts list sends bare 'neighbors', not 'get neighbors'."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["neighbors"]
    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="> NodeA abc"))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()

    calls = [call.args[1] for call in gw.send_command.call_args_list]
    assert "neighbors" in calls
    assert "get neighbors" not in calls


async def test_collect_facts_neighbors_stores_count(db):
    """'neighbors' fact stores the neighbour count as a string, not raw text."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["neighbors"]
    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    raw = "> NodeA abc\n> NodeB def\n> NodeC ghi"
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text=raw))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()

    facts = await db.get_facts("aabbccdd1122")
    assert facts.get("neighbors") == "3"


# ---------------------------------------------------------------------------
# _seed_from_config
# ---------------------------------------------------------------------------

def _make_gateway_with_favourite(contacts=None) -> Gateway:
    gw = _make_gateway(contacts=contacts or [])
    gw.set_contact_favourite = AsyncMock()
    return gw


async def test_seed_from_config_inserts_matched_contact(db):
    """Config node found in gateway contacts and not in DB → inserted into DB."""
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbccdd", label="My Node")])
    contact = _contact("aabbccdd11223344556677889900aabb", name="My Node")
    gw = _make_gateway_with_favourite(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]

    await wq._seed_from_config()

    nodes = await db.get_all_nodes()
    assert len(nodes) == 1
    assert nodes[0].pubkey == "aabbccdd11223344556677889900aabb"
    assert nodes[0].label == "My Node"
    assert nodes[0].state == NodeState.UNKNOWN


async def test_seed_from_config_favourites_unfavourited_contact(db):
    """Config node found in contacts with flags=0 → set_contact_favourite called."""
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbccdd", label="My Node")])
    contact = _contact("aabbccdd11223344556677889900aabb", flags=0)
    gw = _make_gateway_with_favourite(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]

    await wq._seed_from_config()

    gw.set_contact_favourite.assert_awaited_once_with(contact)


async def test_seed_from_config_already_favourited_skips_favourite_call(db):
    """Config node already favourited (flags=1) → set_contact_favourite not called."""
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbccdd", label="My Node")])
    contact = _contact("aabbccdd11223344556677889900aabb", flags=1)
    gw = _make_gateway_with_favourite(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]

    await wq._seed_from_config()

    gw.set_contact_favourite.assert_not_awaited()


async def test_seed_from_config_already_in_db_no_duplicate(db):
    """Config node already in DB → not re-inserted, but still auto-favourited."""
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbccdd", label="My Node")])
    contact = _contact("aabbccdd11223344556677889900aabb", flags=0)
    existing = Node(
        pubkey="aabbccdd11223344556677889900aabb", name="My Node", state=NodeState.CONFIGURED
    )
    await db.upsert_node(existing)

    gw = _make_gateway_with_favourite(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]

    await wq._seed_from_config()

    nodes = await db.get_all_nodes()
    assert len(nodes) == 1  # still just one
    assert nodes[0].state == NodeState.CONFIGURED  # existing state preserved
    gw.set_contact_favourite.assert_awaited_once_with(contact)


async def test_seed_from_config_not_in_contacts_logs_warning(db, caplog):
    """Config node with no matching gateway contact → warning logged, nothing inserted."""
    cfg = _make_config(node_cfgs=[_node(pubkey="deadbeef", label="Ghost Node")])
    gw = _make_gateway_with_favourite(contacts=[])  # no matching contacts
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = []

    import logging
    with caplog.at_level(logging.WARNING, logger="mch.queue"):
        await wq._seed_from_config()

    assert len(await db.get_all_nodes()) == 0
    assert "deadbeef" in caplog.text


async def test_seed_from_config_non_rep_contact_ignored(db):
    """Non-REP contacts (CLI, ROOM) are not matched even if pubkey prefix matches."""
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbccdd", label="My Node")])
    cli_contact = _contact("aabbccdd11223344556677889900aabb", type_=1)  # CLI, not REP
    gw = _make_gateway_with_favourite(contacts=[cli_contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [cli_contact]

    await wq._seed_from_config()

    assert len(await db.get_all_nodes()) == 0
    gw.set_contact_favourite.assert_not_awaited()


# ------------------------------------------------------------------
# open-node warning
# ------------------------------------------------------------------

async def test_open_node_warning_logged_when_no_acl(db, caplog):
    """A WARNING is logged when a node syncs successfully with no ACL protection."""
    node_cfg = _node(pubkey="aabbcc", label="Open Node")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {}  # no password for this node

    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    with caplog.at_level(logging.WARNING, logger="mch.queue"):
        await wq.run_cycle()

    assert "no ACL" in caplog.text


async def test_open_node_warning_written_to_db_logs(db):
    """Open-node warning is written to the DB logs table (visible in mch logs / mch node)."""
    node_cfg = _node(pubkey="aabbcc", label="Open Node")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {}

    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)
    await wq.run_cycle()

    logs = await db.get_logs()  # all logs
    warning_entries = [e for e in logs if "no ACL" in e["message"]]
    assert warning_entries, "open-node warning was not written to the DB logs table"
    assert warning_entries[0]["level"] == "WARNING"


async def test_no_open_node_warning_when_passwd_configured(db, caplog):
    """No open-node warning when a password is configured in secrets."""
    node_cfg = _node(pubkey="aabbcc", label="Secured Node")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {"aabbcc": NodeSecrets(password="hunter2")}

    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    with caplog.at_level(logging.WARNING, logger="mch.queue"):
        await wq.run_cycle()

    assert "no ACL" not in caplog.text


async def test_no_open_node_warning_when_default_password_configured(db, caplog):
    """No open-node warning when a fleet-wide default password is set."""
    node_cfg = _node(pubkey="aabbcc", label="Fleet Node")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {"default": NodeSecrets(password="fleetpassword")}  # no per-node entry

    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    with caplog.at_level(logging.WARNING, logger="mch.queue"):
        await wq.run_cycle()

    assert "no ACL" not in caplog.text


async def test_relogin_uses_default_password_when_no_per_node_entry(db):
    """_send_authed falls back to the default password when no per-node entry exists."""
    node_cfg = _node(
        pubkey="aabbcc", label="FleetNode",
        settings={"name": SettingConfig(desired="FleetNode", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {"default": NodeSecrets(password="fleetpassword")}  # no per-node entry

    await db.upsert_node(Node(pubkey="aabbccdd1122", name="FleetNode", auth_cached=True))

    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.login = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="no permission"),  # permission error
        CommandResult(success=True, text="FleetNode"),      # retry get name
        CommandResult(success=True, text="ok"),             # set name
    ])
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    gw.login.assert_awaited_once_with(contact, "fleetpassword")
    assert results[0].reachable is True


async def test_relogin_prefers_per_node_password_over_default(db):
    """_send_authed uses the per-node password when both per-node and default are set."""
    node_cfg = _node(
        pubkey="aabbcc", label="SpecialNode",
        settings={"name": SettingConfig(desired="SpecialNode", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {
        "aabbcc": NodeSecrets(password="nodespecific"),
        "default": NodeSecrets(password="fleetpassword"),
    }

    await db.upsert_node(Node(pubkey="aabbccdd1122", name="SpecialNode", auth_cached=True))

    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.login = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="no permission"),
        CommandResult(success=True, text="SpecialNode"),
        CommandResult(success=True, text="ok"),
    ])
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()

    gw.login.assert_awaited_once_with(contact, "nodespecific")


async def test_relogin_matches_secret_longer_than_config_prefix(db):
    """Secret lookup uses the full node pubkey, not just the config prefix.

    When secrets.yaml has a longer key (e.g. "aabbccdd") than the node_cfg
    prefix (e.g. "aabb"), the current node pubkey ("aabbccdd1122") should
    still match and the password should be found.
    """
    node_cfg = _node(
        pubkey="aabb", label="ShortPrefix",  # short config prefix
        settings={"name": SettingConfig(desired="ShortPrefix", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    # secret key is longer than the config prefix — bug: startswith on config prefix fails
    cfg.secrets = {"aabbccdd": NodeSecrets(password="correctpassword")}

    await db.upsert_node(Node(pubkey="aabbccdd1122", name="ShortPrefix", auth_cached=True))

    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.login = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="no permission"),  # permission error
        CommandResult(success=True, text="ShortPrefix"),    # retry get name
        CommandResult(success=True, text="ok"),             # set name
    ])
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    gw.login.assert_awaited_once_with(contact, "correctpassword")
    assert results[0].reachable is True
