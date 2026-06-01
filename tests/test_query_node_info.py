from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from meshcore.events import Event, EventType

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "query_node_info.py"
SPEC = importlib.util.spec_from_file_location("query_node_info", MODULE_PATH)
assert SPEC is not None
query_node_info = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = query_node_info
assert SPEC.loader is not None
SPEC.loader.exec_module(query_node_info)


def make_event(etype: EventType, payload: object, **attrs) -> Event:
    return Event(etype, payload, attrs or {})


def make_args(**overrides: object) -> argparse.Namespace:
    values = {
        "path": "",
        "path_hash_mode": 0,
        "radio": "915.800,250,11,5",
        "frequency": None,
        "bandwidth": None,
        "spreading_factor": None,
        "coding_rate": None,
        "owner": False,
        "basic": True,
        "regions": False,
        "target": "62c6ba722167c03908fc00c25391998392842619e3301aaa745000be0e3bd8b2",
        "timeout": 1.0,
        "tcp_host": None,
        "tcp_port": 5000,
        "serial": "/dev/null",
        "baud": 115200,
        "debug": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def make_meshcore() -> SimpleNamespace:
    commands = SimpleNamespace(
        send_appstart=AsyncMock(
            return_value=make_event(
                EventType.SELF_INFO,
                {
                    "radio_freq": 915.075,
                    "radio_bw": 125.0,
                    "radio_sf": 9,
                    "radio_cr": 5,
                },
            )
        ),
        set_radio=AsyncMock(return_value=make_event(EventType.OK, {})),
    )
    return SimpleNamespace(commands=commands, disconnect=AsyncMock())


@pytest.mark.asyncio
async def test_radio_is_restored_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mc = make_meshcore()
    monkeypatch.setattr(query_node_info, "connect", AsyncMock(return_value=fake_mc))
    monkeypatch.setattr(
        query_node_info,
        "send_anon_request",
        AsyncMock(return_value={"success": False, "error": "timeout"}),
    )

    await query_node_info.run(make_args())

    assert fake_mc.commands.set_radio.await_args_list[0].args == (915.8, 250.0, 11, 5)
    assert fake_mc.commands.set_radio.await_args_list[1].args == (915.075, 125.0, 9, 5)
    fake_mc.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_radio_is_restored_after_request_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mc = make_meshcore()
    monkeypatch.setattr(query_node_info, "connect", AsyncMock(return_value=fake_mc))
    monkeypatch.setattr(
        query_node_info,
        "send_anon_request",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await query_node_info.run(make_args())

    assert fake_mc.commands.set_radio.await_args_list[0].args == (915.8, 250.0, 11, 5)
    assert fake_mc.commands.set_radio.await_args_list[1].args == (915.075, 125.0, 9, 5)
    fake_mc.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_radio_restore_is_skipped_when_already_on_requested_radio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_mc = make_meshcore()
    fake_mc.commands.send_appstart.return_value = make_event(
        EventType.SELF_INFO,
        {
            "radio_freq": 915.8,
            "radio_bw": 250.0,
            "radio_sf": 11,
            "radio_cr": 5,
        },
    )
    monkeypatch.setattr(query_node_info, "connect", AsyncMock(return_value=fake_mc))
    monkeypatch.setattr(
        query_node_info,
        "send_anon_request",
        AsyncMock(return_value={"success": False, "error": "timeout"}),
    )

    await query_node_info.run(make_args())

    fake_mc.commands.set_radio.assert_not_awaited()
    fake_mc.disconnect.assert_awaited_once()
