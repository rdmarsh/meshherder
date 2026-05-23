#!/usr/bin/env python3
"""
Bench experiment: why does `set prv.key` time out during commissioning?

Symptom from 2026-05-22 and 2026-05-23 live commissioning: every command
through `password <new>` replies normally; the very next mesh command
(`set radio` then, `set prv.key` now) times out with no response after 30s.

A first experiment (committed as the initial version of this script) refuted
the "password invalidates the session" hypothesis: `set name` worked fine
right after `password <new>` with no re-login. So the session is intact.

This revision tests the next two hypotheses:
  B. `set prv.key` is just slow/unreliable over the mesh on this firmware,
     independently of any preceding password change. Send it alone, after
     factory login, with a generous 90s timeout. If it eventually replies,
     a longer timeout in commissioning is the fix.
  C. The interaction between `password <new>` and a *following* `set prv.key`
     is the bug. After password change, re-login, then send `set prv.key`.
     If B succeeds but C times out, this points at a state issue post-password.

`set prv.key` stages a new private key that only applies on reboot. The
experiment never sends `reboot`, so the staged junk key never takes effect;
the bench node continues advertising on its current identity. The next real
commission attempt overwrites the staged key.

It restores the admin password to factory at the end regardless of outcome.

Usage (on the host with the gateway USB-attached, e.g. luna):
    sudo systemctl stop mchd       # daemon must release the serial port
    /opt/meshherder/.venv/bin/python scripts/bench_session_invalidation.py
    sudo systemctl start mchd

By default reads /etc/meshherder/config.yaml; pass a different path to override.

Phases:
  A. Baseline: factory login → `set name PHASE_A` → expect reply.
  B. Change password, do NOT re-login → `set name PHASE_B` → observe.
       Expected per hypothesis: timeout.
  C. Re-login with new password → `set name PHASE_C` → observe.
       Expected per hypothesis: reply.
  Cleanup. Reset admin pw to factory and confirm with factory re-login.

Direct serial check before and after via:
    meshcore-cli -r -s /dev/cu.usbserial-0001 get name
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from mch.config import load as load_config
from mch.gateway import Gateway
from mch.meshkey import derive_public_key, generate_private_key

FACTORY_PW = "password"
TEST_PW = "bench_temp_pw"

# 64-byte (128-hex-char) deterministic junk key. The staged key is never applied
# because we never send reboot, so this value never affects the node's identity.
JUNK_PRV_KEY = (
    "1111111111111111111111111111111111111111111111111111111111111111"
    "2222222222222222222222222222222222222222222222222222222222222222"
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("bench")


def _redact(command: str) -> str:
    if command.startswith("password "):
        return "password <redacted>"
    return command


async def send_and_observe(
    gw: Gateway, contact: dict[str, object], command: str, *, timeout: float = 30.0,
) -> bool:
    """Send a single mesh CLI command; log the outcome; return True iff a reply arrived."""
    label = _redact(command)
    log.info("[SEND] %s  (timeout=%.0fs)", label, timeout)
    result = await gw.send_command(contact, command, timeout=timeout, await_response=True)
    if result.success:
        log.info("[REPLY] %s -> %r", label, result.text)
        return True
    log.info("[NO REPLY] %s -> %s", label, result.error)
    return False


async def main(config_path: str) -> int:
    cfg = load_config(Path(config_path), load_secrets=False)
    gw = Gateway(cfg.gateway)

    log.info("connecting to gateway on %s", cfg.gateway.device)
    await gw.connect()

    home = (
        float(gw.info.get("radio_freq") or 0),
        float(gw.info.get("radio_bw") or 0),
        int(gw.info.get("radio_sf") or 0),
        int(gw.info.get("radio_cr") or 0),
    )
    log.info("home radio snapshot: %.3f / %g / SF%d / CR%d", *home)

    findings: dict[str, bool | None] = {
        "phase_a_baseline_set_name": None,
        "phase_b_set_prv_key_alone": None,
        "phase_c_set_prv_key_after_password": None,
        "phase_d_set_prv_key_real": None,
    }

    # Pre-generate a real key so we can log it before sending. If the node ever
    # reboots with this staged, it will adopt this identity; the operator can
    # recover by using the prv_key recorded here.
    real_prv_key = generate_private_key()
    real_pub_key = derive_public_key(real_prv_key)
    log.info("Phase D will use a real key. Record these in case of reboot:")
    log.info("  prv_key (full, 128 hex): %s", real_prv_key)
    log.info("  pubkey (derived):        %s", real_pub_key)

    try:
        log.info("hopping to factory frequency %s", Gateway.FACTORY_FREQ)
        await gw.hop_radio(*Gateway.FACTORY_FREQ)

        log.info("discovering factory nodes (dwell 8s)...")
        nodes = await gw.discover(dwell=8.0)
        if not nodes:
            log.error("[ABORT] no nodes found on factory frequency")
            return 2
        target = nodes[0]
        log.info("found %d node(s); using %s", len(nodes), target["pubkey"][:12])

        contact = await gw.add_commission_contact(target)

        log.info("logging in with factory password...")
        if not await gw.login(contact, FACTORY_PW):
            log.error("[ABORT] factory login failed; reset admin pw via USB first")
            return 3
        log.info("factory login OK")

        log.info("\n--- Phase A: baseline `set name` (control) ---")
        findings["phase_a_baseline_set_name"] = await send_and_observe(
            gw, contact, "set name PHASE_A"
        )

        log.info("\n--- Phase B: `set prv.key` alone, no password change, 90s timeout ---")
        findings["phase_b_set_prv_key_alone"] = await send_and_observe(
            gw, contact, f"set prv.key {JUNK_PRV_KEY}", timeout=90.0
        )

        log.info(
            "\n--- Phase C: `password <new>` then re-login then `set prv.key` (90s timeout) ---"
        )
        await send_and_observe(gw, contact, f"password {TEST_PW}")
        relogin_ok = await gw.login(contact, TEST_PW)
        log.info("re-login with TEST_PW: %s", relogin_ok)
        findings["phase_c_set_prv_key_after_password"] = await send_and_observe(
            gw, contact, f"set prv.key {JUNK_PRV_KEY}", timeout=90.0
        )

        log.info("\n--- Phase D: real generated `set prv.key`, 180s timeout ---")
        log.info("(staged only — we never send reboot, so identity does not apply)")
        findings["phase_d_set_prv_key_real"] = await send_and_observe(
            gw, contact, f"set prv.key {real_prv_key}", timeout=180.0
        )

    finally:
        # Cleanup is best-effort. If we lost the session entirely, the operator
        # can recover via USB (`password password`).
        log.info("\n--- Cleanup: reset admin pw to factory ---")
        try:
            await send_and_observe(gw, contact, f"password {FACTORY_PW}")
            # Some firmware paths may need a re-login to confirm. Best-effort.
            await gw.login(contact, FACTORY_PW)
        except Exception as exc:
            log.warning("cleanup error: %s — reset via USB if needed", exc)

        log.info("restoring home radio %.3f / %g / SF%d / CR%d", *home)
        try:
            await gw.restore_radio(*home)
        except Exception as exc:
            log.warning("home radio restore failed: %s", exc)
        await gw.disconnect()

    log.info("\n=== Findings ===")
    for k, v in findings.items():
        log.info("  %-25s : %s", k, "REPLY" if v else ("NO REPLY" if v is False else "skipped"))
    log.info(
        "\nVerify post-state on the USB side:\n"
        "    meshcore-cli -r -s /dev/cu.usbserial-0001 get name"
    )
    a = findings["phase_a_baseline_set_name"]
    b = findings["phase_b_set_prv_key_alone"]
    c = findings["phase_c_set_prv_key_after_password"]
    d = findings["phase_d_set_prv_key_real"]
    log.info("")
    if d is True:
        log.info("Phase D reply confirms a real `set prv.key` *does* return within 180s.")
        log.info("Action: raise the commission timeout for set prv.key (and likely set radio)"
                 " to ~180s and re-test live.")
    elif d is False:
        log.info("Phase D did not reply within 180s with a properly generated key.")
        log.info("Action: the response is genuinely lost, not just slow. Investigate further"
                 " (mesh routing, repeater queue, firmware bug).")
    if a and (b is not None) and (c is not None):
        if not b and not c:
            log.info("(Note: junk keys errored fast in both B and C, confirming validation"
                     " runs and the session is not blocked by password change.)")
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/etc/meshherder/config.yaml"
    sys.exit(asyncio.run(main(path)))
