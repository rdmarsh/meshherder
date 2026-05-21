"""Tests for mch.commission — discover-and-filter, commissioning sequences, paste snippets."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mch.config import Config, GatewayConfig, NodeConfig, NodeSecrets, RadioConfig
from mch.gateway import CommandResult
from mch.models import Node

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
    gw.add_commission_contact = AsyncMock(
        side_effect=lambda ni: {"public_key": ni["pubkey"], "adv_name": ""}
    )
    gw.login = AsyncMock(return_value=True)
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
async def test_commission_new_returns_pubkey_prefix():
    """'new' mode returns pubkey_prefix (first 8 hex chars of factory pubkey) in response data."""
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
    assert resp["data"]["pubkey_prefix"] == pubkey[:8]
    assert "prv_key" not in resp["data"]


# ---------------------------------------------------------------------------
# handle_commission — paste snippets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_new_returns_config_snippet():
    """'new' mode returns a config.yaml snippet with pubkey_prefix, label, group, and via."""
    from mch.commission import handle_commission

    pubkey = "a695e2be" + "0" * 56
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "label": "Test Node",
        "group": "testgroup",
        "name": "Test Node",
        "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    assert resp["ok"]
    snippet = resp["data"]["config_snippet"]
    assert "pubkey_prefix: a695e2be" in snippet
    assert "label: Test Node" in snippet
    assert "group: testgroup" in snippet
    assert "via: 915.075,125,9,5" in snippet


@pytest.mark.asyncio
async def test_commission_new_returns_secrets_snippet():
    """'new' mode returns a secrets.yaml snippet with password; no prv_key."""
    from mch.commission import handle_commission

    pubkey = "a695e2be" + "0" * 56
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "testpass",
        "name": "Test Node",
        "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    assert resp["ok"]
    snippet = resp["data"]["secrets_snippet"]
    assert "a695e2be:" in snippet
    assert "password: testpass" in snippet
    assert "prv_key" not in snippet


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
async def test_commission_new_adds_contact_before_commands():
    """handle_commission adds discovered node to the contact store before sending commands."""
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

    gw.add_commission_contact.assert_called_once()
    call_arg = gw.add_commission_contact.call_args[0][0]
    assert call_arg["pubkey"] == pubkey


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


# ---------------------------------------------------------------------------
# handle_commission — login with factory default password
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_new_logins_with_factory_password():
    """'new' mode logs in with 'password' (factory default) before commands."""
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

    gw.login.assert_called_once()
    _, pwd = gw.login.call_args[0]
    assert pwd == "password"


@pytest.mark.asyncio
async def test_commission_new_login_failure_returns_error():
    """'new' mode returns error when login fails — does not call send_command."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    gw.login = AsyncMock(return_value=False)
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "name": "TestNode", "target_freq": "915.075,125,9,5",
    }, wq, db, cfg)

    assert resp["ok"] is False
    assert "authenticate" in resp["error"]
    gw.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_commission_replace_login_failure_returns_error():
    """'replace' mode also returns error when login fails."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    gw.login = AsyncMock(return_value=False)
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd")],
        secrets={"aabbccdd": NodeSecrets(password="pw", prv_key="deadbeef" * 8)},
    )

    resp = await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    assert resp["ok"] is False
    assert "authenticate" in resp["error"]
    gw.send_command.assert_not_called()


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
