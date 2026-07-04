"""Tests for the canonical read-only fleet report contract."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mch.config import Config, NodeConfig, SettingConfig
from mch.db import Database
from mch.models import (
    DesiredSetting,
    Node,
    NodeFact,
    NodeState,
    Scope,
    SettingPolicy,
    SettingRecord,
)
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
    # Desired state lives in the DB (sunset); the config node entry only marks the node as
    # configured (not a ghost) so plan.unknown still surfaces unobserved desired keys.
    for setting in (
        DesiredSetting(Scope.NODE, node.pubkey, "name", desired="New",
                       policy=SettingPolicy.ENFORCE),
        DesiredSetting(Scope.NODE, node.pubkey, "radio", desired="915.075,125,9,5",
                       policy=SettingPolicy.ENFORCE),
        DesiredSetting(Scope.NODE, node.pubkey, "owner.info", desired="Managed",
                       policy=SettingPolicy.AUDIT),
        DesiredSetting(Scope.NODE, node.pubkey, "powersaving", desired="on",
                       policy=SettingPolicy.SKIP),
        # Facts are policy=fact rows now (sunset), resolved per node. Seed the former
        # default fact set globally so the report's "configured facts" still lists them.
        DesiredSetting(Scope.GLOBAL, "", "ver", desired=None, policy=SettingPolicy.FACT),
        DesiredSetting(Scope.GLOBAL, "", "board", desired=None, policy=SettingPolicy.FACT),
        DesiredSetting(Scope.GLOBAL, "", "role", desired=None, policy=SettingPolicy.FACT),
    ):
        await db.set_desired_setting(setting)
    config = Config(nodes=[NodeConfig(pubkey_prefix="aabbccdd", label="ridge")])

    report = await build_fleet_report(db, config)

    assert report["schema_version"] == 2
    assert report["summary"]["critical_nodes"] == 1
    assert report["summary"]["drifted_settings"] == 1
    api_node = report["nodes"][0]
    assert api_node["fact_keys"] == ["board", "role", "ver"]  # sorted resolved fact keys
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
    detail = legacy_node(report)

    assert detail["next_sync_at"] == next_sync_at.isoformat()


async def test_fleet_report_drops_dead_ghost_and_unseen_categories(db):
    # RECOMMENDATIONS-2 S3.1: post-sunset every DB node is managed and there is no config
    # inventory, so the ghost/unseen categories can never populate — they are removed entirely
    # (schema, summary, and issues), not left as always-empty dead fields.
    node = Node(
        pubkey="aabbccdd" * 8,
        label="ridge",
        name="Ridge",
        state=NodeState.CONFIGURED,
    )
    await db.upsert_node(node)

    report = await build_fleet_report(db, Config(nodes=[]))

    assert "unseen_nodes" not in report
    assert "configured" not in report["nodes"][0]
    assert "unseen_nodes" not in report["summary"]
    assert "ghost_nodes" not in report["summary"]
    assert "ghosts" not in report["issues"]
    assert "unseen_nodes" not in report["issues"]
    assert set(report["issues"]) == {"drifted", "bad_state", "open_nodes"}


async def test_fleet_report_flags_outdated_firmware_as_advisory(db):
    # RECOMMENDATIONS-2 S3.3: fleet-wide firmware currency is an advisory field, separate from the
    # `issues` categories (firmware age is not a fault and must not drive exit-2 alerting).
    old = Node(pubkey="aa" * 32, label="old", name="Old", state=NodeState.CONFIGURED)
    await db.upsert_node(old)
    await db.upsert_fact(NodeFact(node_pubkey=old.pubkey, key="ver", value="v1.14.1"))

    report = await build_fleet_report(db, Config(nodes=[]))

    firmware = report["firmware"]
    assert firmware["latest"]  # catalogue's current version, e.g. v1.16.0
    assert [n["label"] for n in firmware["outdated"]] == ["old"]
    assert firmware["outdated"][0]["releases_behind"] >= 1
    assert report["summary"]["outdated_firmware"] == 1
    # Advisory, not a fault: the node is not pushed into any issue category.
    assert report["issues"]["bad_state"] == []


# ------------------------------------------------------------------
# slice 4: per-setting source (db vs config) + DB overlay in reports
# ------------------------------------------------------------------

async def test_report_marks_db_sourced_setting(db):
    node = Node(pubkey="aabbccdd" * 8, label="ridge", name="Ridge", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="txdelay", desired="14",
        policy=SettingPolicy.ENFORCE,
    ))
    config = Config(nodes=[NodeConfig(pubkey_prefix="aabbccdd", label="ridge")])

    report = await build_fleet_report(db, config)
    settings = {s["key"]: s for s in report["nodes"][0]["settings"]}

    # The DB-authored desired value appears in the report (overlay applied) ...
    assert settings["txdelay"]["desired"] == "14"
    # ... and is attributed to the DB.
    assert settings["txdelay"]["source"] == "db"


async def test_report_db_desired_overrides_config_and_is_sourced_db(db):
    node = Node(pubkey="aabbccdd" * 8, label="ridge", name="Ridge", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="name", desired="FromDB",
        policy=SettingPolicy.ENFORCE,
    ))
    config = Config(nodes=[NodeConfig(
        pubkey_prefix="aabbccdd",
        label="ridge",
        settings={"name": SettingConfig(desired="FromYAML", policy=SettingPolicy.ENFORCE)},
    )])

    report = await build_fleet_report(db, config)
    settings = {s["key"]: s for s in report["nodes"][0]["settings"]}

    assert settings["name"]["desired"] == "FromDB"
    assert settings["name"]["source"] == "db"


async def test_legacy_node_includes_setting_source(db):
    node = Node(pubkey="aabbccdd" * 8, label="ridge", name="Ridge", state=NodeState.CONFIGURED)
    await db.upsert_node(node)
    await db.set_desired_setting(DesiredSetting(
        scope_type=Scope.NODE, scope_id=node.pubkey, key="txdelay", desired="14",
    ))
    config = Config(nodes=[NodeConfig(pubkey_prefix="aabbccdd", label="ridge")])

    report = await build_node_report(db, config, node)
    detail = legacy_node(report)
    sources = {s["key"]: s["source"] for s in detail["settings"]}

    # All desired state resolves from the DB now (sunset), so its source is "db".
    assert sources["txdelay"] == "db"
