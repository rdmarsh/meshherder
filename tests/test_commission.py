"""Tests for mch.commission — discover-and-filter, commissioning sequences, file writes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from mch.config import Config, GatewayConfig, NodeConfig, NodeSecrets, RadioConfig
from mch.gateway import CommandResult
from mch.models import Node, NodeState


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_config(**kwargs) -> Config:
    return Config(gateway=GatewayConfig(), **kwargs)


def make_node(pubkey: str, **kwargs) -> Node:
    return Node(pubkey=pubkey, name="test", **kwargs)


def make_gateway(discovered: list[dict] | None = None) -> MagicMock:
    gw = MagicMock()
    gw.discover = AsyncMock(return_value=discovered or [])
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))
    gw.hop_radio = AsyncMock(return_value=None)
    gw.restore_radio = AsyncMock(return_value=None)
    gw.info = {"radio_freq": 915.075, "radio_bw": 125.0, "radio_sf": 9, "radio_cr": 5}
    return gw


def make_db(nodes: list[Node] | None = None) -> MagicMock:
    db = MagicMock()
    db.get_all_nodes = AsyncMock(return_value=nodes or [])
    return db


def make_wq(gateway=None, db=None) -> MagicMock:
    wq = MagicMock()
    wq._gateway = gateway or make_gateway()
    wq.pause = MagicMock()
    wq.resume = MagicMock()
    return wq


def discover_payload(pubkey: str, node_type: int = 2) -> dict:
    return {"pubkey": pubkey + "0" * (64 - len(pubkey)), "node_type": node_type, "SNR_in": 10.0}


# ---------------------------------------------------------------------------
# discover_unmanaged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_unmanaged_returns_empty_when_no_nodes():
    """Empty discover result → empty unmanaged list."""
    from mch.commission import discover_unmanaged

    gw = make_gateway(discovered=[])
    db = make_db()
    cfg = make_config()

    result = await discover_unmanaged(gw, db, cfg, dwell=0.01)
    assert result == []


@pytest.mark.asyncio
async def test_discover_unmanaged_filters_config_known_nodes():
    """Nodes whose pubkey matches a config pubkey_prefix are excluded."""
    from mch.commission import discover_unmanaged

    known_pubkey = "aa" * 32
    gw = make_gateway(discovered=[discover_payload(known_pubkey[:64])])
    db = make_db()
    cfg = make_config(nodes=[NodeConfig(pubkey_prefix="aa" * 4)])

    result = await discover_unmanaged(gw, db, cfg, dwell=0.01)
    assert result == []


@pytest.mark.asyncio
async def test_discover_unmanaged_filters_db_known_nodes():
    """Nodes whose pubkey is in the DB are excluded."""
    from mch.commission import discover_unmanaged

    known_pubkey = "bb" * 32
    gw = make_gateway(discovered=[discover_payload(known_pubkey)])
    db = make_db(nodes=[make_node(known_pubkey)])
    cfg = make_config()

    result = await discover_unmanaged(gw, db, cfg, dwell=0.01)
    assert result == []


@pytest.mark.asyncio
async def test_discover_unmanaged_returns_truly_new_node():
    """Nodes not in config or DB are returned."""
    from mch.commission import discover_unmanaged

    new_pubkey = "cc" * 32
    payload = discover_payload(new_pubkey)
    gw = make_gateway(discovered=[payload])
    db = make_db()
    cfg = make_config()

    result = await discover_unmanaged(gw, db, cfg, dwell=0.01)
    assert len(result) == 1
    assert result[0]["pubkey"] == new_pubkey


# ---------------------------------------------------------------------------
# handle_commission — discovery-level rejections
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_invalid_mode_returns_error():
    """handle_commission rejects unknown mode values."""
    from mch.commission import handle_commission

    wq = make_wq()
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({"cmd": "commission", "mode": "bad"}, wq, db, cfg)
    assert resp["ok"] is False
    assert "mode" in resp["error"]


@pytest.mark.asyncio
async def test_commission_no_nodes_found_returns_error():
    """Zero unmanaged nodes discovered → descriptive error."""
    from mch.commission import handle_commission

    gw = make_gateway(discovered=[])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({"cmd": "commission", "mode": "new"}, wq, db, cfg)
    assert resp["ok"] is False
    assert "no unmanaged" in resp["error"]


@pytest.mark.asyncio
async def test_commission_multiple_nodes_returns_error():
    """More than one unmanaged node discovered → descriptive error."""
    from mch.commission import handle_commission

    gw = make_gateway(discovered=[
        discover_payload("aa" * 32),
        discover_payload("bb" * 32),
    ])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({"cmd": "commission", "mode": "new"}, wq, db, cfg)
    assert resp["ok"] is False
    assert "2" in resp["error"]
    assert "commission one at a time" in resp["error"]


# ---------------------------------------------------------------------------
# handle_commission — queue pause/resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_pauses_and_resumes_queue():
    """Queue is always paused before discover and resumed after."""
    from mch.commission import handle_commission

    gw = make_gateway(discovered=[])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({"cmd": "commission", "mode": "new"}, wq, db, cfg)

    wq.pause.assert_called_once()
    wq.resume.assert_called_once()


@pytest.mark.asyncio
async def test_commission_resumes_queue_on_discover_error():
    """Queue is resumed even when gateway.discover() raises."""
    from mch.commission import handle_commission

    gw = make_gateway()
    gw.discover = AsyncMock(side_effect=RuntimeError("serial exploded"))
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({"cmd": "commission", "mode": "new"}, wq, db, cfg)

    assert resp["ok"] is False
    wq.resume.assert_called_once()


# ---------------------------------------------------------------------------
# handle_commission — "new" mode command sequence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_new_sends_password():
    """'new' mode sends 'password <pw>' command."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "hunter2",
        "name": "TestNode", "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any("password hunter2" in c for c in sent)


@pytest.mark.asyncio
async def test_commission_new_sends_name():
    """'new' mode sends 'set name <name>' command."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "name": "Quakers Hill North", "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any("set name Quakers Hill North" in c for c in sent)


@pytest.mark.asyncio
async def test_commission_new_sends_radio():
    """'new' mode sends 'set radio <freq>' command."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "name": "TestNode", "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any("set radio 915.075,125,9,5" in c for c in sent)


@pytest.mark.asyncio
async def test_commission_new_sends_guest_password():
    """'new' mode sends 'set guest.password' when configured."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "admin", "guest_password": "guest",
        "name": "TestNode", "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any("set guest.password guest" in c for c in sent)


@pytest.mark.asyncio
async def test_commission_new_sends_prv_key():
    """'new' mode sends 'set prv.key <hex>' command."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "name": "TestNode", "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any(c.startswith("set prv.key ") for c in sent)


@pytest.mark.asyncio
async def test_commission_new_sends_reboot_last():
    """'new' mode always sends reboot as the final command."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "admin",
        "name": "TestNode", "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert sent[-1] == "reboot"


@pytest.mark.asyncio
async def test_commission_new_sends_radio_before_prv_key():
    """'new' mode sends set radio before set prv.key so node boots on right freq."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "name": "TestNode", "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    radio_idx = next(i for i, c in enumerate(sent) if c.startswith("set radio"))
    prv_idx = next(i for i, c in enumerate(sent) if c.startswith("set prv.key"))
    assert radio_idx < prv_idx


@pytest.mark.asyncio
async def test_commission_new_returns_factory_prefix_and_prv_key():
    """'new' mode returns factory_prefix and prv_key in response data."""
    from mch.commission import handle_commission

    pubkey = "a695e2be23f2" + "0" * 52
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "name": "TestNode", "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    assert resp["ok"] is True
    assert resp["data"]["factory_prefix"] == pubkey[:8]
    assert len(resp["data"]["prv_key"]) == 64  # 32 bytes → 64 hex chars


# ---------------------------------------------------------------------------
# handle_commission — file writing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_new_does_not_write_config(tmp_path):
    """'new' mode does not write config.yaml (pubkey unknown until after reboot)."""
    from mch.commission import handle_commission

    config_path = tmp_path / "config.yaml"
    original = "gateway: {}\n"
    config_path.write_text(original)
    secrets_path = tmp_path / "secrets.yaml"

    pubkey = "a695e2be" + "0" * 56
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()
    cfg.config_path = config_path
    cfg.secrets_path = secrets_path

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "label": "Test Node",
        "name": "Test Node",
        "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    assert config_path.read_text() == original


@pytest.mark.asyncio
async def test_commission_new_writes_prv_key_to_secrets(tmp_path):
    """'new' mode saves generated prv_key to secrets.yaml under factory prefix."""
    from mch.commission import handle_commission

    config_path = tmp_path / "config.yaml"
    config_path.write_text("gateway: {}\n")
    secrets_path = tmp_path / "secrets.yaml"

    pubkey = "a695e2be" + "0" * 56
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()
    cfg.config_path = config_path
    cfg.secrets_path = secrets_path

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "testpass",
        "name": "Test Node",
        "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    secrets = yaml.safe_load(secrets_path.read_text())
    factory_prefix = resp["data"]["factory_prefix"]
    entry = secrets["nodes"][factory_prefix]
    assert entry["prv_key"] == resp["data"]["prv_key"]
    assert entry["password"] == "testpass"


# ---------------------------------------------------------------------------
# handle_commission — "replace" mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_replace_missing_config_entry_returns_error():
    """'replace' mode returns error if pubkey_prefix not in config."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()  # no nodes in config

    resp = await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    assert resp["ok"] is False
    assert "aabbccdd" in resp["error"]


@pytest.mark.asyncio
async def test_commission_replace_missing_prv_key_returns_error():
    """'replace' mode returns error if secrets has no prv_key for this prefix."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd")],
        secrets={"aabbccdd": NodeSecrets(password="pw")},  # no prv_key
    )

    resp = await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    assert resp["ok"] is False
    assert "prv_key" in resp["error"]


@pytest.mark.asyncio
async def test_commission_replace_sends_prv_key():
    """'replace' mode sends 'set prv.key <hex>' from secrets.yaml."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd", label="Old Node")],
        secrets={"aabbccdd": NodeSecrets(password="pw", prv_key="deadbeef" * 8)},
    )

    resp = await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    assert resp["ok"] is True
    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any("set prv.key deadbeef" in c for c in sent)


@pytest.mark.asyncio
async def test_commission_replace_sends_password_from_secrets():
    """'replace' mode sends password from secrets.yaml."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd")],
        secrets={"aabbccdd": NodeSecrets(password="nodepass", prv_key="deadbeef" * 8)},
    )

    await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any("password nodepass" in c for c in sent)


@pytest.mark.asyncio
async def test_commission_replace_sends_radio_from_config_via():
    """'replace' mode sends set radio command from config node's via entry."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(
            pubkey_prefix="aabbccdd",
            via=RadioConfig(freq=915.075, bw=125.0, sf=9, cr=5),
        )],
        secrets={"aabbccdd": NodeSecrets(password="pw", prv_key="deadbeef" * 8)},
    )

    await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any("set radio 915.075" in c for c in sent)


@pytest.mark.asyncio
async def test_commission_replace_sends_reboot_last():
    """'replace' mode always sends reboot as the final command."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd")],
        secrets={"aabbccdd": NodeSecrets(password="pw", prv_key="deadbeef" * 8)},
    )

    await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert sent[-1] == "reboot"


# ---------------------------------------------------------------------------
# WorkQueue pause / resume
# ---------------------------------------------------------------------------

def test_workqueue_starts_unpaused():
    """WorkQueue._paused event is set (unpaused) on init."""
    from mch.queue import WorkQueue
    from mch.config import SchedulerConfig

    cfg = make_config()
    wq = WorkQueue(cfg, MagicMock(), MagicMock())
    assert wq._paused.is_set()


def test_workqueue_pause_clears_event():
    """WorkQueue.pause() clears the _paused event."""
    from mch.queue import WorkQueue

    cfg = make_config()
    wq = WorkQueue(cfg, MagicMock(), MagicMock())
    wq.pause()
    assert not wq._paused.is_set()


def test_workqueue_resume_sets_event():
    """WorkQueue.resume() sets the _paused event."""
    from mch.queue import WorkQueue

    cfg = make_config()
    wq = WorkQueue(cfg, MagicMock(), MagicMock())
    wq.pause()
    wq.resume()
    assert wq._paused.is_set()


# ---------------------------------------------------------------------------
# file write helpers
# ---------------------------------------------------------------------------

def test_append_node_config_creates_nodes_list(tmp_path):
    """_append_node_config adds a node entry to a config with no nodes list."""
    from mch.commission import _append_node_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("gateway: {}\n")

    _append_node_config(
        config_path=config_path,
        pubkey_prefix="a695e2be",
        label="Test Site",
        group="testgroup",
        target_freq_str="915.075,125,9,5",
    )

    written = yaml.safe_load(config_path.read_text())
    assert len(written["nodes"]) == 1
    node = written["nodes"][0]
    assert node["pubkey_prefix"] == "a695e2be"
    assert node["label"] == "Test Site"
    assert node["group"] == "testgroup"
    assert node["via"] == "915.075,125,9,5"


def test_append_node_config_preserves_existing_nodes(tmp_path):
    """_append_node_config appends without removing existing node entries."""
    from mch.commission import _append_node_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("nodes:\n  - pubkey_prefix: existing\n    label: Existing\n")

    _append_node_config(
        config_path=config_path,
        pubkey_prefix="newnode",
        label="New Node",
        group=None,
        target_freq_str=None,
    )

    written = yaml.safe_load(config_path.read_text())
    prefixes = [n["pubkey_prefix"] for n in written["nodes"]]
    assert "existing" in prefixes
    assert "newnode" in prefixes


def test_update_secrets_creates_file_if_missing(tmp_path):
    """_update_secrets creates secrets.yaml if it doesn't exist yet."""
    from mch.commission import _update_secrets

    secrets_path = tmp_path / "secrets.yaml"
    _update_secrets(
        secrets_path=secrets_path,
        pubkey_prefix="a695e2be",
        ns=NodeSecrets(password="testpass"),
    )

    written = yaml.safe_load(secrets_path.read_text())
    assert written["nodes"]["a695e2be"]["password"] == "testpass"


def test_update_secrets_preserves_existing_entries(tmp_path):
    """_update_secrets does not remove existing node entries."""
    from mch.commission import _update_secrets

    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text("nodes:\n  existing: existingpass\n")

    _update_secrets(
        secrets_path=secrets_path,
        pubkey_prefix="newprefix",
        ns=NodeSecrets(password="newpass"),
    )

    written = yaml.safe_load(secrets_path.read_text())
    assert "existing" in written["nodes"]
    assert "newprefix" in written["nodes"]


def test_update_secrets_writes_prv_key(tmp_path):
    """_update_secrets includes prv_key when set."""
    from mch.commission import _update_secrets

    secrets_path = tmp_path / "secrets.yaml"
    _update_secrets(
        secrets_path=secrets_path,
        pubkey_prefix="aa",
        ns=NodeSecrets(password="pw", prv_key="deadbeef" * 8),
    )

    written = yaml.safe_load(secrets_path.read_text())
    assert written["nodes"]["aa"]["prv_key"] == "deadbeef" * 8


def test_update_secrets_file_is_mode_600(tmp_path):
    """_update_secrets sets secrets.yaml to mode 0o600."""
    import os
    from mch.commission import _update_secrets

    secrets_path = tmp_path / "secrets.yaml"
    _update_secrets(
        secrets_path=secrets_path,
        pubkey_prefix="aa",
        ns=NodeSecrets(password="pw"),
    )

    mode = oct(os.stat(secrets_path).st_mode)[-3:]
    assert mode == "600"


# ---------------------------------------------------------------------------
# handle_commission — scan mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_scan_pauses_and_resumes_queue():
    """scan mode pauses and resumes the work queue."""
    from mch.commission import handle_commission

    gw = make_gateway(discovered=[discover_payload("aa" * 32)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({"cmd": "commission", "mode": "scan"}, wq, db, cfg)

    wq.pause.assert_called_once()
    wq.resume.assert_called_once()


@pytest.mark.asyncio
async def test_commission_scan_returns_found_nodes():
    """scan mode returns unmanaged nodes in data.nodes."""
    from mch.commission import handle_commission

    pubkey = "aa" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({"cmd": "commission", "mode": "scan"}, wq, db, cfg)

    assert resp["ok"] is True
    assert len(resp["data"]["nodes"]) == 1
    assert resp["data"]["nodes"][0]["pubkey"] == pubkey


@pytest.mark.asyncio
async def test_commission_scan_returns_gateway_radio_string():
    """scan mode includes gateway_radio formatted string when gateway radio is configured."""
    from mch.commission import handle_commission

    gw = make_gateway(discovered=[])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = Config(gateway=GatewayConfig(radio=RadioConfig(freq=915.075, bw=125.0, sf=9, cr=5)))

    resp = await handle_commission({"cmd": "commission", "mode": "scan"}, wq, db, cfg)

    assert resp["ok"] is True
    assert resp["data"]["gateway_radio"] == "915.075,125,9,5"


@pytest.mark.asyncio
async def test_commission_scan_returns_none_gateway_radio_when_not_configured():
    """scan mode returns gateway_radio=None when gateway has no radio config."""
    from mch.commission import handle_commission

    gw = make_gateway(discovered=[])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()  # default GatewayConfig has radio=None

    resp = await handle_commission({"cmd": "commission", "mode": "scan"}, wq, db, cfg)

    assert resp["ok"] is True
    assert resp["data"]["gateway_radio"] is None


# ---------------------------------------------------------------------------
# handle_commission — radio re-hop after discover
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_new_rehops_to_factory_before_commands():
    """After discover() restores home freq, handle_commission re-hops to factory for commands."""
    from mch.commission import handle_commission
    from mch.gateway import Gateway

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "name": "TestNode", "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    gw.hop_radio.assert_called_once_with(*Gateway.FACTORY_FREQ)


@pytest.mark.asyncio
async def test_commission_new_restores_home_radio_after_commissioning():
    """handle_commission restores the home radio freq after commissioning commands."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "name": "TestNode", "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    gw.restore_radio.assert_called_once_with(915.075, 125.0, 9, 5)


@pytest.mark.asyncio
async def test_commission_replace_rehops_to_factory_before_commands():
    """'replace' mode also re-hops to factory freq before sending commands."""
    from mch.commission import handle_commission
    from mch.gateway import Gateway

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd")],
        secrets={"aabbccdd": NodeSecrets(password="pw", prv_key="deadbeef" * 8)},
    )

    await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    gw.hop_radio.assert_called_once_with(*Gateway.FACTORY_FREQ)
