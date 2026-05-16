"""
Gateway — manages the single meshcore_py connection to the locally-attached USB node.

The gateway is the management entry point onto the mesh. All communication with
managed nodes goes through here. One instance per daemon process.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from meshcore import MeshCore
from meshcore.events import EventType

from mcherd.config import GatewayConfig

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
    Wraps the meshcore_py connection.  All send_cmd calls go through
    send_command(), which implements the correct flow:

        send_cmd()
            → wait for MESSAGES_WAITING
            → get_msg() loop until NO_MORE_MSGS
            → return matching CONTACT_MSG_RECV text

    Only one command is in flight at a time (_cmd_lock) because get_msg()
    drains the queue positionally — pipelining would mix up responses.
    """

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._mc: MeshCore | None = None
        self._cmd_lock = asyncio.Lock()
        self.info: dict[str, Any] = {}  # populated by connect(); gateway's own SELF_INFO

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open serial connection and read gateway self-info."""
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
            "gateway connected: name=%r  freq=%.3f  bw=%.0f  sf=%d  cr=%d",
            self.info.get("name"),
            self.info.get("radio_freq"),
            self.info.get("radio_bw"),
            self.info.get("radio_sf"),
            self.info.get("radio_cr"),
        )

        # optionally rename gateway if config specifies a name
        if self._config.name and self._config.name != self.info.get("name"):
            log.info("setting gateway name to %r", self._config.name)
            await self._mc.commands.set_name(self._config.name)

    async def disconnect(self) -> None:
        # meshcore_py has no close() method; just drop the reference
        self._mc = None
        log.info("gateway disconnected")

    @property
    def connected(self) -> bool:
        return self._mc is not None

    def _require_connected(self) -> MeshCore:
        if self._mc is None:
            raise GatewayError("not connected — call connect() first")
        return self._mc

    # ------------------------------------------------------------------
    # contacts
    # ------------------------------------------------------------------

    async def get_contacts(self, managed_only: bool = True) -> list[dict[str, Any]]:
        """
        Return contacts from the gateway.

        managed_only=True  →  only contacts with flags & 1 (favourited/managed nodes).
        managed_only=False →  all 350 stored contacts.
        """
        mc = self._require_connected()
        result = await mc.commands.get_contacts()
        if result.type == EventType.ERROR:
            raise GatewayError(f"get_contacts failed: {result.payload}")
        contacts = list((result.payload or {}).values())
        if managed_only:
            contacts = [c for c in contacts if c.get("flags", 0) & 1]
        log.debug("get_contacts: %d total, managed_only=%s → %d returned",
                  len(result.payload or {}), managed_only, len(contacts))
        return contacts

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------

    async def login(self, contact: dict[str, Any], password: str) -> bool:
        """
        Authenticate to a managed node.  After the first successful login
        the gateway's pubkey is added to the node ACL and future sessions
        may not need a password.
        """
        mc = self._require_connected()
        log.info("logging in to %r", contact.get("adv_name"))
        result = await mc.commands.send_login_sync(
            contact, password, timeout=30, min_timeout=30
        )
        if result and result.type == EventType.LOGIN_SUCCESS:
            log.info(
                "login OK for %r  permissions=%s  is_admin=%s",
                contact.get("adv_name"),
                result.payload.get("permissions"),
                result.payload.get("is_admin"),
            )
            return True
        log.warning("login failed for %r: %r", contact.get("adv_name"), result)
        return False

    # ------------------------------------------------------------------
    # command execution
    # ------------------------------------------------------------------

    async def send_command(
        self,
        contact: dict[str, Any],
        command: str,
        timeout: float = 30.0,
    ) -> CommandResult:
        """
        Send a CLI command to a managed node and return the text response.

        Correct flow (confirmed via live testing):
          1. send_cmd()           → MSG_SENT
          2. wait MESSAGES_WAITING → node has queued a reply
          3. get_msg() loop       → drain until we get our CONTACT_MSG_RECV
          4. return text

        The _cmd_lock ensures only one command is in flight at a time,
        preventing get_msg() from picking up the wrong response.
        """
        mc = self._require_connected()
        pubkey_prefix = contact["public_key"][:12]

        async with self._cmd_lock:
            log.debug("send_command to %r: %r", contact.get("adv_name"), command)

            result = await mc.commands.send_cmd(contact, command)
            if result.type == EventType.ERROR:
                return CommandResult(success=False, error=f"send failed: {result.payload}")

            # wait for node to signal its reply is queued
            notif = await mc.dispatcher.wait_for_event(
                EventType.MESSAGES_WAITING, timeout=timeout
            )
            if notif is None:
                log.warning("timeout waiting for response to %r from %r", command, contact.get("adv_name"))
                return CommandResult(success=False, error="timeout — no response from node")

            # drain the message queue to find our response
            while True:
                msg = await mc.commands.get_msg()
                if msg.type == EventType.NO_MORE_MSGS:
                    return CommandResult(success=False, error="messages drained but no matching response")
                if msg.type == EventType.ERROR:
                    return CommandResult(success=False, error=f"get_msg error: {msg.payload}")
                if msg.type == EventType.CONTACT_MSG_RECV:
                    if msg.payload.get("pubkey_prefix") == pubkey_prefix:
                        text = msg.payload.get("text", "")
                        # CLI responses have a "> " prefix — strip it
                        text = text.removeprefix("> ")
                        log.debug("response from %r: %r", contact.get("adv_name"), text)
                        return CommandResult(success=True, text=text)
                    # message from a different node — discard and keep draining
                    log.debug("discarding message from %r (expected %s)",
                              msg.payload.get("pubkey_prefix"), pubkey_prefix)

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
