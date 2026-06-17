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
from contextlib import suppress
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from meshcore.events import EventType

from mch.api import ReadOnlyApi
from mch.command_registry import validate_value
from mch.command_safety import blocked_exec_command
from mch.config import (
    DIRECTIVE_KEYS,
    Config,
    config_dict_to_yaml,
    config_to_desired_operations,
    config_to_desired_rows,
    desired_rows_to_config_dict,
    normalize_directive_value,
    resolve_desired_configs,
    settable_keys,
    validate_directive_value,
)
from mch.config import load as load_config
from mch.db import Database
from mch.gateway import Gateway, GatewayError
from mch.messenger import Messenger
from mch.models import DesiredSetting, Scope, SettingPolicy
from mch.queue import (
    NodeResult,
    OperationBusyError,
    WorkQueue,
    all_setting_keys,
)
from mch.report import (
    build_fleet_report,
    build_node_report,
    legacy_issues,
    legacy_node,
    legacy_plan,
    legacy_status,
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


def _is_configured_node(pubkey: str, config: Config) -> bool:
    return any(pubkey.startswith(nc.pubkey_prefix) for nc in config.nodes)


async def _stale_fact_candidates(
    db: Database,
    config: Config,
    *,
    query: str | None = None,
    group: str | None = None,
    fact_key: str | None = None,
) -> list[dict[str, Any]]:
    configured_facts = set(config.facts)
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
    sunset), so a key set only via `mch set` - or one on a node that was never in
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
    Convergence is left to the next sync cycle or an explicit ``mch sync`` - recording
    intent and reaching the node are decoupled (DESIGN.md "Desired state in the DB").
    """
    key = str(req.get("key") or "")
    value = req.get("value")
    policy_raw = req.get("policy")

    if not key:
        return {"ok": False, "error": "missing setting key"}
    if key not in settable_keys():
        return {"ok": False, "error": f"{key!r} is not a writable setting"}
    if value is None and policy_raw is None:
        return {"ok": False, "error": "provide a value, a --policy, or both"}

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

    policy: SettingPolicy | None = None
    if policy_raw is not None:
        try:
            policy = SettingPolicy(str(policy_raw))
        except ValueError:
            valid = ", ".join(p.value for p in SettingPolicy)
            return {"ok": False, "error": f"invalid policy {policy_raw!r} (choose from: {valid})"}

    scope, scope_id, data, error = await _resolve_setting_scope(req, db)
    if error or scope is None:
        return {"ok": False, "error": error or "could not resolve scope"}
    # Catch a typo'd group name at write time: a desired row for a group that no node
    # is in would silently never apply. Group membership lives in the DB now (nodes.group),
    # so a group is "real" once at least one node belongs to it. unset does not check this
    # (see resolver docstring).
    if scope is Scope.GROUP:
        group_names = {n.group for n in await db.get_all_nodes() if n.group}
        if scope_id not in group_names:
            return {
                "ok": False,
                "error": (
                    f"group {scope_id!r} has no member nodes - assign a node to it first "
                    "(via `mch import` or commissioning), or check the spelling"
                ),
            }

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

    def resolve_pubkey(prefix: str) -> str | None:
        return next((n.pubkey for n in nodes if n.pubkey.startswith(prefix)), None)

    rows, warnings = config_to_desired_rows(config, resolve_pubkey)
    operations, op_warnings = config_to_desired_operations(config, resolve_pubkey)
    warnings = warnings + op_warnings
    if not dry_run:
        for row in rows:
            await db.set_desired_setting(row)
        for op in operations:
            await db.set_desired_operation(op)

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
            "warnings": warnings,
            "written": 0 if dry_run else total,
            "dry_run": dry_run,
        },
    }


async def _handle_export(db: Database) -> dict[str, Any]:
    """Dump all DB desired state (settings + operations) as config.yaml-shaped YAML."""
    rows = await db.get_all_desired_settings()
    operations = await db.get_all_desired_operations()
    data = desired_rows_to_config_dict(rows, operations)
    yaml_text = config_dict_to_yaml(data) if data else ""
    return {
        "ok": True,
        "data": {"yaml": yaml_text, "total": len(rows) + len(operations)},
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

    if cmd == "fleet":
        return {"ok": True, "data": await build_fleet_report(db, config)}

    if cmd == "fleet_node":
        query = req.get("query", "")
        try:
            node = await _resolve_node(query, db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not node:
            return {"ok": False, "error": f"node {query!r} not found"}
        report = await build_node_report(db, config, node)
        report["management_path"] = wq.management_path_status(node)
        return {"ok": True, "data": report}

    if cmd == "status":
        return {"ok": True, "data": legacy_status(await build_fleet_report(db, config))}

    if cmd == "node":
        pubkey = req.get("pubkey", "")
        try:
            node = await _resolve_node(pubkey, db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not node:
            return {"ok": False, "error": f"node {pubkey!r} not found"}
        report = await build_node_report(db, config, node)
        return {"ok": True, "data": legacy_node(report, config)}

    if cmd == "path":
        query = req.get("query", "")
        try:
            node = await _resolve_node(query, db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not node:
            return {"ok": False, "error": f"node {query!r} not found"}
        return {"ok": True, "data": wq.management_path_status(node)}

    if cmd == "path_discover":
        query = req.get("query", "")
        source = req.get("source", "discovery")
        try:
            node = await _resolve_node(query, db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not node:
            return {"ok": False, "error": f"node {query!r} not found"}
        try:
            return {"ok": True, "data": await wq.discover_management_path(node, source=source)}
        except OperationBusyError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    if cmd == "path_apply":
        query = req.get("query", "")
        path = req.get("path", "")
        try:
            node = await _resolve_node(query, db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not node:
            return {"ok": False, "error": f"node {query!r} not found"}
        try:
            return {"ok": True, "data": await wq.apply_management_path(node, path)}
        except OperationBusyError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    if cmd == "probe":
        query = req.get("query", "")
        raw_radio = req.get("radio")
        advert = bool(req.get("advert", False))
        login = bool(req.get("login", False))
        if not isinstance(raw_radio, list | tuple) or len(raw_radio) != 4:
            return {"ok": False, "error": "radio must be [freq,bw,sf,cr]"}
        try:
            radio = (
                float(raw_radio[0]),
                float(raw_radio[1]),
                int(raw_radio[2]),
                int(raw_radio[3]),
            )
        except (TypeError, ValueError):
            return {"ok": False, "error": "radio must be numeric"}
        try:
            node = await _resolve_node(query, db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not node:
            return {"ok": False, "error": f"node {query!r} not found"}
        probe_progress_cb = None
        if writer is not None:
            async def probe_progress_cb(msg: str) -> None:
                try:
                    writer.write((json.dumps({"type": "progress", "msg": msg}) + "\n").encode())
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    raise ClientDisconnected("probe client disconnected") from None
        try:
            return {
                "ok": True,
                "data": await wq.probe_node(
                    node, radio=radio, advert=advert, login=login, progress=probe_progress_cb
                ),
            }
        except OperationBusyError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    if cmd == "discover":
        raw_radio = req.get("radio")
        if not isinstance(raw_radio, list | tuple) or len(raw_radio) != 4:
            return {"ok": False, "error": "radio must be [freq,bw,sf,cr]"}
        try:
            radio = (
                float(raw_radio[0]),
                float(raw_radio[1]),
                int(raw_radio[2]),
                int(raw_radio[3]),
            )
            dwell = float(req.get("dwell", 5.0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "radio and dwell must be numeric"}
        if dwell <= 0 or dwell > 30:
            return {"ok": False, "error": "dwell must be between 0 and 30 seconds"}
        discover_progress_cb = None
        if writer is not None:
            async def discover_progress_cb(msg: str) -> None:
                try:
                    writer.write((json.dumps({"type": "progress", "msg": msg}) + "\n").encode())
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    raise ClientDisconnected("discover client disconnected") from None
        try:
            return {
                "ok": True,
                "data": await wq.discover_nodes(
                    radio,
                    dwell=dwell,
                    progress=discover_progress_cb,
                ),
            }
        except OperationBusyError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    if cmd == "adopt":
        pubkey = req.get("pubkey", "")
        raw_radio = req.get("radio")
        label = req.get("label")
        if not isinstance(raw_radio, list | tuple) or len(raw_radio) != 4:
            return {"ok": False, "error": "radio must be [freq,bw,sf,cr]"}
        try:
            radio = (
                float(raw_radio[0]),
                float(raw_radio[1]),
                int(raw_radio[2]),
                int(raw_radio[3]),
            )
        except (TypeError, ValueError):
            return {"ok": False, "error": "radio must be numeric"}
        adopt_progress_cb = None
        if writer is not None:
            async def adopt_progress_cb(msg: str) -> None:
                try:
                    writer.write((json.dumps({"type": "progress", "msg": msg}) + "\n").encode())
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    raise ClientDisconnected("adopt client disconnected") from None
        try:
            return {
                "ok": True,
                "data": await wq.adopt_node(
                    pubkey,
                    radio=radio,
                    label=label if isinstance(label, str) and label else None,
                    progress=adopt_progress_cb,
                ),
            }
        except OperationBusyError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    if cmd == "sync":
        prefix = req.get("prefix")
        if prefix:
            # resolve label/name/pubkey-prefix to a full pubkey so run_node gets an
            # unambiguous key regardless of whether the user typed a label or hex prefix
            try:
                node = await _resolve_node(prefix, db)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if node is None:
                return {"ok": False, "error": f"no node found matching {prefix!r}"}
            sync_progress_cb = None
            if writer is not None:
                async def sync_progress_cb(msg: str) -> None:
                    try:
                        writer.write(
                            (json.dumps({"type": "progress", "msg": msg}) + "\n").encode()
                        )
                        await writer.drain()
                    except (ConnectionResetError, BrokenPipeError):
                        raise ClientDisconnected("sync client disconnected") from None
            try:
                sync_awaitable = (
                    wq.run_node(node.pubkey, progress=sync_progress_cb)
                    if sync_progress_cb is not None
                    else wq.run_node(node.pubkey)
                )
                if reader is None:
                    result = await sync_awaitable
                else:
                    sync_task = asyncio.create_task(sync_awaitable)
                    disconnect_task = asyncio.create_task(reader.read())
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
            return {"ok": True, "data": await wq.start_cycle()}
        except OperationBusyError as exc:
            return {"ok": False, "error": str(exc)}

    if cmd == "exec":
        pubkey = req.get("pubkey", "")
        command = (req.get("command") or "").strip()
        blocked = blocked_exec_command(command)
        if blocked:
            return {"ok": False, "error": f"{blocked!r} is blocked - use config operations"}
        try:
            node = await _resolve_node(pubkey, db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not node:
            return {"ok": False, "error": f"node {pubkey!r} not found"}
        try:
            return {"ok": True, "data": await wq.exec_command(node.pubkey, command)}
        except OperationBusyError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    if cmd == "plan":
        return {"ok": True, "data": legacy_plan(await build_fleet_report(db, config))}

    if cmd == "contacts":
        return {"ok": True, "data": await wq.unfiled_contacts()}

    if cmd == "prune":
        dry_run = bool(req.get("dry_run", True))
        nodes = await db.get_all_nodes()
        candidate_nodes = [n for n in nodes if not _is_configured_node(n.pubkey, config)]
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
                await db.delete_node(node.pubkey)
        return {
            "ok": True,
            "data": {
                "candidates": candidates,
                "deleted": 0 if dry_run else len(candidates),
            },
        }

    if cmd == "clean_facts":
        dry_run = bool(req.get("dry_run", True))
        query = req.get("query")
        group = req.get("group")
        fact_key = req.get("fact")
        try:
            candidates = await _stale_fact_candidates(
                db,
                config,
                query=str(query) if query else None,
                group=str(group) if group else None,
                fact_key=str(fact_key) if fact_key else None,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not dry_run:
            for candidate in candidates:
                await db.delete_fact(candidate["pubkey"], candidate["key"])
        return {
            "ok": True,
            "data": {
                "candidates": candidates,
                "deleted": 0 if dry_run else len(candidates),
            },
        }

    if cmd == "clean_settings":
        dry_run = bool(req.get("dry_run", True))
        query = req.get("query")
        group = req.get("group")
        setting_key = req.get("setting")
        try:
            # Fresh variable name (not the shared `candidates`) so mypy keeps the accurate
            # list[dict[str, Any]] element type for the delete_setting call below.
            stale_settings = await _stale_setting_candidates(
                db,
                config,
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
                await db.delete_setting(row["pubkey"], row["key"])
        return {
            "ok": True,
            "data": {
                "candidates": stale_settings,
                "deleted": 0 if dry_run else len(stale_settings),
            },
        }

    if cmd == "set_setting":
        return await _handle_set_setting(req, db, config)

    if cmd == "unset_setting":
        return await _handle_unset_setting(req, db, config)

    if cmd == "import":
        return await _handle_import(req, db, config)

    if cmd == "export":
        return await _handle_export(db)

    if cmd == "remove":
        query = str(req.get("query") or "")
        if not query:
            return {"ok": False, "error": "missing node"}
        node = await _resolve_node(query, db)
        if not node:
            return {"ok": False, "error": f"no such node: {query}"}

        configured = _is_configured_node(node.pubkey, config)
        if configured and not bool(req.get("force", False)):
            return {
                "ok": False,
                "error": (
                    f"{node.label or node.name or node.pubkey[:8]!r} is still in config.yaml"
                    " - remove it from config first, or use --force"
                ),
            }

        data = {
            "pubkey": node.pubkey,
            "label": node.label,
            "name": node.name,
            "state": node.state.value,
            "last_seen": node.last_seen.isoformat() if node.last_seen else None,
            "configured": configured,
            "deleted": 0,
        }
        if not bool(req.get("dry_run", True)):
            await db.delete_node(node.pubkey)
            data["deleted"] = 1
        return {"ok": True, "data": data}

    if cmd == "logs":
        pubkey = req.get("pubkey")
        limit = int(req.get("limit", 50))
        if pubkey:
            try:
                node = await _resolve_node(pubkey, db)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            pubkey = node.pubkey if node else pubkey
        logs = await db.get_logs(pubkey, limit=limit)
        labels = {node.pubkey: node.label for node in await db.get_all_nodes()}
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

    if cmd == "issues":
        return {"ok": True, "data": legacy_issues(await build_fleet_report(db, config))}

    if cmd == "suggest":
        from pathlib import Path as _Path

        from mch.recommend import get_profile, load_profiles
        profiles_file = _Path(config.profiles_file) if config.profiles_file else None
        profiles, universal = load_profiles(
            profiles_url=config.profiles_url,
            profiles_file=profiles_file,
        )
        pubkey = req.get("pubkey")
        nodes = await db.get_all_nodes()
        if pubkey:
            try:
                node = await _resolve_node(pubkey, db)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if not node:
                return {"ok": False, "error": f"node {pubkey!r} not found"}
            nodes = [node]
        suggest_results: list[dict[str, Any]] = []
        for n in nodes:
            facts = await db.get_facts(n.pubkey)
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

    if cmd == "commission":
        from mch.commission import handle_commission
        progress_cb = None
        if writer is not None and req.get("mode") in ("new", "replace"):
            async def progress_cb(msg: str) -> None:
                try:
                    writer.write((json.dumps({"type": "progress", "msg": msg}) + "\n").encode())
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    raise ClientDisconnected("commission client disconnected") from None
        try:
            async with wq.operation("commission", wait=False):
                return await handle_commission(req, wq, db, config, progress=progress_cb)
        except OperationBusyError as exc:
            return {"ok": False, "error": str(exc)}
        except ClientDisconnected as exc:
            log.info("%s", exc)
            return {"ok": False, "error": str(exc)}

    return {"ok": False, "error": f"unknown command: {cmd!r}"}


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
        self._api: ReadOnlyApi | None = None
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

        if config.api.enabled:
            self._api = ReadOnlyApi(config, self._db)
            await self._api.start()

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
            if self._api:
                await self._api.close()
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
