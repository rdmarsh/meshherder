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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mch.command_safety import blocked_exec_command
from mch.config import Config
from mch.config import load as load_config
from mch.db import Database
from meshcore.events import EventType
from mch.gateway import Gateway, GatewayError
from mch.messenger import Messenger
from mch.models import NodeState, SettingPolicy
from mch.queue import (
    OperationBusyError,
    WorkQueue,
    all_setting_keys,
    resolve_desired,
    resolve_policy,
)

log = logging.getLogger(__name__)

FileSignature = tuple[int, int] | None
CONFIG_WATCH_INTERVAL_SECONDS = 10.0

UTC = timezone.utc


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
        resp = await _dispatch(req, wq, db, config, writer)
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


async def _dispatch(
    req: dict[str, Any],
    wq: WorkQueue,
    db: Database,
    config: Config,
    writer: asyncio.StreamWriter | None = None,
) -> dict[str, Any]:
    cmd = req.get("cmd", "")

    if cmd == "status":
        nodes = await db.get_all_nodes()
        pubkey_to_node = {n.pubkey: n for n in nodes}

        drifted_records = await db.get_drifted_settings()
        drifted = [
            {
                "node_pubkey": s.node_pubkey,
                "label": (
                    pubkey_to_node[s.node_pubkey].label if s.node_pubkey in pubkey_to_node else ""
                ),
                "key": s.key,
                "desired": s.desired,
                "observed": s.observed,
                "policy": s.policy.value,
            }
            for s in drifted_records
        ]

        _bad_states = {
            NodeState.UNKNOWN, NodeState.OFFLINE, NodeState.CONFIGURING, NodeState.PARTIAL,
        }
        bad_state = [
            {
                "pubkey": n.pubkey,
                "label": n.label,
                "name": n.name,
                "state": n.state.value,
                "last_seen": n.last_seen.isoformat() if n.last_seen else None,
            }
            for n in nodes if n.state in _bad_states
        ]

        db_pubkeys = {n.pubkey for n in nodes}
        ghosts = [
            {"pubkey_prefix": nc.pubkey_prefix, "label": nc.label}
            for nc in config.nodes
            if not any(pk.startswith(nc.pubkey_prefix) for pk in db_pubkeys)
        ]

        _has_default_password = "default" in config.secrets
        open_nodes = [
            {"pubkey": n.pubkey, "label": n.label, "name": n.name}
            for n in nodes
            if n.state == NodeState.CONFIGURED
            and not n.auth_required
            and not n.auth_cached
            and not _has_default_password
            and not any(n.pubkey.startswith(p) for p in config.secrets)
        ]

        rows = []
        for n in nodes:
            facts = await db.get_facts(n.pubkey)
            rows.append({
                "pubkey": n.pubkey,
                "name": n.name,
                "label": n.label,
                "group": n.group,
                "state": n.state.value,
                "last_seen": n.last_seen.isoformat() if n.last_seen else None,
                "last_connected": n.last_connected.isoformat() if n.last_connected else None,
                "neighbors": facts.get("neighbors"),
                "ver": facts.get("ver"),
            })
        return {
            "ok": True,
            "data": {
                "nodes": rows,
                "drifted": drifted,
                "bad_state": bad_state,
                "ghosts": ghosts,
                "open_nodes": open_nodes,
            },
        }

    if cmd == "node":
        pubkey = req.get("pubkey", "")
        try:
            node = await _resolve_node(pubkey, db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not node:
            return {"ok": False, "error": f"node {pubkey!r} not found"}
        facts = await db.get_facts(node.pubkey)
        db_settings = {s.key: s for s in await db.get_settings(node.pubkey)}
        logs = await db.get_logs(node.pubkey, limit=20)

        # Merge desired config with DB observations so newly-added settings are
        # visible even before the next sync cycle.
        node_cfg = next(
            (nc for nc in config.nodes if node.pubkey.startswith(nc.pubkey_prefix)),
            None,
        )
        groups = {g.name: g for g in config.groups}
        group_cfg = groups.get(node_cfg.group) if node_cfg and node_cfg.group else None

        settings_out: list[dict[str, Any]] = []
        config_keys: set[str] = set()
        if node_cfg:
            for key in sorted(all_setting_keys(node_cfg, group_cfg)):
                policy = resolve_policy(key, node_cfg, group_cfg)
                desired = resolve_desired(key, node_cfg, group_cfg)
                if policy == SettingPolicy.SKIP or desired is None:
                    continue
                config_keys.add(key)
                known = db_settings.get(key)
                observed = known.observed if known else None
                drift = observed is not None and observed != desired
                settings_out.append({
                    "key": key,
                    "desired": desired,
                    "observed": observed,
                    "policy": policy.value,
                    "drift": drift,
                })

        # DB settings no longer in config (stale/removed keys)
        for key, s in sorted(db_settings.items()):
            if key not in config_keys:
                settings_out.append({
                    "key": key,
                    "desired": s.desired,
                    "observed": s.observed,
                    "policy": s.policy.value,
                    "drift": s.drift,
                })

        return {
            "ok": True,
            "data": {
                "pubkey": node.pubkey,
                "name": node.name,
                "label": node.label,
                "group": node.group,
                "state": node.state.value,
                "last_seen": node.last_seen.isoformat() if node.last_seen else None,
                "last_connected": node.last_connected.isoformat() if node.last_connected else None,
                "auth_required": node.auth_required,
                "auth_cached": node.auth_cached,
                "passwd_known": (
                    "default" in config.secrets
                    or any(node.pubkey.startswith(p) for p in config.secrets)
                ),
                "facts": facts,
                "settings": settings_out,
                "logs": logs,
            },
        }

    if cmd == "sync":
        prefix = req.get("prefix")
        if prefix:
            # single-node forced sync — run inline so caller gets an error if prefix is bad
            try:
                result = await wq.run_node(prefix)
                return {"ok": True, "data": f"synced {result.name!r} ({result.pubkey[:8]})"}
            except OperationBusyError as exc:
                return {"ok": False, "error": str(exc)}
            except ValueError as exc:
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
        nodes = await db.get_all_nodes()
        groups = {g.name: g for g in config.groups}

        writes: list[dict[str, Any]] = []
        audits: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []

        for node_cfg in config.nodes:
            node = next((n for n in nodes if n.pubkey.startswith(node_cfg.pubkey_prefix)), None)
            if node is None:
                unknown.append({
                    "pubkey": node_cfg.pubkey_prefix,
                    "label": node_cfg.label,
                    "mesh_name": "",
                    "key": "",
                    "desired": None,
                    "known": None,
                    "policy": "",
                    "reason": "node not known to gateway/DB",
                })
                continue

            group_cfg = groups.get(node_cfg.group) if node_cfg.group else None
            known_settings = {s.key: s for s in await db.get_settings(node.pubkey)}

            for key in sorted(all_setting_keys(node_cfg, group_cfg)):
                policy = resolve_policy(key, node_cfg, group_cfg)
                desired = resolve_desired(key, node_cfg, group_cfg)
                if policy == SettingPolicy.SKIP or desired is None:
                    continue

                known = known_settings.get(key)
                item = {
                    "pubkey": node.pubkey,
                    "label": node.label,
                    "mesh_name": node.name,
                    "key": key,
                    "desired": desired,
                    "known": known.observed if known else None,
                    "policy": policy.value,
                }

                if known is None or known.observed is None:
                    unknown.append({**item, "reason": "no known value; sync must read first"})
                elif known.observed == desired:
                    continue
                elif policy == SettingPolicy.ENFORCE:
                    writes.append({**item, "command": f"set {key} {desired}"})
                elif policy == SettingPolicy.AUDIT:
                    audits.append(item)

        return {"ok": True, "data": {"writes": writes, "audits": audits, "unknown": unknown}}

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
        return {"ok": True, "data": logs}

    if cmd == "issues":
        nodes = await db.get_all_nodes()
        # drifted settings
        drifted_records = await db.get_drifted_settings()
        pubkey_to_node = {n.pubkey: n for n in nodes}
        drifted = [
            {
                "node_pubkey": s.node_pubkey,
                "label": (
                    pubkey_to_node[s.node_pubkey].label if s.node_pubkey in pubkey_to_node else ""
                ),
                "key": s.key,
                "desired": s.desired,
                "observed": s.observed,
                "policy": s.policy.value,
            }
            for s in drifted_records
        ]
        # nodes in a problematic state
        _bad_states = {
            NodeState.UNKNOWN, NodeState.OFFLINE, NodeState.CONFIGURING, NodeState.PARTIAL,
        }
        bad_state = [
            {
                "pubkey": n.pubkey,
                "label": n.label,
                "name": n.name,
                "state": n.state.value,
                "last_seen": n.last_seen.isoformat() if n.last_seen else None,
            }
            for n in nodes if n.state in _bad_states
        ]
        # config entries with no matching DB node
        db_pubkeys = {n.pubkey for n in nodes}
        ghosts = [
            {"pubkey_prefix": nc.pubkey_prefix, "label": nc.label}
            for nc in config.nodes
            if not any(pk.startswith(nc.pubkey_prefix) for pk in db_pubkeys)
        ]
        # nodes that synced successfully with no ACL protection
        _has_default_password = "default" in config.secrets
        open_nodes = [
            {"pubkey": n.pubkey, "label": n.label, "name": n.name}
            for n in nodes
            if n.state == NodeState.CONFIGURED
            and not n.auth_required
            and not n.auth_cached
            and not _has_default_password
            and not any(n.pubkey.startswith(p) for p in config.secrets)
        ]
        return {
            "ok": True,
            "data": {
                "drifted": drifted,
                "bad_state": bad_state,
                "ghosts": ghosts,
                "open_nodes": open_nodes,
            },
        }

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
        self._config_snapshot: dict[Path, FileSignature] = {}
        self._config_watch_task: asyncio.Task[None] | None = None
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
        await self._wq.run()

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
                snapshot[path] = (stat.st_mtime_ns, stat.st_size)
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
                new_config = load_config(Path(self._config.config_path))
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
        log.info("shutting down")
        if self._config_watch_task:
            self._config_watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._config_watch_task
        if self._wq:
            await self._wq.stop()
        if self._messenger:
            self._messenger.stop()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._gateway:
            await self._gateway.disconnect()
        if self._db:
            await self._db.close()


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
        raise SystemExit(1)

    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"error loading config: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    daemon = Daemon(config)
    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
