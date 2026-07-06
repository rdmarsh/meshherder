"""
mchd daemon — main entry point.

Lifecycle:
  1. Load config
  2. Open DB
  3. Connect to gateway
  4. Start Unix socket server (for mch CLI)
  5. Run work queue until SIGTERM/SIGINT
  6. Graceful shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from meshcore.events import EventType

from mch.command_registry import readable_observation_keys, validate_value
from mch.command_safety import blocked_exec_command
from mch.config import (
    DIRECTIVE_KEYS,
    Config,
    GroupConfig,
    NodeConfig,
    config_dict_to_yaml,
    config_to_desired_operations,
    config_to_desired_rows,
    desired_rows_to_config_dict,
    is_full_pubkey,
    normalize_directive_value,
    resolve_desired,
    resolve_desired_configs,
    resolve_name_prefix,
    settable_keys,
    validate_directive_value,
    validate_group_name,
    validate_node_label,
)
from mch.config import load as load_config
from mch.db import Database
from mch.gateway import Gateway, GatewayError
from mch.messenger import Messenger
from mch.models import DesiredSetting, Node, NodeState, Scope, SettingPolicy
from mch.queue import (
    NodeResult,
    OperationBusyError,
    WorkQueue,
    all_setting_keys,
    resolve_policy,
    resolved_fact_keys,
)
from mch.report import (
    build_fleet_report,
    build_node_report,
)

log = logging.getLogger(__name__)

# Include inode/change time so atomic replacements and timestamp-preserving copies are detected.
FileSignature = tuple[int, int, int, int, int] | None
CONFIG_WATCH_INTERVAL_SECONDS = 10.0
CONFIG_ERROR_EXIT = getattr(os, "EX_CONFIG", 78)


def _sync_result_response(result: NodeResult) -> dict[str, Any]:
    """Describe a forced node sync without claiming unresolved work succeeded."""
    target = f"{result.name!r} ({result.pubkey[:8]})"
    if result.error:
        return {"ok": False, "error": f"sync failed for {target}: {result.error}"}
    if result.skipped_reason:
        return {
            "ok": True,
            "data": f"sync skipped for {target}: {result.skipped_reason}",
        }

    partial = []
    if result.enforced_settings_drifted:
        partial.append(f"{result.enforced_settings_drifted} enforced setting(s) still drifted")
    if result.enforced_settings_failed:
        partial.append(f"{result.enforced_settings_failed} enforced setting read(s) failed")
    if result.operations_failed:
        partial.append(f"{result.operations_failed} operation(s) failed")
    if partial:
        details = "; ".join(partial)
        return {"ok": True, "data": f"sync completed for {target} (partial: {details})"}

    return {"ok": True, "data": f"synced {target}"}


async def _handle_advert(db: Database, pubkey: str) -> None:
    """Update last_seen for a known managed node when it sends an advert."""
    node = await db.get_node(pubkey)
    if node is None:
        return
    node.last_seen = datetime.now(UTC)
    await db.upsert_node(node)
    log.debug("advert from %s — last_seen updated", pubkey[:12])


class ClientDisconnected(Exception):
    """Raised when a long-running RPC client disappears before completion."""


# ---------------------------------------------------------------------------
# socket protocol
# ---------------------------------------------------------------------------
# Commands sent by mch CLI over the Unix socket.
# Request:  {"cmd": "<name>", ...args}  (newline-terminated JSON)
# Response: {"ok": true/false, "data": ..., "error": "..."}

async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    wq: WorkQueue,
    db: Database,
    config: Config,
) -> None:
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=10)
        req = json.loads(raw)
        resp = await _dispatch(req, wq, db, config, writer, reader)
    except TimeoutError:
        resp = {"ok": False, "error": "timeout reading command"}
    except json.JSONDecodeError as exc:
        resp = {"ok": False, "error": f"bad JSON: {exc}"}
    except Exception as exc:
        log.exception("client handler error: %s", exc)
        resp = {"ok": False, "error": str(exc)}

    try:
        writer.write((json.dumps(resp) + "\n").encode())
        await writer.drain()
        writer.close()
    except (ConnectionResetError, BrokenPipeError):
        pass  # client disconnected before response was sent (e.g. CLI timed out)


async def _resolve_node(query: str, db: Database) -> Any:
    """Resolve a pubkey prefix, label substring, or name substring to a single Node.

    Returns None if nothing matches.
    Raises ValueError if the query is ambiguous (matches more than one node).
    """
    node = await db.get_node(query)
    if node:
        return node
    all_nodes = await db.get_all_nodes()
    # pubkey prefix match
    node = next((n for n in all_nodes if n.pubkey.startswith(query)), None)
    if node:
        return node
    # label/name substring match
    q = query.lower()
    matches = [
        n for n in all_nodes
        if q in (n.label or "").lower() or q in n.name.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        labels = ", ".join(repr(n.label or n.pubkey[:8]) for n in matches)
        raise ValueError(f"ambiguous: {len(matches)} nodes match {query!r} ({labels})")
    return None


async def _stale_fact_candidates(
    db: Database,
    config: Config,
    *,
    query: str | None = None,
    group: str | None = None,
    fact_key: str | None = None,
) -> list[dict[str, Any]]:
    if query:
        node = await _resolve_node(query, db)
        if not node:
            raise ValueError(f"node {query!r} not found")
        nodes = [node]
    else:
        nodes = await db.get_all_nodes()
        if group is not None:
            nodes = [node for node in nodes if node.group == group]

    candidates: list[dict[str, Any]] = []
    for node in nodes:
        # "Configured facts" is now per-node: the keys resolving to policy=fact (sunset),
        # not one global config.facts list. A cached fact whose key no longer resolves to
        # fact for this node is stale and offered for cleanup.
        node_cfg, group_cfg, global_cfg, _ = await resolve_desired_configs(db, node)
        configured_facts = resolved_fact_keys(node_cfg, group_cfg, global_cfg)
        for fact in await db.get_fact_records(node.pubkey):
            if fact_key is not None and fact.key != fact_key:
                continue
            if fact.key in configured_facts:
                continue
            candidates.append({
                "pubkey": node.pubkey,
                "label": node.label,
                "name": node.name,
                "group": node.group,
                "key": fact.key,
                "value": fact.value,
                "collected_at": fact.collected_at.isoformat(),
            })
    return candidates


async def _wanted_setting_keys(db: Database, node: Any) -> set[str]:
    """Setting keys this node currently wants observed: its resolved desired keys.

    Desired state is resolved entirely from the DB (DESIGN.md "Desired state in the DB" /
    sunset), so a key set only via `mch config set` - or one on a node that was never in
    config.yaml - still counts as wanted and is never treated as orphaned. The node's
    group comes from the DB row, so group-scope keys are always attributable now (the old
    config-only path could only attribute them when the node appeared in config.yaml).
    """
    node_cfg, group_cfg, global_cfg, _ = await resolve_desired_configs(db, node)
    return all_setting_keys(node_cfg, group_cfg, global_cfg)


async def _stale_setting_candidates(
    db: Database,
    config: Config,
    *,
    query: str | None = None,
    group: str | None = None,
    setting_key: str | None = None,
) -> list[dict[str, Any]]:
    """Observed `settings` rows whose key is no longer wanted (mirror of stale facts).

    A key is wanted if it is in the node's resolved desired set (config + DB overlay).
    Orphans appear when a key is unset, dropped from a group/global, or the node leaves
    config; they are safe to delete because observed rows are a re-derivable cache.
    """
    if query:
        node = await _resolve_node(query, db)
        if not node:
            raise ValueError(f"node {query!r} not found")
        nodes = [node]
    else:
        nodes = await db.get_all_nodes()
        if group is not None:
            nodes = [node for node in nodes if node.group == group]

    candidates: list[dict[str, Any]] = []
    for node in nodes:
        wanted = await _wanted_setting_keys(db, node)
        for setting in await db.get_settings(node.pubkey):
            if setting_key is not None and setting.key != setting_key:
                continue
            if setting.key in wanted:
                continue
            candidates.append({
                "pubkey": node.pubkey,
                "label": node.label,
                "name": node.name,
                "group": node.group,
                "key": setting.key,
                "value": setting.observed,
                "collected_at": (
                    setting.last_checked.isoformat() if setting.last_checked else None
                ),
            })
    return candidates


async def _resolve_setting_scope(
    req: dict[str, Any], db: Database
) -> tuple[Scope | None, str, dict[str, Any], str | None]:
    """Resolve the (scope, scope_id, display data) for a set/unset request.

    Returns ``(scope, scope_id, data, error)``; on failure ``scope`` is None and
    ``error`` is set. Node scope resolves the node query to a pubkey and surfaces
    label/name for display; group scope passes the group name through as the scope_id
    (whether it is a real configured group is the caller's concern - set validates it,
    unset does not so a row for a now-removed group can still be cleared); global scope
    needs no target. ``scope`` defaults to ``node`` so pre-scope clients keep working.
    """
    scope_raw = str(req.get("scope") or "node")
    if scope_raw == "node":
        query = str(req.get("query") or "")
        if not query:
            return None, "", {}, "missing node"
        try:
            node = await _resolve_node(query, db)
        except ValueError as exc:
            return None, "", {}, str(exc)
        if not node:
            return None, "", {}, f"node {query!r} not found"
        return Scope.NODE, node.pubkey, {
            "scope": "node",
            "pubkey": node.pubkey,
            "label": node.label,
            "name": node.name,
        }, None
    if scope_raw == "group":
        group = str(req.get("group") or "")
        if not group:
            return None, "", {}, "missing group name"
        return Scope.GROUP, group, {"scope": "group", "group": group}, None
    if scope_raw == "global":
        return Scope.GLOBAL, "", {"scope": "global"}, None
    return None, "", {}, f"invalid scope {scope_raw!r} (choose node, group, or global)"


def _unknown_group_error(name: str) -> str:
    """Uniform error for targeting a group that is not in the registry (R4 S1).

    Groups are first-class: they must be created before `group set`/`node move` can target
    them, so a typo'd or not-yet-created group is rejected with a create hint rather than
    silently writing a dead row.
    """
    return (
        f"unknown group {name!r} - create it first with `mch group create {name}` "
        "(or check the spelling)"
    )


def _scope_target_label(data: dict[str, Any]) -> str:
    """Human label for a resolved scope, used in error messages."""
    if data.get("scope") == "node":
        return data.get("label") or str(data.get("pubkey", ""))[:8]
    if data.get("scope") == "group":
        return f"group {data.get('group')!r}"
    return "global"


async def _handle_set_setting(
    req: dict[str, Any], db: Database, config: Config
) -> dict[str, Any]:
    """Write a desired setting to the DB (the authoritative source) at the given scope.

    Validates the key (must be a writable setting), the value, and the policy before
    recording intent synchronously. Scope is node (default), group, or global.
    Convergence is left to the next sync cycle or an explicit ``mch fleet sync`` - recording
    intent and reaching the node are decoupled (DESIGN.md "Desired state in the DB").
    """
    key = str(req.get("key") or "")
    value = req.get("value")
    policy_raw = req.get("policy")

    if not key:
        return {"ok": False, "error": "missing setting key"}
    if value is None and policy_raw is None:
        return {"ok": False, "error": "provide a value, a --policy, or both"}

    # Parse the policy first so the key gate below can reason about it: a fact
    # (the fourth policy) is legal on read-only keys, a desired value is not.
    policy: SettingPolicy | None = None
    if policy_raw is not None:
        try:
            policy = SettingPolicy(str(policy_raw))
        except ValueError:
            valid = ", ".join(p.value for p in SettingPolicy)
            return {"ok": False, "error": f"invalid policy {policy_raw!r} (choose from: {valid})"}

    # Key gate. Writable settings and directives accept any policy/value. A read-only
    # fact key (ver/board/role/neighbors/owner.name) is not a writable setting, but it
    # can still be collected as a fact (--policy fact) or parked (--policy skip) - never
    # given a desired value, since there is no `set` command for it. enforce/audit are
    # rejected on such keys. See DESIGN.md "Facts vs settings" (fact is a fourth policy).
    if key not in settable_keys():
        is_readable = key in readable_observation_keys()
        if policy in (SettingPolicy.FACT, SettingPolicy.SKIP) and is_readable:
            if value is not None:
                return {"ok": False, "error": f"{key!r} is read-only; drop the value "
                        "(use --policy fact to collect it as an observation)"}
        elif is_readable:
            return {"ok": False, "error": f"{key!r} is a read-only fact; only --policy fact "
                    "or skip is allowed (it cannot be enforced)"}
        else:
            return {"ok": False, "error": f"{key!r} is not a writable setting"}

    # A fact has no opinion, so a desired value is contradictory even on a writable key.
    if policy is SettingPolicy.FACT and value is not None:
        return {"ok": False, "error": "--policy fact takes no value "
                "(it records the observed value, not a desired one)"}

    if value is not None:
        value = str(value)
        # Normalise directive whitespace before validating/storing: leading space dropped
        # (name_prefix keeps a meaningful trailing space), all-space collapses to "" (cleared).
        if key in DIRECTIVE_KEYS:
            value = normalize_directive_value(key, value)
        # validate_value covers node CLI settings; directives have their own validator
        # (e.g. default_policy must be enforce/audit/skip) since they carry no spec.
        error = validate_value(key, value) or validate_directive_value(key, value)
        if error:
            return {"ok": False, "error": f"{key} {error}: {value!r}"}

    scope, scope_id, data, error = await _resolve_setting_scope(req, db)
    if error or scope is None:
        return {"ok": False, "error": error or "could not resolve scope"}
    # Catch a typo'd group name at write time: a desired row for an unregistered group would
    # silently never apply. Groups are first-class now (R4 S1): a group must be created before
    # it can be targeted. unset does not check this (see resolver docstring).
    if scope is Scope.GROUP and not await db.group_exists(scope_id):
        return {"ok": False, "error": _unknown_group_error(scope_id)}

    await db.set_desired_setting(
        DesiredSetting(scope_type=scope, scope_id=scope_id, key=key, desired=value, policy=policy)
    )
    data.update({"key": key, "value": value, "policy": policy.value if policy else None})
    return {"ok": True, "data": data}


async def _handle_unset_setting(
    req: dict[str, Any], db: Database, config: Config
) -> dict[str, Any]:
    """Delete a desired setting at the given scope; observed state is left intact."""
    key = str(req.get("key") or "")
    if not key:
        return {"ok": False, "error": "missing setting key"}

    scope, scope_id, data, error = await _resolve_setting_scope(req, db)
    if error or scope is None:
        return {"ok": False, "error": error or "could not resolve scope"}

    existing = await db.get_desired_settings(scope, scope_id)
    if not any(row.key == key for row in existing):
        return {"ok": False, "error": f"no DB-set {key!r} on {_scope_target_label(data)}"}
    await db.delete_desired_setting(scope, scope_id, key)
    data["key"] = key
    return {"ok": True, "data": data}


def _desired_source(
    key: str,
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
    global_cfg: GroupConfig | None,
) -> str | None:
    """Which scope supplied the resolved desired value (most specific with a value wins)."""
    for scope_name, cfg_obj in (
        ("node", node_cfg), ("group", group_cfg), ("global", global_cfg)
    ):
        sc = cfg_obj.settings.get(key) if cfg_obj else None
        if sc and sc.desired is not None:
            return scope_name
    return None


def _scope_phrase(scope_name: str | None, group: str | None) -> str:
    """Human phrase for a scope in directive notes: node / group 'x' / global."""
    if scope_name == "group":
        return f"group {group!r}"
    return scope_name or "?"


def _directive_note(
    key: str,
    node: Node,
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
    global_cfg: GroupConfig | None,
    resolved_desired: str | None,
) -> str | None:
    """Explain a directive that transformed a key's resolved desired value.

    Only ``name_prefix`` transforms a *settings-key* desired value today (it is prepended
    to ``name``); ``default_policy``/``via_radio`` change reachability/policy resolution,
    not a per-key desired string, so they do not produce a per-row note here. Survey
    evidence (2026-07-05): corbin's stored ``name`` row was 'Quakers Hill Corb' while the
    resolved value was '🦷 Quakers Hill Corb' with nothing on screen explaining the '🦷 '.
    """
    if key != "name" or resolved_desired is None:
        return None
    prefix = resolve_name_prefix(node_cfg, group_cfg, global_cfg)
    if not prefix or not resolved_desired.startswith(prefix):
        return None
    base_source = _desired_source("name", node_cfg, group_cfg, global_cfg)
    for scope_name, cfg_obj in (
        ("node", node_cfg), ("group", group_cfg), ("global", global_cfg)
    ):
        if cfg_obj and getattr(cfg_obj, "name_prefix", None):
            prefix_scope = scope_name
            break
    else:
        prefix_scope = None
    return (
        f"{base_source or 'inherited'} value + name_prefix {prefix!r} "
        f"from {_scope_phrase(prefix_scope, node.group)}"
    )


def _resolve_setting_row(
    key: str,
    node: Node,
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
    global_cfg: GroupConfig | None,
    settings_by_key: dict[str, Any],
    facts_by_key: dict[str, Any],
) -> dict[str, Any]:
    """One key's resolution for the all-keys ``mch config show NODE`` table."""
    resolved_desired = resolve_desired(key, node_cfg, group_cfg, global_cfg)
    observed, observed_at = _observed_for_key(key, settings_by_key, facts_by_key)
    return {
        "key": key,
        "policy": resolve_policy(key, node_cfg, group_cfg, global_cfg).value,
        "desired": resolved_desired,
        "desired_source": _desired_source(key, node_cfg, group_cfg, global_cfg),
        "observed": observed,
        "observed_at": observed_at,
        "directive": _directive_note(
            key, node, node_cfg, group_cfg, global_cfg, resolved_desired
        ),
    }


def _observed_for_key(
    key: str, settings_by_key: dict[str, Any], facts_by_key: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Observed value + iso timestamp for a key (settings store, then node_facts store).

    A key lives in exactly one store per the single-observation invariant, so the settings
    store is checked first and facts only as a fallback.
    """
    setting = settings_by_key.get(key)
    if setting is not None:
        at = setting.last_checked.isoformat() if setting.last_checked else None
        return setting.observed, at
    fact = facts_by_key.get(key)
    if fact is not None:
        return fact.value, fact.collected_at.isoformat()
    return None, None


async def _handle_show_scope(req: dict[str, Any], db: Database) -> dict[str, Any]:
    """List the desired/policy rows stored *at* a group or global scope (no node resolution).

    The read-side complement to `mch config set --group/--global`: a straight dump of the
    rows at that scope, so `show` completes the grammar triangle with `set`/`unset`.
    """
    scope, scope_id, data, error = await _resolve_setting_scope(req, db)
    if error or scope is None:
        return {"ok": False, "error": error or "could not resolve scope"}
    # Mirror `set`'s typo guard: an unregistered group would silently never apply.
    if scope is Scope.GROUP and not await db.group_exists(scope_id):
        return {"ok": False, "error": _unknown_group_error(scope_id)}
    rows = [
        {
            "key": row.key,
            "desired": row.desired,
            "policy": row.policy.value if row.policy else None,
        }
        for row in sorted(await db.get_desired_settings(scope, scope_id), key=lambda r: r.key)
    ]
    data["settings"] = rows
    return {"ok": True, "data": data}


async def _handle_show_setting(
    req: dict[str, Any], db: Database, config: Config
) -> dict[str, Any]:
    """Explain settings for one node (all keys, or one key traced through the scopes).

    The read-side complement to `mch config set`/`mch config unset`. Resolution is
    Node > Group > Global. With a ``scope`` of group/global it dumps that scope's stored
    rows instead (see `_handle_show_scope`). With no ``key`` it lists every resolving key
    (configured keys plus observed-only leftovers) oriented around resolution. With a
    ``key`` it traces that one key: the per-scope desired/policy rows that feed
    `resolve_desired`/`resolve_policy`, plus the value mch has actually observed (settings
    or node_facts store - a key lives in exactly one). It sends no mesh traffic.
    """
    if req.get("scope") in ("group", "global"):
        return await _handle_show_scope(req, db)

    node = await _resolve_node(str(req.get("query") or ""), db)
    if not node:
        return {"ok": False, "error": f"node {req.get('query')!r} not found"}

    node_cfg, group_cfg, global_cfg, _ = await resolve_desired_configs(db, node)
    settings_by_key = {s.key: s for s in await db.get_settings(node.pubkey)}
    facts_by_key = {f.key: f for f in await db.get_fact_records(node.pubkey)}

    key = str(req.get("key") or "")
    if not key:
        # All-keys listing: configured keys plus observed-only setting leftovers, the same
        # key set `mch node status` renders, but oriented around resolution (the FROM column).
        keys = sorted(all_setting_keys(node_cfg, group_cfg, global_cfg) | set(settings_by_key))
        return {"ok": True, "data": {
            "pubkey": node.pubkey,
            "label": node.label,
            "name": node.name,
            "group": node.group,
            "all_keys": True,
            "settings": [
                _resolve_setting_row(
                    k, node, node_cfg, group_cfg, global_cfg, settings_by_key, facts_by_key
                )
                for k in keys
            ],
        }}

    # Single-key trace. Validate the key like `config set` does, so a typo can't masquerade
    # as "nothing is set" (survey: `config show corbin node.owner` happily reported a
    # resolution for a non-key).
    if key not in settable_keys() | readable_observation_keys():
        valid = ", ".join(sorted(settable_keys()))
        return {"ok": False, "error": f"{key!r} is not a valid setting key (choose from: {valid})"}

    configured = key in all_setting_keys(node_cfg, group_cfg, global_cfg)
    resolved_desired = resolve_desired(key, node_cfg, group_cfg, global_cfg)
    observed, observed_at = _observed_for_key(key, settings_by_key, facts_by_key)

    # Per-scope rows that exist for this key, for the resolution trace.
    scopes: list[dict[str, Any]] = []
    scope_lookups: list[tuple[str, Scope, str]] = [("global", Scope.GLOBAL, "")]
    if node.group:
        scope_lookups.append(("group", Scope.GROUP, node.group))
    scope_lookups.append(("node", Scope.NODE, node.pubkey))
    for scope_name, scope, scope_id in scope_lookups:
        for row in await db.get_desired_settings(scope, scope_id):
            if row.key == key:
                scopes.append({
                    "scope": scope_name,
                    "scope_id": scope_id,
                    "desired": row.desired,
                    "policy": row.policy.value if row.policy else None,
                })
                break

    return {"ok": True, "data": {
        "key": key,
        "pubkey": node.pubkey,
        "label": node.label,
        "name": node.name,
        "group": node.group,
        "configured": configured,
        "resolved": {
            "policy": resolve_policy(key, node_cfg, group_cfg, global_cfg).value,
            "desired": resolved_desired,
            "desired_source": _desired_source(key, node_cfg, group_cfg, global_cfg),
            "observed": observed,
            "observed_at": observed_at,
            "directive": _directive_note(
                key, node, node_cfg, group_cfg, global_cfg, resolved_desired
            ),
        },
        "scopes": scopes,
    }}


async def _handle_import(
    req: dict[str, Any], db: Database, config: Config
) -> dict[str, Any]:
    """Seed the DB's desired_settings from a config.yaml (the migration on-ramp).

    Flattens config desired state (settings + directives, all scopes) into rows and upserts
    them. Node scope_ids need the full pubkey, resolved from each prefix against nodes the DB
    already knows; a node not yet seen is skipped with a warning. Dry-run by default - it only
    writes when ``dry_run`` is false. Idempotent: re-importing upserts the same rows.

    With ``path`` set, the source is that explicit (legacy/exported) YAML file, parsed
    leniently. This keeps import working against a strict daemon whose own config.yaml no
    longer carries desired state. Without ``path``, the daemon's loaded config is the source.
    """
    dry_run = bool(req.get("dry_run", True))
    path = req.get("path")
    if path:
        try:
            config = load_config(Path(str(path)), load_secrets=False)
        except Exception as exc:
            return {"ok": False, "error": f"cannot read import file {path}: {exc}"}
    nodes = await db.get_all_nodes()
    node_by_pubkey = {n.pubkey: n for n in nodes}

    def resolve_pubkey(prefix: str) -> str | None:
        match = next((n.pubkey for n in nodes if n.pubkey.startswith(prefix)), None)
        if match:
            return match
        # Restore-from-backup: a full pubkey (as `mch fleet export` emits) is the node identity
        # itself, so accept it even when the gateway/DB has not seen the node yet. The node row
        # is created below; the favourite mirror re-pins it once its contact reappears. A short
        # prefix still needs a known node (its full pubkey is unknowable) and is skipped+warned.
        return prefix if is_full_pubkey(prefix) else None

    # Retired nodes are a deliberate choice on restore (the backup tags them `retired: true`):
    # keeping them restores them as retired, skipping omits them entirely. A short-prefix retired
    # entry that can't resolve is moot. Force the operator to choose before writing.
    retired_cfg = [nc for nc in config.nodes if nc.retired]
    retired_pubkeys = {
        pk for nc in retired_cfg if (pk := resolve_pubkey(nc.pubkey_prefix)) is not None
    }
    mode = req.get("retired")  # "keep" | "skip" | None
    if retired_pubkeys and mode is None and not dry_run:
        return {
            "ok": False,
            "error": (
                f"backup contains {len(retired_pubkeys)} retired node(s) — choose "
                "--retired keep (restore them as retired) or --retired skip (omit them)"
            ),
            "data": {"retired_in_backup": len(retired_pubkeys)},
        }
    drop_retired = mode != "keep"  # skip mode, or the dry-run preview default

    def is_dropped(scope_type: Scope, scope_id: str) -> bool:
        return drop_retired and scope_type is Scope.NODE and scope_id in retired_pubkeys

    rows, warnings = config_to_desired_rows(config, resolve_pubkey)
    operations, op_warnings = config_to_desired_operations(config, resolve_pubkey)
    warnings = warnings + op_warnings
    if drop_retired and retired_pubkeys:
        rows = [r for r in rows if not is_dropped(r.scope_type, r.scope_id)]
        operations = [o for o in operations if not is_dropped(o.scope_type, o.scope_id)]

    # Label and group are inventory metadata on the node row, not desired_settings, so carry
    # them too. A resolvable node not yet in the DB is *created* (the restore path); a known one
    # has its label/group (and retired state, when kept) refreshed.
    inventory_writes: list[Node] = []
    nodes_created = 0
    for nc in config.nodes:
        pk = resolve_pubkey(nc.pubkey_prefix)
        if pk is None:
            continue
        if nc.retired and drop_retired:
            continue  # skip mode: leave retired nodes out of the restore entirely
        target_state = NodeState.RETIRED if nc.retired else NodeState.UNKNOWN
        node = node_by_pubkey.get(pk)
        if node is None:
            inventory_writes.append(Node(
                pubkey=pk, name="", label=nc.label or "", group=nc.group,
                state=target_state,
            ))
            nodes_created += 1
        else:
            changed = False
            if nc.label and node.label != nc.label:
                node.label = nc.label
                changed = True
            if nc.group is not None and node.group != nc.group:
                node.group = nc.group
                changed = True
            if nc.retired and node.state is not NodeState.RETIRED:
                node.state = NodeState.RETIRED
                changed = True
            if changed:
                inventory_writes.append(node)

    if not dry_run:
        for row in rows:
            await db.set_desired_setting(row)
        for op in operations:
            await db.set_desired_operation(op)
        for node in inventory_writes:
            await db.upsert_node(node)
        # Register every group this import references so a later `group set`/`node move` accepts
        # it — import writes group rows and node membership directly, bypassing the create-first
        # gate, so it must seed the registry itself (the restore-from-backup on-ramp). R4 S1.
        imported_groups = (
            {r.scope_id for r in rows if r.scope_type is Scope.GROUP}
            | {o.scope_id for o in operations if o.scope_type is Scope.GROUP}
            | {n.group for n in inventory_writes if n.group}
        )
        for group_name in imported_groups:
            if group_name:
                await db.create_group(group_name)

    counts = {Scope.GLOBAL.value: 0, Scope.GROUP.value: 0, Scope.NODE.value: 0}
    for row in rows:
        counts[row.scope_type.value] += 1
    total = len(rows) + len(operations)
    return {
        "ok": True,
        "data": {
            "counts": {"global": counts["global"], "group": counts["group"],
                       "node": counts["node"]},
            "operations": len(operations),
            "total": total,
            "nodes_created": nodes_created,
            "retired_in_backup": len(retired_pubkeys),
            "retired_mode": mode,
            "warnings": warnings,
            "written": 0 if dry_run else total,
            "dry_run": dry_run,
        },
    }


async def _handle_export(db: Database) -> dict[str, Any]:
    """Dump the whole fleet as config.yaml-shaped YAML — a complete, faithful backup.

    Includes every node's identity, label, group, desired settings, and operations, so
    `mch fleet import` can rebuild the fleet after a server/gateway loss. Retired nodes are tagged
    ``retired: true`` (the one inventory-state field carried in the file); `mch fleet import` then
    lets the operator choose whether to restore or skip them.
    """
    rows = await db.get_all_desired_settings()
    operations = await db.get_all_desired_operations()
    nodes = await db.get_all_nodes()
    data = desired_rows_to_config_dict(rows, operations, node_inventory=nodes)
    yaml_text = config_dict_to_yaml(data) if data else ""
    return {
        "ok": True,
        # total drives the CLI's "nothing to export" message, so count nodes too: a node with
        # inventory but no desired settings is still worth exporting.
        "data": {"yaml": yaml_text, "total": len(rows) + len(operations) + len(nodes)},
    }


@dataclass
class _Dispatch:
    """Everything a request handler might need, bundled so the table has one signature."""
    req: dict[str, Any]
    wq: WorkQueue
    db: Database
    config: Config
    writer: asyncio.StreamWriter | None = None
    reader: asyncio.StreamReader | None = None


# A handler takes the bundled context and returns the RPC response envelope.
Handler = Callable[[_Dispatch], Awaitable[dict[str, Any]]]


def _progress_writer(
    writer: asyncio.StreamWriter | None, disconnect_label: str
) -> Callable[[str], Awaitable[None]] | None:
    """Build a progress callback that streams ``{type:progress,msg}`` lines to a socket client.

    Returns None when there is no writer (a non-streaming call), so a handler can pass the result
    straight through as ``progress=``. Raises ClientDisconnected if the client has gone away, so the
    caller aborts the in-flight operation. Factored from five identical inline closures.
    """
    if writer is None:
        return None

    async def cb(msg: str) -> None:
        try:
            writer.write((json.dumps({"type": "progress", "msg": msg}) + "\n").encode())
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            raise ClientDisconnected(disconnect_label) from None

    return cb


def _parse_radio_arg(raw: Any) -> tuple[float, float, int, int] | None:
    """Parse an RPC ``radio`` argument ([freq,bw,sf,cr]); None if malformed."""
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        return None
    try:
        return (float(raw[0]), float(raw[1]), int(raw[2]), int(raw[3]))
    except (TypeError, ValueError):
        return None


async def _handle_fleet(c: _Dispatch) -> dict[str, Any]:
    return {"ok": True, "data": await build_fleet_report(c.db, c.config)}


async def _handle_fleet_node(c: _Dispatch) -> dict[str, Any]:
    query = c.req.get("query", "")
    try:
        node = await _resolve_node(query, c.db)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not node:
        return {"ok": False, "error": f"node {query!r} not found"}
    report = await build_node_report(c.db, c.config, node)
    report["management_path"] = c.wq.management_path_status(node)
    return {"ok": True, "data": report}


async def _handle_path(c: _Dispatch) -> dict[str, Any]:
    query = c.req.get("query", "")
    try:
        node = await _resolve_node(query, c.db)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not node:
        return {"ok": False, "error": f"node {query!r} not found"}
    return {"ok": True, "data": c.wq.management_path_status(node)}


async def _handle_path_discover(c: _Dispatch) -> dict[str, Any]:
    query = c.req.get("query", "")
    source = c.req.get("source", "discovery")
    try:
        node = await _resolve_node(query, c.db)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not node:
        return {"ok": False, "error": f"node {query!r} not found"}
    try:
        return {"ok": True, "data": await c.wq.discover_management_path(node, source=source)}
    except OperationBusyError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


async def _handle_path_apply(c: _Dispatch) -> dict[str, Any]:
    query = c.req.get("query", "")
    path = c.req.get("path", "")
    try:
        node = await _resolve_node(query, c.db)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not node:
        return {"ok": False, "error": f"node {query!r} not found"}
    try:
        return {"ok": True, "data": await c.wq.apply_management_path(node, path)}
    except OperationBusyError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


async def _handle_probe(c: _Dispatch) -> dict[str, Any]:
    query = c.req.get("query", "")
    advert = bool(c.req.get("advert", False))
    login = bool(c.req.get("login", False))
    radio = _parse_radio_arg(c.req.get("radio"))
    if radio is None:
        return {"ok": False, "error": "radio must be [freq,bw,sf,cr]"}
    try:
        node = await _resolve_node(query, c.db)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not node:
        return {"ok": False, "error": f"node {query!r} not found"}
    progress = _progress_writer(c.writer, "probe client disconnected")
    try:
        return {
            "ok": True,
            "data": await c.wq.probe_node(
                node, radio=radio, advert=advert, login=login, progress=progress
            ),
        }
    except OperationBusyError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


async def _handle_discover(c: _Dispatch) -> dict[str, Any]:
    radio = _parse_radio_arg(c.req.get("radio"))
    if radio is None:
        return {"ok": False, "error": "radio must be [freq,bw,sf,cr]"}
    try:
        dwell = float(c.req.get("dwell", 5.0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "radio and dwell must be numeric"}
    if dwell <= 0 or dwell > 30:
        return {"ok": False, "error": "dwell must be between 0 and 30 seconds"}
    progress = _progress_writer(c.writer, "discover client disconnected")
    try:
        return {
            "ok": True,
            "data": await c.wq.discover_nodes(radio, dwell=dwell, progress=progress),
        }
    except OperationBusyError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


async def _handle_adopt(c: _Dispatch) -> dict[str, Any]:
    pubkey = c.req.get("pubkey", "")
    label = c.req.get("label")
    radio = _parse_radio_arg(c.req.get("radio"))
    if radio is None:
        return {"ok": False, "error": "radio must be [freq,bw,sf,cr]"}
    progress = _progress_writer(c.writer, "adopt client disconnected")
    try:
        return {
            "ok": True,
            "data": await c.wq.adopt_node(
                pubkey,
                radio=radio,
                label=label if isinstance(label, str) and label else None,
                progress=progress,
            ),
        }
    except OperationBusyError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


async def _handle_sync(c: _Dispatch) -> dict[str, Any]:
    prefix = c.req.get("prefix")
    if prefix:
        # resolve label/name/pubkey-prefix to a full pubkey so run_node gets an
        # unambiguous key regardless of whether the user typed a label or hex prefix
        try:
            node = await _resolve_node(prefix, c.db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if node is None:
            return {"ok": False, "error": f"no node found matching {prefix!r}"}
        progress = _progress_writer(c.writer, "sync client disconnected")
        try:
            sync_awaitable = (
                c.wq.run_node(node.pubkey, progress=progress)
                if progress is not None
                else c.wq.run_node(node.pubkey)
            )
            if c.reader is None:
                result = await sync_awaitable
            else:
                sync_task = asyncio.create_task(sync_awaitable)
                disconnect_task = asyncio.create_task(c.reader.read())
                done, _ = await asyncio.wait(
                    {sync_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if sync_task in done:
                    disconnect_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await disconnect_task
                    result = await sync_task
                else:
                    with suppress(ConnectionResetError, BrokenPipeError):
                        await disconnect_task
                    sync_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await sync_task
                    raise ClientDisconnected("sync client disconnected")
            return _sync_result_response(result)
        except OperationBusyError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except ClientDisconnected as exc:
            log.info("%s", exc)
            return {"ok": False, "error": str(exc)}
    # no prefix — full cycle in background
    try:
        return {"ok": True, "data": await c.wq.start_cycle()}
    except OperationBusyError as exc:
        return {"ok": False, "error": str(exc)}


async def _handle_exec(c: _Dispatch) -> dict[str, Any]:
    pubkey = c.req.get("pubkey", "")
    command = (c.req.get("command") or "").strip()
    blocked = blocked_exec_command(command)
    if blocked:
        return {"ok": False, "error": f"{blocked!r} is blocked - use config operations"}
    try:
        node = await _resolve_node(pubkey, c.db)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not node:
        return {"ok": False, "error": f"node {pubkey!r} not found"}
    try:
        return {"ok": True, "data": await c.wq.exec_command(node.pubkey, command)}
    except OperationBusyError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


async def _handle_contacts(c: _Dispatch) -> dict[str, Any]:
    return {"ok": True, "data": await c.wq.unfiled_contacts()}


async def _handle_prune(c: _Dispatch) -> dict[str, Any]:
    # Sunset: the DB is the inventory, so "prune" no longer means "not in config.yaml".
    # It hard-deletes retired nodes (history and all) — the deliberate purge step after
    # `mch node retire`. Active nodes are never prune candidates.
    dry_run = bool(c.req.get("dry_run", True))
    nodes = await c.db.get_all_nodes()
    candidate_nodes = [n for n in nodes if n.state == NodeState.RETIRED]
    candidates = [
        {
            "pubkey": n.pubkey,
            "label": n.label,
            "name": n.name,
            "state": n.state.value,
            "last_seen": n.last_seen.isoformat() if n.last_seen else None,
        }
        for n in candidate_nodes
    ]
    if not dry_run:
        for node in candidate_nodes:
            await c.db.delete_node(node.pubkey)
    return {
        "ok": True,
        "data": {
            "candidates": candidates,
            "deleted": 0 if dry_run else len(candidates),
        },
    }


async def _handle_clean_facts(c: _Dispatch) -> dict[str, Any]:
    dry_run = bool(c.req.get("dry_run", True))
    query = c.req.get("query")
    group = c.req.get("group")
    fact_key = c.req.get("fact")
    try:
        candidates = await _stale_fact_candidates(
            c.db,
            c.config,
            query=str(query) if query else None,
            group=str(group) if group else None,
            fact_key=str(fact_key) if fact_key else None,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not dry_run:
        for candidate in candidates:
            # pubkey and key are always non-None strings here (node.pubkey / fact.key);
            # the candidate dict's mixed value types widen to str | None, so pin them.
            await c.db.delete_fact(str(candidate["pubkey"]), str(candidate["key"]))
    return {
        "ok": True,
        "data": {
            "candidates": candidates,
            "deleted": 0 if dry_run else len(candidates),
        },
    }


async def _handle_clean_settings(c: _Dispatch) -> dict[str, Any]:
    dry_run = bool(c.req.get("dry_run", True))
    query = c.req.get("query")
    group = c.req.get("group")
    setting_key = c.req.get("setting")
    try:
        # Fresh variable name (not the shared `candidates`) so mypy keeps the accurate
        # list[dict[str, Any]] element type for the delete_setting call below.
        stale_settings = await _stale_setting_candidates(
            c.db,
            c.config,
            query=str(query) if query else None,
            group=str(group) if group else None,
            setting_key=str(setting_key) if setting_key else None,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not dry_run:
        # Fresh loop variable (not the shared `candidate`) so mypy keeps the
        # dict[str, Any] element type for the delete_setting call.
        for row in stale_settings:
            await c.db.delete_setting(row["pubkey"], row["key"])
    return {
        "ok": True,
        "data": {
            "candidates": stale_settings,
            "deleted": 0 if dry_run else len(stale_settings),
        },
    }


async def _handle_remove(c: _Dispatch) -> dict[str, Any]:
    query = str(c.req.get("query") or "")
    if not query:
        return {"ok": False, "error": "missing node"}
    node = await _resolve_node(query, c.db)
    if not node:
        return {"ok": False, "error": f"no such node: {query}"}

    # Sunset: a still-managed (non-retired) node is protected — deleting it loses history
    # and it is just re-seeded from gateway favourites on the next restart. Retire it first
    # (keeps history, unfavourites it), or force through.
    active = node.state != NodeState.RETIRED
    if active and not bool(c.req.get("force", False)):
        return {
            "ok": False,
            "error": (
                f"{node.label or node.name or node.pubkey[:8]!r} is still managed"
                " - `mch node retire` it first (keeps history, unfavourites it),"
                " or use --force. An un-retired node is re-seeded from gateway favourites"
                " on restart."
            ),
        }

    data = {
        "pubkey": node.pubkey,
        "label": node.label,
        "name": node.name,
        "state": node.state.value,
        "last_seen": node.last_seen.isoformat() if node.last_seen else None,
        "retired": node.state == NodeState.RETIRED,
        "deleted": 0,
    }
    if not bool(c.req.get("dry_run", True)):
        await c.db.delete_node(node.pubkey)
        data["deleted"] = 1
    return {"ok": True, "data": data}


async def _handle_retire(c: _Dispatch) -> dict[str, Any]:
    cmd = c.req.get("cmd", "")
    query = str(c.req.get("query") or "")
    if not query:
        return {"ok": False, "error": "missing node"}
    try:
        node = await _resolve_node(query, c.db)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not node:
        return {"ok": False, "error": f"no such node: {query}"}
    ident = node.label or node.name or node.pubkey[:8]

    if cmd == "retire":
        if node.state is NodeState.RETIRED:
            return {"ok": False, "error": f"{ident!r} is already retired"}
        node = await c.wq.retire_node(node.pubkey)
    else:
        if node.state is not NodeState.RETIRED:
            return {"ok": False, "error": f"{ident!r} is not retired"}
        node = await c.wq.unretire_node(node.pubkey)

    return {"ok": True, "data": {
        "pubkey": node.pubkey,
        "label": node.label,
        "name": node.name,
        "state": node.state.value,
    }}


async def _handle_set_group(c: _Dispatch) -> dict[str, Any]:
    cmd = c.req.get("cmd", "")
    query = str(c.req.get("query") or "")
    if not query:
        return {"ok": False, "error": "missing node"}
    try:
        node = await _resolve_node(query, c.db)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not node:
        return {"ok": False, "error": f"no such node: {query}"}
    # Empty string clears the field (ungroup / drop label); a value sets it.
    raw = c.req.get("value")
    value = str(raw).strip() if raw is not None and str(raw).strip() else None
    if cmd == "set_group":
        # Assigning to a group requires the group to be registered first (R4 S1); clearing
        # (value None) is always allowed. Enforced here so `node move` can't put
        # a node into a typo'd group that no group-scoped desired state will ever match.
        if value is not None and not await c.db.group_exists(value):
            return {"ok": False, "error": _unknown_group_error(value)}
        await c.db.set_node_group(node.pubkey, value)
        await c.db.write_log(
            node.pubkey, "INFO",
            f"group set to {value!r}" if value else "group cleared",
        )
    else:
        if value is not None:
            try:
                validate_node_label(value)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        await c.db.set_node_label(node.pubkey, value)
        await c.db.write_log(
            node.pubkey, "INFO",
            f"label set to {value!r}" if value else "label cleared",
        )
    updated = await c.db.get_node(node.pubkey)
    assert updated is not None  # just written above; row must exist
    return {"ok": True, "data": {
        "pubkey": updated.pubkey,
        "label": updated.label,
        "name": updated.name,
        "group": updated.group,
    }}


async def _handle_logs(c: _Dispatch) -> dict[str, Any]:
    pubkey = c.req.get("pubkey")
    limit = int(c.req.get("limit", 50))
    if pubkey:
        try:
            node = await _resolve_node(pubkey, c.db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        pubkey = node.pubkey if node else pubkey
    logs = await c.db.get_logs(pubkey, limit=limit)
    labels = {node.pubkey: node.label for node in await c.db.get_all_nodes()}
    return {
        "ok": True,
        "data": [
            {
                **entry,
                "display_pubkey": entry.get("display_pubkey") or entry["node_pubkey"],
                "label": labels.get(entry["node_pubkey"], ""),
            }
            if entry.get("node_pubkey") else entry
            for entry in logs
        ],
    }


async def _handle_suggest(c: _Dispatch) -> dict[str, Any]:
    from pathlib import Path as _Path

    from mch.recommend import get_profile, load_profiles
    profiles_file = _Path(c.config.profiles_file) if c.config.profiles_file else None
    profiles, universal = load_profiles(
        profiles_url=c.config.profiles_url,
        profiles_file=profiles_file,
    )
    pubkey = c.req.get("pubkey")
    nodes = await c.db.get_all_nodes()
    if pubkey:
        try:
            node = await _resolve_node(pubkey, c.db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not node:
            return {"ok": False, "error": f"node {pubkey!r} not found"}
        nodes = [node]
    suggest_results: list[dict[str, Any]] = []
    for n in nodes:
        facts = await c.db.get_facts(n.pubkey)
        raw_count = facts.get("neighbors")
        count = int(raw_count) if raw_count is not None else None
        # treat unknown neighbour count as 0 for profile lookup (mobile = most conservative)
        # count stays None so NBRS displays as '-' in the table
        profile = get_profile(count if count is not None else 0, profiles)
        suggest_results.append({
            "pubkey": n.pubkey,
            "label": n.label,
            "name": n.name,
            "neighbour_count": count,
            "profile": {
                "name": profile.name,
                "txdelay": profile.txdelay,
                "direct_txdelay": profile.direct_txdelay,
                "description": profile.description,
            } if profile else None,
            "universal": [
                {"key": u.key, "value": u.value, "description": u.description}
                for u in universal
            ],
        })
    return {"ok": True, "data": suggest_results}


async def _handle_commission(c: _Dispatch) -> dict[str, Any]:
    from mch.commission import handle_commission
    progress = _progress_writer(
        c.writer if c.req.get("mode") in ("new", "replace") else None,
        "commission client disconnected",
    )
    try:
        async with c.wq.operation("commission", wait=False):
            return await handle_commission(c.req, c.wq, c.db, c.config, progress=progress)
    except OperationBusyError as exc:
        return {"ok": False, "error": str(exc)}
    except ClientDisconnected as exc:
        log.info("%s", exc)
        return {"ok": False, "error": str(exc)}


async def _handle_group_create(c: _Dispatch) -> dict[str, Any]:
    """Register a group so group-scope desired state and `node move` can target it (R4 S1)."""
    name = str(c.req.get("name") or "").strip()
    try:
        validate_group_name(name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    existed = await c.db.group_exists(name)
    await c.db.create_group(name)
    return {"ok": True, "data": {"name": name, "created": not existed}}


async def _handle_group_delete(c: _Dispatch) -> dict[str, Any]:
    """Hard cascade delete of a group (R4 decision 6): preview by default, act on ``yes``.

    Preview lists the member nodes and the group's desired rows so the operator sees exactly
    what a delete would ungroup and drop. With ``yes`` true it performs the one-transaction
    cascade (members ungrouped, desired rows deleted, registry row removed).
    """
    name = str(c.req.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing group name"}
    if not await c.db.group_exists(name):
        return {"ok": False, "error": f"no such group {name!r}"}
    members = [n for n in await c.db.get_all_nodes() if n.group == name]
    settings = await c.db.get_desired_settings(Scope.GROUP, name)
    operations = await c.db.get_desired_operations(Scope.GROUP, name)
    data: dict[str, Any] = {
        "name": name,
        "members": [{"pubkey": n.pubkey, "label": n.label, "name": n.name} for n in members],
        "settings": [
            {"key": s.key, "desired": s.desired, "policy": s.policy.value if s.policy else None}
            for s in settings
        ],
        "operations": [{"command": o.command} for o in operations],
    }
    if not c.req.get("yes"):
        data["deleted"] = False
        return {"ok": True, "data": data}
    await c.db.delete_group(name)
    data["deleted"] = True
    return {"ok": True, "data": data}


async def _handle_group_rename(c: _Dispatch) -> dict[str, Any]:
    """Rename a group and everything scoped to it in one transaction (R4 decision 6)."""
    old = str(c.req.get("old") or "").strip()
    new = str(c.req.get("new") or "").strip()
    if not old or not new:
        return {"ok": False, "error": "rename needs both OLD and NEW group names"}
    try:
        validate_group_name(new)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not await c.db.group_exists(old):
        return {"ok": False, "error": f"no such group {old!r}"}
    if old == new:
        return {"ok": False, "error": "OLD and NEW are the same group"}
    if await c.db.group_exists(new):
        return {"ok": False, "error": f"group {new!r} already exists"}
    await c.db.rename_group(old, new)
    return {"ok": True, "data": {"old": old, "new": new}}


# Command → handler table. The already-extracted DB-only handlers keep their (req, db, config)
# signature and are adapted with a thin lambda; the rest take the bundled _Dispatch context.
_HANDLERS: dict[str, Handler] = {
    "fleet": _handle_fleet,
    "fleet_node": _handle_fleet_node,
    "path": _handle_path,
    "path_discover": _handle_path_discover,
    "path_apply": _handle_path_apply,
    "probe": _handle_probe,
    "discover": _handle_discover,
    "adopt": _handle_adopt,
    "sync": _handle_sync,
    "exec": _handle_exec,
    "contacts": _handle_contacts,
    "prune": _handle_prune,
    "clean_facts": _handle_clean_facts,
    "clean_settings": _handle_clean_settings,
    "set_setting": lambda c: _handle_set_setting(c.req, c.db, c.config),
    "unset_setting": lambda c: _handle_unset_setting(c.req, c.db, c.config),
    "show_setting": lambda c: _handle_show_setting(c.req, c.db, c.config),
    "import": lambda c: _handle_import(c.req, c.db, c.config),
    "export": lambda c: _handle_export(c.db),
    "remove": _handle_remove,
    "retire": _handle_retire,
    "unretire": _handle_retire,
    "set_group": _handle_set_group,
    "set_label": _handle_set_group,
    "group_create": _handle_group_create,
    "group_delete": _handle_group_delete,
    "group_rename": _handle_group_rename,
    "logs": _handle_logs,
    "suggest": _handle_suggest,
    "commission": _handle_commission,
}


async def _dispatch(
    req: dict[str, Any],
    wq: WorkQueue,
    db: Database,
    config: Config,
    writer: asyncio.StreamWriter | None = None,
    reader: asyncio.StreamReader | None = None,
) -> dict[str, Any]:
    cmd = req.get("cmd", "")
    handler = _HANDLERS.get(cmd)
    if handler is None:
        return {"ok": False, "error": f"unknown command: {cmd!r}"}
    return await handler(
        _Dispatch(req=req, wq=wq, db=db, config=config, writer=writer, reader=reader)
    )


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------

class Daemon:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._db: Database | None = None
        self._gateway: Gateway | None = None
        self._wq: WorkQueue | None = None
        self._messenger: Messenger | None = None
        self._server: asyncio.Server | None = None
        self._config_snapshot: dict[Path, FileSignature] = {}
        self._config_watch_task: asyncio.Task[None] | None = None
        self._work_queue_task: asyncio.Task[None] | None = None
        self._shutdown_complete = asyncio.Event()
        self._shutting_down = False
        self._reload_lock = asyncio.Lock()

    async def run(self) -> None:
        config = self._config

        self._db = Database(Path(config.db_path))
        await self._db.open()
        self._gateway = Gateway(config.gateway)
        try:
            await self._gateway.connect()
        except GatewayError as exc:
            log.error("gateway connection failed: %s", exc)
            await self._db.close()
            raise SystemExit(1) from None

        if config.gateway.advert_on_start == "zerohop":
            log.info("sending startup advert.zerohop")
            await self._gateway.advert(zerohop=True)

        # passive last_seen: update DB when a managed node advertises
        db = self._db
        def _advert_event_cb(event: Any) -> None:
            pubkey = (event.payload or {}).get("public_key", "")
            if pubkey:
                asyncio.create_task(_handle_advert(db, pubkey))
        self._gateway.subscribe(EventType.ADVERTISEMENT, _advert_event_cb)

        # messenger: DM control + channel announcements
        msg_cfg = config.messenger
        self._messenger = Messenger(
            gateway=self._gateway,
            db=self._db,
            allowed_prefixes=msg_cfg.allowed_prefixes,
            announce_channel=msg_cfg.announce_channel,
        )
        self._messenger.start()

        self._wq = WorkQueue(config, self._gateway, self._db, messenger=self._messenger)
        self._config_snapshot = self._snapshot_config_files()
        self._config_watch_task = asyncio.create_task(self._watch_config_files())

        # Unix socket server for mch CLI
        sock_path = config.socket_path
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass

        db = self._db
        wq = self._wq

        async def client_connected(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await _handle_client(r, w, wq, db, config)

        self._server = await asyncio.start_unix_server(
            client_connected, path=sock_path
        )
        os.chmod(sock_path, 0o660)  # group-writable so mch group members can connect
        log.info("control socket listening at %s", sock_path)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self._shutdown()))
        sighup = getattr(signal, "SIGHUP", None)
        if sighup is not None:
            loop.add_signal_handler(
                sighup,
                lambda: asyncio.create_task(self._reload_config("SIGHUP")),
            )

        log.info("mchd started")
        self._work_queue_task = asyncio.create_task(self._wq.run(), name="work-queue")
        try:
            await self._work_queue_task
        except asyncio.CancelledError:
            if not self._shutting_down:
                raise
        if self._shutting_down:
            await self._shutdown_complete.wait()

    def _watched_config_paths(self) -> list[Path]:
        paths: list[Path] = []
        for raw_path in (self._config.config_path, self._config.secrets_path):
            if raw_path is None:
                continue
            path = Path(raw_path)
            if path not in paths:
                paths.append(path)
        return paths

    def _snapshot_config_files(self) -> dict[Path, FileSignature]:
        snapshot: dict[Path, FileSignature] = {}
        for path in self._watched_config_paths():
            try:
                stat = path.stat()
            except FileNotFoundError:
                snapshot[path] = None
            else:
                snapshot[path] = (
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                    stat.st_size,
                    stat.st_dev,
                    stat.st_ino,
                )
        return snapshot

    async def _watch_config_files(self) -> None:
        while True:
            await asyncio.sleep(CONFIG_WATCH_INTERVAL_SECONDS)
            snapshot = self._snapshot_config_files()
            if snapshot != self._config_snapshot:
                log.info("config/secrets file change detected")
                await self._reload_config("file change")

    async def _reload_config(self, reason: str) -> None:
        async with self._reload_lock:
            if self._config.config_path is None:
                log.warning("cannot reload config: original config path unknown")
                return

            try:
                new_config = load_config(Path(self._config.config_path), strict=True)
            except Exception as exc:
                log.exception(
                    "config reload from %s failed; keeping previous config: %s",
                    reason,
                    exc,
                )
                self._config_snapshot = self._snapshot_config_files()
                return

            for field in fields(Config):
                setattr(self._config, field.name, getattr(new_config, field.name))

            if self._messenger:
                msg_cfg = self._config.messenger
                self._messenger.update_config(
                    allowed_prefixes=msg_cfg.allowed_prefixes,
                    announce_channel=msg_cfg.announce_channel,
                )
            if self._wq:
                await self._wq.refresh_config_state()

            self._config_snapshot = self._snapshot_config_files()
            log.info(
                "config reloaded from %s (%d node(s))",
                self._config.config_path,
                len(self._config.nodes),
            )

    async def _shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("shutting down")
        try:
            if self._config_watch_task:
                self._config_watch_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._config_watch_task
            if self._wq:
                await self._wq.stop()
            if self._work_queue_task:
                with suppress(asyncio.CancelledError):
                    await self._work_queue_task
            if self._messenger:
                self._messenger.stop()
            if self._server:
                self._server.close()
                await self._server.wait_closed()
            if self._gateway:
                await self._gateway.disconnect()
            if self._db:
                await self._db.close()
        finally:
            self._shutdown_complete.set()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="mchd — Mesh Herder node management daemon")
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        raise SystemExit(CONFIG_ERROR_EXIT)

    try:
        config = load_config(config_path, strict=True)
    except Exception as exc:
        print(f"error loading config: {exc}", file=sys.stderr)
        raise SystemExit(CONFIG_ERROR_EXIT) from None

    daemon = Daemon(config)
    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
