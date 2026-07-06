"""Tests for the Database layer."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from mch.db import _CURRENT_SCHEMA_VERSION, _SCHEMA, Database
from mch.models import (
    DesiredOperation,
    DesiredSetting,
    Node,
    NodeFact,
    NodeState,
    OperationRecord,
    OperationStatus,
    Scope,
    SettingPolicy,
    SettingRecord,
)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


# ------------------------------------------------------------------
# schema migrations (PRAGMA user_version)
# ------------------------------------------------------------------

async def _user_version(path) -> int:
    async with aiosqlite.connect(path) as conn:
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
    return int(row[0])


async def test_fresh_db_records_current_schema_version(tmp_path):
    path = tmp_path / "fresh.db"
    d = Database(path)
    await d.open()
    await d.close()
    assert await _user_version(path) == _CURRENT_SCHEMA_VERSION


async def test_migrations_are_idempotent_on_reopen(tmp_path):
    path = tmp_path / "reopen.db"
    for _ in range(3):
        d = Database(path)
        await d.open()  # must not raise on an already-migrated DB
        await d.close()
    assert await _user_version(path) == _CURRENT_SCHEMA_VERSION


async def test_legacy_db_with_adhoc_columns_adopts_migrations(tmp_path):
    """A pre-versioning DB (columns already present, user_version still 0) opens and adopts cleanly.

    Migration 1's ADD COLUMNs are already satisfied by the base schema on any real DB, so adoption
    must tolerate the duplicate-column error and just bump user_version to current.
    """
    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(_SCHEMA)  # columns present, but user_version left at 0
        await conn.commit()
    assert await _user_version(path) == 0

    d = Database(path)
    await d.open()  # duplicate-column ALTERs in migration 1 must be tolerated during adoption
    await d.upsert_node(Node(pubkey="aabbcc", name="Legacy", label="lab"))
    assert (await d.get_node("aabbcc")).label == "lab"
    await d.close()
    assert await _user_version(path) == _CURRENT_SCHEMA_VERSION


# ------------------------------------------------------------------
# nodes
# ------------------------------------------------------------------

async def test_upsert_and_get_node(db):
    node = Node(pubkey="aabbcc", name="TestNode", group="test-group")
    await db.upsert_node(node)

    fetched = await db.get_node("aabbcc")
    assert fetched is not None
    assert fetched.name == "TestNode"
    assert fetched.group == "test-group"
    assert fetched.state == NodeState.UNKNOWN


async def test_upsert_node_updates_existing(db):
    node = Node(pubkey="aabbcc", name="OldName")
    await db.upsert_node(node)

    node.name = "NewName"
    node.state = NodeState.ONLINE
    await db.upsert_node(node)

    fetched = await db.get_node("aabbcc")
    assert fetched.name == "NewName"
    assert fetched.state == NodeState.ONLINE


async def test_get_node_missing_returns_none(db):
    assert await db.get_node("notexist") is None


async def test_get_all_nodes(db):
    await db.upsert_node(Node(pubkey="bb", name="B"))
    await db.upsert_node(Node(pubkey="aa", name="A"))

    nodes = await db.get_all_nodes()
    assert len(nodes) == 2
    assert nodes[0].name == "A"  # ordered by name


async def test_delete_node_removes_local_state(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    await db.upsert_setting(SettingRecord(
        node_pubkey="aabbcc",
        key="owner.info",
        desired="managed",
        policy=SettingPolicy.AUDIT,
    ))
    await db.upsert_fact(NodeFact(node_pubkey="aabbcc", key="ver", value="v1.14.1"))
    await db.upsert_operation(OperationRecord(node_pubkey="aabbcc", command="clock sync"))
    await db.write_log("aabbcc", "INFO", "test log")

    await db.delete_node("aabbcc")

    assert await db.get_node("aabbcc") is None
    assert await db.get_settings("aabbcc") == []
    assert await db.get_facts("aabbcc") == {}
    assert await db.get_pending_operations("aabbcc") == []
    assert await db.get_logs("aabbcc") == []


# ------------------------------------------------------------------
# groups registry (first-class groups, R4 S1)
# ------------------------------------------------------------------

async def test_create_group_is_idempotent_and_listed_with_member_count(db):
    assert await db.group_exists("field") is False
    await db.create_group("field")
    await db.create_group("field")  # idempotent — no duplicate-key error
    assert await db.group_exists("field") is True

    await db.upsert_node(Node(pubkey="aa", name="A", group="field"))
    await db.upsert_node(Node(pubkey="bb", name="B", group="field"))
    await db.upsert_node(Node(pubkey="cc", name="C"))  # ungrouped

    groups = await db.list_groups()
    assert [g.name for g in groups] == ["field"]
    assert groups[0].member_count == 2


async def test_list_groups_orders_by_name(db):
    await db.create_group("solar")
    await db.create_group("field")
    await db.create_group("rooftop")
    assert [g.name for g in await db.list_groups()] == ["field", "rooftop", "solar"]


async def test_delete_group_cascades_hard(db):
    await db.create_group("field")
    await db.upsert_node(Node(pubkey="aa", name="A", group="field"))
    await db.set_desired_setting(
        DesiredSetting(scope_type=Scope.GROUP, scope_id="field", key="name", desired="x")
    )
    await db.set_desired_operation(
        DesiredOperation(scope_type=Scope.GROUP, scope_id="field", command="clock sync")
    )

    await db.delete_group("field")

    assert await db.group_exists("field") is False
    # members become ungrouped, not deleted
    node = await db.get_node("aa")
    assert node is not None and node.group is None
    # the group's desired rows are gone
    assert await db.get_desired_settings(Scope.GROUP, "field") == []
    assert await db.get_desired_operations(Scope.GROUP, "field") == []


async def test_rename_group_moves_everything_in_one_transaction(db):
    await db.create_group("field")
    await db.upsert_node(Node(pubkey="aa", name="A", group="field"))
    await db.set_desired_setting(
        DesiredSetting(scope_type=Scope.GROUP, scope_id="field", key="name", desired="x")
    )
    await db.set_desired_operation(
        DesiredOperation(scope_type=Scope.GROUP, scope_id="field", command="clock sync")
    )

    await db.rename_group("field", "outdoor")

    assert await db.group_exists("outdoor") is True
    assert await db.group_exists("field") is False
    node = await db.get_node("aa")
    assert node is not None and node.group == "outdoor"
    assert [s.key for s in await db.get_desired_settings(Scope.GROUP, "outdoor")] == ["name"]
    assert await db.get_desired_settings(Scope.GROUP, "field") == []
    assert [o.command for o in await db.get_desired_operations(Scope.GROUP, "outdoor")] == [
        "clock sync"
    ]


async def test_migration_seeds_groups_from_membership_and_scopes(tmp_path):
    """A pre-registry DB (user_version 2) seeds the groups table from existing data on upgrade.

    Sources: distinct nodes.node_group ∪ distinct group-scoped scope_ids in
    desired_settings/desired_operations (R4 S1 decision 5).
    """
    path = tmp_path / "legacy_groups.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(_SCHEMA)
        await conn.execute("INSERT INTO nodes (pubkey, name, node_group) VALUES ('aa','A','field')")
        await conn.execute("INSERT INTO nodes (pubkey, name, node_group) VALUES ('bb','B','field')")
        await conn.execute("INSERT INTO nodes (pubkey, name, node_group) VALUES ('cc','C',NULL)")
        await conn.execute(
            "INSERT INTO desired_settings (scope_type, scope_id, key, desired) "
            "VALUES ('group','solar','name','x')"
        )
        await conn.execute(
            "INSERT INTO desired_operations (scope_type, scope_id, command) "
            "VALUES ('group','rooftop','clock sync')"
        )
        await conn.execute("PRAGMA user_version = 2")
        await conn.commit()
    assert await _user_version(path) == 2

    d = Database(path)
    await d.open()
    groups = {g.name for g in await d.list_groups()}
    field = next(g for g in await d.list_groups() if g.name == "field")
    await d.close()

    assert groups == {"field", "solar", "rooftop"}
    assert field.member_count == 2
    assert await _user_version(path) == _CURRENT_SCHEMA_VERSION


# ------------------------------------------------------------------
# desired_settings (layered, CLI/API-authored source of truth)
# ------------------------------------------------------------------

async def test_set_and_get_desired_setting_node_scope(db):
    setting = DesiredSetting(
        scope_type=Scope.NODE,
        scope_id="aabbcc",
        key="tx",
        desired="14",
        policy=SettingPolicy.ENFORCE,
        poll_hours=12,
    )
    await db.set_desired_setting(setting)

    rows = await db.get_desired_settings(Scope.NODE, "aabbcc")
    assert len(rows) == 1
    got = rows[0]
    assert got.scope_type == Scope.NODE
    assert got.scope_id == "aabbcc"
    assert got.key == "tx"
    assert got.desired == "14"
    assert got.policy == SettingPolicy.ENFORCE
    assert got.poll_hours == 12


async def test_get_all_desired_settings_spans_scopes(db):
    await db.set_desired_setting(DesiredSetting(Scope.GLOBAL, "", "owner.info", desired="rdm"))
    await db.set_desired_setting(DesiredSetting(Scope.GROUP, "solar", "tx", desired="20"))
    await db.set_desired_setting(DesiredSetting(Scope.NODE, "aabbcc", "tx", desired="14"))

    rows = await db.get_all_desired_settings()

    assert len(rows) == 3
    scopes = {(r.scope_type, r.scope_id) for r in rows}
    assert scopes == {(Scope.GLOBAL, ""), (Scope.GROUP, "solar"), (Scope.NODE, "aabbcc")}


async def test_get_all_desired_settings_empty(db):
    assert await db.get_all_desired_settings() == []


# ------------------------------------------------------------------
# desired_operations (the DB home for operations; slice 8i)
# ------------------------------------------------------------------

async def test_set_and_get_desired_operation_node_scope(db):
    op = DesiredOperation(
        scope_type=Scope.NODE, scope_id="aabbcc", command="reboot",
        every_hours=168, fire_and_forget=True,
    )
    await db.set_desired_operation(op)

    rows = await db.get_desired_operations(Scope.NODE, "aabbcc")
    assert rows == [op]


async def test_set_desired_operation_upserts_on_conflict(db):
    await db.set_desired_operation(DesiredOperation(Scope.NODE, "aabbcc", "advert"))
    await db.set_desired_operation(
        DesiredOperation(Scope.NODE, "aabbcc", "advert", every_hours=6)
    )
    rows = await db.get_desired_operations(Scope.NODE, "aabbcc")
    assert len(rows) == 1
    assert rows[0].every_hours == 6


async def test_get_all_desired_operations_spans_scopes(db):
    await db.set_desired_operation(DesiredOperation(Scope.GLOBAL, "", "clock sync"))
    await db.set_desired_operation(DesiredOperation(Scope.GROUP, "solar", "reboot"))
    await db.set_desired_operation(DesiredOperation(Scope.NODE, "aabbcc", "advert"))

    rows = await db.get_all_desired_operations()

    scopes = {(r.scope_type, r.scope_id) for r in rows}
    assert scopes == {(Scope.GLOBAL, ""), (Scope.GROUP, "solar"), (Scope.NODE, "aabbcc")}


async def test_delete_desired_operation(db):
    await db.set_desired_operation(DesiredOperation(Scope.NODE, "aabbcc", "advert"))
    await db.delete_desired_operation(Scope.NODE, "aabbcc", "advert")
    assert await db.get_desired_operations(Scope.NODE, "aabbcc") == []


async def test_set_desired_setting_upserts_on_conflict(db):
    await db.set_desired_setting(
        DesiredSetting(scope_type=Scope.NODE, scope_id="aabbcc", key="tx", desired="14")
    )
    await db.set_desired_setting(
        DesiredSetting(scope_type=Scope.NODE, scope_id="aabbcc", key="tx", desired="20")
    )

    rows = await db.get_desired_settings(Scope.NODE, "aabbcc")
    assert len(rows) == 1
    assert rows[0].desired == "20"


async def test_desired_setting_inherit_fields_roundtrip_as_none(db):
    # policy-only override with no value and inherited policy/poll_hours
    await db.set_desired_setting(
        DesiredSetting(scope_type=Scope.GROUP, scope_id="solar", key="tx")
    )
    rows = await db.get_desired_settings(Scope.GROUP, "solar")
    assert len(rows) == 1
    assert rows[0].desired is None
    assert rows[0].policy is None
    assert rows[0].poll_hours is None


async def test_global_scope_uses_empty_scope_id(db):
    await db.set_desired_setting(
        DesiredSetting(scope_type=Scope.GLOBAL, scope_id="", key="tx", desired="20")
    )
    rows = await db.get_desired_settings(Scope.GLOBAL, "")
    assert len(rows) == 1
    assert rows[0].desired == "20"


async def test_delete_desired_setting(db):
    await db.set_desired_setting(
        DesiredSetting(scope_type=Scope.NODE, scope_id="aabbcc", key="tx", desired="14")
    )
    await db.delete_desired_setting(Scope.NODE, "aabbcc", "tx")
    assert await db.get_desired_settings(Scope.NODE, "aabbcc") == []


# ------------------------------------------------------------------
# settings
# ------------------------------------------------------------------

async def test_upsert_and_get_setting(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    setting = SettingRecord(
        node_pubkey="aabbcc",
        key="owner.info",
        desired="Alice",
        policy=SettingPolicy.AUDIT,
        poll_hours=24,
        update_attempts=3,
    )
    await db.upsert_setting(setting)

    settings = await db.get_settings("aabbcc")
    assert len(settings) == 1
    assert settings[0].key == "owner.info"
    assert settings[0].policy == SettingPolicy.AUDIT
    assert settings[0].desired == "Alice"
    assert settings[0].update_attempts == 3


async def test_setting_drift_tracking(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    setting = SettingRecord(
        node_pubkey="aabbcc",
        key="powersaving",
        desired="on",
        policy=SettingPolicy.ENFORCE,
        observed="off",
        drift=True,
    )
    await db.upsert_setting(setting)

    drifted = await db.get_drifted_settings()
    assert len(drifted) == 1
    assert drifted[0].key == "powersaving"
    assert drifted[0].observed == "off"
    assert drifted[0].desired == "on"


# ------------------------------------------------------------------
# facts
# ------------------------------------------------------------------

async def test_upsert_and_get_fact(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    fact = NodeFact(node_pubkey="aabbcc", key="ver", value="v1.14.1")
    await db.upsert_fact(fact)

    facts = await db.get_facts("aabbcc")
    assert facts["ver"] == "v1.14.1"
    records = await db.get_fact_records("aabbcc")
    assert records[0].key == "ver"
    assert records[0].value == "v1.14.1"
    assert records[0].collected_at == fact.collected_at


async def test_fact_upsert_updates_value(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    await db.upsert_fact(NodeFact(node_pubkey="aabbcc", key="ver", value="v1.13.0"))
    await db.upsert_fact(NodeFact(node_pubkey="aabbcc", key="ver", value="v1.14.1"))

    facts = await db.get_facts("aabbcc")
    assert facts["ver"] == "v1.14.1"


async def test_delete_fact_removes_single_fact(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    await db.upsert_fact(NodeFact(node_pubkey="aabbcc", key="ver", value="v1.14.1"))
    await db.upsert_fact(NodeFact(node_pubkey="aabbcc", key="powersavings", value="on"))

    await db.delete_fact("aabbcc", "powersavings")

    facts = await db.get_facts("aabbcc")
    assert facts == {"ver": "v1.14.1"}


async def test_delete_setting_removes_single_observed_row(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    await db.upsert_setting(SettingRecord(
        node_pubkey="aabbcc", key="name", desired="N", policy=SettingPolicy.ENFORCE, observed="N",
    ))
    await db.upsert_setting(SettingRecord(
        node_pubkey="aabbcc", key="txdelay", desired=None, policy=SettingPolicy.AUDIT,
        observed="14",
    ))

    await db.delete_setting("aabbcc", "txdelay")

    remaining = [s.key for s in await db.get_settings("aabbcc")]
    assert remaining == ["name"]


# ------------------------------------------------------------------
# operations
# ------------------------------------------------------------------

async def test_upsert_and_get_pending_operations(db):
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    op = OperationRecord(node_pubkey="aabbcc", command="clock sync", every_hours=6)
    await db.upsert_operation(op)

    pending = await db.get_pending_operations("aabbcc")
    assert len(pending) == 1
    assert pending[0].command == "clock sync"
    assert pending[0].status == OperationStatus.PENDING


async def test_get_operations_returns_latest_per_command(db):
    # S3.2: node show needs the latest record per command.
    await db.upsert_node(Node(pubkey="aabbcc", name="N"))
    await db.upsert_operation(OperationRecord(node_pubkey="aabbcc", command="clock sync"))
    await db.upsert_operation(OperationRecord(node_pubkey="aabbcc", command="reboot"))

    ops = await db.get_operations("aabbcc")
    assert {o.command for o in ops} == {"clock sync", "reboot"}


# ------------------------------------------------------------------
# logs
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# next_sync_at scheduling
# ------------------------------------------------------------------

async def test_next_sync_at_persisted(db):
    t = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    node = Node(pubkey="aa", name="N", next_sync_at=t)
    await db.upsert_node(node)
    fetched = await db.get_node("aa")
    assert fetched.next_sync_at == t


async def test_set_next_sync(db):
    await db.upsert_node(Node(pubkey="aa", name="N"))
    t = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    await db.set_next_sync("aa", t)
    fetched = await db.get_node("aa")
    assert fetched.next_sync_at == t


async def test_set_node_last_attempt_roundtrips(db):
    await db.upsert_node(Node(pubkey="aa", name="N"))
    t = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    await db.set_node_last_attempt("aa", t)
    assert (await db.get_node("aa")).last_attempt_at == t


async def test_upsert_node_does_not_clobber_last_attempt(db):
    # The probe records last_attempt via its own setter; a later upsert_node (which does not
    # carry the column) must preserve it (S2.4).
    await db.upsert_node(Node(pubkey="aa", name="N"))
    t = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    await db.set_node_last_attempt("aa", t)
    node = await db.get_node("aa")
    node.name = "Renamed"
    await db.upsert_node(node)
    assert (await db.get_node("aa")).last_attempt_at == t


async def test_get_due_node_returns_overdue(db):
    past   = datetime(2020, 1, 1, tzinfo=UTC)
    future = datetime(2099, 1, 1, tzinfo=UTC)
    await db.upsert_node(Node(pubkey="aa", name="A", next_sync_at=future))
    await db.upsert_node(Node(pubkey="bb", name="B", next_sync_at=past))
    node = await db.get_due_node()
    assert node is not None
    assert node.pubkey == "bb"


async def test_get_due_node_returns_none_when_all_future(db):
    future = datetime(2099, 1, 1, tzinfo=UTC)
    await db.upsert_node(Node(pubkey="aa", name="A", next_sync_at=future))
    assert await db.get_due_node() is None


async def test_get_due_node_treats_null_as_overdue(db):
    # next_sync_at=None means never scheduled — treat as immediately due
    await db.upsert_node(Node(pubkey="aa", name="A"))
    node = await db.get_due_node()
    assert node is not None
    assert node.pubkey == "aa"


# ------------------------------------------------------------------
# logs
# ------------------------------------------------------------------

async def test_write_and_get_logs(db):
    await db.write_log("aabbcc", "INFO", "connected")
    await db.write_log("aabbcc", "ERROR", "timeout")
    await db.write_log(None, "INFO", "daemon started")

    node_logs = await db.get_logs("aabbcc")
    assert len(node_logs) == 2
    assert node_logs[0]["message"] == "timeout"   # most recent first

    all_logs = await db.get_logs()
    assert len(all_logs) == 3
