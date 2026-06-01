"""Normalized read-only fleet reporting for CLI, HTTP API, and future UI clients."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mch.command_registry import write_command_for_key
from mch.config import Config, GroupConfig, NodeConfig
from mch.db import Database
from mch.models import Node, NodeState, SettingPolicy, SettingRecord
from mch.queue import all_setting_keys, resolve_desired, resolve_policy
from mch.settings_compare import compare_setting

SCHEMA_VERSION = 1
_BAD_STATES = {
    NodeState.UNKNOWN,
    NodeState.OFFLINE,
    NodeState.UNREACHABLE,
    NodeState.CONFIGURING,
    NodeState.PARTIAL,
}


async def build_fleet_report(db: Database, config: Config) -> dict[str, Any]:
    """Build the canonical read-only representation of desired and observed fleet state."""
    nodes = await db.get_all_nodes()
    node_reports: list[dict[str, Any]] = []
    groups = {group.name: group for group in config.groups}

    for node in nodes:
        node_cfg = _node_config(node, config)
        group_cfg = groups.get(node_cfg.group) if node_cfg and node_cfg.group else None
        facts = await db.get_facts(node.pubkey)
        known_settings = {setting.key: setting for setting in await db.get_settings(node.pubkey)}
        settings = _settings_report(
            node_cfg,
            group_cfg,
            config,
            known_settings,
        )
        open_node = _is_open_node(node, config)
        node_issues = _node_issues(node, settings, open_node)
        health = _node_health(node, node_issues)
        node_reports.append({
            "pubkey": node.pubkey,
            "label": node.label,
            "name": node.name,
            "group": node.group,
            "configured": node_cfg is not None,
            "state": node.state.value,
            "health": health,
            "issues": node_issues,
            "last_seen": _iso(node.last_seen),
            "last_connected": _iso(node.last_connected),
            "next_sync_at": _iso(node.next_sync_at),
            "auth": {
                "required": node.auth_required,
                "cached": node.auth_cached,
                "password_known": _password_known(node, config),
                "open": open_node,
            },
            "fact_keys": list(config.facts),
            "facts": facts,
            "settings": settings,
        })

    missing = _missing_config_nodes(nodes, config)
    plan = _plan_report(node_reports, missing)
    fleet_issues = _issues_report(node_reports, missing)
    summary = _summary(node_reports, missing, fleet_issues)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": summary,
        "nodes": node_reports,
        "missing_nodes": missing,
        "issues": fleet_issues,
        "plan": plan,
    }


async def build_node_report(
    db: Database,
    config: Config,
    node: Node,
    *,
    log_limit: int = 20,
) -> dict[str, Any]:
    """Return one canonical node report with recent activity attached."""
    fleet = await build_fleet_report(db, config)
    result = next(entry for entry in fleet["nodes"] if entry["pubkey"] == node.pubkey)
    result = dict(result)
    result["logs"] = await db.get_logs(node.pubkey, limit=log_limit)
    return result


def legacy_status(report: dict[str, Any]) -> dict[str, Any]:
    """Adapt the canonical report to the established CLI status RPC payload."""
    return {
        "nodes": [
            {
                "pubkey": node["pubkey"],
                "name": node["name"],
                "label": node["label"],
                "group": node["group"],
                "state": node["state"],
                "last_seen": node["last_seen"],
                "last_connected": node["last_connected"],
                "neighbors": node["facts"].get("neighbors"),
                "ver": node["facts"].get("ver"),
            }
            for node in report["nodes"]
        ],
        **report["issues"],
    }


def legacy_plan(report: dict[str, Any]) -> dict[str, Any]:
    """Adapt the canonical report to the established CLI plan RPC payload."""
    return {
        "writes": report["plan"]["writes"],
        "audits": report["plan"]["audits"],
        "unknown": report["plan"]["unknown"],
    }


def legacy_issues(report: dict[str, Any]) -> dict[str, Any]:
    """Adapt the canonical report to the established issues RPC payload."""
    return dict(report["issues"])


def legacy_node(node: dict[str, Any], config: Config) -> dict[str, Any]:
    """Adapt one canonical node report to the established detail RPC payload."""
    configured_facts: dict[str, str | None] = {key: "???" for key in config.facts}
    configured_facts.update(node["facts"])
    return {
        "pubkey": node["pubkey"],
        "name": node["name"],
        "label": node["label"],
        "group": node["group"],
        "state": node["state"],
        "last_seen": node["last_seen"],
        "last_connected": node["last_connected"],
        "auth_required": node["auth"]["required"],
        "auth_cached": node["auth"]["cached"],
        "passwd_known": node["auth"]["password_known"],
        "facts": configured_facts,
        "settings": [
            {
                "key": setting["key"],
                "desired": setting["desired"],
                "observed": setting["observed"],
                "policy": setting["policy"],
                "update_attempts": setting["update_attempts"],
                "drift": setting["status"] == "drift",
                "approximate": setting["status"] == "nominal",
            }
            for setting in node["settings"]
            if setting["status"] != "ignored"
            and (setting["desired"] is not None or setting["observed"] is not None)
        ],
        "logs": node["logs"],
    }


def _settings_report(
    node_cfg: NodeConfig | None,
    group_cfg: GroupConfig | None,
    config: Config,
    known_settings: dict[str, SettingRecord],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    configured_keys: set[str] = set()
    if node_cfg:
        for key in sorted(all_setting_keys(node_cfg, group_cfg, config.global_settings)):
            configured_keys.add(key)
            policy = resolve_policy(key, node_cfg, group_cfg, config.global_settings)
            desired = resolve_desired(key, node_cfg, group_cfg, config.global_settings)
            known = known_settings.get(key)
            observed = known.observed if known else None
            items.append(_setting_item(key, desired, observed, policy, known))

    # Keep observations for values removed from config visible as history/evidence.
    for key, known in sorted(known_settings.items()):
        if key not in configured_keys:
            items.append(_setting_item(key, known.desired, known.observed, known.policy, known))
    return items


def _setting_item(
    key: str,
    desired: str | None,
    observed: str | None,
    policy: SettingPolicy,
    known: SettingRecord | None,
) -> dict[str, Any]:
    comparison = compare_setting(key, desired, observed) if desired is not None else None
    if policy == SettingPolicy.SKIP:
        status = "ignored"
        action = "ignore"
    elif desired is None:
        status = "unknown" if observed is None else "ok"
        action = "read" if observed is None else "none"
    elif observed is None:
        status = "unknown"
        action = "read"
    elif comparison and comparison.equal and comparison.approximate:
        status = "nominal"
        action = "none"
    elif comparison and comparison.equal:
        status = "ok"
        action = "none"
    elif policy == SettingPolicy.ENFORCE:
        status = "drift"
        action = "write"
    else:
        status = "drift"
        action = "report"
    return {
        "key": key,
        "desired": desired,
        "observed": observed,
        "policy": policy.value,
        "status": status,
        "action": action,
        "command": (
            write_command_for_key(key, desired)
            if action == "write" and desired is not None
            else None
        ),
        "update_attempts": (
            known.update_attempts
            if known
            and policy == SettingPolicy.ENFORCE
            and known.desired == desired
            else 0
        ),
    }


def _node_issues(
    node: Node,
    settings: list[dict[str, Any]],
    open_node: bool,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if node.state in _BAD_STATES:
        issues.append({"code": "state", "severity": "critical", "detail": node.state.value})
    if open_node:
        issues.append({"code": "open_node", "severity": "critical", "detail": "no ACL protection"})
    for setting in settings:
        if setting["status"] != "drift":
            continue
        severity = "warning" if setting["action"] == "report" else "critical"
        issues.append({"code": "drift", "severity": severity, "detail": setting["key"]})
    return issues


def _node_health(node: Node, issues: list[dict[str, str]]) -> str:
    if any(issue["severity"] == "critical" for issue in issues):
        return "critical"
    if issues:
        return "warning"
    if node.state in {NodeState.CONFIGURED, NodeState.ONLINE}:
        return "healthy"
    return "unknown"


def _plan_report(
    nodes: list[dict[str, Any]],
    missing: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    writes: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = [
        {
            "pubkey": item["pubkey_prefix"],
            "label": item["label"],
            "mesh_name": "",
            "key": "",
            "desired": None,
            "known": None,
            "policy": "",
            "reason": "node not known to gateway/DB",
        }
        for item in missing
    ]
    for node in nodes:
        for setting in node["settings"]:
            base = {
                "pubkey": node["pubkey"],
                "label": node["label"],
                "mesh_name": node["name"],
                "key": setting["key"],
                "desired": setting["desired"],
                "known": setting["observed"],
                "policy": setting["policy"],
            }
            if setting["action"] == "write":
                writes.append({**base, "command": setting["command"]})
            elif setting["action"] == "report" and setting["desired"] is not None:
                audits.append(base)
            elif (
                setting["action"] == "read"
                and setting["desired"] is not None
                and node["configured"]
            ):
                unknown.append({**base, "reason": "not observed yet"})
    return {"writes": writes, "audits": audits, "unknown": unknown, "blocked": []}


def _issues_report(
    nodes: list[dict[str, Any]],
    missing: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    drifted: list[dict[str, Any]] = []
    bad_state: list[dict[str, Any]] = []
    open_nodes: list[dict[str, Any]] = []
    for node in nodes:
        if node["state"] in {state.value for state in _BAD_STATES}:
            bad_state.append({
                "pubkey": node["pubkey"],
                "label": node["label"],
                "name": node["name"],
                "state": node["state"],
                "last_seen": node["last_seen"],
            })
        if node["auth"]["open"]:
            open_nodes.append({
                "pubkey": node["pubkey"],
                "label": node["label"],
                "name": node["name"],
            })
        for setting in node["settings"]:
            if setting["status"] == "drift":
                drifted.append({
                    "node_pubkey": node["pubkey"],
                    "label": node["label"],
                    "key": setting["key"],
                    "desired": setting["desired"],
                    "observed": setting["observed"],
                    "policy": setting["policy"],
                    "update_attempts": setting["update_attempts"],
                })
    return {
        "drifted": drifted,
        "bad_state": bad_state,
        "ghosts": [
            {"pubkey_prefix": node["pubkey_prefix"], "label": node["label"]}
            for node in missing
        ],
        "open_nodes": open_nodes,
    }


def _summary(
    nodes: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    issues: dict[str, list[dict[str, Any]]],
) -> dict[str, int]:
    return {
        "known_nodes": len(nodes),
        "configured_missing_nodes": len(missing),
        "healthy_nodes": sum(node["health"] == "healthy" for node in nodes),
        "warning_nodes": sum(node["health"] == "warning" for node in nodes),
        "critical_nodes": sum(node["health"] == "critical" for node in nodes),
        "drifted_settings": len(issues["drifted"]),
        "open_nodes": len(issues["open_nodes"]),
    }


def _missing_config_nodes(nodes: list[Node], config: Config) -> list[dict[str, Any]]:
    return [
        {
            "pubkey_prefix": node_cfg.pubkey_prefix,
            "label": node_cfg.label,
            "health": "unknown",
            "issues": [{
                "code": "not_known",
                "severity": "critical",
                "detail": "node not known to gateway/DB",
            }],
        }
        for node_cfg in config.nodes
        if not any(node.pubkey.startswith(node_cfg.pubkey_prefix) for node in nodes)
    ]


def _node_config(node: Node, config: Config) -> NodeConfig | None:
    return next(
        (entry for entry in config.nodes if node.pubkey.startswith(entry.pubkey_prefix)),
        None,
    )


def _password_known(node: Node, config: Config) -> bool:
    return "default" in config.secrets or any(
        node.pubkey.startswith(prefix) for prefix in config.secrets
    )


def _is_open_node(node: Node, config: Config) -> bool:
    return (
        node.state == NodeState.CONFIGURED
        and not node.auth_required
        and not node.auth_cached
        and not _password_known(node, config)
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
