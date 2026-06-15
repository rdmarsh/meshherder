"""Tests for the loopback-only read-only HTTP API."""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

import pytest

from mch.api import ReadOnlyApi
from mch.config import ApiConfig, Config, NodeConfig
from mch.db import Database
from mch.models import DesiredSetting, Node, NodeState, Scope, SettingPolicy, SettingRecord


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
        nodes=[NodeConfig(pubkey_prefix="aabbccdd", label="tower")],
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
    # Desired state lives in the DB now (sunset): name should converge to "New".
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="name", desired="New",
        policy=SettingPolicy.ENFORCE,
    ))
    return node


async def test_api_routes_mirror_read_only_cli_commands(db, config):
    await _seed_node(db)
    api = ReadOnlyApi(config, db)

    health_status, health = await api.route("/health")
    status, fleet = await api.route("/status")
    node_status, node = await api.route("/status/tower")
    issues_status, issues = await api.route("/issues")
    plan_status, plan = await api.route("/plan")
    logs_status, logs = await api.route("/logs?node=tower&limit=20")

    assert health_status == HTTPStatus.OK
    assert health == {"ok": True}
    assert status == HTTPStatus.OK
    assert isinstance(fleet, dict)
    assert fleet["schema_version"] == 2
    assert node_status == HTTPStatus.OK
    assert isinstance(node, dict)
    assert node["settings"][0]["action"] == "write"
    assert plan_status == HTTPStatus.OK
    assert isinstance(plan, dict)
    assert plan["writes"][0]["command"] == "set name New"
    assert issues_status == HTTPStatus.OK
    assert isinstance(issues, dict)
    assert logs_status == HTTPStatus.OK
    assert isinstance(logs, list)


@pytest.mark.parametrize(
    "path",
    [
        "/healthz",
        "/api/v1/fleet",
        "/api/v1/nodes/tower",
        "/api/v1/issues",
        "/api/v1/plan",
        "/api/v1/logs?node=tower&limit=20",
    ],
)
async def test_abandoned_draft_api_routes_are_not_exposed(path, db, config):
    await _seed_node(db)
    api = ReadOnlyApi(config, db)

    status, data = await api.route(path)

    assert status == HTTPStatus.NOT_FOUND
    assert data == {"error": "not found"}


async def test_http_server_rejects_non_get_methods(db, config):
    api = ReadOnlyApi(config, db)
    reader = asyncio.StreamReader()
    reader.feed_data(b"POST /status HTTP/1.1\r\nHost: localhost\r\n\r\n")
    reader.feed_eof()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    await api._handle_client(reader, writer)

    response = b"".join(call.args[0] for call in writer.write.call_args_list)
    head, body = response.split(b"\r\n\r\n", maxsplit=1)
    assert b"405 Method Not Allowed" in head
    assert json.loads(body.decode())["error"] == "read-only API accepts GET only"
