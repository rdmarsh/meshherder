"""
Work queue — drives the fleet management cycle.

Each cycle:
  1. Load nodes from DB
  2. Match against live gateway contacts
  3. For each matched node (sequentially, rate-limited):
     a. Login if needed
     b. Collect declared facts (ver, board, role, …)
     c. Poll/enforce settings (get → compare → set if enforce+drift)
     d. Run pending operations (clock sync, etc.)
  4. Sleep until next cycle
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mcherd.config import Config, NodeConfig, GroupConfig, SettingConfig
from mcherd.db import Database
from mcherd.gateway import CommandResult, Gateway, GatewayError

if TYPE_CHECKING:
    from mcherd.messenger import Messenger
from mcherd.models import (
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
    global_default: SettingPolicy = SettingPolicy.ENFORCE,
) -> SettingPolicy:
    """
    Return the effective policy for a setting key on a node.

    Resolution order (most specific wins):
      1. Per-node setting policy override
      2. Group setting_overrides for this key
      3. Node default_policy
      4. Group default_policy
      5. global_default (ENFORCE)
    """
    # 1. per-node setting override
    node_setting = node_cfg.settings.get(key)
    if node_setting and node_setting.policy is not None:
        return node_setting.policy

    # 2. group setting_override
    if group_cfg:
        group_override = group_cfg.setting_overrides.get(key)
        if group_override and group_override.policy is not None:
            return group_override.policy

    # 3. node default
    if node_cfg.default_policy is not None:
        return node_cfg.default_policy

    # 4. group default
    if group_cfg and group_cfg.default_policy is not None:
        return group_cfg.default_policy

    return global_default


def resolve_desired(
    key: str,
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
) -> str | None:
    """Return the desired value for a setting, node overrides group."""
    node_setting = node_cfg.settings.get(key)
    if node_setting and node_setting.desired is not None:
        return node_setting.desired
    if group_cfg:
        group_setting = group_cfg.settings.get(key)
        if group_setting and group_setting.desired is not None:
            return group_setting.desired
    return None


def all_setting_keys(
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
) -> set[str]:
    """All setting keys relevant to this node (from both node and group config)."""
    keys: set[str] = set(node_cfg.settings.keys())
    if group_cfg:
        keys |= set(group_cfg.settings.keys())
        keys |= set(group_cfg.setting_overrides.keys())
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
        self._current_task: asyncio.Task | None = None

    async def run(self) -> None:
        """Main loop — run cycles on schedule until cancelled."""
        self._running = True
        log.info("work queue started (interval=%dm)", self._config.scheduler.sync_interval_minutes)
        while self._running:
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("cycle failed: %s", exc)
            if not self._running:
                break
            interval = self._config.scheduler.sync_interval_minutes * 60
            log.info("next cycle in %ds", interval)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        self._running = False
        if self._current_task:
            self._current_task.cancel()

    async def run_cycle(self) -> list[NodeResult]:
        """One full pass through all managed nodes. Returns per-node results."""
        log.info("--- cycle start ---")
        contacts = await self._gateway.get_contacts(managed_only=False)
        contact_by_prefix = {
            c["public_key"][:len(prefix)]: c
            for c in contacts
            for prefix in [c["public_key"][:12]]  # index by 12-char prefix
        }
        # also index by full pubkey for exact matches
        contact_by_pubkey = {c["public_key"]: c for c in contacts}

        # register all visible contacts with the messenger so it can reply to DMs
        if self._messenger:
            for c in contacts:
                self._messenger.register_contact(c["public_key"][:12], c)

        node_configs = {nc.pubkey_prefix: nc for nc in self._config.nodes}
        group_configs = {g.name: g for g in self._config.groups}

        results: list[NodeResult] = []

        for node_cfg in self._config.nodes:
            # find matching contact
            contact = self._find_contact(node_cfg.pubkey_prefix, contact_by_prefix, contact_by_pubkey)
            if contact is None:
                log.warning("node %r (%s) not in gateway contacts — skipping",
                            node_cfg.name, node_cfg.pubkey_prefix)
                results.append(NodeResult(
                    pubkey=node_cfg.pubkey_prefix,
                    name=node_cfg.name,
                    reachable=False,
                    error="not in gateway contacts",
                ))
                continue

            group_cfg = group_configs.get(node_cfg.group) if node_cfg.group else None

            result = await self._process_node(node_cfg, group_cfg, contact)
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
        by_prefix: dict[str, Any],
        by_pubkey: dict[str, Any],
    ) -> dict[str, Any] | None:
        # exact full-key match first
        if pubkey_prefix in by_pubkey:
            return by_pubkey[pubkey_prefix]
        # prefix match (config stores short prefixes for readability)
        for pubkey, contact in by_pubkey.items():
            if pubkey.startswith(pubkey_prefix):
                return contact
        return None

    async def _process_node(
        self,
        node_cfg: NodeConfig,
        group_cfg: GroupConfig | None,
        contact: dict[str, Any],
    ) -> NodeResult:
        pubkey = contact["public_key"]
        name = contact.get("adv_name", node_cfg.name)
        result = NodeResult(pubkey=pubkey, name=name)

        log.info("processing node %r (%s…)", name, pubkey[:12])

        # ensure node exists in db
        node = await self._db.get_node(pubkey) or Node(pubkey=pubkey, name=name)
        node.name = name
        node.state = NodeState.CONFIGURING
        await self._db.upsert_node(node)

        # login if needed
        password = self._config.secrets.get(node_cfg.pubkey_prefix)
        if password and not node.auth_cached:
            ok = await self._gateway.login(contact, password)
            if not ok:
                log.warning("login failed for %r — skipping this cycle", name)
                node.state = NodeState.OFFLINE
                await self._db.upsert_node(node)
                await self._db.write_log(pubkey, "WARNING", "login failed")
                result.error = "login failed"
                return result
            node.auth_cached = True
            await self._db.upsert_node(node)

        result.reachable = True
        now = datetime.now(UTC)
        node.last_seen = now
        node.last_connected = now

        # collect facts
        result.facts_collected = await self._collect_facts(pubkey, contact)

        # enforce / audit settings
        s_checked, s_enforced, s_drifted = await self._process_settings(
            pubkey, contact, node_cfg, group_cfg
        )
        result.settings_checked = s_checked
        result.settings_enforced = s_enforced
        result.settings_drifted = s_drifted

        # run pending operations
        op_run, op_failed = await self._run_operations(pubkey, contact, node_cfg)
        result.operations_run = op_run
        result.operations_failed = op_failed

        # mark node state
        if s_drifted > 0 or op_failed > 0:
            node.state = NodeState.PARTIAL
        else:
            node.state = NodeState.CONFIGURED
        await self._db.upsert_node(node)

        log.info(
            "  %r done — facts:%d  settings:%d/%d enforced  drift:%d  ops:%d/%d failed",
            name, result.facts_collected,
            s_enforced, s_checked, s_drifted,
            op_run, op_failed,
        )
        return result

    async def _collect_facts(self, pubkey: str, contact: dict) -> int:
        """Run all declared fact commands and store results."""
        collected = 0
        for key in self._config.facts:
            cmd = f"get {key}" if key not in ("ver", "board") else key
            res = await self._gateway.send_command(contact, cmd)
            if res.success and res.text:
                await self._db.upsert_fact(NodeFact(
                    node_pubkey=pubkey,
                    key=key,
                    value=res.text.strip(),
                ))
                collected += 1
            else:
                log.debug("fact %r failed for %s: %s", key, pubkey[:12], res.error)
        return collected

    async def _process_settings(
        self,
        pubkey: str,
        contact: dict,
        node_cfg: NodeConfig,
        group_cfg: GroupConfig | None,
    ) -> tuple[int, int, int]:
        """Check and optionally enforce all settings. Returns (checked, enforced, drifted)."""
        checked = enforced = drifted = 0
        keys = all_setting_keys(node_cfg, group_cfg)

        for key in sorted(keys):
            policy = resolve_policy(key, node_cfg, group_cfg)
            desired = resolve_desired(key, node_cfg, group_cfg)

            if policy == SettingPolicy.SKIP:
                continue

            # get current value
            res = await self._gateway.send_command(contact, f"get {key}")
            if not res.success:
                log.warning("get %s failed for %s: %s", key, pubkey[:12], res.error)
                continue

            checked += 1
            observed = (res.text or "").strip()
            is_drift = desired is not None and observed != desired

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
                    set_res = await self._gateway.send_command(
                        contact, f"set {key} {desired}"
                    )
                    if set_res.success:
                        enforced += 1
                        setting.drift = False
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

    async def _run_operations(
        self,
        pubkey: str,
        contact: dict,
        node_cfg: NodeConfig,
    ) -> tuple[int, int]:
        """Run pending one-shot operations. Returns (run, failed)."""
        run = failed = 0
        pending = await self._db.get_pending_operations(pubkey)

        for op in pending:
            log.info("  operation: %r", op.command)
            res = await self._gateway.send_command(contact, op.command)
            op.run_at = datetime.now(UTC)
            op.response = res.text or res.error

            if res.success:
                op.status = OperationStatus.SUCCESS
                run += 1
                await self._db.write_log(pubkey, "INFO", f"operation ok: {op.command!r}")
            else:
                op.status = OperationStatus.FAILED
                failed += 1
                await self._db.write_log(
                    pubkey, "ERROR", f"operation failed: {op.command!r}: {res.error}"
                )

            await self._db.upsert_operation(op)

        return run, failed
