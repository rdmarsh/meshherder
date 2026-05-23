#!/usr/bin/env python3
"""
Bench experiment: does `password <new>` over the mesh invalidate the gateway's
admin session on v1.15-era MeshCore firmware?

Symptom from 2026-05-23 live commissioning: factory login OK, `set name`,
`set guest.password`, `password <new>` all reply normally, then the very next
mesh command (`set prv.key` or `set radio`) times out with no response.

Hypothesis: the admin session bound to the previous password is invalidated
when the password changes. Without a re-login, the next command is silently
dropped. An explicit re-login with the new password should restore admin and
let subsequent commands through.

This script tests that hypothesis safely. It does NOT send `set prv.key`,
`set radio`, or `reboot`, so it cannot brick the node further. It probes the
behavior using `set name`, which is reversible. It restores the admin password
to factory at the end regardless of outcome.

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

import yaml

from mch.config import GatewayConfig
from mch.gateway import Gateway

FACTORY_PW = "password"
TEST_PW = "bench_temp_pw"

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
    cfg = yaml.safe_load(Path(config_path).read_text())
    gw_cfg = GatewayConfig(**(cfg.get("gateway") or {}))
    gw = Gateway(gw_cfg)

    log.info("connecting to gateway on %s", gw_cfg.device)
    await gw.connect()

    home = (
        float(gw.info.get("radio_freq") or 0),
        float(gw.info.get("radio_bw") or 0),
        int(gw.info.get("radio_sf") or 0),
        int(gw.info.get("radio_cr") or 0),
    )
    log.info("home radio snapshot: %.3f / %g / SF%d / CR%d", *home)

    findings: dict[str, bool | None] = {
        "phase_a_baseline": None,
        "phase_b_no_relogin": None,
        "phase_c_relogin": None,
    }

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

        log.info("\n--- Phase A: baseline (pre-password-change) ---")
        findings["phase_a_baseline"] = await send_and_observe(gw, contact, "set name PHASE_A")

        log.info("\n--- Phase B: change password, do NOT re-login ---")
        # Reduce timeout because we expect this to time out per the hypothesis.
        pw_replied = await send_and_observe(gw, contact, f"password {TEST_PW}")
        if not pw_replied:
            log.warning("password change did not reply; hypothesis already partial")
        log.info("(deliberately skipping re-login)")
        findings["phase_b_no_relogin"] = await send_and_observe(
            gw, contact, "set name PHASE_B", timeout=15.0
        )

        log.info("\n--- Phase C: re-login with new password, retry ---")
        relogin_ok = await gw.login(contact, TEST_PW)
        log.info("re-login with TEST_PW: %s", relogin_ok)
        findings["phase_c_relogin"] = await send_and_observe(gw, contact, "set name PHASE_C")

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
    # Exit 0 if the hypothesis holds clearly: Phase A reply, Phase B no reply, Phase C reply.
    if (findings["phase_a_baseline"] is True
            and findings["phase_b_no_relogin"] is False
            and findings["phase_c_relogin"] is True):
        log.info("\nResult: HYPOTHESIS CONFIRMED — password change invalidates session;"
                 " explicit re-login restores it.")
        return 0
    log.info("\nResult: pattern does not match the hypothesis cleanly — investigate further.")
    return 1


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/etc/meshherder/config.yaml"
    sys.exit(asyncio.run(main(path)))
