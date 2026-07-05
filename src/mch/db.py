"""SQLite persistence layer via aiosqlite."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

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

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    pubkey          TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    label           TEXT NOT NULL DEFAULT '',
    node_group      TEXT,
    state           TEXT NOT NULL DEFAULT 'unknown',
    last_seen       TEXT,
    last_connected  TEXT,
    auth_required   INTEGER NOT NULL DEFAULT 0,
    auth_cached     INTEGER NOT NULL DEFAULT 0,
    next_sync_at    TEXT,
    last_attempt_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_pubkey     TEXT NOT NULL,
    key             TEXT NOT NULL,
    desired         TEXT,
    policy          TEXT NOT NULL DEFAULT 'enforce',
    poll_hours      INTEGER NOT NULL DEFAULT 24,
    observed        TEXT,
    last_checked    TEXT,
    last_set        TEXT,
    update_attempts INTEGER NOT NULL DEFAULT 0,
    drift           INTEGER NOT NULL DEFAULT 0,
    UNIQUE(node_pubkey, key),
    FOREIGN KEY (node_pubkey) REFERENCES nodes(pubkey)
);

CREATE TABLE IF NOT EXISTS desired_settings (
    scope_type      TEXT NOT NULL,           -- 'global' | 'group' | 'node'
    scope_id        TEXT NOT NULL DEFAULT '', -- '' for global; group name; node pubkey
    key             TEXT NOT NULL,
    desired         TEXT,                     -- NULL = policy-only override (no value)
    policy          TEXT,                     -- NULL = inherit from a less specific scope
    poll_hours      INTEGER,                  -- NULL = inherit / built-in default
    PRIMARY KEY (scope_type, scope_id, key)
);

CREATE TABLE IF NOT EXISTS desired_operations (
    scope_type      TEXT NOT NULL,           -- 'global' | 'group' | 'node'
    scope_id        TEXT NOT NULL DEFAULT '', -- '' for global; group name; node pubkey
    command         TEXT NOT NULL,
    every_hours     INTEGER NOT NULL DEFAULT 24,
    fire_and_forget INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope_type, scope_id, command)
);

CREATE TABLE IF NOT EXISTS node_facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_pubkey     TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT,
    collected_at    TEXT NOT NULL,
    UNIQUE(node_pubkey, key),
    FOREIGN KEY (node_pubkey) REFERENCES nodes(pubkey)
);

CREATE TABLE IF NOT EXISTS operations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_pubkey     TEXT NOT NULL,
    command         TEXT NOT NULL,
    every_hours     INTEGER NOT NULL DEFAULT 24,
    status          TEXT NOT NULL DEFAULT 'pending',
    queued_at       TEXT NOT NULL,
    run_at          TEXT,
    response        TEXT,
    FOREIGN KEY (node_pubkey) REFERENCES nodes(pubkey)
);

CREATE TABLE IF NOT EXISTS logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_pubkey     TEXT,
    display_pubkey  TEXT,
    display_label   TEXT,
    timestamp       TEXT NOT NULL,
    level           TEXT NOT NULL,
    message         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS setting_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_pubkey     TEXT NOT NULL,
    key             TEXT NOT NULL,
    previous        TEXT,
    new_value       TEXT,
    command         TEXT NOT NULL,
    changed_at      TEXT NOT NULL,
    FOREIGN KEY (node_pubkey) REFERENCES nodes(pubkey)
);
"""


# Named, ordered schema migrations tracked by SQLite's PRAGMA user_version (the DB is now the only
# copy of desired state — post-sunset — so unversioned ad-hoc ALTERs are a real operator risk;
# RECOMMENDATIONS C3). `_SCHEMA` above is the base (CREATE TABLE IF NOT EXISTS, always applied);
# each migration below upgrades user_version n-1 -> n. Append new migrations; never edit a released
# one. Migration 1 adopts the columns that were previously added by ad-hoc ALTERs in open(): a
# legacy DB from before versioning sits at user_version 0 with those columns already present, so the
# apply step tolerates a duplicate-column error for this one-time adoption. Later migrations rely on
# the version gate running them exactly once and need no such guard.
_MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        # add label column to existing databases
        "ALTER TABLE nodes ADD COLUMN label TEXT NOT NULL DEFAULT ''",
        # count write attempts for each setting's current desired value
        "ALTER TABLE settings ADD COLUMN update_attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE logs ADD COLUMN display_pubkey TEXT",
        "ALTER TABLE logs ADD COLUMN display_label TEXT",
    ]),
    (2, [
        # when mch last *tried* to reach the node (any probe outcome), so an "unreachable"
        # row can say when the verdict was reached rather than implying it just happened (S2.4)
        "ALTER TABLE nodes ADD COLUMN last_attempt_at TEXT",
    ]),
]

_CURRENT_SCHEMA_VERSION = _MIGRATIONS[-1][0] if _MIGRATIONS else 0


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._run_migrations()
        await self._db.commit()
        log.debug("database opened: %s", self._path)

    async def _run_migrations(self) -> None:
        """Apply any schema migrations newer than the DB's recorded user_version."""
        db = self._db_required()
        async with db.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        version = int(row[0]) if row else 0
        for target, statements in _MIGRATIONS:
            if version >= target:
                continue
            for sql in statements:
                try:
                    await db.execute(sql)
                except aiosqlite.OperationalError as exc:
                    # Tolerate a column a pre-versioning (ad-hoc ALTER) DB already carries; this
                    # only matters for the one-time adoption in migration 1 (see _MIGRATIONS).
                    if "duplicate column name" not in str(exc).lower():
                        raise
            # PRAGMA cannot be parameterised; target is an int literal we control.
            await db.execute(f"PRAGMA user_version = {target}")
            version = target
            log.info("applied DB migration → schema version %d", target)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
            log.debug("database closed")

    def _db_required(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("database not open — call open() first")
        return self._db

    # ------------------------------------------------------------------
    # nodes
    # ------------------------------------------------------------------

    async def upsert_node(self, node: Node) -> None:
        db = self._db_required()
        await db.execute(
            """
            INSERT INTO nodes (pubkey, name, label, node_group, state, last_seen, last_connected,
                               auth_required, auth_cached, next_sync_at)
            VALUES (:pubkey, :name, :label, :group, :state, :last_seen, :last_connected,
                    :auth_required, :auth_cached, :next_sync_at)
            ON CONFLICT(pubkey) DO UPDATE SET
                name           = excluded.name,
                label          = excluded.label,
                node_group     = excluded.node_group,
                state          = excluded.state,
                last_seen      = excluded.last_seen,
                last_connected = excluded.last_connected,
                auth_required  = excluded.auth_required,
                auth_cached    = excluded.auth_cached,
                next_sync_at   = excluded.next_sync_at
            """,
            {
                "pubkey":         node.pubkey,
                "name":           node.name,
                "label":          node.label,
                "group":          node.group,
                "state":          node.state.value,
                "last_seen":      _dt(node.last_seen),
                "last_connected": _dt(node.last_connected),
                "auth_required":  int(node.auth_required),
                "auth_cached":    int(node.auth_cached),
                "next_sync_at":   _dt(node.next_sync_at),
            },
        )
        await db.commit()

    async def set_node_group(self, pubkey: str, group: str | None) -> None:
        """Update only a node's group (inventory metadata); None clears membership."""
        db = self._db_required()
        await db.execute(
            "UPDATE nodes SET node_group = ? WHERE pubkey = ?",
            (group, pubkey),
        )
        await db.commit()

    async def set_node_label(self, pubkey: str, label: str | None) -> None:
        """Update only a node's local label (inventory metadata); None clears it."""
        db = self._db_required()
        await db.execute(
            "UPDATE nodes SET label = ? WHERE pubkey = ?",
            (label, pubkey),
        )
        await db.commit()

    async def set_next_sync(self, pubkey: str, when: datetime) -> None:
        """Update only the next_sync_at timestamp for a node."""
        db = self._db_required()
        await db.execute(
            "UPDATE nodes SET next_sync_at = ? WHERE pubkey = ?",
            (_dt(when), pubkey),
        )
        await db.commit()

    async def set_node_last_attempt(self, pubkey: str, when: datetime) -> None:
        """Record when mch last *tried* to reach a node (any probe outcome).

        A dedicated setter (like set_next_sync) so `upsert_node` never has to carry this
        column and can't clobber it — the probe records the attempt directly. See S2.4.
        """
        db = self._db_required()
        await db.execute(
            "UPDATE nodes SET last_attempt_at = ? WHERE pubkey = ?",
            (_dt(when), pubkey),
        )
        await db.commit()

    async def get_due_node(self) -> Node | None:
        """Return the node with the earliest next_sync_at that is now due.

        Retired nodes are never due — they are kept for history but excluded from sync.
        """
        db = self._db_required()
        now = _dt(datetime.now(UTC))
        async with db.execute(
            """
            SELECT * FROM nodes
            WHERE (next_sync_at IS NULL OR next_sync_at <= ?)
              AND state != ?
            ORDER BY next_sync_at ASC NULLS FIRST
            LIMIT 1
            """,
            (now, NodeState.RETIRED.value),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_node(row) if row else None

    async def get_node(self, pubkey: str) -> Node | None:
        db = self._db_required()
        async with db.execute("SELECT * FROM nodes WHERE pubkey = ?", (pubkey,)) as cur:
            row = await cur.fetchone()
        return _row_to_node(row) if row else None

    async def get_all_nodes(self) -> list[Node]:
        db = self._db_required()
        async with db.execute("SELECT * FROM nodes ORDER BY name") as cur:
            rows = await cur.fetchall()
        return [_row_to_node(r) for r in rows]

    async def delete_node(self, pubkey: str) -> None:
        """Delete one node and its local DB state."""
        db = self._db_required()
        await db.execute("DELETE FROM settings WHERE node_pubkey = ?", (pubkey,))
        await db.execute("DELETE FROM node_facts WHERE node_pubkey = ?", (pubkey,))
        await db.execute("DELETE FROM operations WHERE node_pubkey = ?", (pubkey,))
        await db.execute("DELETE FROM logs WHERE node_pubkey = ?", (pubkey,))
        await db.execute("DELETE FROM nodes WHERE pubkey = ?", (pubkey,))
        await db.commit()

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    async def upsert_setting(self, setting: SettingRecord) -> None:
        db = self._db_required()
        await db.execute(
            """
            INSERT INTO settings (node_pubkey, key, desired, policy, poll_hours,
                                  observed, last_checked, last_set, update_attempts, drift)
            VALUES (:node_pubkey, :key, :desired, :policy, :poll_hours,
                    :observed, :last_checked, :last_set, :update_attempts, :drift)
            ON CONFLICT(node_pubkey, key) DO UPDATE SET
                desired     = excluded.desired,
                policy      = excluded.policy,
                poll_hours  = excluded.poll_hours,
                observed    = excluded.observed,
                last_checked= excluded.last_checked,
                last_set    = excluded.last_set,
                update_attempts= excluded.update_attempts,
                drift       = excluded.drift
            """,
            {
                "node_pubkey":  setting.node_pubkey,
                "key":          setting.key,
                "desired":      setting.desired,
                "policy":       setting.policy.value,
                "poll_hours":   setting.poll_hours,
                "observed":     setting.observed,
                "last_checked": _dt(setting.last_checked),
                "last_set":     _dt(setting.last_set),
                "update_attempts": setting.update_attempts,
                "drift":        int(setting.drift),
            },
        )
        await db.commit()

    async def get_settings(self, node_pubkey: str) -> list[SettingRecord]:
        db = self._db_required()
        async with db.execute(
            "SELECT * FROM settings WHERE node_pubkey = ? ORDER BY key",
            (node_pubkey,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_setting(r) for r in rows]

    async def get_drifted_settings(self) -> list[SettingRecord]:
        """Return all settings where observed != desired (drift=1)."""
        db = self._db_required()
        async with db.execute(
            "SELECT * FROM settings WHERE drift = 1 ORDER BY node_pubkey, key"
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_setting(r) for r in rows]

    async def record_setting_change(
        self,
        node_pubkey: str,
        key: str,
        previous: str | None,
        new_value: str | None,
        command: str,
    ) -> None:
        """Append a successful setting write to rollback/audit history."""
        db = self._db_required()
        await db.execute(
            """
            INSERT INTO setting_history
                (node_pubkey, key, previous, new_value, command, changed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (node_pubkey, key, previous, new_value, command, _dt(datetime.now(UTC))),
        )
        await db.commit()

    async def get_setting_history(self, node_pubkey: str) -> list[dict[str, Any]]:
        db = self._db_required()
        async with db.execute(
            "SELECT * FROM setting_history WHERE node_pubkey = ? "
            "ORDER BY changed_at DESC, id DESC",
            (node_pubkey,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # desired_settings (layered, CLI/API-authored source of truth)
    # ------------------------------------------------------------------

    async def set_desired_setting(self, setting: DesiredSetting) -> None:
        """Upsert one desired setting at its scope. (scope_type, scope_id, key) is unique."""
        db = self._db_required()
        await db.execute(
            """
            INSERT INTO desired_settings (scope_type, scope_id, key, desired, policy, poll_hours)
            VALUES (:scope_type, :scope_id, :key, :desired, :policy, :poll_hours)
            ON CONFLICT(scope_type, scope_id, key) DO UPDATE SET
                desired    = excluded.desired,
                policy     = excluded.policy,
                poll_hours = excluded.poll_hours
            """,
            {
                "scope_type": setting.scope_type.value,
                "scope_id":   setting.scope_id,
                "key":        setting.key,
                "desired":    setting.desired,
                "policy":     setting.policy.value if setting.policy else None,
                "poll_hours": setting.poll_hours,
            },
        )
        await db.commit()

    async def get_desired_settings(
        self, scope_type: Scope, scope_id: str
    ) -> list[DesiredSetting]:
        """Return all desired settings authored at one scope, ordered by key."""
        db = self._db_required()
        async with db.execute(
            "SELECT * FROM desired_settings WHERE scope_type = ? AND scope_id = ? ORDER BY key",
            (scope_type.value, scope_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_desired_setting(r) for r in rows]

    async def get_all_desired_settings(self) -> list[DesiredSetting]:
        """Return every desired setting across all scopes (for ``mch config export``)."""
        db = self._db_required()
        async with db.execute(
            "SELECT * FROM desired_settings ORDER BY scope_type, scope_id, key"
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_desired_setting(r) for r in rows]

    async def delete_desired_setting(
        self, scope_type: Scope, scope_id: str, key: str
    ) -> None:
        """Remove one desired setting (e.g. ``mch config unset``); leaves observed state alone."""
        db = self._db_required()
        await db.execute(
            "DELETE FROM desired_settings WHERE scope_type = ? AND scope_id = ? AND key = ?",
            (scope_type.value, scope_id, key),
        )
        await db.commit()

    # ------------------------------------------------------------------
    # desired operations (recurring commands; the DB home for operations)
    # ------------------------------------------------------------------

    async def set_desired_operation(self, op: DesiredOperation) -> None:
        """Upsert one desired operation at its scope. (scope_type, scope_id, command) is unique."""
        db = self._db_required()
        await db.execute(
            """
            INSERT INTO desired_operations
                (scope_type, scope_id, command, every_hours, fire_and_forget)
            VALUES (:scope_type, :scope_id, :command, :every_hours, :fire_and_forget)
            ON CONFLICT(scope_type, scope_id, command) DO UPDATE SET
                every_hours     = excluded.every_hours,
                fire_and_forget = excluded.fire_and_forget
            """,
            {
                "scope_type":      op.scope_type.value,
                "scope_id":        op.scope_id,
                "command":         op.command,
                "every_hours":     op.every_hours,
                "fire_and_forget": int(op.fire_and_forget),
            },
        )
        await db.commit()

    async def get_desired_operations(
        self, scope_type: Scope, scope_id: str
    ) -> list[DesiredOperation]:
        """Return all desired operations authored at one scope, ordered by command."""
        db = self._db_required()
        async with db.execute(
            "SELECT * FROM desired_operations WHERE scope_type = ? AND scope_id = ? "
            "ORDER BY command",
            (scope_type.value, scope_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_desired_operation(r) for r in rows]

    async def get_all_desired_operations(self) -> list[DesiredOperation]:
        """Return every desired operation across all scopes (for ``mch config export``)."""
        db = self._db_required()
        async with db.execute(
            "SELECT * FROM desired_operations ORDER BY scope_type, scope_id, command"
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_desired_operation(r) for r in rows]

    async def delete_desired_operation(
        self, scope_type: Scope, scope_id: str, command: str
    ) -> None:
        """Remove one desired operation; leaves run history (the operations table) alone."""
        db = self._db_required()
        await db.execute(
            "DELETE FROM desired_operations WHERE scope_type = ? AND scope_id = ? "
            "AND command = ?",
            (scope_type.value, scope_id, command),
        )
        await db.commit()

    # ------------------------------------------------------------------
    # facts (read-only CMDB inventory)
    # ------------------------------------------------------------------

    async def upsert_fact(self, fact: NodeFact) -> None:
        db = self._db_required()
        await db.execute(
            """
            INSERT INTO node_facts (node_pubkey, key, value, collected_at)
            VALUES (:node_pubkey, :key, :value, :collected_at)
            ON CONFLICT(node_pubkey, key) DO UPDATE SET
                value        = excluded.value,
                collected_at = excluded.collected_at
            """,
            {
                "node_pubkey":   fact.node_pubkey,
                "key":           fact.key,
                "value":         fact.value,
                "collected_at":  _dt(fact.collected_at),
            },
        )
        await db.commit()

    async def get_facts(self, node_pubkey: str) -> dict[str, str | None]:
        """Return all facts for a node as a plain dict."""
        db = self._db_required()
        async with db.execute(
            "SELECT key, value FROM node_facts WHERE node_pubkey = ?",
            (node_pubkey,),
        ) as cur:
            rows = await cur.fetchall()
        return {r["key"]: r["value"] for r in rows}

    async def get_fact_records(self, node_pubkey: str) -> list[NodeFact]:
        """Return all facts for a node with collection timestamps."""
        db = self._db_required()
        async with db.execute(
            "SELECT * FROM node_facts WHERE node_pubkey = ? ORDER BY key",
            (node_pubkey,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_fact(r) for r in rows]

    async def delete_fact(self, node_pubkey: str, key: str) -> None:
        """Delete one collected fact from local DB state."""
        db = self._db_required()
        await db.execute(
            "DELETE FROM node_facts WHERE node_pubkey = ? AND key = ?",
            (node_pubkey, key),
        )
        await db.commit()

    async def delete_setting(self, node_pubkey: str, key: str) -> None:
        """Delete one observed setting row (e.g. ``mch node clean settings``).

        Observed rows are a cache, never authoritative, so removing an orphaned one
        is always safe - it is re-observed next cycle if the key is still wanted.
        Desired state in ``desired_settings`` is untouched.
        """
        db = self._db_required()
        await db.execute(
            "DELETE FROM settings WHERE node_pubkey = ? AND key = ?",
            (node_pubkey, key),
        )
        await db.commit()

    # ------------------------------------------------------------------
    # operations
    # ------------------------------------------------------------------

    async def upsert_operation(self, op: OperationRecord) -> None:
        db = self._db_required()
        await db.execute(
            """
            INSERT INTO operations
                (node_pubkey, command, every_hours, status, queued_at, run_at, response)
            VALUES
                (:node_pubkey, :command, :every_hours, :status, :queued_at, :run_at, :response)
            ON CONFLICT DO NOTHING
            """,
            {
                "node_pubkey": op.node_pubkey,
                "command":     op.command,
                "every_hours": op.every_hours,
                "status":      op.status.value,
                "queued_at":   _dt(op.queued_at),
                "run_at":      _dt(op.run_at),
                "response":    op.response,
            },
        )
        await db.commit()

    async def get_last_operation(self, node_pubkey: str, command: str) -> OperationRecord | None:
        """Return the most recent record for (node, command), or None if never run."""
        db = self._db_required()
        async with db.execute(
            "SELECT * FROM operations WHERE node_pubkey = ? AND command = ?"
            " ORDER BY COALESCE(run_at, queued_at) DESC LIMIT 1",
            (node_pubkey, command),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_operation(row) if row else None

    async def get_pending_operations(self, node_pubkey: str) -> list[OperationRecord]:
        db = self._db_required()
        async with db.execute(
            "SELECT * FROM operations"
            " WHERE node_pubkey = ? AND status = 'pending' ORDER BY queued_at",
            (node_pubkey,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_operation(r) for r in rows]

    async def update_operation(self, op: OperationRecord) -> None:
        db = self._db_required()
        await db.execute(
            "UPDATE operations SET status=?, run_at=?, response=? WHERE id=?",
            (op.status.value, _dt(op.run_at), op.response, getattr(op, "id", None)),
        )
        await db.commit()

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------

    async def write_log(
        self,
        node_pubkey: str | None,
        level: str,
        message: str,
        *,
        display_pubkey: str | None = None,
        display_label: str | None = None,
    ) -> None:
        db = self._db_required()
        await db.execute(
            """
            INSERT INTO logs (node_pubkey, display_pubkey, display_label, timestamp, level, message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (node_pubkey, display_pubkey, display_label, _dt(datetime.now(UTC)), level, message),
        )
        await db.commit()

    async def get_logs(
        self, node_pubkey: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        db = self._db_required()
        if node_pubkey:
            async with db.execute(
                "SELECT * FROM logs WHERE node_pubkey = ? ORDER BY timestamp DESC LIMIT ?",
                (node_pubkey, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _dt(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _row_to_node(row: aiosqlite.Row) -> Node:
    return Node(
        pubkey=row["pubkey"],
        name=row["name"],
        label=row["label"],
        group=row["node_group"],
        state=NodeState(row["state"]),
        last_seen=_parse_dt(row["last_seen"]),
        last_connected=_parse_dt(row["last_connected"]),
        auth_required=bool(row["auth_required"]),
        auth_cached=bool(row["auth_cached"]),
        next_sync_at=_parse_dt(row["next_sync_at"]),
        last_attempt_at=_parse_dt(row["last_attempt_at"]),
    )


def _row_to_setting(row: aiosqlite.Row) -> SettingRecord:
    return SettingRecord(
        node_pubkey=row["node_pubkey"],
        key=row["key"],
        desired=row["desired"],
        policy=SettingPolicy(row["policy"]),
        poll_hours=row["poll_hours"],
        observed=row["observed"],
        last_checked=_parse_dt(row["last_checked"]),
        last_set=_parse_dt(row["last_set"]),
        update_attempts=row["update_attempts"],
        drift=bool(row["drift"]),
    )


def _row_to_desired_setting(row: aiosqlite.Row) -> DesiredSetting:
    return DesiredSetting(
        scope_type=Scope(row["scope_type"]),
        scope_id=row["scope_id"],
        key=row["key"],
        desired=row["desired"],
        policy=SettingPolicy(row["policy"]) if row["policy"] else None,
        poll_hours=row["poll_hours"],
    )


def _row_to_desired_operation(row: aiosqlite.Row) -> DesiredOperation:
    return DesiredOperation(
        scope_type=Scope(row["scope_type"]),
        scope_id=row["scope_id"],
        command=row["command"],
        every_hours=row["every_hours"],
        fire_and_forget=bool(row["fire_and_forget"]),
    )


def _row_to_fact(row: aiosqlite.Row) -> NodeFact:
    return NodeFact(
        node_pubkey=row["node_pubkey"],
        key=row["key"],
        value=row["value"],
        collected_at=_parse_dt(row["collected_at"]) or datetime.now(UTC),
    )


def _row_to_operation(row: aiosqlite.Row) -> OperationRecord:
    return OperationRecord(
        node_pubkey=row["node_pubkey"],
        command=row["command"],
        every_hours=row["every_hours"],
        status=OperationStatus(row["status"]),
        queued_at=_parse_dt(row["queued_at"]) or datetime.now(UTC),
        run_at=_parse_dt(row["run_at"]),
        response=row["response"],
        id=row["id"],
    )
