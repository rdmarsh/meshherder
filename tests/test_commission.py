"""Tests for mch.commission — discover-and-filter, commissioning sequences, paste snippets."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from mch.config import (
    Config,
    GatewayConfig,
    GroupConfig,
    NodeConfig,
    NodeSecrets,
    RadioConfig,
    SettingConfig,
    load,
)
from mch.gateway import CommandResult
from mch.meshkey import derive_public_key
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
    gw.send_command = AsyncMock(return_value=CommandResult(success=True, text="ok"))

    async def _discover(*args, **kwargs) -> list[dict]:
        if gw.discover.call_count == 1:
            return discovered or []
        for call in gw.send_command.call_args_list:
            command = call.args[1]
            if command.startswith("set prv.key "):
                return [discover_payload(derive_public_key(command.split()[-1]))]
        return discovered or []

    gw.discover = AsyncMock(side_effect=_discover)
    gw.switch_radio_temporarily = AsyncMock(return_value=None)
    gw.restore_radio = AsyncMock(return_value=None)
    gw.add_commission_contact = AsyncMock(
        side_effect=lambda ni: {"public_key": ni["pubkey"], "adv_name": ""}
    )
    gw.login = AsyncMock(return_value=True)
    gw.info = {"radio_freq": 915.800, "radio_bw": 250.0, "radio_sf": 10, "radio_cr": 5}
    return gw


def make_db(nodes: list[Node] | None = None) -> MagicMock:
    db = MagicMock()
    db.get_all_nodes = AsyncMock(return_value=nodes or [])
    return db


def make_wq(gateway=None, db=None) -> MagicMock:
    wq = MagicMock()
    gw = gateway or make_gateway()
    wq.pause = MagicMock()
    wq.resume = MagicMock()

    @asynccontextmanager
    async def _commission_session():
        wq.pause()
        try:
            yield gw
        finally:
            wq.resume()

    wq.commission_session = MagicMock(side_effect=_commission_session)
    return wq


def discover_payload(pubkey: str, node_type: int = 2) -> dict:
    return {"pubkey": pubkey + "0" * (64 - len(pubkey)), "node_type": node_type, "SNR_in": 10.0}


GENERATED_PRV_KEY = (
    "489E11DCC0A5E037E65C90D2327AA11A42EAFE0C9F68DEBE82B0F71C88C0874B"
    "CC291D9B2B98A54F5C1426B7AB8156B0D684EAA4EBA755AC614A9FD32B74C308"
)
GENERATED_PUBKEY = derive_public_key(GENERATED_PRV_KEY)


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
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
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
        "name": "Quakers Hill North", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
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
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any("set radio 915.800,250,10,5" in c for c in sent)


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
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
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
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert sent[-1] == "reboot"


@pytest.mark.asyncio
async def test_commission_new_waits_for_radio_then_reboots_without_waiting():
    """'new' mode waits for set radio before sending the no-wait reboot."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "admin",
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    calls_by_command = {call.args[1]: call for call in gw.send_command.call_args_list}
    assert calls_by_command["set radio 915.800,250,10,5"].kwargs["await_response"] is True
    assert calls_by_command["reboot"].kwargs["await_response"] is False


@pytest.mark.asyncio
async def test_commission_new_returns_pubkey_prefix():
    """'new' mode returns pubkey_prefix derived from the generated private key."""
    from mch.commission import handle_commission

    factory_pubkey = "a695e2be23f2" + "0" * 52
    gw = make_gateway(discovered=[discover_payload(factory_pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    assert resp["ok"] is True
    assert resp["data"]["pubkey_prefix"] == GENERATED_PUBKEY[:8]
    assert resp["data"]["public_key"] == GENERATED_PUBKEY


@pytest.mark.asyncio
async def test_commission_new_sends_password_and_prv_key_late():
    """'new' mode sends session-invalidating password/key changes after normal settings."""
    from mch.commission import handle_commission

    factory_pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(factory_pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "hunter2",
        "name": "TestNode",
        "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert sent == [
        "set name TestNode",
        "password hunter2",
        f"set prv.key {GENERATED_PRV_KEY}",
        "set radio 915.800,250,10,5",
        "reboot",
    ]


@pytest.mark.asyncio
async def test_commission_new_treats_cli_error_response_as_failure():
    """CLI error text is a failed command even when it arrives as a normal response."""
    from mch.commission import handle_commission

    factory_pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(factory_pubkey)])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="ok"),
        CommandResult(success=True, text="ERR_CODE_INVALID_STATE"),
    ])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "hunter2",
        "name": "TestNode",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    assert resp["ok"] is False
    assert "password <redacted>" in resp["error"]
    assert "ERR_CODE_INVALID_STATE" in resp["error"]
    assert "hunter2" not in resp["error"]


@pytest.mark.asyncio
async def test_commission_new_redacts_prv_key_on_failure():
    """Private keys are not leaked in command failure errors."""
    from mch.commission import handle_commission

    factory_pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(factory_pubkey)])
    gw.send_command = AsyncMock(side_effect=[
        CommandResult(success=True, text="ok"),
        CommandResult(success=True, text="ok"),
        CommandResult(success=False, error="timeout"),
    ])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "hunter2",
        "name": "TestNode",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    assert resp["ok"] is False
    assert "set prv.key <redacted>" in resp["error"]
    assert GENERATED_PRV_KEY not in resp["error"]


@pytest.mark.asyncio
async def test_commission_new_fails_when_generated_identity_not_discovered(monkeypatch):
    """'new' mode does not print snippets unless the generated pubkey appears after reboot."""
    import mch.commission as commission
    from mch.commission import handle_commission

    monkeypatch.setattr(commission.asyncio, "sleep", AsyncMock())
    factory_pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(factory_pubkey)])
    gw.discover = AsyncMock(side_effect=[
        [discover_payload(factory_pubkey)],
        [discover_payload(factory_pubkey)],
        [discover_payload(factory_pubkey)],
        [discover_payload(factory_pubkey)],
    ])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "hunter2",
        "name": "TestNode",
        "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    assert resp["ok"] is False
    assert "verification failed" in resp["error"]
    assert GENERATED_PUBKEY[:12] in resp["error"]


@pytest.mark.asyncio
async def test_commission_new_retries_identity_verification(monkeypatch):
    """'new' mode tolerates a rebooting node missing from the first verification scan."""
    import mch.commission as commission
    from mch.commission import handle_commission

    monkeypatch.setattr(commission.asyncio, "sleep", AsyncMock())
    factory_pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(factory_pubkey)])
    gw.discover = AsyncMock(side_effect=[
        [discover_payload(factory_pubkey)],
        [discover_payload("75ab85174506" + "0" * 52)],
        [discover_payload(GENERATED_PUBKEY)],
    ])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "hunter2",
        "name": "TestNode",
        "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    assert resp["ok"] is True
    assert resp["data"]["public_key"] == GENERATED_PUBKEY
    assert gw.discover.await_count == 3


# ---------------------------------------------------------------------------
# handle_commission — paste snippets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_new_returns_config_snippet(tmp_path):
    """'new' mode returns a config.yaml snippet with derived pubkey_prefix, label, group, via."""
    from mch.commission import handle_commission

    factory_pubkey = "a695e2be" + "0" * 56
    gw = make_gateway(discovered=[discover_payload(factory_pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(groups=[GroupConfig(name="testgroup")])

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "label": "Test-Node",
        "group": "testgroup",
        "name": "Test Node",
        "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    assert resp["ok"]
    snippet = resp["data"]["config_snippet"]
    assert f"pubkey_prefix: {GENERATED_PUBKEY[:8]}" in snippet
    assert "label: Test-Node" in snippet
    assert "group: testgroup" in snippet
    assert "via: 915.800,250,10,5" in snippet
    assert 'desired: "Test Node"' in snippet
    assert "policy: enforce" in snippet

    path = tmp_path / "config.yaml"
    path.write_text("groups:\n  - name: testgroup\nnodes:\n" + snippet + "\n")
    loaded = load(path, load_secrets=False)
    assert loaded.nodes[0].label == "Test-Node"
    assert loaded.nodes[0].settings["name"].desired == "Test Node"


@pytest.mark.asyncio
async def test_commission_new_rejects_invalid_label_before_commands():
    from mch.commission import handle_commission

    factory_pubkey = "a695e2be" + "0" * 56
    gw = make_gateway(discovered=[discover_payload(factory_pubkey)])
    wq = make_wq(gateway=gw)

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "label": "Test Node",
        "name": "Test Node",
    }, wq, make_db(), make_config())

    assert resp["ok"] is False
    assert "invalid characters" in resp["error"]
    gw.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_commission_new_rejects_unknown_group_before_commands():
    from mch.commission import handle_commission

    factory_pubkey = "a695e2be" + "0" * 56
    gw = make_gateway(discovered=[discover_payload(factory_pubkey)])
    wq = make_wq(gateway=gw)

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "label": "Test-Node",
        "group": "undefined",
    }, wq, make_db(), make_config())

    assert resp == {"ok": False, "error": "group 'undefined' is not configured"}
    gw.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_commission_new_returns_secrets_snippet():
    """'new' mode returns a secrets.yaml snippet with password and generated prv_key."""
    from mch.commission import handle_commission

    factory_pubkey = "a695e2be" + "0" * 56
    gw = make_gateway(discovered=[discover_payload(factory_pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "testpass",
        "name": "Test Node",
        "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    assert resp["ok"]
    snippet = resp["data"]["secrets_snippet"]
    assert f"{GENERATED_PUBKEY[:8]}:" in snippet
    assert "password: testpass" in snippet
    assert f"prv_key: {GENERATED_PRV_KEY}" in snippet


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
            via=RadioConfig(freq=915.800, bw=250.0, sf=10, cr=5),
        )],
        secrets={"aabbccdd": NodeSecrets(password="pw", prv_key="deadbeef" * 8)},
    )

    await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any("set radio 915.800" in c for c in sent)


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


@pytest.mark.asyncio
async def test_commission_uses_long_timeout_for_slow_commands():
    """`set prv.key` and `set radio` use a longer timeout than fast commands.

    Bench-measured on v1.15-era firmware: a valid set prv.key derives the new
    pubkey and writes flash before replying, taking well over 30s. set radio
    showed the same pattern. Fast commands (set name, password, etc.) reply
    instantly and should keep the short default."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "hunter2",
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    timeouts_by_command = {
        call.args[1]: call.kwargs["timeout"]
        for call in gw.send_command.call_args_list
    }
    # Fast commands keep the short default
    assert timeouts_by_command["set name TestNode"] == 30.0
    assert timeouts_by_command["password hunter2"] == 30.0
    # Slow commands get the extended timeout
    assert timeouts_by_command[f"set prv.key {GENERATED_PRV_KEY}"] == 180.0
    assert timeouts_by_command["set radio 915.800,250,10,5"] == 180.0


@pytest.mark.asyncio
async def test_commission_replace_waits_for_radio_then_reboots_without_waiting():
    """'replace' mode waits for set radio before sending the no-wait reboot."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(
            pubkey_prefix="aabbccdd",
            via=RadioConfig(freq=915.800, bw=250.0, sf=10, cr=5),
        )],
        secrets={"aabbccdd": NodeSecrets(password="pw", prv_key="deadbeef" * 8)},
    )

    await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    calls_by_command = {call.args[1]: call for call in gw.send_command.call_args_list}
    assert calls_by_command["set radio 915.800,250.0,10,5"].kwargs["await_response"] is True
    assert calls_by_command["reboot"].kwargs["await_response"] is False


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
    cfg = Config(gateway=GatewayConfig(radio=RadioConfig(freq=915.800, bw=250.0, sf=10, cr=5)))

    resp = await handle_commission({"cmd": "commission", "mode": "scan"}, wq, db, cfg)

    assert resp["ok"] is True
    assert resp["data"]["gateway_radio"] == "915.800,250,10,5"
    assert resp["data"]["group_options"] == []
    assert resp["data"]["radio_options"] == [
        {"label": "gateway/home radio", "value": "915.800,250,10,5"}
    ]


@pytest.mark.asyncio
async def test_commission_scan_returns_group_options():
    """scan mode includes configured group names for the new-node wizard."""
    from mch.commission import handle_commission

    gw = make_gateway(discovered=[])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = Config(groups=[GroupConfig(name="QH"), GroupConfig(name="test")])

    resp = await handle_commission({"cmd": "commission", "mode": "scan"}, wq, db, cfg)

    assert resp["ok"] is True
    assert resp["data"]["group_options"] == ["QH", "test"]


@pytest.mark.asyncio
async def test_commission_scan_returns_config_radio_options():
    """scan mode includes radio choices declared in groups, nodes, and via."""
    from mch.commission import handle_commission

    gw = make_gateway(discovered=[])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = Config(
        gateway=GatewayConfig(radio=RadioConfig(freq=915.800, bw=250.0, sf=10, cr=5)),
        global_settings=GroupConfig(
            name="global",
            settings={"radio": SettingConfig(desired="915.075,125,9,5")},
        ),
        groups=[
            GroupConfig(
                name="repeaters",
                settings={"radio": SettingConfig(desired="915.500,125,9,5")},
            )
        ],
        nodes=[
            NodeConfig(
                pubkey_prefix="aabbccdd",
                label="site-a",
                settings={"radio": SettingConfig(desired="916.575,62.5,7,8")},
                via=RadioConfig(freq=917.000, bw=125.0, sf=8, cr=5),
            )
        ],
    )

    resp = await handle_commission({"cmd": "commission", "mode": "scan"}, wq, db, cfg)

    assert resp["ok"] is True
    assert resp["data"]["radio_options"] == [
        {"label": "gateway/home radio", "value": "915.800,250,10,5"},
        {"label": "global", "value": "915.075,125,9,5"},
        {"label": "group repeaters", "value": "915.500,125,9,5"},
        {"label": "node site-a", "value": "916.575,62.5,7,8"},
        {"label": "node site-a", "value": "917.000,125,8,5"},
    ]


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
# handle_commission - return to factory radio after discover
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
        "name": "TestNode", "target_freq": "915.800,250,10,5",
    }, wq, db, cfg)

    gw.add_commission_contact.assert_called_once()
    call_arg = gw.add_commission_contact.call_args[0][0]
    assert call_arg["pubkey"] == pubkey


@pytest.mark.asyncio
async def test_commission_new_switches_back_to_factory_before_commands():
    """After discover() restores home freq, commission switches back for commands."""
    from mch.commission import handle_commission
    from mch.gateway import Gateway

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "name": "TestNode", "target_freq": "915.800,250,10,5",
    }, wq, db, cfg)

    gw.switch_radio_temporarily.assert_called_once_with(*Gateway.FACTORY_FREQ)


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
        "name": "TestNode", "target_freq": "915.800,250,10,5",
    }, wq, db, cfg)

    gw.restore_radio.assert_called_once_with(915.800, 250.0, 10, 5)


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
        "name": "TestNode", "target_freq": "915.800,250,10,5",
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
        "name": "TestNode", "target_freq": "915.800,250,10,5",
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
async def test_commission_replace_switches_back_to_factory_before_commands():
    """'replace' mode also switches to factory radio before sending commands."""
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

    gw.switch_radio_temporarily.assert_called_once_with(*Gateway.FACTORY_FREQ)


# ---------------------------------------------------------------------------
# handle_commission — login progress messages (pin)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_new_emits_login_progress_messages():
    """'new' mode emits 'logging in...' then 'login OK' progress messages on success."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()
    messages: list[str] = []

    async def _capture(msg: str) -> None:
        messages.append(msg)

    await handle_commission({
        "cmd": "commission", "mode": "new",
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg, progress=_capture)

    assert "logging in..." in messages
    # login OK message immediately follows the logging-in message
    idx = messages.index("logging in...")
    assert messages[idx + 1].startswith("login OK")


@pytest.mark.asyncio
async def test_commission_replace_emits_login_progress_messages():
    """'replace' mode emits 'logging in...' then 'login OK' progress messages on success."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd")],
        secrets={"aabbccdd": NodeSecrets(password="pw", prv_key="deadbeef" * 8)},
    )
    messages: list[str] = []

    async def _capture(msg: str) -> None:
        messages.append(msg)

    await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg, progress=_capture)

    assert "logging in..." in messages
    idx = messages.index("logging in...")
    assert messages[idx + 1].startswith("login OK")


# ---------------------------------------------------------------------------
# handle_commission — new-mode admin-password fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commission_new_falls_back_to_new_admin_password_when_factory_fails():
    """When factory login fails in new mode, the user-supplied admin password is tried."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])

    async def _login(contact, password):
        return password == "newadminpw"
    gw.login = AsyncMock(side_effect=_login)

    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "newadminpw",
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    assert resp["ok"] is True
    passwords_tried = [call.args[1] for call in gw.login.call_args_list]
    assert passwords_tried == ["password", "newadminpw"]
    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any("password newadminpw" in c for c in sent)


@pytest.mark.asyncio
async def test_commission_new_does_not_try_fallback_when_factory_succeeds():
    """Regression guard: factory login success means the new admin password is never tried."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "newadminpw",
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    assert resp["ok"] is True
    gw.login.assert_called_once()
    _, pwd = gw.login.call_args[0]
    assert pwd == "password"


@pytest.mark.asyncio
async def test_commission_new_no_fallback_when_admin_password_is_factory_default():
    """When the operator-chosen admin password is also 'password', no second attempt is made."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    gw.login = AsyncMock(return_value=False)
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        # No admin_password → defaults to 'password' (factory) → no fallback opportunity
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg)

    assert resp["ok"] is False
    gw.login.assert_called_once()
    _, pwd = gw.login.call_args[0]
    assert pwd == "password"


@pytest.mark.asyncio
async def test_commission_new_emits_fallback_progress_message():
    """When the new-mode fallback fires, progress announces it to the operator."""
    from mch.commission import handle_commission

    pubkey = "a1" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])

    async def _login(contact, password):
        return password == "newadminpw"
    gw.login = AsyncMock(side_effect=_login)

    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config()
    messages: list[str] = []

    async def _capture(msg: str) -> None:
        messages.append(msg)

    resp = await handle_commission({
        "cmd": "commission", "mode": "new",
        "admin_password": "newadminpw",
        "name": "TestNode", "target_freq": "915.800,250,10,5",
        "prv_key": GENERATED_PRV_KEY,
    }, wq, db, cfg, progress=_capture)

    assert resp["ok"] is True
    assert any("factory login failed" in m.lower() for m in messages)
    assert any("new admin password" in m.lower() for m in messages)


# ---------------------------------------------------------------------------
# handle_commission — replace-mode managed-password fallback
# ---------------------------------------------------------------------------

def _make_login_mock(*, factory_ok: bool, managed_ok: bool, managed_password: str):
    """Build a gateway.login side_effect that distinguishes factory and managed passwords."""
    async def _login(contact, password):
        if password == "password":
            return factory_ok
        if password == managed_password:
            return managed_ok
        return False
    return AsyncMock(side_effect=_login)


@pytest.mark.asyncio
async def test_commission_replace_falls_back_to_managed_password_when_factory_fails():
    """When factory login fails in replace mode, the managed password from secrets is tried."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    gw.login = _make_login_mock(factory_ok=False, managed_ok=True, managed_password="nodepass")
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd")],
        secrets={"aabbccdd": NodeSecrets(password="nodepass", prv_key="deadbeef" * 8)},
    )

    resp = await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    assert resp["ok"] is True
    # Both attempts were made, factory first then managed
    passwords_tried = [call.args[1] for call in gw.login.call_args_list]
    assert passwords_tried == ["password", "nodepass"]
    # The command sequence still ran
    sent = [call.args[1] for call in gw.send_command.call_args_list]
    assert any("password nodepass" in c for c in sent)
    assert any("set prv.key deadbeef" in c for c in sent)


@pytest.mark.asyncio
async def test_commission_replace_does_not_try_managed_when_factory_succeeds():
    """Regression guard: factory login success means managed password is never tried."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    # Default gw.login from make_gateway() returns True — verify only one call is made.
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd")],
        secrets={"aabbccdd": NodeSecrets(password="nodepass", prv_key="deadbeef" * 8)},
    )

    resp = await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    assert resp["ok"] is True
    gw.login.assert_called_once()
    _, pwd = gw.login.call_args[0]
    assert pwd == "password"


@pytest.mark.asyncio
async def test_commission_replace_no_login_succeeds_factory_fails_no_managed_password():
    """Factory login fails and secrets has no managed password → error, no commands sent."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    gw.login = AsyncMock(return_value=False)
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd")],
        # No password in secrets — only prv_key
        secrets={"aabbccdd": NodeSecrets(prv_key="deadbeef" * 8)},
    )

    resp = await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    assert resp["ok"] is False
    assert "authenticate" in resp["error"]
    # Only one login attempt — no managed password to try
    gw.login.assert_called_once()
    _, pwd = gw.login.call_args[0]
    assert pwd == "password"
    gw.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_commission_replace_no_login_succeeds_both_passwords_fail():
    """Factory and managed both reject → error mentions both attempts."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    gw.login = _make_login_mock(factory_ok=False, managed_ok=False, managed_password="nodepass")
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd")],
        secrets={"aabbccdd": NodeSecrets(password="nodepass", prv_key="deadbeef" * 8)},
    )

    resp = await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg)

    assert resp["ok"] is False
    assert "factory" in resp["error"].lower()
    assert "managed" in resp["error"].lower()
    passwords_tried = [call.args[1] for call in gw.login.call_args_list]
    assert passwords_tried == ["password", "nodepass"]
    gw.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_commission_replace_emits_fallback_progress_message():
    """When the managed-password fallback fires, progress announces it to the operator."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32
    gw = make_gateway(discovered=[discover_payload(pubkey)])
    gw.login = _make_login_mock(factory_ok=False, managed_ok=True, managed_password="nodepass")
    wq = make_wq(gateway=gw)
    db = make_db()
    cfg = make_config(
        nodes=[NodeConfig(pubkey_prefix="aabbccdd")],
        secrets={"aabbccdd": NodeSecrets(password="nodepass", prv_key="deadbeef" * 8)},
    )
    messages: list[str] = []

    async def _capture(msg: str) -> None:
        messages.append(msg)

    resp = await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq, db, cfg, progress=_capture)

    assert resp["ok"] is True
    # The operator must see that the fallback fired — both that factory failed and
    # that we're now using the managed credentials.
    assert any("factory login failed" in m.lower() for m in messages)
    assert any("managed credentials" in m.lower() for m in messages)


@pytest.mark.asyncio
async def test_commission_replace_command_sequence_unchanged_when_fallback_fires():
    """Regression guard: the command sequence sent via the fallback path matches the
    sequence sent on a normal factory-login pass. Re-sending these idempotent commands
    is what makes stateless retry safe."""
    from mch.commission import handle_commission

    pubkey = "cc" * 32

    # Reference: factory login succeeds
    gw_a = make_gateway(discovered=[discover_payload(pubkey)])
    wq_a = make_wq(gateway=gw_a)
    db_a = make_db()
    cfg_a = make_config(
        nodes=[NodeConfig(
            pubkey_prefix="aabbccdd",
            via=RadioConfig(freq=915.800, bw=250.0, sf=10, cr=5),
        )],
        secrets={
            "aabbccdd": NodeSecrets(
                password="nodepass", guest_password="guestpass", prv_key="deadbeef" * 8,
            ),
        },
    )
    await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq_a, db_a, cfg_a)
    sequence_a = [call.args[1] for call in gw_a.send_command.call_args_list]

    # Fallback path: factory rejects, managed succeeds
    gw_b = make_gateway(discovered=[discover_payload(pubkey)])
    gw_b.login = _make_login_mock(factory_ok=False, managed_ok=True, managed_password="nodepass")
    wq_b = make_wq(gateway=gw_b)
    db_b = make_db()
    cfg_b = make_config(
        nodes=[NodeConfig(
            pubkey_prefix="aabbccdd",
            via=RadioConfig(freq=915.800, bw=250.0, sf=10, cr=5),
        )],
        secrets={
            "aabbccdd": NodeSecrets(
                password="nodepass", guest_password="guestpass", prv_key="deadbeef" * 8,
            ),
        },
    )
    await handle_commission({
        "cmd": "commission", "mode": "replace",
        "pubkey_prefix": "aabbccdd",
    }, wq_b, db_b, cfg_b)
    sequence_b = [call.args[1] for call in gw_b.send_command.call_args_list]

    assert sequence_a == sequence_b
