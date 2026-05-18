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

from mch.config import Config, load as load_config
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
        resp = await _dispatch(req, wq, db, config)
    except asyncio.TimeoutError:
        resp = {"ok": False, "error": "timeout reading command"}
    except json.JSONDecodeError as exc:
        resp = {"ok": False, "error": f"bad JSON: {exc}"}
    except Exception as exc:
        log.exception("client handler error: %s", exc)
        resp = {"ok": False, "error": str(exc)}

    writer.write((json.dumps(resp) + "\n").encode())
    await writer.drain()
    writer.close()


async def _dispatch(
    req: dict[str, Any],
    wq: WorkQueue,
    db: Database,
    config: Config,
) -> dict[str, Any]:
    cmd = req.get("cmd", "")

    if cmd == "status":
        nodes = await db.get_all_nodes()
        drifted = await db.get_drifted_settings()
        return {
            "ok": True,
            "data": {
                "nodes": [
                    {
                        "pubkey": n.pubkey,
                        "name": n.name,
                        "state": n.state.value,
                        "last_seen": n.last_seen.isoformat() if n.last_seen else None,
                    }
                    for n in nodes
                ],
                "drifted_settings": len(drifted),
            },
        }

    if cmd == "nodes":
        nodes = await db.get_all_nodes()
        return {
            "ok": True,
            "data": [
                {
                    "pubkey": n.pubkey,
                    "name": n.name,
                    "label": n.label,
                    "group": n.group,
                    "state": n.state.value,
                    "last_seen": n.last_seen.isoformat() if n.last_seen else None,
                    "last_connected": n.last_connected.isoformat() if n.last_connected else None,
                }
                for n in nodes
            ],
        }

    if cmd == "node":
        pubkey = req.get("pubkey", "")
        node = await db.get_node(pubkey)
        if not node:
            # fall back to prefix match
            all_nodes = await db.get_all_nodes()
            node = next((n for n in all_nodes if n.pubkey.startswith(pubkey)), None)
        if not node:
            return {"ok": False, "error": f"node {pubkey!r} not found"}
        facts = await db.get_facts(pubkey)
        settings = await db.get_settings(pubkey)
        logs = await db.get_logs(pubkey, limit=20)
        return {
            "ok": True,
            "data": {
                "pubkey": node.pubkey,
                "name": node.name,
                "group": node.group,
                "state": node.state.value,
                "last_seen": node.last_seen.isoformat() if node.last_seen else None,
                "last_connected": node.last_connected.isoformat() if node.last_connected else None,
                "auth_required": node.auth_required,
                "auth_cached": node.auth_cached,
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

    if cmd == "contacts":
        from mch.queue import get_unfiled_contacts
        gateway = wq._gateway
        unfiled = await get_unfiled_contacts(config, gateway)
        return {"ok": True, "data": unfiled}

    if cmd == "logs":
        pubkey = req.get("pubkey")
        limit = int(req.get("limit", 50))
        if pubkey:
            # expand prefix to full pubkey using the same logic as the node handler
            node = await db.get_node(pubkey)
            if not node:
                all_nodes = await db.get_all_nodes()
                node = next((n for n in all_nodes if n.pubkey.startswith(pubkey)), None)
            pubkey = node.pubkey if node else pubkey
        logs = await db.get_logs(pubkey, limit=limit)
        return {"ok": True, "data": logs}

    if cmd == "drift":
        drifted = await db.get_drifted_settings()
        return {
            "ok": True,
            "data": [
                {
                    "node_pubkey": s.node_pubkey,
                    "key": s.key,
                    "desired": s.desired,
                    "observed": s.observed,
                    "policy": s.policy.value,
                }
                for s in drifted
            ],
        }

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
        await self._db.write_log(None, "INFO", "mchd starting")

        self._gateway = Gateway(config.gateway)
        try:
            await self._gateway.connect()
        except GatewayError as exc:
            log.error("gateway connection failed: %s", exc)
            await self._db.write_log(None, "ERROR", f"gateway connection failed: {exc}")
            await self._db.close()
            raise SystemExit(1)

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

        async def client_connected(r, w):
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
            await self._db.write_log(None, "INFO", "mchd stopped")
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
        raise SystemExit(1)

    daemon = Daemon(config)
    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
