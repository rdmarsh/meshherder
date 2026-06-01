"""Tests for the canonical read-only fleet report contract."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mch.config import Config, NodeConfig, SettingConfig
from mch.db import Database
from mch.models import Node, NodeFact, NodeState, SettingPolicy, SettingRecord
from mch.report import build_fleet_report, build_node_report, legacy_node


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "report.db")
    await database.open()
    yield database
    await database.close()


async def test_fleet_report_classifies_node_health_settings_and_actions(db):
    node = Node(
        pubkey="aabbccdd" * 8,
        label="ridge",
        name="Ridge",
        state=NodeState.CONFIGURED,
        auth_cached=True,
    )
    await db.upsert_node(node)
    collected = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    await db.upsert_fact(NodeFact(
        node_pubkey=node.pubkey,
        key="lat",
        value="-33.731",
        collected_at=collected,
    ))
    await db.upsert_fact(NodeFact(node_pubkey=node.pubkey, key="lon", value="150.902"))
    await db.upsert_setting(SettingRecord(
        node_pubkey=node.pubkey,
        key="name",
        desired="Old",
        observed="Old",
        policy=SettingPolicy.AUDIT,
    ))
    await db.upsert_setting(SettingRecord(
        node_pubkey=node.pubkey,
        key="radio",
        desired="915.075,125,9,5",
        observed="915.0750122,125,9,5",
        policy=SettingPolicy.ENFORCE,
    ))
    config = Config(nodes=[NodeConfig(
        pubkey_prefix="aabbccdd",
        label="ridge",
        settings={
            "name": SettingConfig(desired="New", policy=SettingPolicy.ENFORCE),
            "radio": SettingConfig(
                desired="915.075,125,9,5",
                policy=SettingPolicy.ENFORCE,
            ),
            "owner.info": SettingConfig(desired="Managed", policy=SettingPolicy.AUDIT),
            "powersaving": SettingConfig(desired="on", policy=SettingPolicy.SKIP),
        },
    )])

    report = await build_fleet_report(db, config)

    assert report["schema_version"] == 2
    assert report["summary"]["critical_nodes"] == 1
    assert report["summary"]["drifted_settings"] == 1
    api_node = report["nodes"][0]
    assert api_node["fact_keys"] == ["ver", "board", "role"]
    assert api_node["facts"]["lat"] == "-33.731"
    assert api_node["facts_collected_at"]["lat"] == collected.isoformat()
    settings = {setting["key"]: setting for setting in api_node["settings"]}
    assert settings["name"]["status"] == "drift"
    assert settings["name"]["action"] == "write"
    assert settings["name"]["command"] == "set name New"
    assert settings["radio"]["status"] == "nominal"
    assert settings["radio"]["action"] == "none"
    assert settings["owner.info"]["status"] == "unknown"
    assert settings["owner.info"]["action"] == "read"
    assert settings["powersaving"]["status"] == "ignored"
    assert settings["powersaving"]["action"] == "ignore"
    assert report["plan"]["writes"][0]["key"] == "name"
    assert report["plan"]["unknown"][0]["key"] == "owner.info"


async def test_legacy_node_includes_next_sync_at(db):
    next_sync_at = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    node = Node(
        pubkey="aabbccdd" * 8,
        label="ridge",
        name="Ridge",
        state=NodeState.CONFIGURED,
        next_sync_at=next_sync_at,
    )
    await db.upsert_node(node)
    config = Config(nodes=[NodeConfig(pubkey_prefix="aabbccdd", label="ridge")])

    report = await build_node_report(db, config, node)
    detail = legacy_node(report, config)

    assert detail["next_sync_at"] == next_sync_at.isoformat()


async def test_fleet_report_includes_configured_nodes_missing_from_observed_state(db):
    config = Config(nodes=[NodeConfig(pubkey_prefix="deadbeef", label="lost-site")])

    report = await build_fleet_report(db, config)

    assert report["summary"]["unseen_nodes"] == 1
    assert report["unseen_nodes"][0]["pubkey_prefix"] == "deadbeef"
    assert report["issues"]["unseen_nodes"] == report["unseen_nodes"]
    assert report["issues"]["ghosts"] == []
    assert report["plan"]["unknown"][0]["reason"] == "node not known to gateway/DB"


async def test_fleet_report_lists_db_nodes_outside_config_as_ghosts(db):
    node = Node(
        pubkey="aabbccdd" * 8,
        label="retired",
        name="Retired",
        state=NodeState.CONFIGURED,
    )
    await db.upsert_node(node)

    report = await build_fleet_report(db, Config(nodes=[]))

    assert report["summary"]["ghost_nodes"] == 1
    assert report["issues"]["ghosts"] == [{
        "pubkey": node.pubkey,
        "label": "retired",
        "name": "Retired",
        "state": "configured",
        "last_seen": None,
    }]
    assert report["issues"]["unseen_nodes"] == []
