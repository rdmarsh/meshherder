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
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from mch.command_registry import command_timeout, read_command_for_key, write_command_for_key
from mch.command_safety import fire_and_forget_command
from mch.config import Config, GroupConfig, NodeConfig, OperationConfig, resolve_via_radio
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
ProgressCallback = Callable[[str], Awaitable[None]] | None
_PATH_HEX_RE = re.compile(r"^[0-9a-fA-F,]+(?::[0-3])?$")
_MIN_RECOVERY_SF = 5
_MAX_RECOVERY_SF = 12


async def _progress(callback: ProgressCallback, message: str) -> None:
    if callback is not None:
        await callback(message)


def _format_radio(freq: Any, bw: Any, sf: Any, cr: Any) -> str:
    return f"{float(freq):.3f}/{float(bw):g}/SF{int(sf)}/CR{int(cr)}"


def _format_radio_tuple(radio: tuple[float, float, int, int]) -> str:
    return _format_radio(*radio)


def _resolve_operations(
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
    global_cfg: GroupConfig | None,
) -> list[OperationConfig]:
    """Return operations with Node > Group > Global precedence by command."""
    by_command: dict[str, OperationConfig] = {}
    for source in (global_cfg, group_cfg, node_cfg):
        if source is None:
            continue
        for op in source.operations:
            by_command[op.command] = op
    return list(by_command.values())


def _operation_succeeded(res: CommandResult) -> bool:
    text = (res.text or "").strip()
    return res.success and not text.startswith("ERR:")


def _describe_cached_management_path(contact: dict[str, Any]) -> str:
    path_len = contact.get("out_path_len")
    if path_len == -1:
        return "flood"
    if path_len == 0:
        return "direct"
    if isinstance(path_len, int) and path_len > 0:
        details = [f"out_path_len={path_len}"]
        out_path = contact.get("out_path")
        if out_path:
            details.insert(0, f"out_path={out_path}")
        hash_mode = contact.get("out_path_hash_mode")
        if hash_mode is not None:
            details.append(f"hash-mode={hash_mode}")
        return f"stored ({'; '.join(details)})"
    return "unknown (no route metadata in cached contact)"


def _uses_stored_path(contact: dict[str, Any]) -> bool:
    path_len = contact.get("out_path_len")
    return isinstance(path_len, int) and path_len > 0


def _uses_flood_route(contact: dict[str, Any]) -> bool:
    return contact.get("out_path_len") == -1


def _flood_route_contact(contact: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a contact that sends via MeshCore flood routing."""
    routed = dict(contact)
    routed["out_path"] = ""
    routed["out_path_len"] = -1
    routed["out_path_hash_mode"] = 0
    return routed


def _clean_path_hex(path: str) -> str:
    return path.replace(",", "").lower()


def _format_path_hex(path: str, path_hash_mode: int) -> str:
    cleaned = _clean_path_hex(path)
    group = (path_hash_mode + 1) * 2
    if not cleaned or group <= 0:
        return cleaned
    return ",".join(cleaned[i:i + group] for i in range(0, len(cleaned), group))


def _parse_operator_path(raw: str, default_hash_mode: int | None = None) -> tuple[str, int]:
    value = raw.strip()
    if not value or not _PATH_HEX_RE.match(value):
        raise ValueError("path must be hex hops, optionally comma-separated, with optional :mode")

    path_part = value
    hash_mode = default_hash_mode
    if ":" in value:
        path_part, mode_part = value.rsplit(":", 1)
        hash_mode = int(mode_part)
    if hash_mode is None:
        hash_mode = 0
    if not 0 <= hash_mode <= 3:
        raise ValueError("path hash mode must be 0, 1, 2, or 3")

    path = _clean_path_hex(path_part)
    hop_hex_len = (hash_mode + 1) * 2
    if len(path) == 0:
        raise ValueError("path must not be empty")
    if len(path) % hop_hex_len != 0:
        raise ValueError(
            f"path length must be a multiple of {hop_hex_len} hex characters for mode {hash_mode}"
        )
    return path, hash_mode


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


def _radio_from_setting(raw: str | None, source: str) -> _RadioCandidate | None:
    if raw is None:
        return None
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 4:
        return None
    try:
        return _RadioCandidate(
            freq=round(float(parts[0]), 3),
            bw=float(parts[1]),
            sf=int(parts[2]),
            cr=int(parts[3]),
            source=source,
        )
    except ValueError:
        return None


def _radio_from_config(
    radio: Any,
    source: str,
) -> _RadioCandidate | None:
    if radio is None:
        return None
    return _RadioCandidate(
        freq=round(float(radio.freq), 3),
        bw=float(radio.bw),
        sf=int(radio.sf),
        cr=int(radio.cr),
        source=source,
    )


def _candidate_with_sf(candidate: _RadioCandidate, sf: int, source: str) -> _RadioCandidate:
    return _RadioCandidate(
        freq=candidate.freq,
        bw=candidate.bw,
        sf=sf,
        cr=candidate.cr,
        source=source,
    )


def _append_radio_candidate(
    candidates: list[_RadioCandidate],
    seen_channels: set[tuple[float, float, int]],
    candidate: _RadioCandidate | None,
) -> None:
    if candidate is None or not _MIN_RECOVERY_SF <= candidate.sf <= _MAX_RECOVERY_SF:
        return
    if candidate.channel in seen_channels:
        return
    seen_channels.add(candidate.channel)
    candidates.append(candidate)


def radio_recovery_candidates(
    config: Config,
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
    global_cfg: GroupConfig | None,
    tried_channels: set[tuple[float, float, int]],
) -> list[_RadioCandidate]:
    """Return a bounded list of config-derived radios to try after reachability fails."""
    candidates: list[_RadioCandidate] = []
    seen_channels = set(tried_channels)

    effective: list[_RadioCandidate] = []
    effective_via = _radio_from_config(
        resolve_via_radio(node_cfg, group_cfg, global_cfg),
        "effective via_radio",
    )
    if effective_via is not None:
        effective.append(effective_via)
    effective_desired = _radio_from_setting(
        resolve_desired("radio", node_cfg, group_cfg, global_cfg),
        "effective desired radio",
    )
    if effective_desired is not None:
        effective.append(effective_desired)
    if not effective:
        return []

    for base in effective:
        _append_radio_candidate(candidates, seen_channels, base)
        for delta in (-1, 1):
            sf = base.sf + delta
            _append_radio_candidate(
                candidates,
                seen_channels,
                _candidate_with_sf(base, sf, f"{base.source} SF{sf} fallback"),
            )

    configured: list[_RadioCandidate | None] = [
        _radio_from_config(config.gateway.radio, "gateway.radio"),
        _radio_from_config(global_cfg.via_radio if global_cfg else None, "global via_radio"),
        _radio_from_setting(
            global_cfg.settings.get("radio").desired
            if global_cfg and global_cfg.settings.get("radio") else None,
            "global desired radio",
        ),
    ]
    for group in config.groups:
        configured.append(_radio_from_config(group.via_radio, f"group {group.name} via_radio"))
        configured.append(_radio_from_setting(
            group.settings.get("radio").desired if group.settings.get("radio") else None,
            f"group {group.name} desired radio",
        ))
    for node in config.nodes:
        label = node.label or node.pubkey_prefix
        configured.append(_radio_from_config(node.via_radio, f"node {label} via_radio"))
        configured.append(_radio_from_setting(
            node.settings.get("radio").desired if node.settings.get("radio") else None,
            f"node {label} desired radio",
        ))

    for candidate in configured:
        _append_radio_candidate(candidates, seen_channels, candidate)
        if len(candidates) >= _RADIO_FALLBACK_MAX_CANDIDATES:
            break

    return candidates[:_RADIO_FALLBACK_MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# per-node result
# ---------------------------------------------------------------------------

@dataclass
class NodeResult:
    pubkey: str
    name: str
    reachable: bool = False
    skipped_reason: str | None = None
    facts_collected: int = 0
    settings_checked: int = 0
    settings_enforced: int = 0
    settings_drifted: int = 0
    settings_failed: int = 0
    enforced_settings_drifted: int = 0
    enforced_settings_failed: int = 0
    operations_run: int = 0
    operations_failed: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# permission error detection
# ---------------------------------------------------------------------------

# MeshCore nodes reply with this text when a command is rejected due to
# insufficient permissions (node ACL not yet authorised for this gateway).
_PERMISSION_ERROR_MARKERS = ("no permission",)

# Reachability probe: a cheap read sent before a full fact/setting pass so a
# node that simply is not answering this cycle does not cost a timeout on every
# fact and every setting. A single miss is inconclusive on a marginal mesh link
# (observed on luna: a node timed out on `ver` in one sync and replied in the
# next), so the probe retries before giving up; two consecutive misses skip the
# rest of this node's pass. `ver` is a cheap bare read already collected as a
# fact, so its reply is reused rather than read twice.
_PROBE_COMMAND = "ver"
_PROBE_ATTEMPTS = 2
_STORED_PATH_READ_RETRY_BUDGET = 3
_RADIO_FALLBACK_MAX_CANDIDATES = 3


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


@dataclass(frozen=True)
class _RadioCandidate:
    freq: float
    bw: float
    sf: int
    cr: int
    source: str

    @property
    def channel(self) -> tuple[float, float, int]:
        return (round(self.freq, 3), self.bw, self.sf)


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
        self._operation_tasks: set[asyncio.Task[Any]] = set()
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
        current_task = asyncio.current_task()
        if current_task is not None:
            self._current_task = current_task
        self._running = True
        log.info("work queue started (interval=%.1fd)", self._config.scheduler.sync_interval_days)

        self._contacts = await self._gateway.get_contacts(managed_only=False)
        await self._seed_from_config()
        await self._seed_node_labels()
        await self._recover_interrupted_nodes()
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
        current = asyncio.current_task()
        tasks = {
            task
            for task in (
                self._current_task,
                self._manual_cycle_task,
                *self._operation_tasks,
            )
            if task is not None and task is not current and not task.done()
        }
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
        owner = asyncio.current_task()
        if owner is not None:
            self._operation_tasks.add(owner)
        try:
            yield
        finally:
            if owner is not None:
                self._operation_tasks.discard(owner)
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
        try:
            await self._db.write_log(None, "INFO", "manual sync started: full cycle")
        except Exception:
            self._operation_name = None
            self._operation_lock.release()
            raise

        task = asyncio.create_task(self._run_cycle_locked())
        self._manual_cycle_task = task
        self._operation_tasks.add(task)
        task.add_done_callback(self._manual_cycle_done)
        return "sync started"

    def _manual_cycle_done(self, task: asyncio.Task[list[NodeResult]]) -> None:
        self._operation_tasks.discard(task)
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

    async def discover_nodes(
        self,
        radio: tuple[float, float, int, int],
        *,
        dwell: float,
        progress: ProgressCallback = None,
    ) -> dict[str, Any]:
        """Run a bounded active MeshCore discovery on a temporary gateway radio."""
        async with self.operation("discover", wait=False):
            self.pause()
            try:
                radio_text = _format_radio_tuple(radio)
                await _progress(progress, f"discovering on {radio_text} for {dwell:g}s")
                nodes = await self._gateway.discover(factory_freq=radio, dwell=dwell)
                await _progress(progress, f"discovered {len(nodes)} node(s)")
                return {
                    "radio": {
                        "freq": radio[0],
                        "bw": radio[1],
                        "sf": radio[2],
                        "cr": radio[3],
                        "text": radio_text,
                    },
                    "dwell": dwell,
                    "nodes": nodes,
                }
            finally:
                self.resume()

    def management_path_status(self, node: Node) -> dict[str, Any]:
        """Return cached gateway route metadata for a managed node without mesh traffic."""
        contact = self._contact_for_pubkey(node.pubkey)
        if contact is None:
            return {
                "pubkey": node.pubkey,
                "label": node.label,
                "name": node.name,
                "contact_present": False,
                "cached_path": "not in gateway contacts",
                "route": "missing",
                "path_repair_needed": True,
            }

        path_len = contact.get("out_path_len")
        if path_len == -1:
            route = "flood"
        elif path_len == 0:
            route = "direct"
        elif isinstance(path_len, int) and path_len > 0:
            route = "stored"
        else:
            route = "unknown"

        return {
            "pubkey": node.pubkey,
            "label": node.label,
            "name": node.name,
            "contact_present": True,
            "cached_path": _describe_cached_management_path(contact),
            "route": route,
            "path_repair_needed": route in {"flood", "missing"},
            "contact_name": contact.get("adv_name") or "",
            "contact_type": contact.get("type"),
            "contact_flags": contact.get("flags"),
            "out_path": contact.get("out_path") or "",
            "out_path_len": path_len,
            "out_path_hash_mode": contact.get("out_path_hash_mode"),
        }

    async def discover_management_path(
        self,
        node: Node,
        *,
        source: str = "discovery",
    ) -> dict[str, Any]:
        """Learn a candidate management path without applying it to the gateway contact."""
        async with self.operation(f"path {source} {node.pubkey[:8]}", wait=False):
            contact = self._contact_for_pubkey(node.pubkey)
            if contact is None:
                raise ValueError(f"no gateway contact for {node.pubkey[:12]}")

            if source == "discovery":
                candidate = await self._gateway.discover_contact_path(contact)
                path = candidate.get("path") or ""
                path_len = candidate.get("path_len")
                path_hash_mode = int(candidate.get("path_hash_mode", 0))
            elif source == "advert":
                candidate = await self._gateway.get_contact_advert_path(contact)
                path = candidate.get("path") or ""
                path_len = candidate.get("path_len")
                path_hash_mode = int(candidate.get("path_hash_mode", 0))
            else:
                raise ValueError(f"unknown path source {source!r}")

            formatted = _format_path_hex(path, path_hash_mode)
            await self._db.write_log(
                node.pubkey,
                "INFO",
                f"management path {source} candidate: {formatted or '-'}:{path_hash_mode}",
            )
            return {
                "pubkey": node.pubkey,
                "label": node.label,
                "name": node.name,
                "source": source,
                "path": formatted,
                "path_raw": _clean_path_hex(path),
                "path_len": path_len,
                "path_hash_mode": path_hash_mode,
                "apply_value": f"{formatted}:{path_hash_mode}" if formatted else "",
            }

    async def apply_management_path(self, node: Node, raw_path: str) -> dict[str, Any]:
        """Apply an operator-approved replacement path to the gateway contact."""
        async with self.operation(f"path apply {node.pubkey[:8]}", wait=False):
            contact = self._contact_for_pubkey(node.pubkey)
            if contact is None:
                raise ValueError(f"no gateway contact for {node.pubkey[:12]}")

            default_hash_mode = contact.get("out_path_hash_mode")
            if not isinstance(default_hash_mode, int):
                default_hash_mode = 0
            path, path_hash_mode = _parse_operator_path(raw_path, default_hash_mode)
            previous = _describe_cached_management_path(contact)

            await self._gateway.change_contact_path(contact, path, path_hash_mode)
            refreshed = self.management_path_status(node)
            applied = _format_path_hex(path, path_hash_mode)
            await self._db.write_log(
                node.pubkey,
                "WARNING",
                f"management path changed from {previous} to {applied}:{path_hash_mode}",
            )
            return refreshed

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
            "gateway radio: %.3f/%.0f/SF%d/CR%d",
            self._gateway.info.get("radio_freq", 0),
            self._gateway.info.get("radio_bw", 0),
            self._gateway.info.get("radio_sf", 0),
            self._gateway.info.get("radio_cr", 0),
        )

        # block here if commission (or any other operation) has paused the queue
        await self._paused.wait()

        operation_label = (
            (node_cfg.label if node_cfg else "")
            or node.label
            or node.name
            or node.pubkey[:12]
        )
        async with self.operation(f"scheduled sync {operation_label}", wait=True):
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
                message = "config node not visible to gateway - cannot manage"
                log.warning(
                    "config node %r (prefix: %s) not visible to gateway - cannot manage",
                    node_cfg.label or node_cfg.pubkey_prefix,
                    node_cfg.pubkey_prefix,
                )
                await self._db.write_log(
                    None,
                    "WARNING",
                    message,
                    display_pubkey=node_cfg.pubkey_prefix,
                    display_label=node_cfg.label or node_cfg.pubkey_prefix,
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

    async def _recover_interrupted_nodes(self) -> None:
        """Mark nodes left mid-sync by an earlier daemon stop as needing attention."""
        for node in await self._db.get_all_nodes():
            if node.state != NodeState.CONFIGURING:
                continue
            node.state = NodeState.PARTIAL
            await self._db.upsert_node(node)
            await self._db.write_log(
                node.pubkey,
                "ERROR",
                "sync interrupted before completion; marked partial on daemon startup",
            )
            log.warning("recovered interrupted sync for %r (%s)", node.label, node.pubkey[:12])

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

    async def run_node(self, prefix: str, *, progress: ProgressCallback = None) -> NodeResult:
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

            global_cfg: GroupConfig | None = self._config.global_settings
            if node_cfg is None:
                # not in config — create a minimal NodeConfig so we can at least process it
                from mch.config import NodeConfig as _NC
                node_cfg = _NC(pubkey_prefix=pubkey[:8])
                global_cfg = None

            await self._db.write_log(pubkey, "INFO", "manual sync started")
            try:
                result = await self._process_node(
                    node_cfg, group_cfg, global_cfg, contact, progress=progress
                )
            except asyncio.CancelledError:
                await self._db.write_log(pubkey, "WARNING", "manual sync aborted")
                raise
            except Exception as exc:
                await self._db.write_log(pubkey, "ERROR", f"manual sync failed: {exc}")
                raise

            interval_s = self._config.scheduler.sync_interval_days * 86400
            jitter = random.uniform(-interval_s * 0.1, interval_s * 0.1)
            next_sync = datetime.now(UTC) + timedelta(seconds=interval_s + jitter)
            await self._db.set_next_sync(pubkey, next_sync)

            if result.error:
                await self._db.write_log(pubkey, "ERROR", f"manual sync failed: {result.error}")
            elif (
                result.enforced_settings_drifted
                or result.enforced_settings_failed
                or result.operations_failed
            ):
                await self._db.write_log(pubkey, "INFO", "manual sync completed: partial result")
            else:
                await self._db.write_log(pubkey, "INFO", "manual sync completed")
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
            results = await self._run_cycle_body()
        except asyncio.CancelledError:
            await self._db.write_log(None, "WARNING", "manual sync aborted: full cycle")
            raise
        except Exception as exc:
            await self._db.write_log(None, "ERROR", f"manual sync failed: full cycle: {exc}")
            raise
        else:
            await self._db.write_log(
                None, "INFO", f"manual sync completed: full cycle ({len(results)} nodes processed)"
            )
            return results
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
        *,
        progress: ProgressCallback = None,
    ) -> NodeResult:
        pubkey = contact["public_key"]
        name = contact.get("adv_name", node_cfg.label)
        result = NodeResult(pubkey=pubkey, name=name)

        log.info("processing node %r (%s…)", name, pubkey[:12])
        await _progress(progress, f"syncing {name!r} ({pubkey[:12]})")

        # ensure node exists in db
        node = await self._db.get_node(pubkey) or Node(pubkey=pubkey, name=name)
        node.name = name
        node.label = node_cfg.label
        node.state = NodeState.CONFIGURING
        node.last_seen = datetime.now(UTC)
        await self._db.upsert_node(node)

        # result.reachable and last_connected are set only after the reachability
        # probe below confirms the node is actually answering this cycle.
        saved_radio: tuple[float, float, int, int] | None = None

        try:
            node_radio = resolve_via_radio(node_cfg, group_cfg, global_cfg)
            # CR controls the sender's redundancy and need not match for reception.
            gateway_channel = (
                round(float(self._gateway.info.get("radio_freq") or 0), 3),
                float(self._gateway.info.get("radio_bw") or 0),
                int(self._gateway.info.get("radio_sf") or 0),
            )
            required_channel = (
                (node_radio.freq, node_radio.bw, node_radio.sf)
                if node_radio is not None else None
            )
            tried_radio_channels = {gateway_channel}
            if required_channel is not None:
                tried_radio_channels.add(required_channel)
            switch_required = node_radio is not None and required_channel != gateway_channel
            if switch_required:
                assert node_radio is not None
                management_radio = _format_radio(
                    node_radio.freq, node_radio.bw, node_radio.sf, node_radio.cr
                )
                radio_source = "via_radio temporary switch"
            else:
                management_radio = _format_radio(
                    self._gateway.info.get("radio_freq") or 0,
                    self._gateway.info.get("radio_bw") or 0,
                    self._gateway.info.get("radio_sf") or 0,
                    self._gateway.info.get("radio_cr") or 0,
                )
                radio_source = "gateway current radio; no switch"
            management_path = _describe_cached_management_path(contact)
            log.info("  management radio: %s (%s)", management_radio, radio_source)
            log.info("  cached management path: %s", management_path)
            await _progress(progress, f"management radio: {management_radio} ({radio_source})")
            await _progress(progress, f"cached management path: {management_path}")

            # Ensure gateway metadata/radio setup failures also terminate this sync state.
            if not contact.get("flags", 0) & 1:
                await self._gateway.set_contact_favourite(contact)

            if switch_required:
                assert node_radio is not None
                log.info(
                    "  switching gateway radio to %.3f/%g/SF%d/CR%d for %r",
                    node_radio.freq, node_radio.bw, node_radio.sf, node_radio.cr, name,
                )
                await _progress(
                    progress,
                    (
                        f"switching gateway radio to "
                        f"{node_radio.freq:.3f}/{node_radio.bw:g}/"
                        f"SF{node_radio.sf}/CR{node_radio.cr}"
                    ),
                )
                await self._db.write_log(
                    pubkey,
                    "INFO",
                    (
                        "switching gateway radio to "
                        f"{node_radio.freq:.3f}/{node_radio.bw:g}/"
                        f"SF{node_radio.sf}/CR{node_radio.cr}"
                    ),
                )
                saved_radio = await self._gateway.switch_radio_temporarily(
                    node_radio.freq, node_radio.bw, node_radio.sf, node_radio.cr
                )

            if _uses_flood_route(contact):
                probe_result = await self._probe_reachable(
                    contact, node, node_cfg, progress=progress
                )
                if probe_result is None:
                    recovered = await self._try_radio_recovery(
                        node_cfg,
                        group_cfg,
                        global_cfg,
                        contact,
                        node,
                        tried_radio_channels,
                        current_saved_radio=saved_radio,
                        progress=progress,
                    )
                    if recovered is not None:
                        probe_result, saved_radio = recovered
                        result.reachable = True
                        result.skipped_reason = "path repair needed before full sync"
                        node.state = NodeState.PARTIAL
                        node.last_connected = datetime.now(UTC)
                        await self._db.upsert_node(node)
                        await self._db.write_log(
                            pubkey,
                            "WARNING",
                            (
                                "radio fallback reached flood-routed contact; "
                                "path repair needed before full sync"
                            ),
                        )
                        await _progress(
                            progress,
                            "radio fallback reached flood-routed contact; "
                            "path repair needed before full sync",
                        )
                        return result  # finally still runs to restore the gateway radio

                    saved_radio = None
                    node.state = NodeState.UNREACHABLE
                    await self._db.upsert_node(node)
                    log.warning(
                        "node %r (%s) is flood-routed and did not answer %d "
                        "reachability probes - skipping sync this cycle",
                        name, pubkey[:12], _PROBE_ATTEMPTS,
                    )
                    await self._db.write_log(
                        pubkey, "WARNING",
                        f"node unreachable over flood: no reply to {_PROBE_ATTEMPTS} "
                        "reachability probes - skipped sync this cycle",
                    )
                    await _progress(progress, "node unreachable over flood - skipping sync")
                    result.skipped_reason = "unreachable over flood"
                    return result  # finally still runs to restore the gateway radio

                result.reachable = True
                result.skipped_reason = "path repair needed before full sync"
                node.state = NodeState.PARTIAL
                node.last_connected = datetime.now(UTC)
                await self._db.upsert_node(node)
                log.warning(
                    "node %r (%s) is flood-routed; probe succeeded but full sync is "
                    "deferred until path is repaired",
                    name, pubkey[:12],
                )
                await self._db.write_log(
                    pubkey,
                    "WARNING",
                    "flood-routed contact reached by probe; path repair needed before full sync",
                )
                await _progress(
                    progress,
                    "flood reached node but full sync is deferred until path is repaired",
                )
                return result  # finally still runs to restore the gateway radio

            # reachability probe — skip the full pass on a node that is not
            # answering this cycle rather than timing out on every read.
            probe_result = await self._probe_reachable(
                contact, node, node_cfg, progress=progress
            )
            if probe_result is None:
                recovered = await self._try_radio_recovery(
                    node_cfg,
                    group_cfg,
                    global_cfg,
                    contact,
                    node,
                    tried_radio_channels,
                    current_saved_radio=saved_radio,
                    progress=progress,
                )
                if recovered is None:
                    saved_radio = None
                    node.state = NodeState.UNREACHABLE
                    await self._db.upsert_node(node)
                    log.warning(
                        "node %r (%s) did not answer %d reachability probes - "
                        "skipping sync this cycle", name, pubkey[:12], _PROBE_ATTEMPTS,
                    )
                    await self._db.write_log(
                        pubkey, "WARNING",
                        f"node unreachable: no reply to {_PROBE_ATTEMPTS} reachability "
                        "probes - skipped sync this cycle",
                    )
                    await _progress(progress, "node unreachable - skipping sync this cycle")
                    result.skipped_reason = "unreachable"
                    return result  # finally still runs to restore the gateway radio
                probe_result, saved_radio = recovered
            result.reachable = True
            node.last_connected = datetime.now(UTC)
            await self._db.upsert_node(node)
            read_retry_budget = [_STORED_PATH_READ_RETRY_BUDGET]

            # collect facts (reusing the probe's `ver` reply where applicable)
            result.facts_collected = await self._collect_facts(
                pubkey, contact, node, node_cfg, progress=progress,
                probe_result=probe_result, read_retry_budget=read_retry_budget,
            )

            # enforce / audit settings
            (
                s_checked,
                s_enforced,
                s_drifted,
                s_failed,
                enforce_drifted,
                enforce_failed,
            ) = await self._process_settings(
                pubkey, contact, node_cfg, group_cfg, global_cfg, node,
                progress=progress, read_retry_budget=read_retry_budget,
            )
            result.settings_checked = s_checked
            result.settings_enforced = s_enforced
            result.settings_drifted = s_drifted
            result.settings_failed = s_failed
            result.enforced_settings_drifted = enforce_drifted
            result.enforced_settings_failed = enforce_failed

            # seed operations from config into DB, then run what's pending
            operations = _resolve_operations(node_cfg, group_cfg, global_cfg)
            await self._seed_operations(pubkey, operations)
            op_run, op_failed = await self._run_operations(
                pubkey, contact, node_cfg, node, operations, progress=progress
            )
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
        except asyncio.CancelledError:
            await self._mark_sync_incomplete(node, "sync interrupted before completion")
            raise
        except Exception as exc:
            await self._mark_sync_incomplete(node, f"sync failed before completion: {exc}")
            raise

        finally:
            if saved_radio is not None:
                try:
                    await self._gateway.restore_radio(*saved_radio)
                    restored = _format_radio_tuple(saved_radio)
                    await self._db.write_log(
                        node.pubkey, "INFO", f"restored gateway radio to {restored}"
                    )
                    await _progress(progress, f"restored gateway radio to {restored}")
                except asyncio.CancelledError:
                    await self._mark_sync_incomplete(
                        node, "sync interrupted restoring gateway radio"
                    )
                    raise
                except Exception as exc:
                    await self._mark_sync_incomplete(
                        node, f"sync failed restoring gateway radio: {exc}"
                    )
                    raise

        # mark node state
        if enforce_drifted > 0 or enforce_failed > 0 or op_failed > 0:
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
            "  %r done — facts:%d settings:%d/%d drift:%d enforce-unresolved:%d "
            "read-failed:%d ops:%d/%d failed",
            name, result.facts_collected,
            s_enforced, s_checked, s_drifted, enforce_drifted, s_failed,
            op_run, op_failed,
        )
        await _progress(
            progress,
            f"done: facts={result.facts_collected} settings={s_checked} "
            f"drift={s_drifted} read-failed={s_failed} ops-failed={op_failed}",
        )
        return result

    async def _mark_sync_incomplete(self, node: Node, message: str) -> None:
        """Persist a terminal state if node processing aborts unexpectedly."""
        node.state = NodeState.PARTIAL
        await self._db.upsert_node(node)
        await self._db.write_log(node.pubkey, "ERROR", message)

    async def _send_authed(
        self,
        contact: dict[str, Any],
        command: str,
        node: Node,
        node_cfg: NodeConfig,
        *,
        timeout: float | None = None,
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
        if timeout is None:
            result = await self._gateway.send_command(contact, command)
        else:
            result = await self._gateway.send_command(contact, command, timeout=timeout)

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
            if timeout is None:
                result = await self._gateway.send_command(contact, command)
            else:
                result = await self._gateway.send_command(contact, command, timeout=timeout)

        return result

    async def _try_radio_recovery(
        self,
        node_cfg: NodeConfig,
        group_cfg: GroupConfig | None,
        global_cfg: GroupConfig | None,
        contact: dict[str, Any],
        node: Node,
        tried_channels: set[tuple[float, float, int]],
        *,
        current_saved_radio: tuple[float, float, int, int] | None,
        progress: ProgressCallback = None,
    ) -> tuple[CommandResult, tuple[float, float, int, int]] | None:
        candidates = radio_recovery_candidates(
            self._config, node_cfg, group_cfg, global_cfg, tried_channels
        )
        if not candidates:
            return None

        if current_saved_radio is not None:
            await _progress(progress, "restoring gateway radio before fallback probes")
            await self._gateway.restore_radio(*current_saved_radio)
            restored = _format_radio_tuple(current_saved_radio)
            await self._db.write_log(
                node.pubkey, "INFO", f"restored gateway radio to {restored}"
            )
            await _progress(progress, f"restored gateway radio to {restored}")

        await self._db.write_log(
            node.pubkey,
            "WARNING",
            (
                "normal reachability failed; trying "
                f"{len(candidates)} config-derived radio fallback(s)"
            ),
        )
        hunt_saved_radio: tuple[float, float, int, int] | None = None
        reached = False
        try:
            for candidate in candidates:
                candidate_text = _format_radio(
                    candidate.freq, candidate.bw, candidate.sf, candidate.cr
                )
                await _progress(progress, f"trying radio fallback: {candidate_text}")
                await self._db.write_log(
                    node.pubkey,
                    "WARNING",
                    f"trying radio fallback {candidate_text} ({candidate.source})",
                )
                if hunt_saved_radio is None:
                    hunt_saved_radio = await self._gateway.switch_radio_temporarily(
                        candidate.freq, candidate.bw, candidate.sf, candidate.cr
                    )
                else:
                    await self._gateway.switch_radio_temporarily(
                        candidate.freq, candidate.bw, candidate.sf, candidate.cr
                    )
                probe = await self._probe_reachable(contact, node, node_cfg, progress=progress)
                if probe is not None:
                    assert hunt_saved_radio is not None
                    reached = True
                    await self._db.write_log(
                        node.pubkey,
                        "WARNING",
                        f"radio fallback reached node on {candidate_text} ({candidate.source})",
                    )
                    await _progress(progress, f"radio fallback reached node: {candidate_text}")
                    return probe, hunt_saved_radio

            await self._db.write_log(
                node.pubkey,
                "WARNING",
                f"radio fallback exhausted ({len(candidates)} candidate(s))",
            )
            return None
        finally:
            if hunt_saved_radio is not None and not reached:
                await self._gateway.restore_radio(*hunt_saved_radio)
                await self._db.write_log(
                    node.pubkey,
                    "INFO",
                    f"restored gateway radio to {_format_radio_tuple(hunt_saved_radio)}",
                )

    async def _probe_reachable(
        self,
        contact: dict[str, Any],
        node: Node,
        node_cfg: NodeConfig,
        *,
        progress: ProgressCallback = None,
    ) -> CommandResult | None:
        """Confirm the node is answering before spending a full fact/setting pass.

        Sends a cheap bare `ver` up to _PROBE_ATTEMPTS times. Returns the first
        successful CommandResult (so the caller can reuse it as the `ver` fact and
        avoid a second read over the mesh), or None if every attempt failed.

        Uses _send_authed, so a node that needs re-authentication is handled here;
        a re-login failure raises _ReloginFailed, which the caller treats as
        offline (we cannot manage a node we cannot authenticate to).
        """
        stored_path_failed = False
        for attempt in range(1, _PROBE_ATTEMPTS + 1):
            await _progress(
                progress, f"checking reachability ({attempt}/{_PROBE_ATTEMPTS})"
            )
            res = await self._send_authed(contact, _PROBE_COMMAND, node, node_cfg)
            if res.success:
                return res
            stored_path_failed = stored_path_failed or _uses_stored_path(contact)
            log.debug(
                "reachability probe %d/%d failed for %s: %s",
                attempt, _PROBE_ATTEMPTS, node.pubkey[:12], res.error or "no response",
            )
        if stored_path_failed:
            await _progress(progress, "stored path failed; checking flood reachability once")
            flood_contact = _flood_route_contact(contact)
            res = await self._send_authed(flood_contact, _PROBE_COMMAND, node, node_cfg)
            if res.success:
                await self._db.write_log(
                    node.pubkey,
                    "WARNING",
                    "stored path failed but flood probe reached node; path repair needed",
                )
                await _progress(
                    progress,
                    "flood reached node but full sync is deferred until path is repaired",
                )
            else:
                await self._db.write_log(
                    node.pubkey,
                    "WARNING",
                    f"stored path and flood probe both failed: {res.error or 'no response'}",
                )
        return None

    async def _send_enforced_write(
        self,
        pubkey: str,
        contact: dict[str, Any],
        command: str,
        node: Node,
        node_cfg: NodeConfig,
        *,
        timeout: float | None,
        progress: ProgressCallback = None,
    ) -> tuple[CommandResult, int]:
        """Send an idempotent desired-state write over the cached management path."""
        result = await self._send_authed(contact, command, node, node_cfg, timeout=timeout)
        return result, 1

    async def _send_read_with_lossy_retry(
        self,
        pubkey: str,
        contact: dict[str, Any],
        command: str,
        node: Node,
        node_cfg: NodeConfig,
        *,
        read_retry_budget: list[int],
        progress: ProgressCallback = None,
    ) -> CommandResult:
        """Read over the cached path, retrying a few stored-path misses per node.

        qh-corbin live testing showed that a learned repeater path can be correct
        yet still lose an isolated command. Retrying every command would hammer
        a weak mesh, so retries are only used for stored paths and are capped for
        the whole node pass. Flood is deliberately not used as a recovery path.
        """
        result = await self._send_authed(contact, command, node, node_cfg)
        if result.success:
            return result
        if not _uses_stored_path(contact) or read_retry_budget[0] <= 0:
            return result

        read_retry_budget[0] -= 1
        error = result.error or "no response"
        log.info(
            "read %r failed for %s on stored path (%s); retrying once "
            "(remaining read retries for node: %d)",
            command, pubkey[:12], error, read_retry_budget[0],
        )
        await _progress(progress, f"read {command!r} missed; retrying on stored path")
        retry = await self._send_authed(contact, command, node, node_cfg)
        if retry.success:
            await self._db.write_log(
                pubkey,
                "INFO",
                f"read {command!r} succeeded on stored-path retry",
            )
        return retry

    async def _collect_facts(
        self,
        pubkey: str,
        contact: dict[str, Any],
        node: Node,
        node_cfg: NodeConfig,
        *,
        progress: ProgressCallback = None,
        probe_result: CommandResult | None = None,
        read_retry_budget: list[int] | None = None,
    ) -> int:
        """Run all declared fact commands and store results.

        Fact order is randomised each cycle so a marginal link does not keep
        losing the same trailing observations. If the reachability probe already
        read a value (it sends bare `ver`), that reply is reused instead of
        reading the same value again over the mesh.
        """
        collected = 0
        if read_retry_budget is None:
            read_retry_budget = [0]
        keys = list(self._config.facts)
        random.shuffle(keys)
        for key in keys:
            # neighbors, ver/board, and clock are bare commands; everything else is `get <key>`
            cmd = read_command_for_key(key)
            if probe_result is not None and cmd == _PROBE_COMMAND:
                # reuse the reachability probe's reply — no second read over the mesh
                res = probe_result
            else:
                await _progress(progress, f"reading fact: {key}")
                res = await self._send_read_with_lossy_retry(
                    pubkey,
                    contact,
                    cmd,
                    node,
                    node_cfg,
                    read_retry_budget=read_retry_budget,
                    progress=progress,
                )
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
                error = res.error or "no response"
                log.debug("fact %r failed for %s: %s", key, pubkey[:12], error)
                await self._db.write_log(pubkey, "WARNING", f"fact {key} failed: {error}")
                await _progress(progress, f"fact {key} failed: {error}")
        return collected

    async def _process_settings(
        self,
        pubkey: str,
        contact: dict[str, Any],
        node_cfg: NodeConfig,
        group_cfg: GroupConfig | None,
        global_cfg: GroupConfig | None,
        node: Node,
        *,
        progress: ProgressCallback = None,
        read_retry_budget: list[int] | None = None,
    ) -> tuple[int, int, int, int, int, int]:
        """Check settings, including reportable drift and enforce-blocking failures."""
        checked = enforced = drifted = read_failed = 0
        enforced_drifted = enforced_read_failed = 0
        if read_retry_budget is None:
            read_retry_budget = [0]
        keys = list(all_setting_keys(node_cfg, group_cfg, global_cfg))
        # Randomise read order each cycle so a marginal link does not keep losing
        # the same trailing settings (mirrors the fact-collection shuffle).
        random.shuffle(keys)
        existing = {s.key: s for s in await self._db.get_settings(pubkey)}

        for key in keys:
            policy = resolve_policy(key, node_cfg, group_cfg, global_cfg)
            desired = resolve_desired(key, node_cfg, group_cfg, global_cfg)

            if policy == SettingPolicy.SKIP:
                continue

            read_cmd = read_command_for_key(key)
            await _progress(progress, f"reading setting: {key}")
            res = await self._send_read_with_lossy_retry(
                pubkey,
                contact,
                read_cmd,
                node,
                node_cfg,
                read_retry_budget=read_retry_budget,
                progress=progress,
            )
            if not res.success:
                error = res.error or "no response"
                log.warning("get %s failed for %s: %s", key, pubkey[:12], error)
                await self._db.write_log(pubkey, "WARNING", f"read {key} failed: {error}")
                await _progress(progress, f"setting {key} read failed: {error}")
                read_failed += 1
                if policy == SettingPolicy.ENFORCE:
                    enforced_read_failed += 1
                continue

            checked += 1
            observed = (res.text or "").strip()
            if key == "name" and observed:
                node.name = observed
            comparison = compare_setting(key, desired, observed)
            is_drift = desired is not None and not comparison.equal
            previous = existing.get(key)

            setting = SettingRecord(
                node_pubkey=pubkey,
                key=key,
                desired=desired,
                policy=policy,
                observed=observed,
                last_checked=datetime.now(UTC),
                last_set=(
                    previous.last_set
                    if previous is not None and previous.desired == desired
                    else None
                ),
                update_attempts=(
                    previous.update_attempts
                    if policy == SettingPolicy.ENFORCE
                    and previous is not None
                    and previous.desired == desired
                    else 0
                ),
                drift=is_drift,
            )

            if is_drift:
                drifted += 1
                if policy == SettingPolicy.ENFORCE:
                    enforced_drifted += 1
                    log.info("  enforcing %s: observed=%r desired=%r", key, observed, desired)
                    await _progress(progress, f"enforcing setting: {key}")
                    assert desired is not None
                    write_cmd = write_command_for_key(key, desired)
                    set_res, write_attempts = await self._send_enforced_write(
                        pubkey,
                        contact,
                        write_cmd,
                        node,
                        node_cfg,
                        timeout=command_timeout(write_cmd),
                        progress=progress,
                    )
                    setting.update_attempts += write_attempts
                    if set_res.success:
                        enforced += 1
                        drifted -= 1  # enforcement fixed it — don't count as residual drift
                        enforced_drifted -= 1
                        setting.drift = False
                        setting.observed = desired  # update observed to reflect the new value
                        if key == "name" and desired:
                            node.name = desired
                        setting.last_set = datetime.now(UTC)
                        await self._db.record_setting_change(
                            pubkey,
                            key,
                            observed,
                            desired,
                            write_cmd,
                        )
                        await self._db.write_log(
                            pubkey, "INFO", f"set {key}={desired!r} (was {observed!r})"
                        )
                        await _progress(progress, f"setting {key} updated")
                    else:
                        log.warning("  set %s failed: %s", key, set_res.error)
                        await self._db.write_log(
                            pubkey, "ERROR", f"set {key} failed: {set_res.error}"
                        )
                        await _progress(progress, f"setting {key} write failed: {set_res.error}")
                else:
                    log.info(
                        "  drift: %s observed=%r desired=%r (policy=%s — not enforcing)",
                        key, observed, desired, policy.value,
                    )
                    await self._db.write_log(
                        pubkey, "WARNING",
                        f"drift: {key} observed={observed!r} desired={desired!r} ({policy.value})"
                    )
                    await _progress(progress, f"setting {key} drifted ({policy.value} only)")

            await self._db.upsert_setting(setting)

        return checked, enforced, drifted, read_failed, enforced_drifted, enforced_read_failed

    async def _seed_operations(self, pubkey: str, operations: list[OperationConfig]) -> None:
        """Queue any config operations that are due (never run, or past every_hours)."""
        for op_cfg in operations:
            last = await self._db.get_last_operation(pubkey, op_cfg.command)
            if last is None:
                # never seen before — queue it
                pass
            elif last.status == OperationStatus.SUCCESS and last.run_at is not None:
                due_after = last.run_at + timedelta(hours=op_cfg.every_hours)
                if datetime.now(UTC) < due_after:
                    continue  # ran recently, not due yet
                # past the window — fall through to queue a new run
            elif last.status == OperationStatus.PENDING:
                continue  # already queued
            elif last.run_at is not None:
                due_after = last.run_at + timedelta(hours=op_cfg.every_hours)
                if datetime.now(UTC) < due_after:
                    continue  # failed recently, wait for the next retry window
                # past the retry window — fall through to queue a new run
            else:
                continue  # malformed historical row without run_at; leave it alone

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
        operations: list[OperationConfig],
        *,
        progress: ProgressCallback = None,
    ) -> tuple[int, int]:
        """Run pending one-shot operations. Returns (run, failed)."""
        run = failed = 0
        pending = await self._db.get_pending_operations(pubkey)

        op_cfg_by_cmd = {o.command: o for o in operations}

        for op in pending:
            log.info("  operation: %r", op.command)
            await _progress(progress, f"running operation: {op.command}")
            faf = op_cfg_by_cmd.get(op.command, OperationConfig()).fire_and_forget
            res = await self._send_authed(contact, op.command, node, node_cfg)
            op.run_at = datetime.now(UTC)
            op.response = res.text or res.error

            success = _operation_succeeded(res)
            if success or faf:
                op.status = OperationStatus.SUCCESS
                run += 1
                if faf and not success:
                    log.info("  operation sent (fire-and-forget): %r", op.command)
                    await self._db.write_log(
                        pubkey, "INFO", f"operation sent (fire-and-forget): {op.command!r}"
                    )
                else:
                    await self._db.write_log(pubkey, "INFO", f"operation ok: {op.command!r}")
                    if op.command == "clock sync":
                        await self._refresh_clock_fact_after_sync(
                            pubkey, contact, node, node_cfg, progress=progress
                        )
            else:
                op.status = OperationStatus.FAILED
                failed += 1
                error = res.error or res.text or "no response"
                await self._db.write_log(
                    pubkey, "ERROR", f"operation failed: {op.command!r}: {error}"
                )

            await self._db.update_operation(op)

        return run, failed

    async def _refresh_clock_fact_after_sync(
        self,
        pubkey: str,
        contact: dict[str, Any],
        node: Node,
        node_cfg: NodeConfig,
        *,
        progress: ProgressCallback = None,
    ) -> None:
        await _progress(progress, "reading fact: clock")
        res = await self._send_authed(contact, "clock", node, node_cfg)
        if res.success and res.text:
            await self._db.upsert_fact(NodeFact(
                node_pubkey=pubkey,
                key="clock",
                value=res.text.strip(),
            ))
        else:
            error = res.error or "no response"
            await self._db.write_log(pubkey, "WARNING", f"fact clock refresh failed: {error}")
            await _progress(progress, f"fact clock refresh failed: {error}")


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
