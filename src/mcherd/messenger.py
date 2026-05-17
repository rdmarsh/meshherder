"""
Mesh messenger — receives DMs from authorised contacts and processes commands.

Supported DM commands
---------------------
  help              list available commands
  status            fleet summary (nodes, drift count)
  nodes             table of all known nodes
  node <name>       details for one node (partial name or pubkey match)
  sync              trigger an immediate sync cycle
  drift             list settings that are drifted from desired
  logs [n]          last n daemon log lines (default 10)

Access control
--------------
Senders must have a pubkey_prefix matching one of the entries in
config.messenger.allowed_prefixes.  All others are silently ignored.

Channel announcements
---------------------
If config.messenger.announce_channel is set, the daemon posts a brief
status message to that channel number at the end of each sync cycle.
"""

from __future__ import annotations

import logging
from typing import Any

from mcherd.db import Database
from mcherd.gateway import Gateway

log = logging.getLogger(__name__)

# Maximum DM reply length — MeshCore CLI messages cap out around 200 bytes.
# Long replies are split into chunks and sent sequentially.
_MAX_CHUNK = 180


class Messenger:
    def __init__(
        self,
        gateway: Gateway,
        db: Database,
        allowed_prefixes: list[str],
        announce_channel: int | None = None,
    ) -> None:
        self._gateway = gateway
        self._db = db
        self._allowed_prefixes = [p.lower() for p in allowed_prefixes]
        self._announce_channel = announce_channel
        # contact cache: pubkey_prefix → contact dict (populated on incoming messages)
        self._contacts: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        """Register the DM callback with the gateway."""
        self._gateway.set_dm_callback(self._on_dm)
        log.info(
            "messenger started — %d authorised prefix(es), announce_channel=%s",
            len(self._allowed_prefixes),
            self._announce_channel,
        )

    def stop(self) -> None:
        self._gateway.set_dm_callback(None)

    # ------------------------------------------------------------------
    # incoming DM handler
    # ------------------------------------------------------------------

    async def _on_dm(self, payload: dict[str, Any]) -> None:
        """Called by the gateway receive loop for every non-command CONTACT_MSG_RECV."""
        prefix = payload.get("pubkey_prefix", "")
        text = (payload.get("text", "") or "").strip()

        if not self._is_authorised(prefix):
            log.debug("DM from unauthorised sender %s — ignored", prefix)
            return

        log.info("DM from %s: %r", prefix, text)

        # Keep a mini contact record so we can reply.  The gateway doesn't give
        # us the full contact object on a CONTACT_MSG_RECV, just the prefix.
        # Store it anyway so send_dm can look it up.
        contact = self._contacts.get(prefix) or {"public_key": prefix, "adv_name": prefix}

        response = await self._handle_command(text)
        await self._reply(contact, response)

    def _is_authorised(self, pubkey_prefix: str) -> bool:
        for allowed in self._allowed_prefixes:
            if pubkey_prefix.startswith(allowed) or allowed.startswith(pubkey_prefix):
                return True
        return False

    # ------------------------------------------------------------------
    # reply helpers
    # ------------------------------------------------------------------

    async def _reply(self, contact: dict[str, Any], text: str) -> None:
        """Send a DM reply, splitting into chunks if needed."""
        chunks = _chunk(text, _MAX_CHUNK)
        for chunk in chunks:
            ok = await self._gateway.send_dm(contact, chunk)
            if not ok:
                log.warning("failed to send DM reply chunk to %s", contact.get("public_key"))
                break

    # ------------------------------------------------------------------
    # command dispatch
    # ------------------------------------------------------------------

    async def _handle_command(self, text: str) -> str:
        parts = text.strip().split(None, 1)
        if not parts:
            return _help_text()

        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("help", "?"):
            return _help_text()
        if cmd == "cmd":
            return "status nodes node drift logs help"
        if cmd == "status":
            return await self._cmd_status()
        if cmd == "nodes":
            return await self._cmd_nodes()
        if cmd == "node":
            return await self._cmd_node(arg)
        if cmd == "sync":
            return "sync: not supported over DM (daemon handles scheduling)"
        if cmd == "drift":
            return await self._cmd_drift()
        if cmd == "logs":
            n = int(arg) if arg.isdigit() else 10
            return await self._cmd_logs(n)

        return f"unknown command: {cmd!r}\nsend 'help' for a list"

    # ------------------------------------------------------------------
    # individual commands
    # ------------------------------------------------------------------

    async def _cmd_status(self) -> str:
        nodes = await self._db.get_all_nodes()
        drifted = await self._db.get_drifted_settings()
        state_counts: dict[str, int] = {}
        for n in nodes:
            state_counts[n.state.value] = state_counts.get(n.state.value, 0) + 1

        lines = [f"mcherd status — {len(nodes)} nodes"]
        for state, count in sorted(state_counts.items()):
            lines.append(f"  {state}: {count}")
        if drifted:
            lines.append(f"drift: {len(drifted)} setting(s)")
        return "\n".join(lines)

    async def _cmd_nodes(self) -> str:
        nodes = await self._db.get_all_nodes()
        if not nodes:
            return "no nodes in database"
        lines = []
        for n in nodes:
            last = (n.last_seen.strftime("%m-%d %H:%M") if n.last_seen else "never")
            lines.append(f"{n.name[:18]} [{n.state.value}] {last}")
        return "\n".join(lines)

    async def _cmd_node(self, name_or_prefix: str) -> str:
        if not name_or_prefix:
            return "usage: node <name-or-pubkey-prefix>"
        nodes = await self._db.get_all_nodes()
        match = _find_node(nodes, name_or_prefix)
        if not match:
            return f"no node matching {name_or_prefix!r}"

        facts = await self._db.get_facts(match.pubkey)
        settings = await self._db.get_settings(match.pubkey)
        drifted = [s for s in settings if s.drift]

        lines = [
            f"{match.name}",
            f"state: {match.state.value}",
            f"pubkey: {match.pubkey[:16]}…",
        ]
        if facts.get("ver"):
            lines.append(f"ver: {facts['ver']}")
        if facts.get("board"):
            lines.append(f"board: {facts['board']}")
        if drifted:
            lines.append(f"drift: {', '.join(s.key for s in drifted)}")
        else:
            lines.append("no drift")
        return "\n".join(lines)

    async def _cmd_drift(self) -> str:
        drifted = await self._db.get_drifted_settings()
        if not drifted:
            return "no drift"
        lines = [f"drift ({len(drifted)})"]
        for s in drifted[:10]:
            lines.append(f"  {s.node_pubkey[:8]}… {s.key}: {s.observed!r} → {s.desired!r}")
        if len(drifted) > 10:
            lines.append(f"  … and {len(drifted) - 10} more")
        return "\n".join(lines)

    async def _cmd_logs(self, n: int) -> str:
        entries = await self._db.get_logs(limit=min(n, 20))
        if not entries:
            return "no logs"
        lines = []
        for e in entries:
            ts = (e.get("timestamp") or "")[:16]
            lines.append(f"[{e['level'][:4]}] {ts} {e['message'][:60]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # channel announcements
    # ------------------------------------------------------------------

    async def announce_cycle_complete(self, node_count: int, drift_count: int) -> None:
        """Post a brief summary to the announce channel after each cycle."""
        if self._announce_channel is None:
            return
        drift_str = f", {drift_count} drifted" if drift_count else ""
        msg = f"mcherd: cycle done — {node_count} nodes{drift_str}"
        ok = await self._gateway.send_channel_msg(self._announce_channel, msg)
        if not ok:
            log.warning("failed to post cycle announcement to channel %d", self._announce_channel)

    def register_contact(self, pubkey_prefix: str, contact: dict[str, Any]) -> None:
        """Store a contact so we can DM them by prefix. Called when we see them in get_contacts."""
        self._contacts[pubkey_prefix[:12]] = contact


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _help_text() -> str:
    return (
        "mcherd commands:\n"
        "  status        fleet overview\n"
        "  nodes         list all nodes\n"
        "  node <name>   node detail\n"
        "  drift         drifted settings\n"
        "  logs [n]      last n log lines\n"
        "  help          this message"
    )


def _chunk(text: str, size: int) -> list[str]:
    """Split text into chunks no larger than size bytes (UTF-8 safe)."""
    encoded = text.encode("utf-8")
    chunks = []
    while encoded:
        chunk = encoded[:size]
        # back off one byte at a time until we have a valid UTF-8 boundary
        while chunk:
            try:
                decoded = chunk.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        else:
            break  # input is not valid UTF-8 — shouldn't happen in practice
        chunks.append(decoded)
        encoded = encoded[len(chunk):]
    return chunks


def _find_node(nodes, query: str):
    query_lower = query.lower()
    # exact name match first
    for n in nodes:
        if n.name.lower() == query_lower:
            return n
    # prefix match on name
    for n in nodes:
        if n.name.lower().startswith(query_lower):
            return n
    # pubkey prefix match
    for n in nodes:
        if n.pubkey.startswith(query_lower):
            return n
    return None
