"""
Work queue — drives the fleet management cycle.

Scheduling model:
  Each managed node has a next_sync_at timestamp in the DB. The run() loop
  finds the next due node, sleeps until it's time, processes it, then
  reschedules it (now + sync_interval + jitter). This spreads 40 nodes
  evenly across the sync interval rather than hammering all at once.

  On startup, any node with a past or missing next_sync_at is rescheduled
  to a random time within the interval so a daemon restart never triggers
  a sync storm.

  run_node(prefix) bypasses the schedule for operator-triggered syncs.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from mch.command_registry import read_command_for_key, write_command_for_key
from mch.command_safety import fire_and_forget_command
from mch.config import Config, GroupConfig, NodeConfig, OperationConfig
from mch.db import Database
from mch.gateway import CommandResult, Gateway
from mch.settings_compare import compare_setting

if TYPE_CHECKING:
    from mch.messenger import Messenger
from mch.models import (
    CONTACT_TYPE_REPEATER,
    Node,
    NodeFact,
    NodeState,
    OperationRecord,
    OperationStatus,
    SettingPolicy,
    SettingRecord,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# policy resolution
# ---------------------------------------------------------------------------

def resolve_policy(
    key: str,
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
    global_cfg: GroupConfig | None = None,
    global_default: SettingPolicy = SettingPolicy.AUDIT,
) -> SettingPolicy:
    """
    Return the effective policy for a setting key on a node.

    Resolution order (most specific wins):
      1. Per-node setting policy override
      2. Node default_policy
      3. Group setting policy override
      4. Group setting_overrides for this key
      5. Group default_policy
      6. Global setting policy override
      7. Global setting_overrides for this key
      8. Global default_policy
      9. global_default (AUDIT)
    """
    # 1. per-node setting override
    node_setting = node_cfg.settings.get(key)
    if node_setting and node_setting.policy is not None:
        return node_setting.policy

    # 2. node default
    if node_cfg.default_policy is not None:
        return node_cfg.default_policy

    if group_cfg:
        # 3. policy attached to a group desired setting
        group_setting = group_cfg.settings.get(key)
        if group_setting and group_setting.policy is not None:
            return group_setting.policy

        # 4. policy-only group setting_override
        group_override = group_cfg.setting_overrides.get(key)
        if group_override and group_override.policy is not None:
            return group_override.policy

    # 5. group default
    if group_cfg and group_cfg.default_policy is not None:
        return group_cfg.default_policy

    if global_cfg:
        # 6. policy attached to a global desired setting
        global_setting = global_cfg.settings.get(key)
        if global_setting and global_setting.policy is not None:
            return global_setting.policy

        # 7. policy-only global setting_override
        global_override = global_cfg.setting_overrides.get(key)
        if global_override and global_override.policy is not None:
            return global_override.policy

        # 8. global default
        if global_cfg.default_policy is not None:
            return global_cfg.default_policy

    return global_default


def resolve_desired(
    key: str,
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
    global_cfg: GroupConfig | None = None,
) -> str | None:
    """Return the desired value for a setting, node overrides group overrides global."""
    node_setting = node_cfg.settings.get(key)
    if node_setting and node_setting.desired is not None:
        return node_setting.desired
    if group_cfg:
        group_setting = group_cfg.settings.get(key)
        if group_setting and group_setting.desired is not None:
            return group_setting.desired
    if global_cfg:
        global_setting = global_cfg.settings.get(key)
        if global_setting and global_setting.desired is not None:
            return global_setting.desired
    return None


def all_setting_keys(
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
    global_cfg: GroupConfig | None = None,
) -> set[str]:
    """All setting keys relevant to this node."""
    keys: set[str] = set(node_cfg.settings.keys())
    if group_cfg:
        keys |= set(group_cfg.settings.keys())
        keys |= set(group_cfg.setting_overrides.keys())
    if global_cfg:
        keys |= set(global_cfg.settings.keys())
        keys |= set(global_cfg.setting_overrides.keys())
    return keys


# ---------------------------------------------------------------------------
# per-node result
# ---------------------------------------------------------------------------

@dataclass
class NodeResult:
    pubkey: str
    name: str
    reachable: bool = False
    facts_collected: int = 0
    settings_checked: int = 0
    settings_enforced: int = 0
    settings_drifted: int = 0
    operations_run: int = 0
    operations_failed: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# permission error detection
# ---------------------------------------------------------------------------

# MeshCore nodes reply with this text when a command is rejected due to
# insufficient permissions (node ACL not yet authorised for this gateway).
_PERMISSION_ERROR_MARKERS = ("no permission",)


def _is_permission_error(text: str) -> bool:
    """Return True if the node response indicates insufficient permissions."""
    lower = text.strip().lower()
    # strip the "> " CLI prefix if present
    if lower.startswith("> "):
        lower = lower[2:]
    return any(marker in lower for marker in _PERMISSION_ERROR_MARKERS)


class _ReloginFailed(Exception):
    """Raised by _send_authed when re-login after a permission error fails."""


class OperationBusyError(RuntimeError):
    """Raised when a manual gateway-owning operation is already in progress."""


# ---------------------------------------------------------------------------
# work queue
# ---------------------------------------------------------------------------

class WorkQueue:
    def __init__(
        self,
        config: Config,
        gateway: Gateway,
        db: Database,
        messenger: Messenger | None = None,
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._db = db
        self._messenger = messenger
        self._running = False
        self._current_task: asyncio.Task[None] | None = None
        self._manual_cycle_task: asyncio.Task[list[NodeResult]] | None = None
        self._operation_lock = asyncio.Lock()
        self._operation_name: str | None = None
        self._contacts: list[dict[str, Any]] = []  # cached at startup; refreshed by run_cycle
        self._paused: asyncio.Event = asyncio.Event()
        self._paused.set()  # starts unpaused — event set means "OK to proceed"

    async def run(self) -> None:
        """
        Main scheduling loop — processes nodes one at a time on their individual schedules.

        On startup, spreads any overdue nodes across the sync interval so a restart
        never triggers a sync storm. Then repeatedly finds the next due node, sleeps
        until it's time, processes it, and reschedules it.
        """
        self._running = True
        log.info("work queue started (interval=%.1fd)", self._config.scheduler.sync_interval_days)

        self._contacts = await self._gateway.get_contacts(managed_only=False)
        await self._seed_from_config()
        await self._seed_node_labels()
        await self._reschedule_overdue()

        while self._running:
            try:
                await self._run_next_due(self._contacts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("scheduler error: %s", exc)
                await asyncio.sleep(60)

    async def stop(self) -> None:
        self._running = False
        if self._current_task:
            self._current_task.cancel()

    def pause(self) -> None:
        """Signal the queue not to start new node operations (e.g. during commissioning)."""
        self._paused.clear()

    def resume(self) -> None:
        """Allow the queue to resume processing nodes."""
        self._paused.set()

    @asynccontextmanager
    async def commission_session(self) -> AsyncIterator[Gateway]:
        """Pause scheduled work while a commissioning workflow owns the gateway."""
        self.pause()
        try:
            yield self._gateway
        finally:
            self.resume()

    @asynccontextmanager
    async def operation(self, name: str, *, wait: bool = False) -> Any:
        """Own the gateway/radio workflow for one high-level operation."""
        if not wait and self._operation_lock.locked():
            current = self._operation_name or "another operation"
            raise OperationBusyError(f"busy: {current}")
        await self._operation_lock.acquire()
        self._operation_name = name
        try:
            yield
        finally:
            self._operation_name = None
            self._operation_lock.release()

    async def start_cycle(self) -> str:
        """Start a manual full sync cycle in the background."""
        if self._manual_cycle_task and not self._manual_cycle_task.done():
            raise OperationBusyError("busy: sync cycle")
        if self._operation_lock.locked():
            current = self._operation_name or "another operation"
            raise OperationBusyError(f"busy: {current}")
        await self._operation_lock.acquire()
        self._operation_name = "sync cycle"

        task = asyncio.create_task(self._run_cycle_locked())
        self._manual_cycle_task = task
        task.add_done_callback(self._manual_cycle_done)
        return "sync started"

    def _manual_cycle_done(self, task: asyncio.Task[list[NodeResult]]) -> None:
        try:
            results = task.result()
        except asyncio.CancelledError:
            log.info("manual sync cycle cancelled")
        except Exception as exc:
            log.exception("manual sync cycle failed: %s", exc)
        else:
            log.info("manual sync cycle finished: %d nodes", len(results))

    async def refresh_config_state(self) -> None:
        """Refresh gateway contacts and reseed DB metadata after config reload."""
        self.pause()
        try:
            self._contacts = await self._gateway.get_contacts(managed_only=False)
            await self._seed_from_config()
            await self._seed_node_labels()
        finally:
            self.resume()

    async def exec_command(self, node_pubkey: str, command: str) -> str:
        """Run an operator command against a known node through the gateway."""
        async with self.operation(f"exec {node_pubkey[:8]}", wait=False):
            contact = self._contact_for_pubkey(node_pubkey)
            if contact is None:
                raise ValueError(f"no gateway contact for {node_pubkey[:12]}")

            faf = fire_and_forget_command(command) is not None
            result = await self._gateway.send_command(contact, command, timeout=60)
            if faf and not result.success:
                return "(sent - no response expected)"
            if not result.success:
                raise ValueError(result.error or "no response")
            return result.text or "(ok)"

    async def unfiled_contacts(self) -> list[dict[str, Any]]:
        """Return visible repeater contacts that are not yet in config."""
        return await get_unfiled_contacts(self._config, self._gateway)

    def _contact_for_pubkey(self, pubkey: str) -> dict[str, Any] | None:
        return next(
            (c for c in self._contacts if c.get("public_key", "")[:12] == pubkey[:12]),
            None,
        )

    async def _run_next_due(self, contacts: list[dict[str, Any]]) -> None:
        """Find the next due node, sleep until it's time, and process it."""
        node = await self._db.get_due_node()
        if node is None:
            # all nodes scheduled in future — sleep 60s and re-check
            await asyncio.sleep(60)
            return

        now = datetime.now(UTC)
        if node.next_sync_at and node.next_sync_at > now:
            wait = (node.next_sync_at - now).total_seconds()
            log.debug("next sync: %r in %.0fs", node.name, wait)
            # sleep interruptibly so stop() can cancel us
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.sleep(wait)), timeout=wait + 1)
            except (TimeoutError, asyncio.CancelledError):
                if not self._running:
                    return

        # find matching contact for this node
        contact = next(
            (c for c in contacts if c["public_key"] == node.pubkey or
             c["public_key"].startswith(node.pubkey)),
            None,
        )

        # find config entry
        node_cfg = next(
            (nc for nc in self._config.nodes if node.pubkey.startswith(nc.pubkey_prefix)),
            None,
        )
        group_cfg = (
            {g.name: g for g in self._config.groups}.get(node_cfg.group)
            if node_cfg and node_cfg.group else None
        )

        log.debug(
            "gateway radio: %.3f/%.0f/SF%d",
            self._gateway.info.get("radio_freq", 0),
            self._gateway.info.get("radio_bw", 0),
            self._gateway.info.get("radio_sf", 0),
        )

        # block here if commission (or any other operation) has paused the queue
        await self._paused.wait()

        async with self.operation(f"scheduled sync {node.label or node.pubkey[:8]}", wait=True):
            if contact and node_cfg:
                await self._process_node(
                    node_cfg, group_cfg, self._config.global_settings, contact
                )
            else:
                log.warning(
                    "node %r not in current contacts or config — skipping",
                    node.pubkey[:12],
                )

        interval_s = self._config.scheduler.sync_interval_days * 86400
        jitter = random.uniform(-interval_s * 0.1, interval_s * 0.1)
        next_sync = datetime.now(UTC) + timedelta(seconds=interval_s + jitter)
        await self._db.set_next_sync(node.pubkey, next_sync)

    async def _seed_from_config(self) -> None:
        """Seed DB entries for config nodes visible to the gateway.

        Cross-references config.nodes against all gateway contacts. Inserts any
        config-listed node found in contacts that isn't already in the DB.
        Auto-favourites matched nodes so they aren't evicted on a dense mesh.
        Logs a warning for config nodes with no matching gateway contact.
        """
        db_nodes = await self._db.get_all_nodes()
        known_pubkeys = {n.pubkey for n in db_nodes}

        for node_cfg in self._config.nodes:
            contact = next(
                (c for c in self._contacts
                 if c["public_key"].startswith(node_cfg.pubkey_prefix)
                 and c.get("type") == CONTACT_TYPE_REPEATER),
                None,
            )
            if contact is None:
                log.warning(
                    "config node '%s' (prefix: %s) not visible to gateway — cannot manage",
                    node_cfg.label or node_cfg.pubkey_prefix,
                    node_cfg.pubkey_prefix,
                )
                continue

            pubkey = contact["public_key"]

            if pubkey not in known_pubkeys:
                node = Node(
                    pubkey=pubkey,
                    name=contact.get("adv_name", ""),
                    label=node_cfg.label or "",
                    group=node_cfg.group,
                    state=NodeState.UNKNOWN,
                )
                await self._db.upsert_node(node)
                log.info(
                    "seeded node '%s' from config (%s)",
                    node_cfg.label or pubkey[:12],
                    pubkey[:12],
                )

            # auto-favourite: guard against eviction on a dense mesh
            if not contact.get("flags", 0) & 1:
                await self._gateway.set_contact_favourite(contact)
                log.info("favourited '%s' (%s)", node_cfg.label or pubkey[:12], pubkey[:12])

    async def _seed_node_labels(self) -> None:
        """Write labels and groups from config into the DB for any nodes already in the DB.

        Ensures labels and groups are current immediately on restart — not just after the
        next sync cycle for each node. Fixes stale group fields after config edits.
        """
        all_nodes = await self._db.get_all_nodes()
        pubkey_to_node = {n.pubkey: n for n in all_nodes}
        for node_cfg in self._config.nodes:
            node = next(
                (n for pk, n in pubkey_to_node.items() if pk.startswith(node_cfg.pubkey_prefix)),
                None,
            )
            if node is None:
                continue
            changed = False
            if node.label != node_cfg.label:
                node.label = node_cfg.label
                changed = True
            if node.group != node_cfg.group:
                node.group = node_cfg.group
                changed = True
            if changed:
                await self._db.upsert_node(node)

    async def _reschedule_overdue(self) -> None:
        """
        Spread any nodes with past or missing next_sync_at randomly across the sync
        interval. Called on startup so a daemon restart never causes a sync storm.
        Nodes already scheduled in the future are left alone.
        """
        interval_s = self._config.scheduler.sync_interval_days * 86400
        now = datetime.now(UTC)
        nodes = await self._db.get_all_nodes()
        for node in nodes:
            if node.next_sync_at is None or node.next_sync_at < now:
                delay = random.uniform(0, interval_s)
                await self._db.set_next_sync(node.pubkey, now + timedelta(seconds=delay))
                log.debug("rescheduled %r in %.0fs", node.pubkey[:12], delay)

    async def run_node(self, prefix: str) -> NodeResult:
        """
        Force an immediate sync of a single node identified by pubkey prefix.

        prefix must have an even number of hex chars (2, 4, 6, 8 …).
        Raises ValueError if the prefix is odd-length, matches no contacts,
        or matches more than one contact.
        """
        if len(prefix) % 2 != 0:
            raise ValueError(
                f"prefix must have an even number of hex chars (got {len(prefix)!r} - {prefix!r})"
            )

        async with self.operation(f"sync {prefix}", wait=False):
            # use cached contacts — calling get_contacts() concurrently with the receive loop
            # races on meshcore_py's dispatcher and causes timeouts
            contacts = self._contacts
            repeaters = [c for c in contacts if c.get("type") == CONTACT_TYPE_REPEATER]
            matches = [c for c in repeaters if c["public_key"].startswith(prefix)]

            if len(matches) == 0:
                raise ValueError(f"no node found matching prefix {prefix!r}")
            if len(matches) > 1:
                names = ", ".join(c.get("adv_name", c["public_key"][:12]) for c in matches)
                raise ValueError(
                    f"ambiguous prefix {prefix!r} matches {len(matches)} nodes: {names}"
                )

            contact = matches[0]
            pubkey = contact["public_key"]

            node_cfg = next(
                (nc for nc in self._config.nodes if pubkey.startswith(nc.pubkey_prefix)),
                None,
            )
            group_cfg = (
                {g.name: g for g in self._config.groups}.get(node_cfg.group)
                if node_cfg and node_cfg.group else None
            )

            global_cfg = self._config.global_settings
            if node_cfg is None:
                # not in config — create a minimal NodeConfig so we can at least process it
                from mch.config import NodeConfig as _NC
                node_cfg = _NC(pubkey_prefix=pubkey[:8])
                global_cfg = None

            result = await self._process_node(node_cfg, group_cfg, global_cfg, contact)

            interval_s = self._config.scheduler.sync_interval_days * 86400
            jitter = random.uniform(-interval_s * 0.1, interval_s * 0.1)
            next_sync = datetime.now(UTC) + timedelta(seconds=interval_s + jitter)
            await self._db.set_next_sync(pubkey, next_sync)

            return result

    async def run_cycle(self) -> list[NodeResult]:
        """
        One full pass through all managed nodes in config order.
        Used for testing and for mch sync (no-arg full sweep).
        """
        async with self.operation("sync cycle", wait=False):
            return await self._run_cycle_body()

    async def _run_cycle_locked(self) -> list[NodeResult]:
        try:
            return await self._run_cycle_body()
        finally:
            self._operation_name = None
            self._operation_lock.release()

    async def _run_cycle_body(self) -> list[NodeResult]:
        log.info("--- cycle start ---")
        self._contacts = await self._gateway.get_contacts(managed_only=False)
        contact_by_pubkey = {c["public_key"]: c for c in self._contacts}

        # register all visible contacts with the messenger so it can reply to DMs
        if self._messenger:
            for c in self._contacts:
                self._messenger.register_contact(c["public_key"][:12], c)

        group_configs = {g.name: g for g in self._config.groups}
        results: list[NodeResult] = []

        for node_cfg in self._config.nodes:
            contact = self._find_contact(node_cfg.pubkey_prefix, contact_by_pubkey)
            if contact is None:
                log.warning("node %r (%s) not in gateway contacts — skipping",
                            node_cfg.label, node_cfg.pubkey_prefix)
                results.append(NodeResult(
                    pubkey=node_cfg.pubkey_prefix,
                    name=node_cfg.label,
                    reachable=False,
                    error="not in gateway contacts",
                ))
                continue

            if contact.get("type") != CONTACT_TYPE_REPEATER:
                log.warning("node %r (%s) is not a repeater (type=%s) — skipping",
                            node_cfg.label, node_cfg.pubkey_prefix, contact.get("type"))
                results.append(NodeResult(
                    pubkey=node_cfg.pubkey_prefix,
                    name=node_cfg.label,
                    reachable=False,
                    error="not a repeater",
                ))
                continue

            group_cfg = group_configs.get(node_cfg.group) if node_cfg.group else None
            result = await self._process_node(
                node_cfg, group_cfg, self._config.global_settings, contact
            )
            results.append(result)

            if len(self._config.nodes) > 1:
                delay = self._config.scheduler.inter_node_delay_seconds
                log.debug("rate-limit delay: %ds", delay)
                await asyncio.sleep(delay)

        log.info("--- cycle done: %d nodes processed ---", len(results))

        if self._messenger:
            drift_count = sum(r.settings_drifted for r in results)
            await self._messenger.announce_cycle_complete(len(results), drift_count)

        return results

    def _find_contact(
        self,
        pubkey_prefix: str,
        by_pubkey: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        if pubkey_prefix in by_pubkey:
            return by_pubkey[pubkey_prefix]
        for pubkey, contact in by_pubkey.items():
            if pubkey.startswith(pubkey_prefix):
                return contact
        return None

    async def _process_node(
        self,
        node_cfg: NodeConfig,
        group_cfg: GroupConfig | None,
        global_cfg: GroupConfig | None,
        contact: dict[str, Any],
    ) -> NodeResult:
        pubkey = contact["public_key"]
        name = contact.get("adv_name", node_cfg.label)
        result = NodeResult(pubkey=pubkey, name=name)

        log.info("processing node %r (%s…)", name, pubkey[:12])

        # ensure node exists in db
        node = await self._db.get_node(pubkey) or Node(pubkey=pubkey, name=name)
        node.name = name
        node.label = node_cfg.label
        node.state = NodeState.CONFIGURING
        await self._db.upsert_node(node)

        result.reachable = True
        now = datetime.now(UTC)
        node.last_seen = now
        node.last_connected = now

        # ensure this contact is favourited on the gateway so it's protected from
        # auto-eviction when the 350-contact store fills up
        if not contact.get("flags", 0) & 1:
            await self._gateway.set_contact_favourite(contact)

        # hop the gateway radio if this node lives on a different frequency
        node_radio = node_cfg.via
        gw_freq = self._gateway.info.get("radio_freq")
        saved_radio: tuple[float, float, int, int] | None = None
        if node_radio is not None and node_radio.freq != gw_freq:
            log.info(
                "  hopping to %.3f/%d/SF%d for %r",
                node_radio.freq, node_radio.bw, node_radio.sf, name,
            )
            saved_radio = await self._gateway.hop_radio(
                node_radio.freq, node_radio.bw, node_radio.sf, node_radio.cr
            )

        try:
            # collect facts
            result.facts_collected = await self._collect_facts(pubkey, contact, node, node_cfg)

            # enforce / audit settings
            s_checked, s_enforced, s_drifted = await self._process_settings(
                pubkey, contact, node_cfg, group_cfg, global_cfg, node
            )
            result.settings_checked = s_checked
            result.settings_enforced = s_enforced
            result.settings_drifted = s_drifted

            # seed operations from config into DB, then run what's pending
            await self._seed_operations(pubkey, node_cfg)
            op_run, op_failed = await self._run_operations(pubkey, contact, node_cfg, node)
            result.operations_run = op_run
            result.operations_failed = op_failed

        except _ReloginFailed as exc:
            log.warning("re-login failed for %r — marking offline: %s", name, exc)
            node.state = NodeState.OFFLINE
            node.auth_cached = False
            await self._db.upsert_node(node)
            await self._db.write_log(pubkey, "ERROR", f"re-login failed: {exc}")
            result.error = str(exc)
            return result  # finally still runs to restore radio

        finally:
            if saved_radio is not None:
                await self._gateway.restore_radio(*saved_radio)

        # mark node state
        if s_drifted > 0 or op_failed > 0:
            node.state = NodeState.PARTIAL
        else:
            node.state = NodeState.CONFIGURED
            passwd_configured = (
                "default" in self._config.secrets
                or any(node.pubkey.startswith(p) for p in self._config.secrets)
            )
            if not node.auth_required and not node.auth_cached and not passwd_configured:
                log.warning(
                    "node %s (%s) has no ACL protection — anyone on the mesh can send commands",
                    node.label or node.name, node.pubkey[:8],
                )
                await self._db.write_log(
                    node.pubkey, "WARNING",
                    "no ACL protection — anyone on the mesh can send commands",
                )
        await self._db.upsert_node(node)

        log.info(
            "  %r done — facts:%d settings:%d/%d enforced drift:%d ops:%d/%d failed",
            name, result.facts_collected,
            s_enforced, s_checked, s_drifted,
            op_run, op_failed,
        )
        return result

    async def _send_authed(
        self,
        contact: dict[str, Any],
        command: str,
        node: Node,
        node_cfg: NodeConfig,
    ) -> CommandResult:
        """
        Send a command; detect permission errors and re-authenticate + retry once.

        If the node replies with a permission-denied response (detected by
        _is_permission_error), this method:
          1. Clears auth_cached in the DB.
          2. Attempts login with the configured password.
          3. If login succeeds, sets auth_cached=True and retries the command.
          4. If login fails or no password is configured, returns a failure result.
        """
        result = await self._gateway.send_command(contact, command)

        if result.success and _is_permission_error(result.text or ""):
            log.warning(
                "permission error on %r for %r — clearing auth cache and re-logging in",
                command, node.name,
            )
            node.auth_cached = False
            await self._db.upsert_node(node)

            ns = next(
                (ns for prefix, ns in self._config.secrets.items()
                 if prefix != "default" and node.pubkey.startswith(prefix)),
                None,
            ) or self._config.secrets.get("default")
            password = ns.password if ns else None
            if not password:
                raise _ReloginFailed("permission denied — no password configured")

            ok = await self._gateway.login(contact, password)
            if not ok:
                raise _ReloginFailed("permission denied — re-login failed")

            node.auth_cached = True
            await self._db.upsert_node(node)
            # retry the command now that we're authenticated
            result = await self._gateway.send_command(contact, command)

        return result

    async def _collect_facts(
        self, pubkey: str, contact: dict[str, Any], node: Node, node_cfg: NodeConfig
    ) -> int:
        """Run all declared fact commands and store results."""
        collected = 0
        for key in self._config.facts:
            # neighbors, ver/board, and clock are bare commands; everything else is `get <key>`
            cmd = read_command_for_key(key)
            res = await self._send_authed(contact, cmd, node, node_cfg)
            if res.success and res.text:
                if key == "neighbors":
                    # store the count, not the raw multi-line list
                    from mch.recommend import parse_neighbour_count
                    value = str(parse_neighbour_count(res.text))
                else:
                    value = res.text.strip()
                await self._db.upsert_fact(NodeFact(
                    node_pubkey=pubkey,
                    key=key,
                    value=value,
                ))
                collected += 1
            else:
                log.debug("fact %r failed for %s: %s", key, pubkey[:12], res.error)
        return collected

    async def _process_settings(
        self,
        pubkey: str,
        contact: dict[str, Any],
        node_cfg: NodeConfig,
        group_cfg: GroupConfig | None,
        global_cfg: GroupConfig | None,
        node: Node,
    ) -> tuple[int, int, int]:
        """Check and optionally enforce all settings. Returns (checked, enforced, drifted)."""
        checked = enforced = drifted = 0
        keys = all_setting_keys(node_cfg, group_cfg, global_cfg)

        for key in sorted(keys):
            policy = resolve_policy(key, node_cfg, group_cfg, global_cfg)
            desired = resolve_desired(key, node_cfg, group_cfg, global_cfg)

            if policy == SettingPolicy.SKIP:
                continue

            read_cmd = read_command_for_key(key)
            res = await self._send_authed(contact, read_cmd, node, node_cfg)
            if not res.success:
                log.warning("get %s failed for %s: %s", key, pubkey[:12], res.error)
                continue

            checked += 1
            observed = (res.text or "").strip()
            if key == "name" and observed:
                node.name = observed
            comparison = compare_setting(key, desired, observed)
            is_drift = desired is not None and not comparison.equal

            setting = SettingRecord(
                node_pubkey=pubkey,
                key=key,
                desired=desired,
                policy=policy,
                observed=observed,
                last_checked=datetime.now(UTC),
                drift=is_drift,
            )

            if is_drift:
                drifted += 1
                if policy == SettingPolicy.ENFORCE:
                    log.info("  enforcing %s: observed=%r desired=%r", key, observed, desired)
                    write_cmd = write_command_for_key(key, desired)
                    set_res = await self._send_authed(
                        contact, write_cmd, node, node_cfg
                    )
                    if set_res.success:
                        enforced += 1
                        drifted -= 1  # enforcement fixed it — don't count as residual drift
                        setting.drift = False
                        setting.observed = desired  # update observed to reflect the new value
                        if key == "name" and desired:
                            node.name = desired
                        setting.last_set = datetime.now(UTC)
                        await self._db.write_log(
                            pubkey, "INFO", f"set {key}={desired!r} (was {observed!r})"
                        )
                    else:
                        log.warning("  set %s failed: %s", key, set_res.error)
                        await self._db.write_log(
                            pubkey, "ERROR", f"set {key} failed: {set_res.error}"
                        )
                else:
                    log.info(
                        "  drift: %s observed=%r desired=%r (policy=%s — not enforcing)",
                        key, observed, desired, policy.value,
                    )
                    await self._db.write_log(
                        pubkey, "WARNING",
                        f"drift: {key} observed={observed!r} desired={desired!r} ({policy.value})"
                    )

            await self._db.upsert_setting(setting)

        return checked, enforced, drifted

    async def _seed_operations(self, pubkey: str, node_cfg: NodeConfig) -> None:
        """Queue any config operations that are due (never run, or past every_hours)."""
        for op_cfg in node_cfg.operations:
            last = await self._db.get_last_operation(pubkey, op_cfg.command)
            if last is None:
                # never seen before — queue it
                pass
            elif last.status == OperationStatus.SUCCESS and last.run_at is not None:
                due_after = last.run_at + timedelta(hours=op_cfg.every_hours)
                if datetime.now(UTC) < due_after:
                    continue  # ran recently, not due yet
                # past the window — fall through to queue a new run
            else:
                continue  # pending or failed — already in queue, don't add again

            await self._db.upsert_operation(OperationRecord(
                node_pubkey=pubkey,
                command=op_cfg.command,
                every_hours=op_cfg.every_hours,
                status=OperationStatus.PENDING,
                queued_at=datetime.now(UTC),
            ))
            log.debug("queued operation %r for %s", op_cfg.command, pubkey[:12])

    async def _run_operations(
        self,
        pubkey: str,
        contact: dict[str, Any],
        node_cfg: NodeConfig,
        node: Node,
    ) -> tuple[int, int]:
        """Run pending one-shot operations. Returns (run, failed)."""
        run = failed = 0
        pending = await self._db.get_pending_operations(pubkey)

        op_cfg_by_cmd = {o.command: o for o in node_cfg.operations}

        for op in pending:
            log.info("  operation: %r", op.command)
            faf = op_cfg_by_cmd.get(op.command, OperationConfig()).fire_and_forget
            res = await self._send_authed(contact, op.command, node, node_cfg)
            op.run_at = datetime.now(UTC)
            op.response = res.text or res.error

            if res.success or faf:
                op.status = OperationStatus.SUCCESS
                run += 1
                if faf and not res.success:
                    log.info("  operation sent (fire-and-forget): %r", op.command)
                    await self._db.write_log(
                        pubkey, "INFO", f"operation sent (fire-and-forget): {op.command!r}"
                    )
                else:
                    await self._db.write_log(pubkey, "INFO", f"operation ok: {op.command!r}")
            else:
                op.status = OperationStatus.FAILED
                failed += 1
                await self._db.write_log(
                    pubkey, "ERROR", f"operation failed: {op.command!r}: {res.error}"
                )

            await self._db.update_operation(op)

        return run, failed


# ---------------------------------------------------------------------------
# unfiled contacts — for mch contacts
# ---------------------------------------------------------------------------

async def get_unfiled_contacts(config: Config, gateway: Gateway) -> list[dict[str, Any]]:
    """
    Return gateway contacts that are REP nodes but not yet in config.

    Used by 'mch contacts' to show operators what they can add to config.yaml.
    """
    contacts = await gateway.get_contacts(managed_only=False)
    managed_prefixes = [nc.pubkey_prefix for nc in config.nodes]

    unfiled = []
    for c in contacts:
        if c.get("type") != CONTACT_TYPE_REPEATER:
            continue
        pubkey = c["public_key"]
        already_managed = any(pubkey.startswith(p) for p in managed_prefixes)
        if not already_managed:
            unfiled.append(c)
    return unfiled
