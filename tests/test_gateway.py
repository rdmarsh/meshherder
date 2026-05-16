"""Tests for the Gateway class using a mock meshcore connection."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meshcore.events import Event, EventType

from mcherd.config import GatewayConfig
from mcherd.gateway import CommandResult, Gateway, GatewayError


def make_event(etype: EventType, payload: object, **attrs) -> Event:
    return Event(etype, payload, attrs or {})


def make_config(**kwargs) -> GatewayConfig:
    return GatewayConfig(device="/dev/null", baud=115200, **kwargs)


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_sets_info():
    cfg = make_config()
    gw = Gateway(cfg)

    fake_mc = MagicMock()
    fake_mc.commands.send_appstart = AsyncMock(return_value=make_event(
        EventType.SELF_INFO,
        {"name": "TestGW", "radio_freq": 915.0, "radio_bw": 125.0, "radio_sf": 9, "radio_cr": 5},
    ))
    # receive loop must not spin on an unmocked dispatcher — exit cleanly via CancelledError
    fake_mc.dispatcher.wait_for_event = AsyncMock(side_effect=asyncio.CancelledError)

    with patch("mcherd.gateway.MeshCore.create_serial", AsyncMock(return_value=fake_mc)):
        await gw.connect()

    try:
        assert gw.info["name"] == "TestGW"
        assert gw.connected
    finally:
        await gw.disconnect()


@pytest.mark.asyncio
async def test_connect_raises_when_no_device():
    cfg = make_config()
    gw = Gateway(cfg)

    with patch("mcherd.gateway.MeshCore.create_serial", AsyncMock(return_value=None)):
        with pytest.raises(GatewayError, match="no response from gateway"):
            await gw.connect()


@pytest.mark.asyncio
async def test_connect_sets_name_when_configured():
    cfg = make_config(name="MyGateway")
    gw = Gateway(cfg)

    fake_mc = MagicMock()
    fake_mc.commands.send_appstart = AsyncMock(return_value=make_event(
        EventType.SELF_INFO,
        {"name": "OldName", "radio_freq": 915.0, "radio_bw": 125.0, "radio_sf": 9, "radio_cr": 5},
    ))
    fake_mc.commands.set_name = AsyncMock(return_value=make_event(EventType.OK, {}))
    fake_mc.dispatcher.wait_for_event = AsyncMock(side_effect=asyncio.CancelledError)

    with patch("mcherd.gateway.MeshCore.create_serial", AsyncMock(return_value=fake_mc)):
        await gw.connect()

    try:
        fake_mc.commands.set_name.assert_awaited_once_with("MyGateway")
    finally:
        await gw.disconnect()


# ---------------------------------------------------------------------------
# send_command
# ---------------------------------------------------------------------------

def make_connected_gateway(fake_mc: MagicMock) -> Gateway:
    cfg = make_config()
    gw = Gateway(cfg)
    gw._mc = fake_mc
    gw.info = {"name": "TestGW"}
    return gw


def start_receive_loop(gw: Gateway) -> None:
    """Start the background receive loop for tests that exercise send_command."""
    gw._receive_task = asyncio.create_task(gw._receive_loop(), name="gateway-receive")


def make_contact(prefix: str = "aabbcc112233") -> dict:
    return {"public_key": prefix + "0" * (64 - len(prefix)), "adv_name": "TestNode"}


@pytest.mark.asyncio
async def test_send_command_returns_response_text():
    contact = make_contact("aabbcc112233")
    fake_mc = MagicMock()
    gw = make_connected_gateway(fake_mc)

    fake_mc.commands.send_cmd = AsyncMock(return_value=make_event(
        EventType.MSG_SENT, {"suggested_timeout": 2000}
    ))
    # deliver MESSAGES_WAITING once, then exit the loop cleanly
    fake_mc.dispatcher.wait_for_event = AsyncMock(side_effect=[
        make_event(EventType.MESSAGES_WAITING, {}),
        asyncio.CancelledError,
    ])
    fake_mc.commands.get_msg = AsyncMock(side_effect=[
        make_event(EventType.CONTACT_MSG_RECV,
                   {"pubkey_prefix": "aabbcc112233", "text": "> v1.14.1"},
                   pubkey_prefix="aabbcc112233"),
        make_event(EventType.NO_MORE_MSGS, {}),
    ])

    start_receive_loop(gw)
    try:
        result = await gw.send_command(contact, "ver")
        assert result.success is True
        assert result.text == "v1.14.1"   # "> " prefix stripped
    finally:
        await gw.disconnect()


@pytest.mark.asyncio
async def test_send_command_skips_other_nodes_messages():
    """Messages from a different node are discarded; we keep draining until ours arrives."""
    contact = make_contact("aabbcc112233")
    fake_mc = MagicMock()
    gw = make_connected_gateway(fake_mc)

    fake_mc.commands.send_cmd = AsyncMock(return_value=make_event(
        EventType.MSG_SENT, {"suggested_timeout": 2000}
    ))
    fake_mc.dispatcher.wait_for_event = AsyncMock(side_effect=[
        make_event(EventType.MESSAGES_WAITING, {}),
        asyncio.CancelledError,
    ])
    fake_mc.commands.get_msg = AsyncMock(side_effect=[
        # first msg from a different node
        make_event(EventType.CONTACT_MSG_RECV,
                   {"pubkey_prefix": "ffeeddccbbaa", "text": "> noise"},
                   pubkey_prefix="ffeeddccbbaa"),
        # then ours
        make_event(EventType.CONTACT_MSG_RECV,
                   {"pubkey_prefix": "aabbcc112233", "text": "> repeater"},
                   pubkey_prefix="aabbcc112233"),
        make_event(EventType.NO_MORE_MSGS, {}),
    ])

    start_receive_loop(gw)
    try:
        result = await gw.send_command(contact, "get role")
        assert result.success is True
        assert result.text == "repeater"
    finally:
        await gw.disconnect()


@pytest.mark.asyncio
async def test_send_command_timeout():
    contact = make_contact()
    fake_mc = MagicMock()
    gw = make_connected_gateway(fake_mc)

    fake_mc.commands.send_cmd = AsyncMock(return_value=make_event(
        EventType.MSG_SENT, {"suggested_timeout": 2000}
    ))
    # MESSAGES_WAITING never arrives. Must use a real sleep so the receive loop
    # actually yields to the event loop — AsyncMock(return_value=None) resolves
    # synchronously and starves asyncio.wait_for's timeout callback.
    async def _no_messages(*_a, **_kw):
        await asyncio.sleep(0.05)
        return None
    fake_mc.dispatcher.wait_for_event = _no_messages

    start_receive_loop(gw)
    try:
        result = await gw.send_command(contact, "ver", timeout=0.3)
        assert result.success is False
        assert "timeout" in result.error
    finally:
        await gw.disconnect()


@pytest.mark.asyncio
async def test_send_command_requires_connection():
    gw = Gateway(make_config())  # not connected

    with pytest.raises(GatewayError, match="not connected"):
        await gw.send_command(make_contact(), "ver")


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_success():
    contact = make_contact()
    fake_mc = MagicMock()
    gw = make_connected_gateway(fake_mc)

    fake_mc.commands.send_login_sync = AsyncMock(return_value=make_event(
        EventType.LOGIN_SUCCESS,
        {"permissions": 3, "is_admin": True, "pubkey_prefix": "aabbcc112233"},
    ))

    ok = await gw.login(contact, "hunter2")
    assert ok is True


@pytest.mark.asyncio
async def test_login_failure():
    contact = make_contact()
    fake_mc = MagicMock()
    gw = make_connected_gateway(fake_mc)

    fake_mc.commands.send_login_sync = AsyncMock(return_value=None)

    ok = await gw.login(contact, "wrongpassword")
    assert ok is False
