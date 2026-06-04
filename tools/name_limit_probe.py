#!/usr/bin/env python3
"""Throwaway bench experiment: find the stored node-name byte limit via CLI.

The locally-attached test node (dave_mac_test, v1.15.0) speaks the plain-text
MeshCore CLI over serial - the same `set name {value}` / `get name` path mch
uses on remote nodes. We set the name to a series of test strings, read each
back, and report sent-vs-stored bytes/chars. Captures and restores the
original name and coordinates.

Goal: confirm the stored cap (knowledge says 31 bytes; 32->31 on v1.14.1) and
learn how multi-byte emoji are truncated (byte cap vs char cap, mid-sequence
corruption), plus whether having a location set changes the stored name cap.
"""

from __future__ import annotations

import argparse
import time

import serial


class CLI:
    def __init__(self, port: str, baud: int) -> None:
        self.s = serial.Serial(port, baud, timeout=1)
        time.sleep(0.5)
        self.s.reset_input_buffer()

    def cmd(self, text: str) -> str:
        """Send a CLI command (UTF-8) and return the response payload text."""
        self.s.reset_input_buffer()
        self.s.write(text.encode("utf-8") + b"\r\n")
        time.sleep(0.8)
        raw = self.s.read(8000)
        # Firmware echoes the command line then replies with '  -> <payload>'.
        out = raw.decode("utf-8", "replace")
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        for ln in lines:
            if ln.startswith("->"):
                return ln[2:].strip()
        return lines[-1] if lines else ""

    def get_name(self) -> str:
        r = self.cmd("get name")
        # 'get name' replies '> <name>'
        return r[1:].strip() if r.startswith(">") else r

    def close(self) -> None:
        self.s.close()


def describe(label: str, sent: str, stored: str) -> None:
    sent_b = sent.encode("utf-8")
    stored_b = stored.encode("utf-8")
    flag = "OK " if sent == stored else ">> DIFF"
    print(
        f"{flag} {label}: sent {len(sent)}ch/{len(sent_b)}B -> "
        f"stored {len(stored)}ch/{len(stored_b)}B  stored={stored!r}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="/dev/cu.usbserial-0001")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--test-location", action="store_true")
    args = ap.parse_args()

    cli = CLI(args.serial, args.baud)
    try:
        orig_name = cli.get_name()
        orig_lat = cli.cmd("get lat")
        orig_lon = cli.cmd("get lon")
        print(f"original name={orig_name!r} lat={orig_lat!r} lon={orig_lon!r}")
        print(f"ver={cli.cmd('ver')!r}")
        print("=" * 64)

        def probe(label: str, name: str) -> str:
            cli.cmd(f"set name {name}")
            stored = cli.get_name()
            describe(label, name, stored)
            return stored

        for n in (29, 30, 31, 32, 33, 40):
            probe(f"ascii x{n}", "A" * n)

        probe("emoji 1x4B (rocket)", "\U0001f680")
        probe("emoji 7x4B=28B", "\U0001f680" * 7)
        probe("emoji 8x4B=32B", "\U0001f680" * 8)
        probe("emoji 9x4B=36B", "\U0001f680" * 9)
        probe("prefix rocket + Repeater01", "\U0001f680Repeater01")
        # 30 ascii + 4B emoji = 34B: does it cut mid-emoji (corruption) or drop it?
        probe("30B ascii + 1x4B emoji", "A" * 30 + "\U0001f680")
        # 28B ascii + 4B emoji = 32B: emoji exactly at the 31/32 boundary.
        probe("28B ascii + 1x4B emoji", "A" * 28 + "\U0001f680")

        if args.test_location:
            print("-" * 64)
            print("setting location, then retest caps")
            cli.cmd("set lat 51.5")
            cli.cmd("set lon -0.1")
            print(f"   lat={cli.cmd('get lat')!r} lon={cli.cmd('get lon')!r}")
            probe("ascii x32 (with location)", "A" * 32)
            probe("ascii x40 (with location)", "A" * 40)
            probe("emoji 8x4B=32B (with location)", "\U0001f680" * 8)

        print("=" * 64)
        cli.cmd(f"set name {orig_name}")
        if args.test_location:
            cli.cmd(f"set lat {orig_lat}")
            cli.cmd(f"set lon {orig_lon}")
        print(f"restored name={cli.get_name()!r} "
              f"lat={cli.cmd('get lat')!r} lon={cli.cmd('get lon')!r}")
        return 0
    finally:
        cli.close()


if __name__ == "__main__":
    raise SystemExit(main())
