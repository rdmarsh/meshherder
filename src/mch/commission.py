"""
Commission a fresh MeshCore node into the fleet.

Two modes:
  new     — node has a factory-generated identity; set password/name/radio and write
             a config entry so the daemon picks it up.
  replace — restore a known node identity from a prv_key in secrets.yaml, then push
             password, radio, and other config from the existing entry.

The caller (daemon _dispatch) is responsible for calling wq.pause()/resume() and for
ensuring gateway.discover() is called with the right dwell time.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from mch.config import Config, NodeSecrets
from mch.gateway import Gateway

if TYPE_CHECKING:
    from mch.db import Database
    from mch.queue import WorkQueue

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

async def discover_unmanaged(
    gateway: Gateway,
    db: "Database",
    config: Config,
    *,
    dwell: float = 8.0,
) -> list[dict[str, Any]]:
    """
    Discover nodes on the factory frequency and filter out already-managed ones.

    A node is considered managed if its pubkey matches any config pubkey_prefix
    or any full pubkey in the DB.
    """
    discovered = await gateway.discover(dwell=dwell)

    known_prefixes = {nc.pubkey_prefix for nc in config.nodes}
    db_nodes = await db.get_all_nodes()
    known_pubkeys = {n.pubkey for n in db_nodes}

    unmanaged = []
    for node in discovered:
        pubkey = node.get("pubkey", "")
        if any(pubkey.startswith(p) for p in known_prefixes):
            log.debug("discover: filtering known config node %s", pubkey[:12])
            continue
        if any(pubkey.startswith(p) for p in known_pubkeys):
            log.debug("discover: filtering known DB node %s", pubkey[:12])
            continue
        unmanaged.append(node)

    return unmanaged


# ---------------------------------------------------------------------------
# RPC entry point
# ---------------------------------------------------------------------------

async def handle_commission(
    req: dict[str, Any],
    wq: "WorkQueue",
    db: "Database",
    config: Config,
) -> dict[str, Any]:
    """
    Handle the 'commission' RPC request.

    Pauses the work queue, discovers unmanaged nodes, validates exactly one is
    found, then delegates to _commission_new or _commission_replace.
    The queue is always resumed in the finally block.
    """
    mode = req.get("mode")
    if mode not in ("new", "replace", "list_replace", "scan"):
        return {"ok": False, "error": f"mode must be 'new', 'replace', 'list_replace', or 'scan', got {mode!r}"}

    if mode == "list_replace":
        # Return config nodes that have a prv_key in secrets — no discovery needed.
        replaceable = [
            {"pubkey_prefix": nc.pubkey_prefix, "label": nc.label}
            for nc in config.nodes
            if nc.pubkey_prefix in config.secrets and config.secrets[nc.pubkey_prefix].prv_key
        ]
        return {"ok": True, "data": {"replaceable": replaceable}}

    gateway = wq._gateway

    if mode == "scan":
        # Discover unmanaged nodes and return them with the gateway's home radio string.
        wq.pause()
        try:
            unknown = await discover_unmanaged(gateway, db, config)
            r = config.gateway.radio
            gateway_radio = (
                f"{r.freq:.3f},{r.bw:g},{r.sf},{r.cr}" if r else None
            )
            return {"ok": True, "data": {"nodes": unknown, "gateway_radio": gateway_radio}}
        except Exception as exc:
            log.exception("commission scan error: %s", exc)
            return {"ok": False, "error": str(exc)}
        finally:
            wq.resume()

    wq.pause()
    try:
        # Save home radio state before discover() hops and restores it.
        home_radio = (
            gateway.info.get("radio_freq"),
            gateway.info.get("radio_bw"),
            gateway.info.get("radio_sf"),
            gateway.info.get("radio_cr"),
        )

        unknown = await discover_unmanaged(gateway, db, config)

        if len(unknown) == 0:
            return {
                "ok": False,
                "error": "no unmanaged nodes found — is it in range and powered on with an antenna?",
            }
        if len(unknown) > 1:
            pubkeys = ", ".join(n["pubkey"][:12] for n in unknown)
            return {
                "ok": False,
                "error": (
                    f"found {len(unknown)} unmanaged nodes — commission one at a time ({pubkeys})"
                ),
            }

        node_info = unknown[0]

        # discover() restored home freq; re-hop to factory so commissioning commands reach the node.
        await gateway.hop_radio(*Gateway.FACTORY_FREQ)
        try:
            # DISCOVER_RESPONSE does not add the node to the gateway's contact store.
            # add_commission_contact() inserts it with flood routing so send_command() succeeds.
            contact = await gateway.add_commission_contact(node_info)

            # Factory nodes require authentication before accepting CLI commands.
            # Factory default password is 'password'.
            login_ok = await gateway.login(contact, "password")
            if not login_ok:
                return {
                    "ok": False,
                    "error": (
                        "could not authenticate to factory node — "
                        "ensure the node is factory-fresh (default password 'password') and retry"
                    ),
                }

            if mode == "new":
                return await _commission_new(req, gateway, config, contact)
            else:
                return await _commission_replace(req, gateway, config, contact)
        finally:
            await gateway.restore_radio(*home_radio)

    except Exception as exc:
        log.exception("commission error: %s", exc)
        return {"ok": False, "error": str(exc)}
    finally:
        wq.resume()


# ---------------------------------------------------------------------------
# commissioning sequences
# ---------------------------------------------------------------------------

async def _commission_new(
    req: dict[str, Any],
    gateway: Gateway,
    config: Config,
    contact: dict[str, Any],
) -> dict[str, Any]:
    """
    Commission a brand-new node.

    Generates a fresh 32-byte private key, sets it on the node (which changes
    the node's mesh identity after reboot), and saves the key to secrets.yaml
    under the factory pubkey prefix as a temporary placeholder.

    The operator must rename the secrets.yaml entry to the node's new pubkey
    prefix after it reboots, and add a config.yaml entry using mch contacts.
    """
    import secrets as _secrets

    # Always set an admin password; fall back to 'password' if not provided.
    admin_password = req.get("admin_password") or "password"
    guest_password = req.get("guest_password")
    name = req.get("name") or req.get("label") or ""
    label = req.get("label") or ""
    group = req.get("group")
    target_freq_str = req.get("target_freq")

    pubkey = contact["public_key"]
    factory_prefix = pubkey[:8]

    prv_key_bytes = _secrets.token_bytes(32)
    prv_key_hex = prv_key_bytes.hex()

    # Commands in order: (command, fire_and_forget)
    # Radio is set before prv.key so the node boots on the right frequency.
    # prv.key is set before reboot; it changes identity after restart.
    # reboot is fire-and-forget — the node may auto-reboot from prv.key.
    cmds: list[tuple[str, bool]] = []
    cmds.append((f"password {admin_password}", False))
    if guest_password:
        cmds.append((f"set guest.password {guest_password}", False))
    if name:
        cmds.append((f"set name {name}", False))
    if target_freq_str:
        cmds.append((f"set radio {target_freq_str}", False))
    cmds.append((f"set prv.key {prv_key_hex}", False))
    cmds.append(("reboot", True))

    for cmd, faf in cmds:
        result = await gateway.send_command(contact, cmd, timeout=30.0)
        if not result.success and not faf:
            return {"ok": False, "error": f"commission failed at {cmd!r}: {result.error}"}

    # Save prv_key to secrets.yaml under the factory prefix (temporary — operator
    # must rename this entry to the node's new pubkey prefix after it reboots).
    if config.secrets_path:
        _update_secrets(
            secrets_path=config.secrets_path,
            pubkey_prefix=factory_prefix,
            ns=NodeSecrets(
                password=admin_password or None,
                guest_password=guest_password,
                prv_key=prv_key_hex,
            ),
        )

    log.info("commissioned new node (factory: %s) label=%r", factory_prefix, label or name)
    return {
        "ok": True,
        "data": {
            "factory_prefix": factory_prefix,
            "prv_key": prv_key_hex,
            "label": label,
            "name": name,
            "group": group,
            "target_freq": target_freq_str,
        },
    }


async def _commission_replace(
    req: dict[str, Any],
    gateway: Gateway,
    config: Config,
    contact: dict[str, Any],
) -> dict[str, Any]:
    """
    Restore a node's identity from secrets.yaml and push its config.

    Sends: set prv.key (from secrets), password, guest.password, set radio (from via),
    then reboot. Config and secrets files are not modified — the existing entries
    are already correct.
    """
    pubkey_prefix = req.get("pubkey_prefix", "")

    node_cfg = next(
        (nc for nc in config.nodes if nc.pubkey_prefix == pubkey_prefix),
        None,
    )
    if not node_cfg:
        return {"ok": False, "error": f"no config entry for pubkey_prefix {pubkey_prefix!r}"}

    ns = config.secrets.get(pubkey_prefix)
    if not ns or not ns.prv_key:
        return {"ok": False, "error": f"no prv_key in secrets for {pubkey_prefix!r}"}

    cmds: list[tuple[str, bool]] = []
    cmds.append((f"set prv.key {ns.prv_key}", False))
    if ns.password:
        cmds.append((f"password {ns.password}", False))
    if ns.guest_password:
        cmds.append((f"set guest.password {ns.guest_password}", False))
    if node_cfg.via:
        v = node_cfg.via
        cmds.append((f"set radio {v.freq:.3f},{v.bw},{v.sf},{v.cr}", False))
    cmds.append(("reboot", True))

    for cmd, faf in cmds:
        result = await gateway.send_command(contact, cmd, timeout=30.0)
        if not result.success and not faf:
            return {"ok": False, "error": f"commission failed at {cmd!r}: {result.error}"}

    log.info("replaced node identity for %s (%s)", pubkey_prefix, node_cfg.label or "")
    return {
        "ok": True,
        "data": {
            "pubkey_prefix": pubkey_prefix,
            "label": node_cfg.label,
        },
    }


# ---------------------------------------------------------------------------
# file write helpers (used by _commission_new and tests)
# ---------------------------------------------------------------------------

def _append_node_config(
    config_path: Path,
    pubkey_prefix: str,
    label: str,
    group: str | None,
    target_freq_str: str | None,
) -> None:
    """Atomically append a new node entry to config.yaml."""
    raw = yaml.safe_load(config_path.read_text()) or {}
    nodes = raw.setdefault("nodes", [])

    entry: dict[str, Any] = {"pubkey_prefix": pubkey_prefix}
    if label:
        entry["label"] = label
    if group:
        entry["group"] = group
    if target_freq_str:
        entry["via"] = target_freq_str

    nodes.append(entry)

    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(yaml.dump(raw, default_flow_style=False))
    tmp.replace(config_path)
    log.info("wrote config entry for %s to %s", pubkey_prefix, config_path)


def _update_secrets(
    secrets_path: Path,
    pubkey_prefix: str,
    ns: NodeSecrets,
) -> None:
    """Atomically update secrets.yaml with a new or updated node entry (mode 0o600)."""
    raw: dict = {}
    if secrets_path.exists():
        raw = yaml.safe_load(secrets_path.read_text()) or {}
    nodes = raw.setdefault("nodes", {})

    entry: dict[str, Any] = {}
    if ns.password:
        entry["password"] = ns.password
    if ns.guest_password:
        entry["guest_password"] = ns.guest_password
    if ns.prv_key:
        entry["prv_key"] = ns.prv_key
    nodes[pubkey_prefix] = entry

    tmp = secrets_path.with_name(secrets_path.name + ".tmp")
    tmp.write_text(yaml.dump(raw, default_flow_style=False))
    os.chmod(tmp, 0o600)
    tmp.replace(secrets_path)
    log.info("wrote secrets entry for %s to %s", pubkey_prefix, secrets_path)
