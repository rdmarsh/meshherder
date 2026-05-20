"""
Gateway — manages the single meshcore_py connection to the locally-attached USB node.

The gateway is the management entry point onto the mesh. All communication with
managed nodes goes through here. One instance per daemon process.

Message flow
------------
The firmware queues replies; MESSAGES_WAITING is the push signal that the queue
is non-empty. A background _receive_loop drains the queue whenever that fires,
routing each message to:
  • a pending command Future  (keyed by pubkey_prefix)
  • the dm_callback           (anything else — incoming DMs, unsolicited messages)

send_command() registers a Future before sending, then awaits it — it never
calls get_msg() directly. This means DMs received while a command is in flight
are still delivered to dm_callback rather than silently discarded.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from meshcore import MeshCore
from meshcore.events import EventType

from mch.config import GatewayConfig

log = logging.getLogger(__name__)


@dataclass
class CommandResult:
    success: bool
    text: str | None = None
    error: str | None = None


class GatewayError(Exception):
    pass


class Gateway:
    """
    Wraps the meshcore_py connection.

    send_command() uses a background receive loop rather than a per-call
    get_msg() drain. Only one send_cmd is in flight at a time (_send_lock)
    but the receive loop always runs, so DMs are never lost to a command drain.
    """

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._mc: MeshCore | None = None
        self._send_lock = asyncio.Lock()   # one send_cmd in flight at a time
        self._pending: dict[str, asyncio.Future[str]] = {}  # pubkey_prefix → response future
        self._dm_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._receive_task: asyncio.Task | None = None
        self.info: dict[str, Any] = {}  # populated by connect(); gateway's own SELF_INFO

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open serial connection, read gateway self-info, start receive loop."""
        log.info("connecting to gateway on %s", self._config.device)
        self._mc = await MeshCore.create_serial(self._config.device, self._config.baud)
        if self._mc is None:
            raise GatewayError(
                f"no response from gateway on {self._config.device} — "
                "is the node connected and powered on?"
            )

        result = await self._mc.commands.send_appstart()
        if result.type == EventType.ERROR:
            raise GatewayError(f"send_appstart failed: {result.payload}")

        self.info = result.payload
        log.info(
            "gateway connected: name=%r freq=%.3f bw=%g sf=%d cr=%d",
            self.info.get("name"),
            self.info.get("radio_freq"),
            self.info.get("radio_bw"),
            self.info.get("radio_sf"),
            self.info.get("radio_cr"),
        )

        if self._config.name and self._config.name != self.info.get("name"):
            log.info("setting gateway name to %r", self._config.name)
            await self._mc.commands.set_name(self._config.name)

        if self._config.radio:
            r = self._config.radio
            if (r.freq != self.info.get("radio_freq")
                    or r.bw != self.info.get("radio_bw")
                    or r.sf != self.info.get("radio_sf")):
                log.info(
                    "forcing gateway to home freq: %.3f/%d/SF%d",
                    r.freq, r.bw, r.sf,
                )
                await self._mc.commands.set_radio(r.freq, r.bw, r.sf, r.cr)
                self.info["radio_freq"] = r.freq
                self.info["radio_bw"] = r.bw
                self.info["radio_sf"] = r.sf
                self.info["radio_cr"] = r.cr

        self._receive_task = asyncio.create_task(
            self._receive_loop(), name="gateway-receive"
        )

    async def disconnect(self) -> None:
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        # meshcore_py has no close() — drop the reference
        self._mc = None
        log.info("gateway disconnected")

    @property
    def connected(self) -> bool:
        return self._mc is not None

    def _require_connected(self) -> MeshCore:
        if self._mc is None:
            raise GatewayError("not connected — call connect() first")
        return self._mc

    def set_dm_callback(
        self, callback: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        """Register an async callback for incoming DMs (not command responses)."""
        self._dm_callback = callback

    # ------------------------------------------------------------------
    # background receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        """
        Run forever: wait for MESSAGES_WAITING, then drain the queue.

        Routing:
          pubkey_prefix in self._pending → resolve the registered Future
          anything else                  → call dm_callback if set
        """
        mc = self._require_connected()
        log.debug("receive loop started")
        while True:
            try:
                notif = await mc.dispatcher.wait_for_event(
                    EventType.MESSAGES_WAITING, timeout=120
                )
                if notif is None:
                    # timeout — nothing queued, loop again
                    continue
                await self._drain_inbox(mc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("receive loop error: %s", exc)
                # prevent busy-spin if an unexpected error repeats
                await asyncio.sleep(5)

    async def _drain_inbox(self, mc: MeshCore) -> None:
        """Drain the firmware message queue, routing each message appropriately."""
        while True:
            msg = await mc.commands.get_msg()
            if msg.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
                break
            if msg.type != EventType.CONTACT_MSG_RECV:
                continue

            prefix = msg.payload.get("pubkey_prefix", "")
            text = msg.payload.get("text", "")
            # CLI command responses have a "> " prompt prefix
            text = text.removeprefix("> ")

            if prefix in self._pending:
                fut = self._pending.pop(prefix)
                if not fut.done():
                    fut.set_result(text)
                log.debug("command response from %s: %r", prefix, text)
            else:
                log.debug("incoming message from %s: %r", prefix, text)
                if self._dm_callback:
                    asyncio.create_task(
                        self._dm_callback(msg.payload),
                        name=f"dm-{prefix}",
                    )

    # ------------------------------------------------------------------
    # contacts
    # ------------------------------------------------------------------

    async def get_contacts(
        self,
        managed_only: bool = True,
        include_pending: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Return contacts from the gateway.

        managed_only=True   →  only contacts with flags & 1 (favourited/managed nodes).
        managed_only=False  →  all 350 stored contacts.
        include_pending     →  also include mc.pending_contacts (nodes heard via NEW_CONTACT
                                events that have not yet been confirmed as stored contacts).
                                Pending contacts are always included regardless of managed_only
                                since they have no flags set yet.
        """
        mc = self._require_connected()
        result = await mc.commands.get_contacts()
        if result.type == EventType.ERROR:
            raise GatewayError(f"get_contacts failed: {result.payload}")
        stored = result.payload or {}
        contacts = list(stored.values())
        if managed_only:
            contacts = [c for c in contacts if c.get("flags", 0) & 1]

        if include_pending:
            # pending contacts are nodes that have advertised but aren't in the stored list
            pending = mc.pending_contacts
            for pubkey, c in pending.items():
                if pubkey not in stored:
                    contacts.append(c)
            if pending:
                log.debug("get_contacts: %d pending contacts included", len(pending))

        log.debug("get_contacts: %d stored, managed_only=%s, include_pending=%s → %d returned",
                  len(stored), managed_only, include_pending, len(contacts))
        return contacts

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------

    async def hop_radio(
        self, freq: float, bw: float, sf: int, cr: int
    ) -> tuple[float, float, int, int]:
        """
        Temporarily change the gateway radio config to reach a node on a different frequency.

        Returns the saved (freq, bw, sf, cr) so the caller can restore it.
        Always use in a try/finally so the radio is always restored.
        """
        mc = self._require_connected()
        saved = (
            self.info.get("radio_freq"),
            self.info.get("radio_bw"),
            self.info.get("radio_sf"),
            self.info.get("radio_cr"),
        )
        log.info(
            "hopping radio: %.3f/%g/SF%d → %.3f/%g/SF%d",
            saved[0], saved[1], saved[2], freq, bw, sf,
        )
        await mc.commands.set_radio(freq, bw, sf, cr)
        await asyncio.sleep(0.5)  # let radio settle before sending
        self.info["radio_freq"] = freq
        self.info["radio_bw"] = bw
        self.info["radio_sf"] = sf
        self.info["radio_cr"] = cr
        return saved

    async def restore_radio(
        self, freq: float, bw: float, sf: int, cr: int
    ) -> None:
        """Restore the gateway radio config after a frequency hop."""
        mc = self._require_connected()
        await mc.commands.set_radio(freq, bw, sf, cr)
        self.info["radio_freq"] = freq
        self.info["radio_bw"] = bw
        self.info["radio_sf"] = sf
        self.info["radio_cr"] = cr
        log.info("radio restored: %.3f/%.0f/SF%d", freq, bw, sf)

    async def login(self, contact: dict[str, Any], password: str) -> bool:
        """
        Authenticate to a managed node.  After the first successful login
        the gateway's pubkey is added to the node ACL and future sessions
        may not need a password.
        """
        mc = self._require_connected()
        label = contact.get("adv_name") or contact.get("public_key", "")[:12]
        log.info("logging in to %s", label)
        result = await mc.commands.send_login_sync(
            contact, password, timeout=30, min_timeout=30
        )
        if result and result.type == EventType.LOGIN_SUCCESS:
            log.info(
                "login OK for %s  permissions=%s  is_admin=%s",
                label,
                result.payload.get("permissions"),
                result.payload.get("is_admin"),
            )
            return True
        log.warning("login failed for %s: %r", label, result)
        return False

    # ------------------------------------------------------------------
    # command execution
    # ------------------------------------------------------------------

    async def send_command(
        self,
        contact: dict[str, Any],
        command: str,
        timeout: float = 30.0,
        await_response: bool = True,
    ) -> CommandResult:
        """
        Send a CLI command to a managed node and return the text response.

        When await_response=True (default): registers a Future keyed by pubkey_prefix
        and waits for the background receive loop to resolve it via CONTACT_MSG_RECV.

        When await_response=False: sends the command and returns immediately without
        waiting for a response. Use for commissioning commands where flood-routed
        contacts cannot route their responses back (no path to gateway in routing table).

        Only one send_cmd is in flight at a time (_send_lock) to avoid the
        gateway's suggested_timeout calculation going wrong when two commands
        are queued simultaneously.
        """
        mc = self._require_connected()
        pubkey_prefix = contact["public_key"][:12]

        if await_response:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[str] = loop.create_future()
            self._pending[pubkey_prefix] = fut

        async with self._send_lock:
            log.debug("send_command to %r: %r", contact.get("adv_name"), command)
            result = await mc.commands.send_cmd(contact, command)
            if result.type == EventType.ERROR:
                if await_response:
                    self._pending.pop(pubkey_prefix, None)
                return CommandResult(success=False, error=f"send failed: {result.payload}")

        if not await_response:
            log.debug("sent (no-wait) to %r: %r", contact.get("adv_name"), command)
            return CommandResult(success=True)

        # _send_lock released — receive loop will now resolve the future
        try:
            text = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(pubkey_prefix, None)
            log.warning("timeout waiting for response to %r from %r", command, contact.get("adv_name"))
            return CommandResult(success=False, error="timeout — no response from node")

        log.debug("response from %r: %r", contact.get("adv_name"), text)
        return CommandResult(success=True, text=text)

    # ------------------------------------------------------------------
    # messaging (DM and channel)
    # ------------------------------------------------------------------

    async def send_dm(
        self,
        contact: dict[str, Any],
        text: str,
    ) -> bool:
        """
        Send a direct message to a contact.  Uses send_msg_with_retry for
        reliability (ACK-confirmed, up to 3 attempts with path reset fallback).
        Returns True if the message was ACK'd, False otherwise.
        """
        mc = self._require_connected()
        log.debug("send_dm to %r: %r", contact.get("adv_name"), text)
        result = await mc.commands.send_msg_with_retry(
            contact, text, timeout=30, min_timeout=30
        )
        if result is None:
            log.warning("send_dm to %r failed (no ACK)", contact.get("adv_name"))
            return False
        return True

    async def send_channel_msg(self, channel: int, text: str) -> bool:
        """Send a message to a numbered channel slot. Returns True on OK."""
        mc = self._require_connected()
        result = await mc.commands.send_chan_msg(channel, text)
        if result.type == EventType.ERROR:
            log.warning("send_channel_msg(%d) failed: %s", channel, result.payload)
            return False
        return True

    async def advert(self, zerohop: bool = True) -> None:
        """Send an advertisement from the local gateway node.

        zerohop=True  → advert.zerohop (flood=False) — reaches immediate neighbors only.
        zerohop=False → full flood advert.
        """
        mc = self._require_connected()
        log.info("sending advert (zerohop=%s)", zerohop)
        await mc.commands.send_advert(flood=not zerohop)

    async def set_contact_favourite(self, contact: dict[str, Any]) -> bool:
        """Set the favourite flag (bit 0) on a gateway contact. Returns True on success."""
        mc = self._require_connected()
        new_flags = contact.get("flags", 0) | 1
        result = await mc.commands.change_contact_flags(contact, new_flags)
        if result.type == EventType.ERROR:
            log.warning("set_contact_favourite failed for %s: %s",
                        contact.get("public_key", "?")[:12], result.payload)
            return False
        contact["flags"] = new_flags
        log.debug("favourited contact %s", contact.get("public_key", "?")[:12])
        return True

    # ------------------------------------------------------------------
    # radio
    # ------------------------------------------------------------------

    async def set_radio(
        self, freq: float, bw: float, sf: int, cr: int
    ) -> None:
        """Permanently change gateway radio settings. Caller is responsible for save/restore."""
        mc = self._require_connected()
        result = await mc.commands.set_radio(freq, bw, sf, cr)
        if result.type == EventType.ERROR:
            raise GatewayError(f"set_radio failed: {result.payload}")
        log.info("gateway radio changed to %.3f/%.0f/%d/%d", freq, bw, sf, cr)

    async def current_radio(self) -> tuple[float, float, int, int]:
        """Return (freq, bw, sf, cr) from a fresh send_appstart."""
        mc = self._require_connected()
        result = await mc.commands.send_appstart()
        p = result.payload
        return p["radio_freq"], p["radio_bw"], p["radio_sf"], p["radio_cr"]

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------

    FACTORY_FREQ: tuple[float, float, int, int] = (869.618, 62.5, 8, 5)

    async def discover(
        self,
        factory_freq: tuple[float, float, int, int] = FACTORY_FREQ,
        dwell: float = 8.0,
    ) -> list[dict[str, Any]]:
        """
        Hop to factory frequency, broadcast a node-discover request, collect
        DISCOVER_RESPONSE events for `dwell` seconds, then restore radio.

        Subscribe before sending to avoid the race where a fast responder
        replies before we've registered the callback.

        Returns a list of response payloads (dicts with pubkey, node_type, SNR_in, …).
        """
        mc = self._require_connected()
        freq, bw, sf, cr = factory_freq
        saved = await self.hop_radio(freq, bw, sf, cr)

        discovered: list[dict[str, Any]] = []

        def _on_discover_response(event: Any) -> None:
            discovered.append(event.payload)
            log.debug(
                "discover: found %s (type=%d SNR=%.1f)",
                event.payload.get("pubkey", "?")[:12],
                event.payload.get("node_type", -1),
                event.payload.get("SNR_in", 0),
            )

        sub = mc.dispatcher.subscribe(EventType.DISCOVER_RESPONSE, _on_discover_response)
        try:
            # prefix_only=False: get the full 32-byte pubkey in the response.
            # The default (True) returns only 8 bytes, which is insufficient for login.
            await mc.commands.send_node_discover_req(filter=0x0F, prefix_only=False)
            await asyncio.sleep(dwell)
        finally:
            sub.unsubscribe()
            await self.restore_radio(*saved)

        log.info("discover: found %d node(s) in %.1fs on %.3f MHz", len(discovered), dwell, freq)
        return discovered

    async def add_commission_contact(self, node_info: dict[str, Any]) -> dict[str, Any]:
        """Add a node from a DISCOVER_RESPONSE to the gateway's contact store.

        DISCOVER_RESPONSE gives us the pubkey but does NOT add the node to the
        gateway's contact store. send_cmd fails with ERR_CODE_NOT_FOUND if the
        destination isn't there. This method adds it with flood routing (no known
        path to a fresh node) so subsequent send_command() calls succeed.

        Returns the contact dict to pass to send_command().
        """
        mc = self._require_connected()
        contact = {
            "public_key": node_info["pubkey"],
            "type": node_info.get("node_type", 2),
            "flags": 0,
            "out_path": "",
            "out_path_len": -1,  # -1 = flood routing
            "out_path_hash_mode": 0,
            "adv_name": "",
            "last_advert": 0,
            "adv_lat": 0,
            "adv_lon": 0,
        }
        result = await mc.commands.add_contact(contact)
        if result.type == EventType.ERROR:
            log.warning(
                "add_commission_contact: could not add %s to contact store (%s) — send_command may fail",
                node_info["pubkey"][:12],
                result.payload,
            )
        else:
            log.info("commission: added %s to contact store (flood routing)", node_info["pubkey"][:12])
        return contact
