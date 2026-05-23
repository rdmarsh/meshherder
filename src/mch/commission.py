"""
Commission a fresh MeshCore node into the fleet.

Two modes:
  new     — node starts with a factory-generated identity; generate and set a
             managed identity, then set password/name/radio and print snippets.
  replace — restore a known node identity from a prv_key in secrets.yaml, then push
             password, radio, and other config from the existing entry.

The caller (daemon _dispatch) is responsible for calling wq.pause()/resume() and for
ensuring gateway.discover() is called with the right dwell time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mch.config import Config
from mch.gateway import Gateway
from mch.meshkey import derive_public_key, generate_private_key

if TYPE_CHECKING:
    from mch.db import Database
    from mch.queue import WorkQueue

log = logging.getLogger(__name__)


def _redact_command(command: str) -> str:
    """Return a command label safe for progress/error text."""
    if command.startswith("set prv.key "):
        return "set prv.key <redacted>"
    if command.startswith("password "):
        return "password <redacted>"
    if command.startswith("set guest.password "):
        return "set guest.password <redacted>"
    return command


def _looks_like_cli_error_response(text: str | None) -> bool:
    """Detect firmware CLI error replies that still arrived as normal messages."""
    if not text:
        return False
    normalized = text.strip().lower()
    if not normalized:
        return False
    if "err_code_" in normalized:
        return True
    return normalized.startswith((
        "err",
        "error",
        "failed",
        "invalid",
        "unknown",
        "not supported",
        "permission denied",
        "unauthorized",
        "unauthorised",
    ))


def _parse_radio_tuple(raw: str | None) -> tuple[float, float, int, int]:
    """Parse freq,bw,sf,cr for post-commission discovery."""
    if raw is None:
        return Gateway.FACTORY_FREQ
    freq, bw, sf, cr = raw.split(",")
    return round(float(freq), 3), float(bw), int(sf), int(cr)


async def _login_factory(
    gateway: Gateway,
    contact: dict[str, Any],
    progress: Callable[[str], Awaitable[None]] | None,
) -> bool:
    """Try the MeshCore factory default password and emit progress messages."""
    if progress is not None:
        await progress("logging in...")
    ok = await gateway.login(contact, "password")
    if ok and progress is not None:
        await progress("login OK")
    return ok


async def _login_for_new(
    gateway: Gateway,
    contact: dict[str, Any],
    new_admin_password: str,
    progress: Callable[[str], Awaitable[None]] | None,
) -> tuple[bool, str | None]:
    """
    Try factory password first; fall back to the user-supplied admin password.

    If a previous attempt got past `password <new>` and then failed later
    (e.g. a session-invalidating timeout on the next command), the node is now
    holding the new admin password. The CLI wizard preserves the same form
    values (and prv_key) across retries, so re-authenticating with the
    just-supplied password lets the idempotent command sequence resume without
    a manual reset.
    """
    if await _login_factory(gateway, contact, progress):
        return True, None
    if new_admin_password and new_admin_password != "password":
        if progress is not None:
            await progress("factory login failed; trying the new admin password")
        if await gateway.login(contact, new_admin_password):
            if progress is not None:
                await progress("login OK (new admin password)")
            return True, None
    return False, (
        "could not authenticate to factory node — "
        "ensure it is factory-fresh (default password 'password') and retry"
    )


async def _login_for_replace(
    gateway: Gateway,
    contact: dict[str, Any],
    managed_password: str | None,
    progress: Callable[[str], Awaitable[None]] | None,
) -> tuple[bool, str | None]:
    """
    Try factory password first; fall back to the managed password from secrets.

    Returns (ok, error_message). Falling back recovers from a previous replacement
    attempt that already changed the admin password before failing on a later
    command (e.g. set radio timing out). A truly factory-fresh node never reaches
    the managed branch because the first attempt succeeds. The four mutation
    commands sent after login are idempotent for the same target values, so
    re-sending them via the fallback path is safe.
    """
    if await _login_factory(gateway, contact, progress):
        return True, None
    if managed_password:
        if progress is not None:
            await progress("factory login failed; resuming with managed credentials")
        if await gateway.login(contact, managed_password):
            if progress is not None:
                await progress("login OK (managed credentials)")
            return True, None
        return False, (
            "could not authenticate to node — neither the factory password "
            "nor the managed password from secrets.yaml was accepted"
        )
    return False, (
        "could not authenticate to factory node — ensure it is factory-fresh "
        "(default password 'password') and retry"
    )


async def _send_commission_command(
    gateway: Gateway,
    contact: dict[str, Any],
    command: str,
    *,
    fire_and_forget: bool,
) -> str | None:
    """Send one commissioning command and return an operator-safe error if it failed."""
    result = await gateway.send_command(
        contact, command, timeout=30.0, await_response=not fire_and_forget
    )
    if not result.success and not fire_and_forget:
        return f"commission failed at {_redact_command(command)!r}: {result.error}"
    if not fire_and_forget and _looks_like_cli_error_response(result.text):
        return (
            f"commission failed at {_redact_command(command)!r}: "
            f"node replied {result.text!r}"
        )
    return None


async def _verify_commissioned_identity(
    gateway: Gateway,
    expected_pubkey: str,
    radio: tuple[float, float, int, int],
    progress: Callable[[str], Awaitable[None]] | None = None,
    *,
    attempts: int = 3,
    retry_delay: float = 5.0,
) -> str | None:
    """Confirm the node advertises with the generated identity after reboot/radio change."""
    found: list[str] = []
    for attempt in range(1, attempts + 1):
        if progress is not None:
            suffix = "" if attempts == 1 else f" ({attempt}/{attempts})"
            await progress(f"verifying identity{suffix}")
        discovered = await gateway.discover(factory_freq=radio, dwell=8.0)
        found = [str(node.get("pubkey", "")).lower() for node in discovered]
        if expected_pubkey.lower() in found:
            return None
        if attempt < attempts:
            await asyncio.sleep(retry_delay)
    short_found = ", ".join(pubkey[:12] for pubkey in found) or "none"
    return (
        "commission verification failed: expected new public key "
        f"{expected_pubkey[:12]}..., discovered {short_found}"
    )


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

async def discover_unmanaged(
    gateway: Gateway,
    db: Database,
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
    wq: WorkQueue,
    db: Database,
    config: Config,
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """
    Handle the 'commission' RPC request.

    Pauses the work queue, discovers unmanaged nodes, validates exactly one is
    found, then delegates to _commission_new or _commission_replace.
    The queue is always resumed in the finally block.
    """
    mode = req.get("mode")
    if mode not in ("new", "replace", "list_replace", "scan"):
        return {
            "ok": False,
            "error": f"mode must be 'new', 'replace', 'list_replace', or 'scan', got {mode!r}",
        }

    if mode == "list_replace":
        # Return config nodes that have a prv_key in secrets — no discovery needed.
        replaceable = [
            {"pubkey_prefix": nc.pubkey_prefix, "label": nc.label}
            for nc in config.nodes
            if nc.pubkey_prefix in config.secrets and config.secrets[nc.pubkey_prefix].prv_key
        ]
        return {"ok": True, "data": {"replaceable": replaceable}}

    async def _prog(msg: str) -> None:
        if progress is not None:
            await progress(msg)

    if mode == "scan":
        # Discover unmanaged nodes and return them with the gateway's home radio string.
        async with wq.commission_session() as gateway:
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

    async with wq.commission_session() as gateway:
        try:
            # Save home radio state before discover() hops and restores it.
            home_radio: tuple[float, float, int, int] = (
                float(gateway.info.get("radio_freq") or 0),
                float(gateway.info.get("radio_bw") or 0),
                int(gateway.info.get("radio_sf") or 0),
                int(gateway.info.get("radio_cr") or 0),
            )

            await _prog("switching to factory frequency...")
            await _prog("discovering factory nodes for about 8 seconds...")
            unknown = await discover_unmanaged(gateway, db, config)

            if len(unknown) == 0:
                return {
                    "ok": False,
                    "error": (
                        "no unmanaged nodes found - is it powered on, in range, with an antenna?"
                    ),
                }
            if len(unknown) > 1:
                pubkeys = ", ".join(n["pubkey"][:12] for n in unknown)
                return {
                    "ok": False,
                    "error": (
                        f"found {len(unknown)} unmanaged nodes — "
                        f"commission one at a time ({pubkeys})"
                    ),
                }

            node_info = unknown[0]
            await _prog(f"found node {node_info['pubkey'][:12]}")

            # discover() restored home freq; re-hop to factory so commands reach the node.
            await gateway.hop_radio(*Gateway.FACTORY_FREQ)
            try:
                # DISCOVER_RESPONSE does not add the node to the gateway's contact store.
                # add_commission_contact() inserts it with flood routing so send_command() succeeds.
                contact = await gateway.add_commission_contact(node_info)

                # discover() uses prefix_only=False so we have the full 32-byte pubkey.
                # Login is delegated to the per-mode function: 'new' uses the factory
                # password only; 'replace' falls back to the managed password from
                # secrets.yaml so it can recover from a previous partial commission.
                if mode == "new":
                    return await _commission_new(req, gateway, config, contact, _prog)
                return await _commission_replace(req, gateway, config, contact, _prog)
            finally:
                await gateway.restore_radio(*home_radio)

        except Exception as exc:
            log.exception("commission error: %s", exc)
            return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# commissioning sequences
# ---------------------------------------------------------------------------

async def _commission_new(
    req: dict[str, Any],
    gateway: Gateway,
    config: Config,
    contact: dict[str, Any],
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """
    Commission a brand-new node.

    Generates a MeshCore identity, sets that private key on the factory node,
    then applies password, name, and radio. The factory-generated identity is
    discarded so mch can print the private key needed for future replacement.
    """
    # Always set an admin password; fall back to 'password' if not provided.
    admin_password = req.get("admin_password") or "password"

    # Login with factory password first; if that fails, try the admin password
    # the operator supplied. A previous attempt may have got past the password
    # command before failing later (e.g. set prv.key/set radio timing out due
    # to session invalidation), leaving the node already on the new password.
    login_ok, login_error = await _login_for_new(gateway, contact, admin_password, progress)
    if not login_ok:
        return {"ok": False, "error": login_error}
    guest_password = req.get("guest_password")
    name = req.get("name") or req.get("label") or ""
    label = req.get("label") or ""
    group = req.get("group")
    target_freq_str = req.get("target_freq")

    prv_key = req.get("prv_key") or generate_private_key()
    pubkey = derive_public_key(prv_key)
    pubkey_prefix = pubkey[:8]

    # Commands in order: (command, fire_and_forget)
    # Password and identity changes go late because either may invalidate the
    # current admin session or contact identity on some firmware builds.
    # v1.15-era firmware replies to 'set radio' with OK and applies it on reboot.
    # Wait for that reply so the following no-wait reboot cannot race past it.
    cmds: list[tuple[str, bool]] = []
    if name:
        cmds.append((f"set name {name}", False))
    if guest_password:
        cmds.append((f"set guest.password {guest_password}", False))
    cmds.append((f"password {admin_password}", False))
    cmds.append((f"set prv.key {prv_key}", False))
    if target_freq_str:
        cmds.append((f"set radio {target_freq_str}", False))
    cmds.append(("reboot", True))

    _cmd_labels = {
        f"set prv.key {prv_key}": "setting generated private key",
        f"password {admin_password}": "setting password",
        f"set guest.password {guest_password}": "setting guest password",
        f"set name {name}": "setting name",
        f"set radio {target_freq_str}": "setting radio",
        "reboot": "rebooting",
    }
    for cmd, faf in cmds:
        if progress is not None:
            await progress(_cmd_labels.get(cmd, cmd))
        error = await _send_commission_command(
            gateway, contact, cmd, fire_and_forget=faf
        )
        if error:
            return {"ok": False, "error": error}

    verify_error = await _verify_commissioned_identity(
        gateway, pubkey, _parse_radio_tuple(target_freq_str), progress
    )
    if verify_error:
        return {"ok": False, "error": verify_error}

    config_snippet = _build_config_snippet(pubkey_prefix, label, group, target_freq_str)
    secrets_snippet = _build_secrets_snippet(
        pubkey_prefix, admin_password, guest_password, prv_key
    )

    log.info("commissioned new node %s label=%r", pubkey_prefix, label or name)
    return {
        "ok": True,
        "data": {
            "pubkey_prefix": pubkey_prefix,
            "public_key": pubkey,
            "label": label,
            "name": name,
            "group": group,
            "target_freq": target_freq_str,
            "config_snippet": config_snippet,
            "secrets_snippet": secrets_snippet,
        },
    }


async def _commission_replace(
    req: dict[str, Any],
    gateway: Gateway,
    config: Config,
    contact: dict[str, Any],
    progress: Callable[[str], Awaitable[None]] | None = None,
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

    # Login with factory password first; if that fails, try the managed password
    # from secrets so we can recover a node that was partially commissioned
    # earlier (admin password already changed, but radio/key not yet rebooted).
    login_ok, login_error = await _login_for_replace(gateway, contact, ns.password, progress)
    if not login_ok:
        return {"ok": False, "error": login_error}

    # Password and identity changes go late because either may invalidate the
    # current admin session or contact identity on some firmware builds.
    cmds: list[tuple[str, bool]] = []
    if ns.guest_password:
        cmds.append((f"set guest.password {ns.guest_password}", False))
    if ns.password:
        cmds.append((f"password {ns.password}", False))
    cmds.append((f"set prv.key {ns.prv_key}", False))
    if node_cfg.via:
        v = node_cfg.via
        cmds.append((f"set radio {v.freq:.3f},{v.bw},{v.sf},{v.cr}", False))
    cmds.append(("reboot", True))

    for cmd, faf in cmds:
        if progress is not None:
            label = cmd.split()[0] if " " in cmd else cmd
            await progress(f"{label}...")
        error = await _send_commission_command(
            gateway, contact, cmd, fire_and_forget=faf
        )
        if error:
            return {"ok": False, "error": error}

    log.info("replaced node identity for %s (%s)", pubkey_prefix, node_cfg.label or "")
    return {
        "ok": True,
        "data": {
            "pubkey_prefix": pubkey_prefix,
            "label": node_cfg.label,
        },
    }


# ---------------------------------------------------------------------------
# paste snippet builders — output displayed to operator for manual copy-paste
# ---------------------------------------------------------------------------

def _build_config_snippet(
    pubkey_prefix: str,
    label: str,
    group: str | None,
    target_freq_str: str | None,
) -> str:
    """Return a ready-to-paste config.yaml node entry (indented for under nodes:)."""
    lines = [f"  - pubkey_prefix: {pubkey_prefix}"]
    if label:
        lines.append(f"    label: {label}")
    if group:
        lines.append(f"    group: {group}")
    if target_freq_str:
        lines.append(f"    via: {target_freq_str}")
    return "\n".join(lines)


def _build_secrets_snippet(
    pubkey_prefix: str,
    password: str | None,
    guest_password: str | None,
    prv_key: str | None = None,
) -> str:
    """Return a ready-to-paste secrets.yaml node entry (indented for under nodes:)."""
    lines = [f"  {pubkey_prefix}:"]
    if password:
        lines.append(f"    password: {password}")
    if guest_password:
        lines.append(f"    guest_password: {guest_password}")
    if prv_key:
        lines.append(f"    prv_key: {prv_key}")
    return "\n".join(lines)
