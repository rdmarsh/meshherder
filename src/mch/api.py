"""Loopback-only read-only HTTP API backed by the normalized fleet report model."""

from __future__ import annotations

import asyncio
import json
import logging
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from mch.config import Config
from mch.db import Database
from mch.models import Node
from mch.report import build_fleet_report, build_node_report

log = logging.getLogger(__name__)
_MAX_REQUEST_LINE = 16384


class ReadOnlyApi:
    """Small HTTP/1.1 JSON server; mutations intentionally do not exist here."""

    def __init__(self, config: Config, db: Database) -> None:
        self._config = config
        self._db = db
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        api = self._config.api
        self._server = await asyncio.start_server(self._handle_client, api.host, api.port)
        sockets: list[Any] = list(self._server.sockets or [])
        bound = sockets[0].getsockname() if sockets else (api.host, api.port)
        log.info("read-only HTTP API listening at http://%s:%s/status", bound[0], bound[1])

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def port(self) -> int:
        """Return the actual listening port, including ephemeral ports used by tests."""
        if not self._server or not self._server.sockets:
            raise RuntimeError("API server is not listening")
        return int(self._server.sockets[0].getsockname()[1])

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            if len(raw) > _MAX_REQUEST_LINE:
                await _write_json(
                    writer,
                    HTTPStatus.REQUEST_URI_TOO_LONG,
                    {"error": "URI too long"},
                )
                return
            parts = raw.decode("ascii", errors="replace").strip().split()
            if len(parts) != 3:
                await _write_json(writer, HTTPStatus.BAD_REQUEST, {"error": "bad request line"})
                return
            method, target, _http_version = parts
            # Consume headers before replying so a normal HTTP client can reuse its parser safely.
            while True:
                header = await asyncio.wait_for(reader.readline(), timeout=10)
                if header in {b"", b"\r\n", b"\n"}:
                    break
            if method != "GET":
                await _write_json(
                    writer,
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "read-only API accepts GET only"},
                    extra_headers={"Allow": "GET"},
                )
                return
            status, data = await self.route(target)
            await _write_json(writer, status, data)
        except (TimeoutError, UnicodeError):
            await _write_json(writer, HTTPStatus.BAD_REQUEST, {"error": "bad request"})
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def route(self, target: str) -> tuple[HTTPStatus, dict[str, Any] | list[dict[str, Any]]]:
        """Resolve an API request target; public for direct unit testing."""
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)
        if path in {"/health", "/healthz"}:
            return HTTPStatus.OK, {"ok": True}
        if path in {"/status", "/api/v1/fleet"}:
            return HTTPStatus.OK, await build_fleet_report(self._db, self._config)
        if path in {"/issues", "/api/v1/issues"}:
            report = await build_fleet_report(self._db, self._config)
            return HTTPStatus.OK, report["issues"]
        if path in {"/plan", "/api/v1/plan"}:
            report = await build_fleet_report(self._db, self._config)
            return HTTPStatus.OK, report["plan"]
        if path in {"/logs", "/api/v1/logs"}:
            try:
                limit = min(max(int(params.get("limit", ["50"])[0]), 1), 500)
            except ValueError:
                return HTTPStatus.BAD_REQUEST, {"error": "limit must be an integer"}
            query = params.get("node", [None])[0]
            pubkey: str | None = None
            if query:
                try:
                    node = await _resolve_node(unquote(query), self._db)
                except ValueError as exc:
                    return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
                if node is None:
                    return HTTPStatus.NOT_FOUND, {"error": f"node {query!r} not found"}
                pubkey = node.pubkey
            logs = await self._db.get_logs(pubkey, limit=limit)
            labels = {node.pubkey: node.label for node in await self._db.get_all_nodes()}
            return HTTPStatus.OK, [
                {**entry, "label": labels.get(entry["node_pubkey"], "")}
                if entry.get("node_pubkey") else entry
                for entry in logs
            ]
        prefixes = ("/status/", "/api/v1/nodes/")
        prefix = next((candidate for candidate in prefixes if path.startswith(candidate)), None)
        if prefix:
            query = unquote(path[len(prefix):])
            try:
                node = await _resolve_node(query, self._db)
            except ValueError as exc:
                return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
            if node is None:
                return HTTPStatus.NOT_FOUND, {"error": f"node {query!r} not found"}
            return HTTPStatus.OK, await build_node_report(self._db, self._config, node)
        return HTTPStatus.NOT_FOUND, {"error": "not found"}


async def _resolve_node(query: str, db: Database) -> Node | None:
    node = await db.get_node(query)
    if node:
        return node
    all_nodes = await db.get_all_nodes()
    prefix_matches = [node for node in all_nodes if node.pubkey.startswith(query)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        raise ValueError(f"ambiguous: {len(prefix_matches)} nodes match {query!r}")
    lowered = query.lower()
    matches = [
        node for node in all_nodes
        if lowered in (node.label or "").lower() or lowered in node.name.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"ambiguous: {len(matches)} nodes match {query!r}")
    return None


async def _write_json(
    writer: asyncio.StreamWriter,
    status: HTTPStatus,
    data: dict[str, Any] | list[dict[str, Any]],
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "Cache-Control": "no-store",
        "Connection": "close",
        **(extra_headers or {}),
    }
    head = f"HTTP/1.1 {status.value} {status.phrase}\r\n"
    head += "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    writer.write(head.encode("ascii") + b"\r\n" + body)
    await writer.drain()
