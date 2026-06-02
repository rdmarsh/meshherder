"""Tests for the Gateway class using a mock meshcore connection."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from meshcore.events import Event, EventType

from mch.config import GatewayConfig, RadioConfig
from mch.gateway import Gateway, GatewayError


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

    with patch("mch.gateway.MeshCore.create_serial", AsyncMock(return_value=fake_mc)):
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

    with patch("mch.gateway.MeshCore.create_serial", AsyncMock(return_value=None)):
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

    with patch("mch.gateway.MeshCore.create_serial", AsyncMock(return_value=fake_mc)):
        await gw.connect()

    try:
        fake_mc.commands.set_name.assert_awaited_once_with("MyGateway")
    finally:
        await gw.disconnect()


@pytest.mark.asyncio
async def test_connect_does_not_reset_gateway_radio_when_only_cr_differs():
    cfg = make_config(radio=RadioConfig(freq=915.0, bw=125.0, sf=9, cr=8))
    gw = Gateway(cfg)

    fake_mc = MagicMock()
    fake_mc.commands.send_appstart = AsyncMock(return_value=make_event(
        EventType.SELF_INFO,
        {"name": "TestGW", "radio_freq": 915.0, "radio_bw": 125.0, "radio_sf": 9, "radio_cr": 5},
    ))
    fake_mc.commands.set_radio = AsyncMock(return_value=make_event(EventType.OK, {}))
    fake_mc.dispatcher.wait_for_event = AsyncMock(side_effect=asyncio.CancelledError)

    with patch("mch.gateway.MeshCore.create_serial", AsyncMock(return_value=fake_mc)):
        await gw.connect()

    try:
        fake_mc.commands.set_radio.assert_not_awaited()
        assert gw.info["radio_cr"] == 5
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


@pytest.mark.asyncio
async def test_send_command_no_wait_returns_immediately():
    """await_response=False returns immediately after send_cmd without registering a future."""
    contact = make_contact("aabbcc112233")
    fake_mc = MagicMock()
    gw = make_connected_gateway(fake_mc)

    fake_mc.commands.send_cmd = AsyncMock(return_value=make_event(
        EventType.MSG_SENT, {"suggested_timeout": 2000}
    ))
    # Receive loop not started — if await_response were True this would hang forever.
    result = await gw.send_command(contact, "password test", await_response=False)
    assert result.success is True
    fake_mc.commands.send_cmd.assert_called_once()
    assert gw._pending == {}


@pytest.mark.asyncio
async def test_send_command_no_wait_send_failure_returns_error():
    """await_response=False still reports send-level errors."""
    contact = make_contact("aabbcc112233")
    fake_mc = MagicMock()
    gw = make_connected_gateway(fake_mc)

    fake_mc.commands.send_cmd = AsyncMock(return_value=make_event(
        EventType.ERROR, "ERR_CODE_NOT_FOUND"
    ))
    result = await gw.send_command(contact, "password test", await_response=False)
    assert result.success is False
    assert "send failed" in result.error


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


# ---------------------------------------------------------------------------
# advert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advert_zerohop_calls_send_advert_not_flood():
    """advert(zerohop=True) calls mc.commands.send_advert(flood=False)."""
    fake_mc = MagicMock()
    gw = make_connected_gateway(fake_mc)
    fake_mc.commands.send_advert = AsyncMock(return_value=make_event(EventType.OK, {}))

    await gw.advert(zerohop=True)

    fake_mc.commands.send_advert.assert_awaited_once_with(flood=False)


# ---------------------------------------------------------------------------
# get_contacts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_contacts_times_out_when_gateway_hangs():
    """A hung meshcore get_contacts must raise GatewayError, not block forever.

    Regression guard: an unbounded get_contacts() in the config-reload path could
    leave `systemctl restart mchd` stuck in `deactivating` until SIGKILL (observed
    during the 0.9.29 luna deploy). The call must be bounded by a timeout.
    """
    fake_mc = MagicMock()
    gw = make_connected_gateway(fake_mc)

    # Real sleep so wait_for's timeout callback gets a chance to fire (a plain
    # AsyncMock would resolve synchronously and starve the timeout).
    async def _hang(*_a, **_kw):
        await asyncio.sleep(10)
    fake_mc.commands.get_contacts = _hang

    with pytest.raises(GatewayError, match="timeout"):
        await gw.get_contacts(timeout=0.3)


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

FACTORY_FREQ = (869.618, 62.5, 8, 5)
HOME_RADIO = {"radio_freq": 915.0, "radio_bw": 125.0, "radio_sf": 9, "radio_cr": 5}


def make_discover_mc():
    """Return a fake MeshCore wired up for discover() tests."""
    fake_mc = MagicMock()
    fake_mc.commands.set_radio = AsyncMock(return_value=make_event(EventType.OK, {}))
    fake_mc.commands.send_node_discover_req = AsyncMock(return_value=make_event(EventType.OK, {}))
    return fake_mc


def capture_subscribe(fake_mc):
    """
    Replace fake_mc.dispatcher.subscribe with a version that captures the callback
    and returns a mock subscription.

    Returns (holder, mock_sub, ready):
      - holder: list; the discover callback is appended when subscribe is called.
      - mock_sub: the MagicMock subscription object (has .unsubscribe()).
      - ready: asyncio.Event set when subscribe is called (use to synchronise fire tasks).
    """
    holder: list = []
    ready = asyncio.Event()
    mock_sub = MagicMock()

    def _subscribe(event_type, callback, **_kw):
        holder.append(callback)
        ready.set()
        return mock_sub

    fake_mc.dispatcher.subscribe = _subscribe
    return holder, mock_sub, ready


def make_discover_payload(pubkey_hex: str = "a695e2be23f2" + "0" * 52) -> dict:
    return {
        "pubkey": pubkey_hex,
        "node_type": 2,  # REP
        "SNR_in": 14.0,
        "tag": "00000000",
    }


@pytest.mark.asyncio
async def test_discover_switches_to_factory_freq_and_restores():
    """discover() switches to factory radio tuple then restores the home tuple."""
    fake_mc = make_discover_mc()
    gw = make_connected_gateway(fake_mc)
    gw.info = {**HOME_RADIO, "name": "TestGW"}
    capture_subscribe(fake_mc)  # return values unused here

    await gw.discover(factory_freq=FACTORY_FREQ, dwell=0.01)

    calls = fake_mc.commands.set_radio.await_args_list
    assert len(calls) == 2
    # first call: switch to factory frequency
    assert calls[0].args == FACTORY_FREQ
    # second call: restore home freq
    assert calls[1].args == (
        HOME_RADIO["radio_freq"], HOME_RADIO["radio_bw"],
        HOME_RADIO["radio_sf"], HOME_RADIO["radio_cr"],
    )


@pytest.mark.asyncio
async def test_discover_subscribes_before_sending_request():
    """discover() subscribes to DISCOVER_RESPONSE before calling send_node_discover_req."""
    from meshcore.events import EventType as ET
    fake_mc = make_discover_mc()
    gw = make_connected_gateway(fake_mc)
    gw.info = {**HOME_RADIO, "name": "TestGW"}

    call_order: list[str] = []
    mock_sub = MagicMock()

    def _subscribe(event_type, callback, **_kw):
        assert event_type == ET.DISCOVER_RESPONSE
        call_order.append("subscribe")
        return mock_sub

    original_send = fake_mc.commands.send_node_discover_req

    async def _send(*args, **kwargs):
        call_order.append("send")
        return await original_send(*args, **kwargs)

    fake_mc.dispatcher.subscribe = _subscribe
    fake_mc.commands.send_node_discover_req = _send

    await gw.discover(factory_freq=FACTORY_FREQ, dwell=0.01)

    assert call_order == ["subscribe", "send"]


@pytest.mark.asyncio
async def test_discover_sends_req_with_all_types_filter():
    """discover() calls send_node_discover_req with filter=0x0F and prefix_only=False."""
    fake_mc = make_discover_mc()
    gw = make_connected_gateway(fake_mc)
    gw.info = {**HOME_RADIO, "name": "TestGW"}
    capture_subscribe(fake_mc)

    await gw.discover(factory_freq=FACTORY_FREQ, dwell=0.01)

    fake_mc.commands.send_node_discover_req.assert_awaited_once_with(filter=0x0F, prefix_only=False)


@pytest.mark.asyncio
async def test_discover_returns_discovered_nodes():
    """discover() returns nodes collected via DISCOVER_RESPONSE during dwell."""
    fake_mc = make_discover_mc()
    gw = make_connected_gateway(fake_mc)
    gw.info = {**HOME_RADIO, "name": "TestGW"}
    holder, _, ready = capture_subscribe(fake_mc)

    payload = make_discover_payload()

    async def fire_event():
        await ready.wait()  # wait until subscribe() has been called
        holder[0](make_event(EventType.DISCOVER_RESPONSE, payload))

    asyncio.create_task(fire_event())

    results = await gw.discover(factory_freq=FACTORY_FREQ, dwell=0.05)

    assert len(results) == 1
    assert results[0]["pubkey"] == payload["pubkey"]
    assert results[0]["node_type"] == 2


@pytest.mark.asyncio
async def test_discover_collects_multiple_nodes():
    """discover() can return multiple nodes if more than one responds."""
    fake_mc = make_discover_mc()
    gw = make_connected_gateway(fake_mc)
    gw.info = {**HOME_RADIO, "name": "TestGW"}
    holder, _, ready = capture_subscribe(fake_mc)

    payload_a = make_discover_payload("aaaa" + "0" * 60)
    payload_b = make_discover_payload("bbbb" + "0" * 60)

    async def fire_events():
        await ready.wait()  # wait until subscribe() has been called
        cb = holder[0]
        cb(make_event(EventType.DISCOVER_RESPONSE, payload_a))
        cb(make_event(EventType.DISCOVER_RESPONSE, payload_b))

    asyncio.create_task(fire_events())

    results = await gw.discover(factory_freq=FACTORY_FREQ, dwell=0.05)

    assert len(results) == 2


@pytest.mark.asyncio
async def test_discover_unsubscribes_after_dwell():
    """discover() always calls sub.unsubscribe() after the dwell period."""
    fake_mc = make_discover_mc()
    gw = make_connected_gateway(fake_mc)
    gw.info = {**HOME_RADIO, "name": "TestGW"}
    _, mock_sub, _ = capture_subscribe(fake_mc)

    await gw.discover(factory_freq=FACTORY_FREQ, dwell=0.01)

    mock_sub.unsubscribe.assert_called_once()


@pytest.mark.asyncio
async def test_discover_restores_radio_on_send_error():
    """discover() restores radio even if send_node_discover_req raises."""
    fake_mc = make_discover_mc()
    gw = make_connected_gateway(fake_mc)
    gw.info = {**HOME_RADIO, "name": "TestGW"}
    capture_subscribe(fake_mc)  # return values unused here
    fake_mc.commands.send_node_discover_req = AsyncMock(side_effect=RuntimeError("radio exploded"))

    with pytest.raises(RuntimeError):
        await gw.discover(factory_freq=FACTORY_FREQ, dwell=0.01)

    # restore_radio must still have been called
    calls = fake_mc.commands.set_radio.await_args_list
    assert any(
        c.args == (HOME_RADIO["radio_freq"], HOME_RADIO["radio_bw"],
                   HOME_RADIO["radio_sf"], HOME_RADIO["radio_cr"])
        for c in calls
    )
