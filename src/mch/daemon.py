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
from pathlib import Path
from typing import Any

from mch.config import Config
from mch.config import load as load_config
from mch.db import Database
from mch.gateway import Gateway, GatewayError
from mch.messenger import Messenger
from mch.models import NodeState
from mch.queue import WorkQueue

log = logging.getLogger(__name__)


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
        settings = await db.get_settings(node.pubkey)
        logs = await db.get_logs(node.pubkey, limit=20)
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
                "settings": [
                    {
                        "key": s.key,
                        "desired": s.desired,
                        "observed": s.observed,
                        "policy": s.policy.value,
                        "drift": s.drift,
                        "last_checked": s.last_checked.isoformat() if s.last_checked else None,
                        "last_set": s.last_set.isoformat() if s.last_set else None,
                    }
                    for s in settings
                ],
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
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        # no prefix — full cycle in background
        asyncio.create_task(wq.run_cycle())
        return {"ok": True, "data": "sync started"}

    if cmd == "exec":
        _BLOCKED = {
            "erase", "start ota", "set prv.key", "password", "setperm", "set guest.password",
        }
        _FIRE_AND_FORGET = {"reboot", "clkreboot"}
        pubkey = req.get("pubkey", "")
        command = (req.get("command") or "").strip()
        cmd_root = command.split()[0].lower() if command else ""
        if cmd_root in _BLOCKED:
            return {"ok": False, "error": f"{cmd_root!r} is blocked - use config operations"}
        try:
            node = await _resolve_node(pubkey, db)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not node:
            return {"ok": False, "error": f"node {pubkey!r} not found"}
        contact = next(
            (c for c in wq._contacts if c.get("public_key", "")[:12] == node.pubkey[:12]),
            None,
        )
        if not contact:
            return {"ok": False, "error": f"no gateway contact for {node.pubkey[:12]}"}
        faf = cmd_root in _FIRE_AND_FORGET
        cmd_result = await wq._gateway.send_command(contact, command, timeout=60)
        if faf and not cmd_result.success:
            return {"ok": True, "data": "(sent - no response expected)"}
        if not cmd_result.success:
            return {"ok": False, "error": cmd_result.error or "no response"}
        return {"ok": True, "data": cmd_result.text or "(ok)"}

    if cmd == "contacts":
        from mch.queue import get_unfiled_contacts
        gateway = wq._gateway
        unfiled = await get_unfiled_contacts(config, gateway)
        return {"ok": True, "data": unfiled}

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

    if cmd == "hint":
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
        hint_results: list[dict[str, Any]] = []
        for n in nodes:
            facts = await db.get_facts(n.pubkey)
            raw_count = facts.get("neighbors")
            count = int(raw_count) if raw_count is not None else None
            # treat unknown neighbour count as 0 for profile lookup (mobile = most conservative)
            # count stays None so NBRS displays as '-' in the table
            profile = get_profile(count if count is not None else 0, profiles)
            hint_results.append({
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
        return {"ok": True, "data": hint_results}

    if cmd == "commission":
        from mch.commission import handle_commission
        progress_cb = None
        if writer is not None and req.get("mode") in ("new", "replace"):
            async def progress_cb(msg: str) -> None:
                try:
                    writer.write((json.dumps({"type": "progress", "msg": msg}) + "\n").encode())
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    pass
        return await handle_commission(req, wq, db, config, progress=progress_cb)

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

        log.info("mchd started")
        await self._wq.run()

    async def _shutdown(self) -> None:
        log.info("shutting down")
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

    parser = argparse.ArgumentParser(description="mchd — Mesh Config Herder node management daemon")
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
