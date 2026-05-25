"""Tests for the loopback-only read-only HTTP API."""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

import pytest

from mch.api import ReadOnlyApi
from mch.config import ApiConfig, Config, NodeConfig, SettingConfig
from mch.db import Database
from mch.models import Node, NodeState, SettingPolicy, SettingRecord


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "api.db")
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def config():
    return Config(
        api=ApiConfig(enabled=True, port=0),
        nodes=[NodeConfig(
            pubkey_prefix="aabbccdd",
            label="tower",
            settings={"name": SettingConfig(desired="New", policy=SettingPolicy.ENFORCE)},
        )],
    )


async def _seed_node(db):
    node = Node(
        pubkey="aabbccdd" * 8,
        name="Tower",
        label="tower",
        state=NodeState.CONFIGURED,
        auth_cached=True,
    )
    await db.upsert_node(node)
    await db.upsert_setting(SettingRecord(
        node_pubkey=node.pubkey,
        key="name",
        desired="Old",
        observed="Old",
        policy=SettingPolicy.ENFORCE,
    ))
    return node


async def test_api_routes_expose_normalized_fleet_node_and_plan(db, config):
    await _seed_node(db)
    api = ReadOnlyApi(config, db)

    status, fleet = await api.route("/api/v1/fleet")
    node_status, node = await api.route("/api/v1/nodes/tower")
    plan_status, plan = await api.route("/api/v1/plan")

    assert status == HTTPStatus.OK
    assert isinstance(fleet, dict)
    assert fleet["schema_version"] == 1
    assert node_status == HTTPStatus.OK
    assert isinstance(node, dict)
    assert node["settings"][0]["action"] == "write"
    assert plan_status == HTTPStatus.OK
    assert isinstance(plan, dict)
    assert plan["writes"][0]["command"] == "set name New"


async def test_http_server_rejects_non_get_methods(db, config):
    api = ReadOnlyApi(config, db)
    reader = asyncio.StreamReader()
    reader.feed_data(b"POST /api/v1/fleet HTTP/1.1\r\nHost: localhost\r\n\r\n")
    reader.feed_eof()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    await api._handle_client(reader, writer)

    response = b"".join(call.args[0] for call in writer.write.call_args_list)
    head, body = response.split(b"\r\n\r\n", maxsplit=1)
    assert b"405 Method Not Allowed" in head
    assert json.loads(body.decode())["error"] == "read-only API accepts GET only"
