"""Tests for the work queue — policy resolution and cycle logic."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mch.config import (
    Config,
    GatewayConfig,
    GroupConfig,
    NodeConfig,
    NodeSecrets,
    OperationConfig,
    RadioConfig,
    SchedulerConfig,
    SettingConfig,
    resolve_name_prefix,
)
from mch.db import Database
from mch.gateway import CommandResult, Gateway
from mch.models import (
    DesiredSetting,
    Node,
    NodeState,
    OperationRecord,
    OperationStatus,
    Scope,
    SettingPolicy,
    SettingRecord,
)
from mch.queue import (
    OperationBusyError,
    WorkQueue,
    all_setting_keys,
    get_unfiled_contacts,
    radio_recovery_candidates,
    resolve_desired,
    resolve_policy,
    resolve_reach_radio,
)

# ---------------------------------------------------------------------------
# policy resolution
# ---------------------------------------------------------------------------

def _node(pubkey="aa", **kwargs) -> NodeConfig:
    return NodeConfig(pubkey_prefix=pubkey, **kwargs)


def _group(**kwargs) -> GroupConfig:
    return GroupConfig(name="g", **kwargs)


def test_resolve_reach_radio_via_radio_overrides_desired():
    node = _node(
        via_radio=RadioConfig(916.575, 62.5, 7, 8),
        settings={"radio": SettingConfig(desired="915.800,250,10,5")},
    )
    radio, source = resolve_reach_radio(node, None, None)
    assert radio is not None
    assert (radio.freq, radio.bw, radio.sf, radio.cr) == (916.575, 62.5, 7, 8)
    assert "via_radio" in source


def test_resolve_reach_radio_falls_back_to_desired_radio():
    node = _node(settings={"radio": SettingConfig(desired="915.800,250,10,5")})
    radio, source = resolve_reach_radio(node, None, None)
    assert radio is not None
    assert (radio.freq, radio.bw, radio.sf, radio.cr) == (915.800, 250.0, 10, 5)
    assert "desired" in source


def test_resolve_reach_radio_desired_inherits_group():
    group = _group(settings={"radio": SettingConfig(desired="915.800,250,10,5")})
    radio, source = resolve_reach_radio(_node(), group, None)
    assert radio is not None
    assert (radio.freq, radio.bw, radio.sf, radio.cr) == (915.800, 250.0, 10, 5)


def test_resolve_reach_radio_none_when_no_via_or_desired():
    radio, source = resolve_reach_radio(_node(), None, None)
    assert radio is None


def test_resolve_policy_node_override_wins():
    node = _node(settings={"tx": SettingConfig(policy=SettingPolicy.AUDIT)})
    group = _group(default_policy=SettingPolicy.ENFORCE)
    assert resolve_policy("tx", node, group) == SettingPolicy.AUDIT


def test_resolve_policy_group_setting_override():
    node = _node()
    group = _group(setting_overrides={"tx": SettingConfig(policy=SettingPolicy.SKIP)})
    assert resolve_policy("tx", node, group) == SettingPolicy.SKIP


def test_resolve_policy_group_setting_policy_wins_over_group_default():
    node = _node()
    group = _group(
        default_policy=SettingPolicy.AUDIT,
        settings={"owner.info": SettingConfig(
            desired="rdm",
            policy=SettingPolicy.ENFORCE,
        )},
    )
    assert resolve_policy("owner.info", node, group) == SettingPolicy.ENFORCE


def test_resolve_policy_node_default_wins_over_group_setting_policy():
    node = _node(default_policy=SettingPolicy.AUDIT)
    group = _group(
        settings={"owner.info": SettingConfig(
            desired="rdm",
            policy=SettingPolicy.ENFORCE,
        )},
    )
    assert resolve_policy("owner.info", node, group) == SettingPolicy.AUDIT


def test_resolve_policy_node_default():
    node = _node(default_policy=SettingPolicy.AUDIT)
    assert resolve_policy("tx", node, None) == SettingPolicy.AUDIT


def test_resolve_policy_group_default():
    node = _node()
    group = _group(default_policy=SettingPolicy.AUDIT)
    assert resolve_policy("tx", node, group) == SettingPolicy.AUDIT


def test_resolve_policy_global_default():
    assert resolve_policy("tx", _node(), None) == SettingPolicy.AUDIT


def test_resolve_policy_global_config_default():
    global_cfg = _group(default_policy=SettingPolicy.ENFORCE)
    assert resolve_policy("tx", _node(), None, global_cfg) == SettingPolicy.ENFORCE


def test_resolve_policy_group_wins_over_global():
    node = _node()
    group = _group(default_policy=SettingPolicy.AUDIT)
    global_cfg = _group(default_policy=SettingPolicy.ENFORCE)
    assert resolve_policy("tx", node, group, global_cfg) == SettingPolicy.AUDIT


def test_resolve_policy_specificity_order():
    # node setting > node default > group setting > group default > global
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


def test_resolve_desired_falls_back_to_global():
    node = _node()
    global_cfg = _group(settings={"tx": SettingConfig(desired="low")})
    assert resolve_desired("tx", node, None, global_cfg) == "low"


def test_resolve_desired_group_wins_over_global():
    node = _node()
    group = _group(settings={"tx": SettingConfig(desired="medium")})
    global_cfg = _group(settings={"tx": SettingConfig(desired="low")})
    assert resolve_desired("tx", node, group, global_cfg) == "medium"


def test_resolve_desired_none_when_no_config():
    assert resolve_desired("tx", _node(), None) is None


# ---------------------------------------------------------------------------
# name_prefix: emoji/string prepended to the desired node name
# ---------------------------------------------------------------------------

def test_resolve_name_prefix_node_wins_over_group():
    node = _node(name_prefix="N")
    group = _group(name_prefix="G")
    assert resolve_name_prefix(node, group) == "N"


def test_resolve_name_prefix_falls_back_to_group():
    group = _group(name_prefix="G")
    assert resolve_name_prefix(_node(), group) == "G"


def test_resolve_name_prefix_falls_back_to_global():
    global_cfg = _group(name_prefix="X")
    assert resolve_name_prefix(_node(), None, global_cfg) == "X"


def test_resolve_name_prefix_none_when_unset():
    assert resolve_name_prefix(_node(), _group(), _group()) is None


def test_resolve_desired_name_applies_group_prefix():
    # The headline use case: group emoji prefix + per-node name.
    node = _node(settings={"name": SettingConfig(desired="NodeA")})
    group = _group(name_prefix="\U0001f680")
    assert resolve_desired("name", node, group) == "\U0001f680NodeA"


def test_resolve_desired_name_no_auto_space():
    # No space is inserted between prefix and name - operators add it if they want one.
    node = _node(settings={"name": SettingConfig(desired="NodeA")})
    group = _group(name_prefix="\U0001f680")
    assert resolve_desired("name", node, group) == "\U0001f680NodeA"
    # An operator who wants a space puts it in the prefix.
    group_space = _group(name_prefix="\U0001f680 ")
    assert resolve_desired("name", node, group_space) == "\U0001f680 NodeA"


def test_resolve_desired_name_no_prefix_unchanged():
    node = _node(settings={"name": SettingConfig(desired="NodeA")})
    assert resolve_desired("name", node, _group()) == "NodeA"


def test_resolve_desired_name_prefix_without_base_is_none():
    # A prefix with no desired base name has nothing to prepend onto.
    group = _group(name_prefix="\U0001f680")
    assert resolve_desired("name", _node(), group) is None


def test_resolve_desired_prefix_only_applies_to_name_key():
    # name_prefix must never leak onto other settings.
    node = _node(settings={"tx": SettingConfig(desired="20")}, name_prefix="\U0001f680")
    assert resolve_desired("tx", node, None) == "20"


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


def test_all_setting_keys_includes_global():
    node = _node(settings={"a": SettingConfig()})
    global_cfg = _group(settings={"b": SettingConfig()}, setting_overrides={"c": SettingConfig()})
    assert all_setting_keys(node, None, global_cfg) == {"a", "b", "c"}


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


async def _seed_desired(db: Database, cfg: Config, contacts: list[dict]) -> None:
    """Seed the DB with the desired state a test expresses via its Config (sunset bridge).

    The sync engine now resolves desired state and node group from the DB, not config.yaml.
    This mirrors `mch import`: it upserts a DB node (with its group) for each config node it
    can match to a contact, then writes every desired setting/directive as a desired_settings
    row via the production config_to_desired_rows. Tests keep declaring intent as a Config; this
    bridges it into the DB so run_cycle/sync paths see it.
    """
    from mch.config import config_to_desired_operations, config_to_desired_rows

    def resolve(prefix: str) -> str | None:
        return next(
            (c["public_key"] for c in contacts if c["public_key"].startswith(prefix)), None
        )

    for nc in cfg.nodes:
        pubkey = resolve(nc.pubkey_prefix)
        if pubkey is None:
            continue
        existing = await db.get_node(pubkey)
        if existing is None:
            await db.upsert_node(Node(
                pubkey=pubkey, name="", label=nc.label, group=nc.group,
                state=NodeState.UNKNOWN,
            ))
        else:
            existing.label = nc.label or existing.label
            existing.group = nc.group or existing.group
            await db.upsert_node(existing)

    rows, _ = config_to_desired_rows(cfg, resolve)
    for row in rows:
        await db.set_desired_setting(row)
    operations, _ = config_to_desired_operations(cfg, resolve)
    for op in operations:
        await db.set_desired_operation(op)


def _make_gateway(contacts=None, cmd_text="ok") -> Gateway:
    gw = MagicMock(spec=Gateway)
    gw.get_contacts = AsyncMock(return_value=contacts or [])
    gw.login = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text=cmd_text))
    gw.discover_contact_path = AsyncMock(return_value={
        "path": "e2605f6c",
        "path_len": 2,
        "path_hash_mode": 1,
    })
    gw.get_contact_advert_path = AsyncMock(return_value={
        "path": "e260",
        "path_len": 1,
        "path_hash_mode": 1,
    })
    gw.change_contact_path = AsyncMock()
    gw.discover = AsyncMock(return_value=[])
    gw.add_commission_contact = AsyncMock(side_effect=lambda node_info: {
        "public_key": node_info["pubkey"],
        "type": node_info.get("node_type", 2),
        "flags": 0,
        "out_path": "",
        "out_path_len": -1,
        "out_path_hash_mode": 0,
        "adv_name": "",
    })
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.advert = AsyncMock()
    gw.switch_radio_temporarily = AsyncMock(return_value=(915.800, 250, 10, 5))
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


async def _probe_ok_else_timeout(contact, command, **kwargs):
    """send_command side effect: the reachability probe ('ver') succeeds, every
    other read times out. Lets a test exercise read failures past the probe."""
    if command == "ver":
        return CommandResult(success=True, text="v1.14.1")
    return CommandResult(success=False, error="timeout")


async def test_exec_command_returns_gateway_text(db):
    contact = _contact("aabbccdd11223344")
    gw = _make_gateway()
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="v1.14.1"))
    wq = WorkQueue(_make_config(), gw, db)
    wq._contacts = [contact]

    result = await wq.exec_command("aabbccdd11223344", "ver")

    assert result == "v1.14.1"
    gw.send_command.assert_awaited_once_with(contact, "ver", timeout=60)


async def test_exec_command_raises_when_contact_missing(db):
    wq = WorkQueue(_make_config(), _make_gateway(), db)
    wq._contacts = []

    with pytest.raises(ValueError, match="no gateway contact"):
        await wq.exec_command("aabbccdd11223344", "ver")


async def test_exec_command_raises_gateway_error(db):
    contact = _contact("aabbccdd11223344")
    gw = _make_gateway()
    gw.send_command = AsyncMock(return_value=CommandResult(success=False, error="denied"))
    wq = WorkQueue(_make_config(), gw, db)
    wq._contacts = [contact]

    with pytest.raises(ValueError, match="denied"):
        await wq.exec_command("aabbccdd11223344", "ver")


async def test_exec_command_fire_and_forget_timeout_returns_sent(db):
    contact = _contact("aabbccdd11223344")
    gw = _make_gateway()
    gw.send_command = AsyncMock(return_value=CommandResult(success=False, error="timeout"))
    wq = WorkQueue(_make_config(), gw, db)
    wq._contacts = [contact]

    result = await wq.exec_command("aabbccdd11223344", "reboot")

    assert result == "(sent - no response expected)"


async def test_exec_command_returns_busy_during_other_operation(db):
    contact = _contact("aabbccdd11223344")
    gw = _make_gateway()
    wq = WorkQueue(_make_config(), gw, db)
    wq._contacts = [contact]

    async with wq.operation("scheduled sync aabbccdd", wait=False):
        with pytest.raises(OperationBusyError, match="busy: scheduled sync aabbccdd"):
            await wq.exec_command("aabbccdd11223344", "ver")

    gw.send_command.assert_not_awaited()


async def test_work_queue_unfiled_contacts_uses_config_and_gateway(db):
    cfg = _make_config([_node("aabbccdd")])
    gw = _make_gateway(contacts=[
        _contact("aabbccdd1122", name="Managed"),
        _contact("ff001122aabb", name="NewREP"),
    ])
    wq = WorkQueue(cfg, gw, db)

    result = await wq.unfiled_contacts()

    assert [c["adv_name"] for c in result] == ["NewREP"]


async def test_start_cycle_rejects_duplicate_manual_cycle(db):
    wq = WorkQueue(_make_config(), _make_gateway(), db)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_cycle_body():
        started.set()
        await release.wait()
        return []

    wq._run_cycle_body = fake_run_cycle_body  # type: ignore[method-assign]

    assert await wq.start_cycle() == "sync started"
    await started.wait()
    with pytest.raises(OperationBusyError, match="busy: sync cycle"):
        await wq.start_cycle()

    release.set()
    await wq._manual_cycle_task
    logs = await db.get_logs()
    messages = [entry["message"] for entry in logs]
    assert "manual sync started: full cycle" in messages
    assert "manual sync completed: full cycle (0 nodes processed)" in messages


async def test_stop_cancels_manual_cycle_and_releases_operation_lock(db):
    wq = WorkQueue(_make_config(), _make_gateway(), db)
    started = asyncio.Event()

    async def blocked_cycle():
        started.set()
        await asyncio.Event().wait()

    wq._run_cycle_body = blocked_cycle  # type: ignore[method-assign]

    assert await wq.start_cycle() == "sync started"
    await started.wait()
    await wq.stop()

    assert wq._manual_cycle_task is not None
    assert wq._manual_cycle_task.cancelled()
    assert not wq._operation_lock.locked()
    logs = await db.get_logs()
    assert any(entry["message"] == "manual sync aborted: full cycle" for entry in logs)


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
# scheduled sync operation name includes label
# ------------------------------------------------------------------

async def test_scheduled_sync_operation_name_uses_label(db):
    """_run_next_due uses the node label in its operation name, not just pubkey prefix.

    This ensures 'busy: scheduled sync qh-tower' appears in error messages
    when the background scheduler is processing that node.
    """
    node = Node(pubkey="aabbccdd" * 8, name="Tower", label="qh-tower",
                state=NodeState.CONFIGURED)
    await db.upsert_node(node)

    node_cfg = _node(pubkey="aabbccdd", label="qh-tower")
    cfg = _make_config(node_cfgs=[node_cfg])
    contact = _contact("aabbccdd" * 8)  # full pubkey must match node.pubkey exactly
    gw = _make_gateway(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]

    captured: list[str] = []
    original_process = wq._process_node

    async def record_op_name(*args, **kwargs):
        captured.append(wq._operation_name or "")
        return await original_process(*args, **kwargs)

    wq._process_node = record_op_name  # type: ignore[method-assign]
    await wq._run_next_due([contact])

    assert captured, "expected _process_node to be called"
    assert any("qh-tower" in op for op in captured), (
        f"label not in operation name; got: {captured}"
    )


async def test_scheduled_sync_operation_name_uses_config_label_when_db_label_missing(db):
    node = Node(pubkey="aabbccdd" * 8, name="Tower", state=NodeState.CONFIGURED)
    await db.upsert_node(node)

    node_cfg = _node(pubkey="aabbccdd", label="qh-tower")
    cfg = _make_config(node_cfgs=[node_cfg])
    contact = _contact("aabbccdd" * 8)
    gw = _make_gateway(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())
    wq._contacts = [contact]

    captured: list[str] = []
    original_process = wq._process_node

    async def record_op_name(*args, **kwargs):
        captured.append(wq._operation_name or "")
        return await original_process(*args, **kwargs)

    wq._process_node = record_op_name  # type: ignore[method-assign]
    await wq._run_next_due([contact])

    assert captured, "expected _process_node to be called"
    assert any("scheduled sync qh-tower" == op for op in captured), (
        f"config label not in operation name; got: {captured}"
    )


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
# reachability probe
# ------------------------------------------------------------------

async def test_probe_failure_skips_sync_and_marks_unreachable(db):
    """Two failed reachability probes skip the pass and mark the node unreachable."""
    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["ver", "board"]
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=False, error="timeout"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    # only the two probe attempts were sent — no fact or setting reads
    sent = [c.args[1] for c in gw.send_command.await_args_list]
    assert sent == ["ver", "ver"]
    assert results[0].reachable is False
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.UNREACHABLE
    logs = await db.get_logs("aabbccdd1122")
    assert any("node unreachable" in entry["message"] for entry in logs)


async def test_probe_retries_once_before_giving_up(db):
    """A node that misses the first probe but answers the second is still synced."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["ver"]
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=False, error="timeout"),  # probe attempt 1 — miss
        CommandResult(success=True, text="v1.14.1"),     # probe attempt 2 — hit
    ])
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    assert results[0].reachable is True
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.CONFIGURED


async def test_probe_ver_reply_reused_as_fact_no_second_read(db):
    """The probe's ver reply is stored as the ver fact without a second read."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["ver"]
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="v1.14.1"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    # ver is the only fact and it is also the probe — exactly one send total
    sent = [c.args[1] for c in gw.send_command.await_args_list]
    assert sent == ["ver"]
    assert results[0].facts_collected == 1
    facts = await db.get_facts("aabbccdd1122")
    assert facts.get("ver") == "v1.14.1"


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
    # probe ver → ok; powersaving (bare read) → "off" (drift); powersaving on (write) → "ok"
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),  # reachability probe
        CommandResult(success=True, text="off"),   # powersaving
        CommandResult(success=True, text="ok"),    # powersaving on
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()
    assert results[0].settings_enforced == 1
    assert results[0].settings_drifted == 0  # drift was fixed — not counted as residual


async def test_cycle_applies_db_node_scope_desired_setting(db):
    # config node carries NO settings; the desired value comes only from the DB
    # (node scope). Proves overlay_db_settings is wired into _process_node: without
    # it, node_cfg.settings is empty, powersaving is never read, and the send_command
    # side_effect sequence below would not be consumed in this order.
    node_cfg = _node(pubkey="aabbcc", label="TestNode", settings={})
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.set_desired_setting(DesiredSetting(
        Scope.NODE, "aabbccdd1122", "powersaving",
        desired="on", policy=SettingPolicy.ENFORCE,
    ))
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),  # reachability probe
        CommandResult(success=True, text="off"),       # powersaving read -> drift
        CommandResult(success=True, text="ok"),         # powersaving on (enforce write)
    ])
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    assert results[0].settings_enforced == 1
    assert [c.args[1] for c in gw.send_command.await_args_list] == [
        "ver", "powersaving", "powersaving on",
    ]


async def test_cycle_applies_db_group_scope_desired_setting(db):
    # The desired value comes only from a DB group-scope row; the node is in group "g"
    # but carries no settings of its own. Proves overlay_db_group_settings is wired into
    # _process_node for the group scope.
    node_cfg = _node(pubkey="aabbcc", label="TestNode", group="g", settings={})
    cfg = _make_config(node_cfgs=[node_cfg], group_cfgs=[_group()])
    cfg.facts = []
    await db.set_desired_setting(DesiredSetting(
        Scope.GROUP, "g", "powersaving",
        desired="on", policy=SettingPolicy.ENFORCE,
    ))
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),  # reachability probe
        CommandResult(success=True, text="off"),       # powersaving read -> drift
        CommandResult(success=True, text="ok"),         # powersaving on (enforce write)
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    assert results[0].settings_enforced == 1
    assert [c.args[1] for c in gw.send_command.await_args_list] == [
        "ver", "powersaving", "powersaving on",
    ]


async def test_cycle_applies_db_global_scope_desired_setting(db):
    # The desired value comes only from a DB global-scope row (scope_id="");
    # the node has no group and no settings. Proves the global overlay is wired in.
    node_cfg = _node(pubkey="aabbcc", label="TestNode", settings={})
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.set_desired_setting(DesiredSetting(
        Scope.GLOBAL, "", "powersaving",
        desired="on", policy=SettingPolicy.ENFORCE,
    ))
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),  # reachability probe
        CommandResult(success=True, text="off"),       # powersaving read -> drift
        CommandResult(success=True, text="ok"),         # powersaving on (enforce write)
    ])
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    assert results[0].settings_enforced == 1
    assert [c.args[1] for c in gw.send_command.await_args_list] == [
        "ver", "powersaving", "powersaving on",
    ]


async def test_cycle_db_name_prefix_directive_prepends_to_name(db):
    # A DB name_prefix *directive* (structured field, not a settings row) must flow
    # through the overlay into resolve_desired("name"), so the enforced write carries
    # the prefixed name. Proves slice 7a's directive overlay reaches reconcile.
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"name": SettingConfig(desired="Tower", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.set_desired_setting(DesiredSetting(
        Scope.NODE, "aabbccdd1122", "name_prefix", desired="\U0001f31e",
    ))
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),  # reachability probe
        CommandResult(success=True, text="OldName"),    # name read -> drift
        CommandResult(success=True, text="ok"),          # set name (enforce write)
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()

    sent = [c.args[1] for c in gw.send_command.await_args_list]
    # the enforced write must carry the prefixed name (no auto-space: prefix + base)
    assert any("\U0001f31eTower" in cmd for cmd in sent), sent


async def test_enforced_write_does_not_fallback_to_flood_after_cached_route_failure(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "e260", "out_path_len": 1, "out_path_hash_mode": 0})
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),  # reachability probe, cached route
        CommandResult(success=True, text="off"),       # powersaving read, cached route
        CommandResult(success=False, error="timeout"), # write fails on cached route
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    assert results[0].settings_enforced == 0
    assert results[0].settings_drifted == 1
    calls = gw.send_command.await_args_list
    assert [c.args[1] for c in calls] == ["ver", "powersaving", "powersaving on"]
    assert calls[2].args[0]["out_path_len"] == 1
    settings = await db.get_settings("aabbccdd1122")
    assert settings[0].update_attempts == 1


async def test_stored_path_setting_read_retries_one_miss(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.AUDIT)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "e260", "out_path_len": 1, "out_path_hash_mode": 0})
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),   # reachability probe
        CommandResult(success=False, error="timeout"), # read miss on stored path
        CommandResult(success=True, text="on"),        # retry succeeds
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    assert results[0].settings_checked == 1
    assert results[0].settings_failed == 0
    assert [c.args[1] for c in gw.send_command.await_args_list] == [
        "ver", "powersaving", "powersaving",
    ]
    assert all(c.args[0]["out_path_len"] == 1 for c in gw.send_command.await_args_list)
    settings = await db.get_settings("aabbccdd1122")
    assert settings[0].observed == "on"
    logs = await db.get_logs("aabbccdd1122")
    assert any("succeeded on stored-path retry" in entry["message"] for entry in logs)


async def test_stored_path_read_retry_budget_is_capped_per_node(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={
            "powersaving": SettingConfig(desired="on", policy=SettingPolicy.AUDIT),
            "owner.info": SettingConfig(desired="rdm", policy=SettingPolicy.AUDIT),
            "txdelay": SettingConfig(desired="1.0", policy=SettingPolicy.AUDIT),
            "direct.txdelay": SettingConfig(desired="0.5", policy=SettingPolicy.AUDIT),
        },
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "e260", "out_path_len": 1, "out_path_hash_mode": 0})
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(return_value=CommandResult(success=False, error="timeout"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    # Two failed stored-path probes get one flood reachability check, but no
    # full pass or per-read retry budget is used.
    assert results[0].reachable is False
    assert len(gw.send_command.await_args_list) == 3
    assert gw.send_command.await_args_list[0].args[0]["out_path_len"] == 1
    assert gw.send_command.await_args_list[1].args[0]["out_path_len"] == 1
    assert gw.send_command.await_args_list[2].args[0]["out_path_len"] == -1


async def test_stored_path_failure_flood_probe_does_not_run_full_sync(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["ver", "board"]
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "e260", "out_path_len": 1, "out_path_hash_mode": 0})
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=False, error="timeout"), # stored path probe 1
        CommandResult(success=False, error="timeout"), # stored path probe 2
        CommandResult(success=True, text="v1.14.1"),   # flood probe reaches node
    ])
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    assert results[0].reachable is False
    assert [c.args[1] for c in gw.send_command.await_args_list] == ["ver", "ver", "ver"]
    assert gw.send_command.await_args_list[2].args[0]["out_path_len"] == -1
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.UNREACHABLE
    logs = await db.get_logs("aabbccdd1122")
    assert any("path repair needed" in entry["message"] for entry in logs)


async def test_flood_routed_contact_probe_defers_full_sync(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["ver", "board"]
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "", "out_path_len": -1, "out_path_hash_mode": 0})
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="v1.14.1"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    assert results[0].reachable is True
    assert results[0].error is None
    assert results[0].skipped_reason == "path repair needed before full sync"
    assert [c.args[1] for c in gw.send_command.await_args_list] == ["ver"]
    assert gw.send_command.await_args_list[0].args[0]["out_path_len"] == -1
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.PARTIAL
    settings = await db.get_settings("aabbccdd1122")
    assert settings == []
    logs = await db.get_logs("aabbccdd1122")
    assert any("path repair needed before full sync" in entry["message"] for entry in logs)


async def test_flood_routed_contact_unreachable_skips_full_sync(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["ver", "board"]
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "", "out_path_len": -1, "out_path_hash_mode": 0})
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(return_value=CommandResult(success=False, error="timeout"))
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    assert results[0].reachable is False
    assert [c.args[1] for c in gw.send_command.await_args_list] == ["ver", "ver"]
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.UNREACHABLE
    logs = await db.get_logs("aabbccdd1122")
    assert any("node unreachable over flood" in entry["message"] for entry in logs)


async def test_flood_routed_contact_tries_desired_radio_before_unreachable(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"radio": SettingConfig(desired="915.800,250,10,5")},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["ver"]
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "", "out_path_len": -1, "out_path_hash_mode": 0})
    gw = _make_gateway(contacts=[contact])
    gw.info = {"radio_freq": 915.075, "radio_bw": 125, "radio_sf": 9, "radio_cr": 5}

    radio_by_channel = (915.075, 125.0, 9)

    async def _switch(freq, bw, sf, cr):
        nonlocal radio_by_channel
        previous = (*radio_by_channel, 5)
        radio_by_channel = (round(freq, 3), float(bw), int(sf))
        return previous

    async def _restore(freq, bw, sf, cr):
        nonlocal radio_by_channel
        radio_by_channel = (round(freq, 3), float(bw), int(sf))

    async def _send(contact, command, **kwargs):
        if command == "ver" and radio_by_channel == (915.800, 250.0, 10):
            return CommandResult(success=True, text="v1.15.0")
        return CommandResult(success=False, error="timeout")

    gw.switch_radio_temporarily = AsyncMock(side_effect=_switch)
    gw.restore_radio = AsyncMock(side_effect=_restore)
    gw.send_command = AsyncMock(side_effect=_send)
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    assert results[0].reachable is True
    assert results[0].skipped_reason == "path repair needed before full sync"
    assert gw.switch_radio_temporarily.await_args_list[-1].args == (915.800, 250.0, 10, 5)
    assert radio_by_channel == (915.075, 125.0, 9)
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.PARTIAL
    logs = await db.get_logs("aabbccdd1122")
    # The node is reached directly on its desired radio by the primary probe
    # (the reach radio now defaults to settings->radio), so the flood contact is
    # marked for path repair without needing the recovery hunt.
    assert any(
        "flood-routed contact reached by probe; path repair needed" in e["message"]
        for e in logs
    )


async def test_stored_path_read_retry_budget_applies_after_successful_probe(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={
            "powersaving": SettingConfig(desired="on", policy=SettingPolicy.AUDIT),
            "owner.info": SettingConfig(desired="rdm", policy=SettingPolicy.AUDIT),
            "txdelay": SettingConfig(desired="1.0", policy=SettingPolicy.AUDIT),
            "direct.txdelay": SettingConfig(desired="0.5", policy=SettingPolicy.AUDIT),
        },
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "e260", "out_path_len": 1, "out_path_hash_mode": 0})
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),   # reachability probe
        CommandResult(success=False, error="timeout"), # read 1
        CommandResult(success=False, error="timeout"), # retry 1
        CommandResult(success=False, error="timeout"), # read 2
        CommandResult(success=False, error="timeout"), # retry 2
        CommandResult(success=False, error="timeout"), # read 3
        CommandResult(success=False, error="timeout"), # retry 3
        CommandResult(success=False, error="timeout"), # read 4, budget exhausted
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    assert results[0].reachable is True
    assert results[0].settings_failed == 4
    assert len(gw.send_command.await_args_list) == 8


async def test_failed_enforcement_counts_attempts_for_current_desired_value(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    # each cycle starts with a reachability probe (ver), then read + write powersaving
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),  # probe (cycle 1)
        CommandResult(success=True, text="off"),
        CommandResult(success=False, error="timeout"),
        CommandResult(success=True, text="v1.14.1"),  # probe (cycle 2)
        CommandResult(success=True, text="off"),
        CommandResult(success=False, error="timeout"),
        CommandResult(success=True, text="v1.14.1"),  # probe (cycle 3)
        CommandResult(success=True, text="on"),
        CommandResult(success=False, error="timeout"),
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()
    await wq.run_cycle()
    settings = await db.get_settings("aabbccdd1122")
    assert settings[0].update_attempts == 2

    # desired now lives in the DB (sunset), so change it there
    await db.set_desired_setting(DesiredSetting(
        Scope.NODE, "aabbccdd1122", "powersaving", desired="off", policy=SettingPolicy.ENFORCE,
    ))
    await wq.run_cycle()
    settings = await db.get_settings("aabbccdd1122")
    assert settings[0].update_attempts == 1


async def test_cycle_does_not_enforce_close_enough_radio_roundoff(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"radio": SettingConfig(
            desired="915.075,125,9,5",
            policy=SettingPolicy.ENFORCE,
        )},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(
        success=True,
        text="915.0750122,125,9,5",
    ))
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    assert results[0].settings_enforced == 0
    assert results[0].settings_drifted == 0
    sent = [c.args[1] for c in gw.send_command.await_args_list]
    assert sent == ["ver", "get radio"]  # reachability probe, then the setting read


async def test_cycle_enforced_radio_write_uses_registered_slow_timeout(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"radio": SettingConfig(
            desired="915.075,125,9,5",
            policy=SettingPolicy.ENFORCE,
        )},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),  # reachability probe
        CommandResult(success=True, text="915.800,250,10,5"),
        CommandResult(success=True, text="ok"),
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()

    # index 0 is the probe (ver); the radio read/write follow
    assert gw.send_command.await_args_list[1].args[1] == "get radio"
    assert "timeout" not in gw.send_command.await_args_list[1].kwargs
    assert gw.send_command.await_args_list[2].args[1] == "set radio 915.075,125,9,5"
    assert gw.send_command.await_args_list[2].kwargs["timeout"] == 180.0


async def test_bare_command_key_uses_bare_read_and_write(db):
    """Keys like 'powersaving' use bare commands — no 'get'/'set' prefix.
    Read: 'powersaving' not 'get powersaving'.
    Write: 'powersaving on' not 'set powersaving on'."""
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),  # reachability probe
        CommandResult(success=True, text="off"),  # read: powersaving
        CommandResult(success=True, text="ok"),   # write: powersaving on
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()

    sent = [c.args[1] for c in gw.send_command.await_args_list]
    assert "powersaving" in sent, f"expected bare 'powersaving' read, got: {sent}"
    assert "powersaving on" in sent, f"expected bare 'powersaving on' write, got: {sent}"
    assert "get powersaving" not in sent, "'get powersaving' must not be sent"
    assert "set powersaving on" not in sent, "'set powersaving on' must not be sent"


async def test_cycle_updates_db_name_when_name_is_enforced(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"name": SettingConfig(desired="New Name", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122", name="Old Advert")])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),   # reachability probe
        CommandResult(success=True, text="Old Name"),  # get name
        CommandResult(success=True, text="ok"),        # set name New Name
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()

    node = await db.get_node("aabbccdd1122")
    assert node.name == "New Name"


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
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()
    assert results[0].settings_drifted == 1
    assert results[0].settings_enforced == 0
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.CONFIGURED
    # set command must NOT have been called
    calls = [str(c) for c in gw.send_command.call_args_list]
    assert not any("set " in c for c in calls)


async def test_changing_enforced_setting_to_audit_clears_update_attempts(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    # each cycle: reachability probe (ver), then read + (cycle 1) write powersaving
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),  # probe (cycle 1)
        CommandResult(success=True, text="off"),
        CommandResult(success=False, error="timeout"),
        CommandResult(success=True, text="v1.14.1"),  # probe (cycle 2)
        CommandResult(success=True, text="off"),
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()
    assert (await db.get_settings("aabbccdd1122"))[0].update_attempts == 1

    # policy now lives in the DB (sunset), so change it there
    await db.set_desired_setting(DesiredSetting(
        Scope.NODE, "aabbccdd1122", "powersaving", desired="on", policy=SettingPolicy.AUDIT,
    ))
    await wq.run_cycle()
    assert (await db.get_settings("aabbccdd1122"))[0].update_attempts == 0


async def test_cycle_desired_setting_without_policy_audits_only(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"powersaving": SettingConfig(desired="on")},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="off"))
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    assert results[0].settings_drifted == 1
    assert results[0].settings_enforced == 0
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
    # the reachability probe (ver) hits the permission error first, triggering
    # the re-login + retry; then get name matches desired so no write is needed
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="no permission"),  # probe ver — permission denied
        CommandResult(success=True, text="v1.14.1"),        # probe ver — retry after re-login
        CommandResult(success=True, text="TestNode"),       # get name — matches desired
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
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()
    assert results[0].operations_run == 1


async def test_group_operations_seeded_for_member_node(db):
    """Operations declared on a group are inherited by member nodes."""
    op_cfg = OperationConfig(command="clock sync", every_hours=24)
    node_cfg = _node(pubkey="aabbcc", label="TestNode", group="g")
    group_cfg = _group(operations=[op_cfg])
    cfg = _make_config(node_cfgs=[node_cfg], group_cfgs=[group_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode", group="g"))
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    assert results[0].operations_run == 1
    assert any(call.args[1] == "clock sync" for call in gw.send_command.await_args_list)


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
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()
    assert results[0].operations_run == 1


async def test_failed_config_operations_reseeded_when_retry_due(db):
    """Failed recurring operations are retried after their every_hours window."""
    op_cfg = OperationConfig(command="clock sync", every_hours=24)
    node_cfg = _node(pubkey="aabbcc", label="TestNode", operations=[op_cfg])
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))
    op = OperationRecord(
        node_pubkey="aabbccdd1122", command="clock sync", every_hours=24,
        status=OperationStatus.FAILED, run_at=datetime.now(UTC) - timedelta(hours=25),
    )
    await db.upsert_operation(op)
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

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
    # probe ver succeeds (node reachable); the reboot op then times out (node
    # reboots before responding) but is fire-and-forget so it counts as success
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),       # reachability probe
        CommandResult(success=False, error="timeout"),     # reboot — faf
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

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
    # probe ver succeeds (node reachable); the clock sync op then times out and,
    # not being fire-and-forget, is recorded as a failure
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),       # reachability probe
        CommandResult(success=False, error="timeout"),     # clock sync — fails
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()
    assert results[0].operations_run == 0
    assert results[0].operations_failed == 1


async def test_clock_sync_success_refreshes_clock_fact(db):
    """After clock sync succeeds, re-read clock so status reflects the corrected value."""
    op_cfg = OperationConfig(command="clock sync", every_hours=24, fire_and_forget=False)
    node_cfg = _node(pubkey="aabbcc", label="TestNode", operations=[op_cfg])
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),                 # reachability probe
        CommandResult(success=True, text="OK - clock set"),          # clock sync
        CommandResult(success=True, text="09:57 - 2/6/2026 UTC"),    # refreshed clock
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    assert results[0].operations_run == 1
    facts = await db.get_facts("aabbccdd1122")
    assert facts["clock"] == "09:57 - 2/6/2026 UTC"


async def test_operation_err_reply_counts_as_failure(db):
    """MeshCore ERR replies are delivered as text but should fail the operation."""
    op_cfg = OperationConfig(command="clock sync", every_hours=24, fire_and_forget=False)
    node_cfg = _node(pubkey="aabbcc", label="TestNode", operations=[op_cfg])
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.14.1"),
        CommandResult(success=True, text="ERR: clock cannot go backwards"),
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    assert results[0].operations_run == 0
    assert results[0].operations_failed == 1
    last = await db.get_last_operation("aabbccdd1122", "clock sync")
    assert last is not None
    assert last.status == OperationStatus.FAILED
    assert last.response == "ERR: clock cannot go backwards"


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


async def test_run_node_logs_manual_sync_start_and_completion(db):
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    contact = _contact("aabbccdd1122")
    wq = WorkQueue(cfg, _make_gateway(contacts=[contact]), db)
    wq._contacts = [contact]

    await wq.run_node("aabbcc")

    logs = await db.get_logs("aabbccdd1122")
    messages = [entry["message"] for entry in logs]
    assert "manual sync started" in messages
    assert "manual sync completed" in messages


async def test_run_node_reports_fact_and_setting_progress(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    # 'board' (not 'ver') so the reachability probe succeeds but the fact read
    # still fails — the probe reuses its reply only for the 'ver' fact
    cfg.facts = ["board"]
    node = Node(pubkey="aabbccdd1122", name="TestNode")
    await db.upsert_node(node)
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])

    # probe ver succeeds; every other read times out
    async def _send(contact, command, **kwargs):
        if command == "ver":
            return CommandResult(success=True, text="v1.14.1")
        return CommandResult(success=False, error="timeout")

    gw.send_command = AsyncMock(side_effect=_send)
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())
    wq._contacts = [_contact("aabbccdd1122")]
    messages: list[str] = []

    async def progress(message: str) -> None:
        messages.append(message)

    await wq.run_node("aabbcc", progress=progress)

    assert "reading fact: board" in messages
    assert "fact board failed: timeout" in messages
    assert "reading setting: powersaving" in messages
    assert "setting powersaving read failed: timeout" in messages


async def test_run_node_progress_describes_radio_switch_without_mesh_hop_wording(db):
    from mch.config import RadioConfig

    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "", "out_path_len": -1, "out_path_hash_mode": 0})
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        via_radio=RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8),
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())
    wq._contacts = [contact]
    messages: list[str] = []

    async def progress(message: str) -> None:
        messages.append(message)

    await wq.run_node("aabbcc", progress=progress)

    assert (
        "management radio: 916.575/62.5/SF7/CR8 (via_radio override, temporary switch)"
        in messages
    )
    assert "cached management path: flood" in messages
    assert "switching gateway radio to 916.575/62.5/SF7/CR8" in messages
    assert "restored gateway radio to 915.800/250/SF10/CR5" in messages
    assert not any("hop" in message for message in messages)

    logs = await db.get_logs("aabbccdd1122")
    log_messages = [entry["message"] for entry in logs]
    assert "switching gateway radio to 916.575/62.5/SF7/CR8" in log_messages
    assert "restored gateway radio to 915.800/250/SF10/CR5" in log_messages


async def test_run_node_progress_describes_gateway_radio_and_direct_cached_path(db):
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "", "out_path_len": 0, "out_path_hash_mode": 0})
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))
    gw = _make_gateway(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    messages: list[str] = []

    async def progress(message: str) -> None:
        messages.append(message)

    await wq.run_node("aabbcc", progress=progress)

    assert "management radio: 915.800/250/SF10/CR5 (gateway current radio; no switch)" in messages
    assert "cached management path: direct" in messages
    gw.switch_radio_temporarily.assert_not_awaited()


async def test_run_node_progress_shows_stored_cached_path_metadata(db):
    contact = _contact("aabbccdd1122")
    contact.update({
        "out_path": "e260,5f6c,68be",
        "out_path_len": 3,
        "out_path_hash_mode": 0,
    })
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="TestNode"))
    gw = _make_gateway(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    messages: list[str] = []

    async def progress(message: str) -> None:
        messages.append(message)

    await wq.run_node("aabbcc", progress=progress)

    assert (
        "cached management path: stored "
        "(out_path=e260,5f6c,68be; out_path_len=3; hash-mode=0)"
    ) in messages


async def test_management_path_status_reports_stored_path_without_mesh_traffic(db):
    contact = _contact("aabbccdd1122")
    contact.update({
        "out_path": "e260,5f6c",
        "out_path_len": 2,
        "out_path_hash_mode": 1,
        "flags": 1,
    })
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbcc", label="TestNode")])
    gw = _make_gateway(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="aabbccdd1122", name="TestNode", label="test")

    status = wq.management_path_status(node)

    assert status["route"] == "stored"
    assert status["path_repair_needed"] is False
    assert status["cached_path"] == "stored (out_path=e260,5f6c; out_path_len=2; hash-mode=1)"
    assert status["out_path"] == "e260,5f6c"
    gw.send_command.assert_not_awaited()


async def test_management_path_status_marks_flood_as_repair_needed(db):
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "", "out_path_len": -1, "out_path_hash_mode": 0})
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbcc", label="TestNode")])
    gw = _make_gateway(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="aabbccdd1122", name="TestNode", label="test")

    status = wq.management_path_status(node)

    assert status["route"] == "flood"
    assert status["cached_path"] == "flood"
    assert status["path_repair_needed"] is True


async def test_discover_nodes_runs_bounded_gateway_discovery(db):
    cfg = _make_config()
    gw = _make_gateway()
    gw.discover.return_value = [{
        "pubkey": "8e4a0d73" + "0" * 56,
        "node_type": 2,
        "SNR_in": 12.5,
        "tag": "00000000",
    }]
    wq = WorkQueue(cfg, gw, db)
    messages: list[str] = []

    async def progress(message: str) -> None:
        messages.append(message)

    result = await wq.discover_nodes((915.8, 250.0, 10, 5), dwell=3.0, progress=progress)

    assert result["radio"]["text"] == "915.800/250/SF10/CR5"
    assert result["nodes"][0]["pubkey"].startswith("8e4a0d73")
    gw.discover.assert_awaited_once_with(factory_freq=(915.8, 250.0, 10, 5), dwell=3.0)
    assert messages == [
        "discovering on 915.800/250/SF10/CR5 for 3s",
        "discovered 1 node(s)",
    ]


async def test_discover_nodes_annotates_config_and_managed_matches(db):
    managed_pubkey = "75ab85174506" + "0" * 52
    config_only_pubkey = "8e4a0d73ab61" + "0" * 52
    await db.upsert_node(Node(
        pubkey=managed_pubkey,
        name="Known",
        label="qh-test",
        state=NodeState.CONFIGURED,
    ))
    cfg = _make_config(node_cfgs=[
        _node(pubkey="75ab85174506", label="qh-test"),
        _node(pubkey="8e4a0d73", label="faulkland"),
    ])
    gw = _make_gateway()
    gw.discover.return_value = [
        {"pubkey": config_only_pubkey, "node_type": 2, "SNR_in": 6.8},
        {"pubkey": managed_pubkey, "node_type": 2, "SNR_in": 6.2},
    ]
    wq = WorkQueue(cfg, gw, db)

    result = await wq.discover_nodes((915.8, 250.0, 11, 5), dwell=3.0)
    by_prefix = {node["pubkey"][:12]: node for node in result["nodes"]}

    assert by_prefix["8e4a0d73ab61"]["status"] == "config-only"
    assert by_prefix["8e4a0d73ab61"]["label"] == "faulkland"
    assert by_prefix["75ab85174506"]["status"] == "managed"
    assert by_prefix["75ab85174506"]["label"] == "qh-test"


async def test_adopt_node_adds_db_node_direct_route_and_confirms(db):
    pubkey = "8e4a0d73ab61" + "0" * 52
    cfg = _make_config(node_cfgs=[_node(pubkey="8e4a0d73", label="faulkland")])
    gw = _make_gateway(cmd_text="v1.14.1")
    wq = WorkQueue(cfg, gw, db)

    result = await wq.adopt_node(pubkey, radio=(915.8, 250.0, 11, 5))

    node = await db.get_node(pubkey)
    assert node is not None
    assert node.label == "faulkland"
    assert result["matched_config"] is True
    assert result["contact_added"] is True
    assert result["favourited"] is True
    # The flood contact is converted to a zero-hop direct route (the keystone).
    assert result["route"] == "direct"
    assert result["path_repair_needed"] is False
    gw.add_commission_contact.assert_awaited_once()
    gw.set_contact_favourite.assert_awaited_once()
    # Direct (zero-hop) route installed: empty path, hash mode 0.
    gw.change_contact_path.assert_awaited_once()
    assert gw.change_contact_path.await_args.args[1] == ""
    assert gw.change_contact_path.await_args.args[2] == 0
    # Confirm probe runs on the discovered radio, then restores the home radio.
    gw.switch_radio_temporarily.assert_awaited_once_with(915.8, 250.0, 11, 5)
    gw.restore_radio.assert_awaited_once_with(915.800, 250, 10, 5)
    probe = gw.send_command.await_args
    assert probe.args[1] == "ver"
    # The directed probe must use the now-direct contact, not flood.
    assert probe.args[0]["out_path_len"] == 0
    assert result["confirm"]["sent"] is True
    assert result["confirm"]["ok"] is True
    assert result["confirm"]["response"] == "v1.14.1"


async def test_adopt_node_confirm_probe_failure_still_restores_radio(db):
    pubkey = "8e4a0d73ab61" + "0" * 52
    cfg = _make_config(node_cfgs=[_node(pubkey="8e4a0d73", label="faulkland")])
    gw = _make_gateway()
    gw.send_command = AsyncMock(
        return_value=CommandResult(success=False, error="timeout — no response from node")
    )
    wq = WorkQueue(cfg, gw, db)

    result = await wq.adopt_node(pubkey, radio=(915.8, 250.0, 11, 5))

    # Route is still installed even if the confirming probe fails.
    assert result["route"] == "direct"
    assert result["confirm"]["sent"] is True
    assert result["confirm"]["ok"] is False
    assert "timeout" in result["confirm"]["response"]
    # Radio is always restored, even on probe failure.
    gw.restore_radio.assert_awaited_once_with(915.800, 250, 10, 5)


async def test_adopt_node_emits_progress(db):
    pubkey = "8e4a0d73ab61" + "0" * 52
    cfg = _make_config(node_cfgs=[_node(pubkey="8e4a0d73", label="faulkland")])
    gw = _make_gateway(cmd_text="v1.14.1")
    wq = WorkQueue(cfg, gw, db)
    messages: list[str] = []

    async def collect(msg: str) -> None:
        messages.append(msg)

    await wq.adopt_node(pubkey, radio=(915.8, 250.0, 11, 5), progress=collect)

    joined = "\n".join(messages)
    assert "direct (zero-hop) route" in joined
    assert "switching gateway radio to 915.800/250/SF11/CR5" in joined
    assert "sending directed `ver`" in joined
    assert "restoring gateway radio" in joined
    assert "confirm probe ok" in joined


async def test_adopt_node_requires_full_pubkey(db):
    wq = WorkQueue(_make_config(), _make_gateway(), db)

    with pytest.raises(ValueError, match="full 64-character"):
        await wq.adopt_node("8e4a0d73ab61", radio=(915.8, 250.0, 11, 5))


async def test_probe_node_sends_directed_ver_on_radio(db):
    contact = _contact("8e4a0d73ab61")
    contact.update({"out_path": "", "out_path_len": 0, "out_path_hash_mode": 0})
    gw = _make_gateway(cmd_text="v1.14.1")
    wq = WorkQueue(_make_config(), gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="8e4a0d73ab61" + "0" * 52, name="", label="faulkland")

    result = await wq.probe_node(node, radio=(915.8, 250.0, 11, 5))

    gw.switch_radio_temporarily.assert_awaited_once_with(915.8, 250.0, 11, 5)
    gw.restore_radio.assert_awaited_once_with(915.800, 250, 10, 5)
    assert gw.send_command.await_args.args[1] == "ver"
    gw.advert.assert_not_awaited()
    assert result["advert_sent"] is False
    assert result["ok"] is True
    assert result["response"] == "v1.14.1"
    assert result["route"] == "direct"


async def test_probe_node_advert_first_sends_advert_before_ver(db, monkeypatch):
    import mch.queue as queue_module
    monkeypatch.setattr(queue_module, "_ADVERT_SETTLE_SECONDS", 0)

    contact = _contact("8e4a0d73ab61")
    contact.update({"out_path": "", "out_path_len": 0, "out_path_hash_mode": 0})
    gw = _make_gateway(cmd_text="v1.14.1")
    order: list[str] = []
    gw.advert = AsyncMock(side_effect=lambda *a, **k: order.append("advert"))
    gw.send_command = AsyncMock(
        side_effect=lambda *a, **k: order.append("ver")
        or CommandResult(success=True, text="v1.14.1")
    )
    wq = WorkQueue(_make_config(), gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="8e4a0d73ab61" + "0" * 52, name="", label="faulkland")

    result = await wq.probe_node(node, radio=(915.8, 250.0, 11, 5), advert=True)

    gw.advert.assert_awaited_once_with(zerohop=True)
    assert order == ["advert", "ver"]  # advert must precede the directed probe
    assert result["advert_sent"] is True
    assert result["ok"] is True


async def test_probe_node_records_ver_fact_on_success(db):
    contact = _contact("8e4a0d73ab61")
    contact.update({"out_path": "", "out_path_len": 0, "out_path_hash_mode": 0})
    gw = _make_gateway(cmd_text="v1.15.0-dee3e26 (Build: 19-Apr-2026)")
    wq = WorkQueue(_make_config(), gw, db)
    wq._contacts = [contact]
    pubkey = "8e4a0d73ab61" + "0" * 52
    node = Node(pubkey=pubkey, name="", label="faulkland")

    result = await wq.probe_node(node, radio=(915.8, 250.0, 11, 5))

    assert result["ok"] is True
    facts = await db.get_fact_records(pubkey)
    ver = next((f for f in facts if f.key == "ver"), None)
    assert ver is not None
    assert ver.value == "v1.15.0-dee3e26 (Build: 19-Apr-2026)"


async def test_probe_node_records_no_fact_on_failure(db):
    contact = _contact("8e4a0d73ab61")
    contact.update({"out_path": "", "out_path_len": 0, "out_path_hash_mode": 0})
    gw = _make_gateway()
    gw.send_command = AsyncMock(
        return_value=CommandResult(success=False, error="timeout — no response from node")
    )
    wq = WorkQueue(_make_config(), gw, db)
    wq._contacts = [contact]
    pubkey = "8e4a0d73ab61" + "0" * 52
    node = Node(pubkey=pubkey, name="", label="faulkland")

    await wq.probe_node(node, radio=(915.8, 250.0, 11, 5))

    facts = await db.get_fact_records(pubkey)
    assert not any(f.key == "ver" for f in facts)


async def test_probe_node_without_contact_raises(db):
    wq = WorkQueue(_make_config(), _make_gateway(), db)
    wq._contacts = []
    node = Node(pubkey="8e4a0d73ab61" + "0" * 52, name="", label="faulkland")

    with pytest.raises(ValueError, match="no gateway contact"):
        await wq.probe_node(node, radio=(915.8, 250.0, 11, 5))


async def test_probe_node_restores_radio_on_failure(db):
    contact = _contact("8e4a0d73ab61")
    contact.update({"out_path": "", "out_path_len": 0, "out_path_hash_mode": 0})
    gw = _make_gateway()
    gw.send_command = AsyncMock(
        return_value=CommandResult(success=False, error="timeout — no response from node")
    )
    wq = WorkQueue(_make_config(), gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="8e4a0d73ab61" + "0" * 52, name="", label="faulkland")

    result = await wq.probe_node(node, radio=(915.8, 250.0, 11, 5))

    assert result["ok"] is False
    assert "timeout" in result["response"]
    gw.restore_radio.assert_awaited_once_with(915.800, 250, 10, 5)


async def test_probe_node_login_authenticates_before_ver(db):
    contact = _contact("8e4a0d73ab61")
    contact.update({"out_path": "", "out_path_len": 0, "out_path_hash_mode": 0})
    gw = _make_gateway()
    order: list[str] = []
    gw.login = AsyncMock(side_effect=lambda *a, **k: order.append("login") or True)
    gw.send_command = AsyncMock(
        side_effect=lambda *a, **k: order.append("ver")
        or CommandResult(success=True, text="v1.15.0")
    )
    cfg = _make_config()
    cfg.secrets = {"8e4a0d73": NodeSecrets(password="newadminpw")}
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="8e4a0d73ab61" + "0" * 52, name="", label="faulkland")

    result = await wq.probe_node(node, radio=(915.8, 250.0, 11, 5), login=True)

    gw.login.assert_awaited_once_with(contact, "newadminpw")
    assert order == ["login", "ver"]  # must authenticate before the directed probe
    assert result["login_attempted"] is True
    assert result["login_ok"] is True
    assert result["ok"] is True
    assert result["response"] == "v1.15.0"


async def test_probe_node_login_without_password_skips_login(db):
    contact = _contact("8e4a0d73ab61")
    contact.update({"out_path": "", "out_path_len": 0, "out_path_hash_mode": 0})
    gw = _make_gateway(cmd_text="v1.15.0")
    cfg = _make_config()
    cfg.secrets = {}  # no password configured for this node
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="8e4a0d73ab61" + "0" * 52, name="", label="faulkland")

    result = await wq.probe_node(node, radio=(915.8, 250.0, 11, 5), login=True)

    gw.login.assert_not_awaited()
    assert result["login_attempted"] is False
    assert result["login_ok"] is None


async def test_probe_reachable_logs_in_when_bare_probe_silently_fails(db):
    """Auth-required node that silently drops unauthenticated ver: probe must log
    in once (entering the node ACL) and re-probe, rather than report unreachable."""
    contact = _contact("8e4a0d73ab61")
    contact.update({"out_path": "", "out_path_len": 0, "out_path_hash_mode": 0})  # direct
    gw = _make_gateway()
    state = {"authed": False}

    async def _send(c, cmd, **k):
        if state["authed"]:
            return CommandResult(success=True, text="v1.15.0")
        return CommandResult(success=False, error="timeout — no response from node")

    async def _login(c, pw):
        state["authed"] = True
        return True

    gw.send_command = AsyncMock(side_effect=_send)
    gw.login = AsyncMock(side_effect=_login)
    cfg = _make_config(node_cfgs=[_node(pubkey="8e4a0d73", label="faulkland")])
    cfg.secrets = {"8e4a0d73": NodeSecrets(password="newadminpw")}
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="8e4a0d73ab61" + "0" * 52, name="", label="faulkland", auth_cached=False)

    res = await wq._probe_reachable(contact, node, cfg.nodes[0])

    assert res is not None and res.success
    gw.login.assert_awaited_once_with(contact, "newadminpw")
    assert node.auth_cached is True


async def test_probe_reachable_no_login_when_already_auth_cached(db):
    """A node already authenticated this session that stops answering is genuinely
    unreachable - do not spend a login (that only fires on an explicit perm error)."""
    contact = _contact("8e4a0d73ab61")
    contact.update({"out_path": "", "out_path_len": 0, "out_path_hash_mode": 0})
    gw = _make_gateway()
    gw.send_command = AsyncMock(
        return_value=CommandResult(success=False, error="timeout — no response from node")
    )
    cfg = _make_config(node_cfgs=[_node(pubkey="8e4a0d73", label="faulkland")])
    cfg.secrets = {"8e4a0d73": NodeSecrets(password="newadminpw")}
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="8e4a0d73ab61" + "0" * 52, name="", label="faulkland", auth_cached=True)

    res = await wq._probe_reachable(contact, node, cfg.nodes[0])

    assert res is None
    gw.login.assert_not_awaited()


async def test_probe_reachable_no_login_when_no_password(db):
    """No password configured: nothing to log in with, so don't attempt it."""
    contact = _contact("8e4a0d73ab61")
    contact.update({"out_path": "", "out_path_len": 0, "out_path_hash_mode": 0})
    gw = _make_gateway()
    gw.send_command = AsyncMock(
        return_value=CommandResult(success=False, error="timeout — no response from node")
    )
    cfg = _make_config(node_cfgs=[_node(pubkey="8e4a0d73", label="faulkland")])
    cfg.secrets = {}  # no password
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="8e4a0d73ab61" + "0" * 52, name="", label="faulkland", auth_cached=False)

    res = await wq._probe_reachable(contact, node, cfg.nodes[0])

    assert res is None
    gw.login.assert_not_awaited()


async def test_management_path_discover_returns_candidate_without_applying(db):
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "", "out_path_len": -1, "out_path_hash_mode": 0})
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbcc", label="TestNode")])
    gw = _make_gateway(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="aabbccdd1122", name="TestNode", label="test")

    candidate = await wq.discover_management_path(node)

    assert candidate["path"] == "e260,5f6c"
    assert candidate["path_hash_mode"] == 1
    assert candidate["apply_value"] == "e260,5f6c:1"
    assert contact["out_path_len"] == -1
    gw.discover_contact_path.assert_awaited_once_with(contact)
    gw.change_contact_path.assert_not_awaited()


async def test_management_path_apply_updates_gateway_contact_and_logs(db):
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "", "out_path_len": -1, "out_path_hash_mode": 0})
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbcc", label="TestNode")])
    gw = _make_gateway(contacts=[contact])

    async def _change_path(changed_contact, path, hash_mode):
        changed_contact["out_path"] = path
        changed_contact["out_path_len"] = 1
        changed_contact["out_path_hash_mode"] = hash_mode

    gw.change_contact_path.side_effect = _change_path
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="aabbccdd1122", name="TestNode", label="test")
    await db.upsert_node(node)

    status = await wq.apply_management_path(node, "e260:1")

    assert status["route"] == "stored"
    assert status["out_path"] == "e260"
    assert status["out_path_hash_mode"] == 1
    gw.change_contact_path.assert_awaited_once_with(contact, "e260", 1)
    logs = await db.get_logs(node.pubkey)
    assert "management path changed from flood to e260:1" in logs[0]["message"]


async def test_management_path_apply_rejects_wrong_mode_length(db):
    contact = _contact("aabbccdd1122")
    contact.update({"out_path": "", "out_path_len": -1, "out_path_hash_mode": 0})
    cfg = _make_config(node_cfgs=[_node(pubkey="aabbcc", label="TestNode")])
    gw = _make_gateway(contacts=[contact])
    wq = WorkQueue(cfg, gw, db)
    wq._contacts = [contact]
    node = Node(pubkey="aabbccdd1122", name="TestNode", label="test")

    with pytest.raises(ValueError, match="multiple of 4"):
        await wq.apply_management_path(node, "e2:1")

    gw.change_contact_path.assert_not_awaited()


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
        CommandResult(success=True, text="v1.14.1"),  # reachability probe
        CommandResult(success=True, text="off"),   # get powersaving — drift
        CommandResult(success=True, text="ok"),    # set powersaving on — success
    ])
    wq = WorkQueue(cfg, gw, db)
    await wq.run_cycle()
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.CONFIGURED


async def test_cycle_state_partial_when_setting_cannot_be_read(db):
    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    # probe succeeds so the node is reachable; the setting read then times out
    gw.send_command = AsyncMock(side_effect=_probe_ok_else_timeout)
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.PARTIAL
    assert results[0].settings_failed == 1


async def test_setting_read_timeout_is_retained_in_activity_log(db):
    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    # probe succeeds so the node is reachable; the setting read then times out
    gw.send_command = AsyncMock(side_effect=_probe_ok_else_timeout)
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()

    logs = await db.get_logs("aabbccdd1122")
    assert any("read powersaving failed: timeout" in entry["message"] for entry in logs)


async def test_fact_read_timeout_is_retained_in_activity_log(db):
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    # 'board' so the probe ('ver') succeeds but the fact read still times out
    cfg.facts = ["board"]
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=_probe_ok_else_timeout)
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()

    logs = await db.get_logs("aabbccdd1122")
    assert any("fact board failed: timeout" in entry["message"] for entry in logs)


async def test_cycle_state_configured_when_audit_setting_cannot_be_read(db):
    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        settings={"powersaving": SettingConfig(desired="on", policy=SettingPolicy.AUDIT)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    # probe succeeds so the node is reachable; the audit setting read then times out
    gw.send_command = AsyncMock(side_effect=_probe_ok_else_timeout)
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.CONFIGURED
    assert results[0].settings_failed == 1
    assert results[0].enforced_settings_failed == 0


async def test_cycle_state_partial_when_node_processing_raises(db):
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["ver"]
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=RuntimeError("serial failure"))
    wq = WorkQueue(cfg, gw, db)

    with pytest.raises(RuntimeError, match="serial failure"):
        await wq.run_cycle()

    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.PARTIAL
    logs = await db.get_logs(node.pubkey)
    assert "sync failed before completion: serial failure" in logs[0]["message"]


async def test_cycle_state_partial_when_node_processing_is_cancelled(db):
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["ver"]
    started = asyncio.Event()

    async def blocked_send(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=blocked_send)
    wq = WorkQueue(cfg, gw, db)
    task = asyncio.create_task(wq.run_cycle())
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.PARTIAL
    logs = await db.get_logs(node.pubkey)
    assert logs[0]["message"] == "sync interrupted before completion"


async def test_stop_cancels_forced_sync_and_restores_temporary_radio(db):
    from mch.config import RadioConfig

    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        via_radio=RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8),
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["ver"]
    started = asyncio.Event()

    async def blocked_send(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.info = {"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 5}
    gw.switch_radio_temporarily = AsyncMock(return_value=(869.618, 62.5, 8, 5))
    gw.send_command = AsyncMock(side_effect=blocked_send)
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())
    wq._contacts = [contact]

    task = asyncio.create_task(wq.run_node("aabbcc"))
    await started.wait()
    await wq.stop()

    with pytest.raises(asyncio.CancelledError):
        await task
    gw.restore_radio.assert_awaited_once_with(869.618, 62.5, 8, 5)
    assert not wq._operation_lock.locked()
    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.PARTIAL
    logs = await db.get_logs(node.pubkey)
    assert any(entry["message"] == "manual sync aborted" for entry in logs)


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
        CommandResult(success=True, text="v1.14.1"),  # reachability probe
        CommandResult(success=True, text="off"),   # get powersaving — drift
        CommandResult(success=True, text="ok"),    # set powersaving on — success
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())
    await wq.run_cycle()
    settings = await db.get_settings("aabbccdd1122")
    ps = next(s for s in settings if s.key == "powersaving")
    assert ps.observed == "on"
    assert ps.drift is False


async def test_successful_enforcement_records_setting_history(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"radio": SettingConfig(desired="915.800,250,11,5", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="v1.15.0"),
        CommandResult(success=True, text="915.800,250,10,5"),
        CommandResult(success=True, text="OK - reboot to apply"),
    ])
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()

    history = await db.get_setting_history("aabbccdd1122")
    assert len(history) == 1
    assert history[0]["key"] == "radio"
    assert history[0]["previous"] == "915.800,250,10,5"
    assert history[0]["new_value"] == "915.800,250,11,5"
    assert history[0]["command"] == "set radio 915.800,250,11,5"


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
# temporary gateway radio switching
# ------------------------------------------------------------------

async def test_node_on_different_freq_switches_radio(db):
    """A node with another radio tuple causes a temporary gateway switch and restore."""
    from mch.config import RadioConfig
    node_cfg = _node(pubkey="aabbcc", label="TestNode",
                     via_radio=RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8))
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.info = {"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 5}
    gw.switch_radio_temporarily = AsyncMock(return_value=(869.618, 62.5, 8, 5))
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()
    gw.switch_radio_temporarily.assert_awaited_once_with(916.575, 62.5, 7, 8)
    gw.restore_radio.assert_awaited_once_with(869.618, 62.5, 8, 5)


async def test_node_on_same_channel_with_different_cr_does_not_switch_radio(db):
    """CR is a sender preference; a receiver remains reachable with another CR."""
    from mch.config import RadioConfig
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        via_radio=RadioConfig(freq=915.800, bw=250, sf=10, cr=8),
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.info = {"radio_freq": 915.800, "radio_bw": 250, "radio_sf": 10, "radio_cr": 5}
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()

    gw.switch_radio_temporarily.assert_not_awaited()


async def test_node_on_same_freq_with_different_sf_switches_radio(db):
    """Frequency, bandwidth, and SF must match to contact a node."""
    from mch.config import RadioConfig
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        via_radio=RadioConfig(freq=915.800, bw=250, sf=9, cr=8),
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.info = {"radio_freq": 915.800, "radio_bw": 250, "radio_sf": 10, "radio_cr": 5}
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()

    gw.switch_radio_temporarily.assert_awaited_once_with(915.800, 250, 9, 8)


async def test_node_inherits_group_via_radio(db):
    """A group via_radio selects the gateway radio when the node has no override."""
    from mch.config import RadioConfig
    node_cfg = _node(pubkey="aabbcc", label="TestNode", group="g")
    group_cfg = _group(via_radio=RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8))
    cfg = _make_config(node_cfgs=[node_cfg], group_cfgs=[group_cfg])
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.info = {"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 5}
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()

    gw.switch_radio_temporarily.assert_awaited_once_with(916.575, 62.5, 7, 8)


async def test_node_via_radio_overrides_group_via_radio(db):
    from mch.config import RadioConfig
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        group="g",
        via_radio=RadioConfig(freq=917.000, bw=125, sf=8, cr=5),
    )
    group_cfg = _group(via_radio=RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8))
    cfg = _make_config(node_cfgs=[node_cfg], group_cfgs=[group_cfg])
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.info = {"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 5}
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()

    gw.switch_radio_temporarily.assert_awaited_once_with(917.000, 125, 8, 5)


async def test_node_inherits_global_via_radio(db):
    """A global via_radio applies when neither group nor node overrides it."""
    from mch.config import RadioConfig
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.global_settings.via_radio = RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8)
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.info = {"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 5}
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()

    gw.switch_radio_temporarily.assert_awaited_once_with(916.575, 62.5, 7, 8)


async def test_group_via_radio_overrides_global_via_radio(db):
    from mch.config import RadioConfig
    node_cfg = _node(pubkey="aabbcc", label="TestNode", group="g")
    group_cfg = _group(via_radio=RadioConfig(freq=917.000, bw=125, sf=8, cr=5))
    cfg = _make_config(node_cfgs=[node_cfg], group_cfgs=[group_cfg])
    cfg.global_settings.via_radio = RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8)
    cfg.facts = []
    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.info = {"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 5}
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()

    gw.switch_radio_temporarily.assert_awaited_once_with(917.000, 125, 8, 5)


async def test_node_no_radio_config_no_switch(db):
    """A node without another radio tuple never switches the gateway radio."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()
    gw.switch_radio_temporarily.assert_not_awaited()
    gw.restore_radio.assert_not_awaited()


def test_radio_recovery_candidates_try_exact_before_nearby_sf_and_other_config_radios():
    node_cfg = _node(
        pubkey="aabbcc",
        settings={"radio": SettingConfig(desired="915.800,250,11,5")},
    )
    other = _node(pubkey="bbccdd", via_radio=RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8))
    cfg = _make_config(node_cfgs=[node_cfg, other])

    candidates = radio_recovery_candidates(
        cfg,
        node_cfg,
        None,
        cfg.global_settings,
        tried_channels={(915.075, 125.0, 9)},
    )

    assert [(c.freq, c.bw, c.sf, c.cr) for c in candidates] == [
        (915.800, 250.0, 11, 5),
        (915.800, 250.0, 10, 5),
        (915.800, 250.0, 12, 5),
    ]


def test_radio_recovery_candidates_ignore_profile_presets():
    from mch.recommend import RadioPreset

    node_cfg = _node(
        pubkey="aabbcc",
        settings={"radio": SettingConfig(desired="915.800,250,11,5")},
    )
    cfg = _make_config(node_cfgs=[node_cfg])

    with patch(
        "mch.recommend.load_radio_presets",
        return_value=[
            RadioPreset(
                name="Bench SF10",
                value="915.800,250,10,5",
                description="Known bench radio",
            )
        ],
    ):
        candidates = radio_recovery_candidates(
            cfg,
            node_cfg,
            None,
            cfg.global_settings,
            tried_channels={(915.075, 125.0, 9)},
        )

    assert [(c.freq, c.bw, c.sf, c.cr, c.source) for c in candidates] == [
        (915.800, 250.0, 11, 5, "effective desired radio"),
        (915.800, 250.0, 10, 5, "effective desired radio SF10 fallback"),
        (915.800, 250.0, 12, 5, "effective desired radio SF12 fallback"),
    ]


def test_radio_recovery_candidates_keep_sf_within_meshcore_bounds():
    low_cfg = _node(
        pubkey="aabbcc",
        settings={"radio": SettingConfig(desired="915.800,250,5,5")},
    )
    high_cfg = _node(
        pubkey="bbccdd",
        settings={"radio": SettingConfig(desired="915.800,250,12,5")},
    )
    cfg = _make_config(node_cfgs=[low_cfg, high_cfg])

    low_candidates = radio_recovery_candidates(
        cfg, low_cfg, None, cfg.global_settings, tried_channels={(915.075, 125.0, 9)}
    )
    high_candidates = radio_recovery_candidates(
        cfg, high_cfg, None, cfg.global_settings, tried_channels={(915.075, 125.0, 9)}
    )

    assert [(c.sf, c.source) for c in low_candidates[:2]] == [
        (5, "effective desired radio"),
        (6, "effective desired radio SF6 fallback"),
    ]
    assert all(c.sf != 4 for c in low_candidates)
    assert [(c.sf, c.source) for c in high_candidates[:2]] == [
        (12, "effective desired radio"),
        (11, "effective desired radio SF11 fallback"),
    ]
    assert all(c.sf != 13 for c in high_candidates)


async def test_unreachable_node_tries_bounded_config_radio_fallback(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"radio": SettingConfig(desired="915.800,250,11,5")},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.info = {"radio_freq": 915.075, "radio_bw": 125, "radio_sf": 9, "radio_cr": 5}

    radio_by_channel = (915.075, 125.0, 9)

    async def _switch(freq, bw, sf, cr):
        nonlocal radio_by_channel
        previous = (*radio_by_channel, 5)
        radio_by_channel = (round(freq, 3), float(bw), int(sf))
        return previous

    async def _restore(freq, bw, sf, cr):
        nonlocal radio_by_channel
        radio_by_channel = (round(freq, 3), float(bw), int(sf))

    async def _send(contact, command, **kwargs):
        if command == "ver" and radio_by_channel == (915.800, 250.0, 10):
            return CommandResult(success=True, text="v1.15.0")
        return CommandResult(success=False, error="timeout")

    gw.switch_radio_temporarily = AsyncMock(side_effect=_switch)
    gw.restore_radio = AsyncMock(side_effect=_restore)
    gw.send_command = AsyncMock(side_effect=_send)
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    result = await wq.run_cycle()

    assert result[0].reachable is True
    assert [call.args for call in gw.switch_radio_temporarily.await_args_list] == [
        (915.800, 250.0, 11, 5),
        (915.800, 250.0, 10, 5),
    ]
    gw.restore_radio.assert_awaited_once_with(915.075, 125.0, 9, 5)
    logs = await db.get_logs("aabbccdd1122")
    assert any("radio fallback reached node on 915.800/250/SF10/CR5" in e["message"] for e in logs)


async def test_radio_recovery_exhaustion_restores_home_once(db):
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"radio": SettingConfig(desired="915.800,250,10,5")},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.info = {"radio_freq": 915.075, "radio_bw": 125, "radio_sf": 9, "radio_cr": 5}
    gw.switch_radio_temporarily = AsyncMock(return_value=(915.075, 125.0, 9, 5))
    gw.restore_radio = AsyncMock()
    gw.send_command = AsyncMock(return_value=CommandResult(success=False, error="timeout"))
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    results = await wq.run_cycle()

    assert results[0].reachable is False
    assert [call.args for call in gw.switch_radio_temporarily.await_args_list] == [
        (915.800, 250.0, 10, 5),
        (915.800, 250.0, 9, 5),
        (915.800, 250.0, 11, 5),
    ]
    gw.restore_radio.assert_awaited_once_with(915.075, 125.0, 9, 5)


async def test_radio_recovery_skipped_when_radio_confirmed(db):
    """A node whose observed radio already matches desired is on the radio we
    expect, so a reachability failure is a path issue - don't hunt SF variants.
    """
    node_cfg = _node(
        pubkey="aabbcc",
        label="TestNode",
        settings={"radio": SettingConfig(desired="915.075,125,9,5")},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.info = {"radio_freq": 915.075, "radio_bw": 125, "radio_sf": 9, "radio_cr": 5}
    gw.switch_radio_temporarily = AsyncMock(return_value=(915.075, 125.0, 9, 5))
    gw.restore_radio = AsyncMock()
    gw.send_command = AsyncMock(return_value=CommandResult(success=False, error="timeout"))

    # Seed a confirmed radio observation: observed matches desired within tolerance
    # (the firmware float form 915.0750122 == 915.075), so the radio is known-good.
    await db.upsert_node(Node(pubkey="aabbccdd1122", name="", label="TestNode"))
    await db.upsert_setting(SettingRecord(
        node_pubkey="aabbccdd1122", key="radio", desired="915.075,125,9,5",
        policy=SettingPolicy.ENFORCE, observed="915.0750122,125,9,5",
    ))

    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())
    results = await wq.run_cycle()

    assert results[0].reachable is False
    gw.switch_radio_temporarily.assert_not_awaited()
    logs = await db.get_logs("aabbccdd1122")
    assert any("radio confirmed" in e["message"] for e in logs)


async def test_radio_restored_on_relogin_failure(db):
    """Radio is restored via finally even when processing raises _ReloginFailed."""
    from mch.config import RadioConfig
    node_cfg = _node(
        pubkey="aabbcc", label="TestNode",
        via_radio=RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8),
        settings={"name": SettingConfig(desired="X", policy=SettingPolicy.ENFORCE)},
    )
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = []
    cfg.secrets = {"aabbcc": NodeSecrets(password="pw")}

    gw = _make_gateway(contacts=[_contact("aabbccdd1122")])
    gw.info = {"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 5}
    gw.switch_radio_temporarily = AsyncMock(return_value=(869.618, 62.5, 8, 5))
    gw.set_contact_favourite = AsyncMock(return_value=True)
    gw.login = AsyncMock(return_value=False)  # re-login fails → _ReloginFailed
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="no permission"))
    wq = WorkQueue(cfg, gw, db)
    await _seed_desired(db, cfg, await gw.get_contacts())

    await wq.run_cycle()
    gw.switch_radio_temporarily.assert_awaited_once()
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
    await _seed_desired(db, cfg, await gw.get_contacts())

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


async def test_recover_interrupted_nodes_marks_configuring_partial(db):
    await db.upsert_node(
        Node(pubkey="aabbccdd1122", name="N", label="L", state=NodeState.CONFIGURING)
    )
    wq = WorkQueue(_make_config(), _make_gateway(), db)

    await wq._recover_interrupted_nodes()

    node = await db.get_node("aabbccdd1122")
    assert node.state == NodeState.PARTIAL
    logs = await db.get_logs(node.pubkey)
    assert "sync interrupted before completion" in logs[0]["message"]


# ---------------------------------------------------------------------------
# neighbors / clock fact collection
# ---------------------------------------------------------------------------

async def test_collect_facts_clock_sends_bare_command(db):
    """'clock' in facts list sends bare 'clock', not 'get clock'."""
    node_cfg = _node(pubkey="aabbcc", label="TestNode")
    cfg = _make_config(node_cfgs=[node_cfg])
    cfg.facts = ["clock"]
    contact = _contact("aabbccdd1122")
    gw = _make_gateway(contacts=[contact])
    gw.send_command = AsyncMock(
        return_value=CommandResult(success=True, text="2026-05-24 00:00:00")
    )
    wq = WorkQueue(cfg, gw, db)

    await wq.run_cycle()

    calls = [call.args[1] for call in gw.send_command.call_args_list]
    assert "clock" in calls
    assert "get clock" not in calls


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

    logs = await db.get_logs()
    assert len(logs) == 1
    assert logs[0]["node_pubkey"] is None
    assert logs[0]["display_pubkey"] == "deadbeef"
    assert logs[0]["display_label"] == "Ghost Node"
    assert logs[0]["level"] == "WARNING"
    assert logs[0]["message"] == "config node not visible to gateway - cannot manage"


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
    # the reachability probe (ver) hits the permission error and drives re-login
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="no permission"),  # probe ver — denied
        CommandResult(success=True, text="v1.14.1"),        # probe ver — after re-login
        CommandResult(success=True, text="FleetNode"),      # get name — matches desired
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
    # the reachability probe (ver) hits the permission error and drives re-login
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="no permission"),  # probe ver — denied
        CommandResult(success=True, text="v1.14.1"),        # probe ver — after re-login
        CommandResult(success=True, text="SpecialNode"),    # get name — matches desired
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
    # the reachability probe (ver) hits the permission error and drives re-login
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="no permission"),  # probe ver — denied
        CommandResult(success=True, text="v1.14.1"),        # probe ver — after re-login
        CommandResult(success=True, text="ShortPrefix"),    # get name — matches desired
    ])
    wq = WorkQueue(cfg, gw, db)

    results = await wq.run_cycle()

    gw.login.assert_awaited_once_with(contact, "correctpassword")
    assert results[0].reachable is True
