#!/usr/bin/env python3
"""Regenerate src/mch/data/repeater_commands.json from the MeshCore Ninja cheat sheet.

MeshCore Ninja (https://meshcore.ninja) already tracks, for every MeshCore *repeater* CLI command,
the first firmware version it appeared in (`addedIn`). They derived it from the firmware CLI source
(`meshcore-dev/MeshCore` `src/helpers/CommonCLI.cpp` + `examples/simple_repeater/MyMesh.cpp`) across
every `repeater-*` release, so we do **not** parse C++ ourselves — we consume their compiled result
and re-vendor it. Unlike the CC0 `data.json` catalogue, this lives in the app **source**, which is
MIT-licensed: attribute *MeshCore Ninja*.

The source module (`src/lib/repeaterCommands.js`) is an ES module exporting `COMMAND_GROUPS`,
`VERSION_ORDER`, `CURRENT_VERSION` and `BASE_VERSION`. We import it with Node (no C++ parsing) and
flatten it into `src/mch/data/repeater_commands.json` so mch can warn — at `mch node exec` time —
when a command predates a node's firmware, **with no runtime network call**. mch never fetches this;
a maintainer runs this tool occasionally and commits the refreshed file (same pattern as
networks.yaml / profiles.yaml).

Usage:
    python tools/refresh_meshcore_commands.py                     # fetch upstream, rewrite vendored
    python tools/refresh_meshcore_commands.py --from cmds.js      # use a local copy instead

Upstream: https://github.com/meshcore-cz/meshcore-index (src/lib/repeaterCommands.js).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

SOURCE_URL = (
    "https://raw.githubusercontent.com/meshcore-cz/meshcore-index/main/src/lib/repeaterCommands.js"
)
OUT = Path(__file__).resolve().parents[1] / "src" / "mch" / "data" / "repeater_commands.json"
# Honest, descriptive identification — not a bot workaround.
_USER_AGENT = "meshherder-refresh (+https://github.com/rdmarsh/meshherder)"

# A tiny Node bridge: import the ES module and print its relevant exports as JSON on stdout. We do
# this instead of parsing the JS ourselves because the module is the maintained source of truth and
# its shape (nested COMMAND_GROUPS with get/set flags) is awkward to regex.
_NODE_BRIDGE = """
import * as m from {module_url};
process.stdout.write(JSON.stringify({{
  command_groups: m.COMMAND_GROUPS,
  version_order: m.VERSION_ORDER,
  current_version: m.CURRENT_VERSION,
  base_version: m.BASE_VERSION,
}}));
"""


def fetch_source(source: str | None) -> str:
    if source:
        return Path(source).read_text()
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310 (fixed https URL)
        return resp.read().decode()


def extract(js_source: str) -> dict:
    """Run the module through Node and return its parsed exports."""
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "repeaterCommands.mjs"
        module.write_text(js_source)
        bridge = Path(tmp) / "bridge.mjs"
        bridge.write_text(_NODE_BRIDGE.format(module_url=json.dumps(module.as_uri())))
        result = subprocess.run(
            ["node", str(bridge)],
            capture_output=True,
            text=True,
            check=True,
        )
    return json.loads(result.stdout)


def flatten_commands(command_groups: list[dict]) -> dict[str, dict]:
    """Collapse the nested groups into one {invocation: {added_in, deprecated_at}} map.

    Each catalogue command is either a bare `cmd` (e.g. `reboot`, `clock sync`, `get acl`) or a
    get/set `name` setting (e.g. `radio`, `path.hash.mode`). We key by the exact string an operator
    types: the `cmd` verbatim, and `get <name>` / `set <name>` for settings that expose those forms.
    `command_support` in mch matches an exec command against these keys.
    """
    commands: dict[str, dict] = {}

    def record(invocation: str, entry: dict) -> None:
        record_entry: dict = {
            "added_in": entry.get("addedIn"),  # None => present since base_version
            "deprecated_at": entry.get("deprecatedAt"),
        }
        # Carry the upstream flags (serial=USB-only, reboot=takes effect after reboot,
        # build=compile-time gated, destructive). mch acts on serial/reboot; the rest are stored
        # for completeness. Only when present, to keep the vendored file tidy.
        flags = entry.get("flags")
        if flags:
            record_entry["flags"] = list(flags)
        commands[invocation] = record_entry

    for group in command_groups:
        for entry in group.get("commands", []):
            cmd = entry.get("cmd")
            name = entry.get("name")
            if cmd:
                record(cmd, entry)
            elif name:
                if entry.get("get"):
                    record(f"get {name}", entry)
                if entry.get("set"):
                    record(f"set {name}", entry)
    return commands


def build(data: dict) -> dict:
    return {
        "_comment": (
            "Vendored from MeshCore Ninja (https://meshcore.ninja), src/lib/repeaterCommands.js "
            "(MIT). CACHED LOCAL COPY — mch never fetches at runtime. Do not hand-edit; regenerate "
            "with `python tools/refresh_meshcore_commands.py`. Fixes belong upstream."
        ),
        "source_url": SOURCE_URL,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "current_version": data["current_version"],
        "base_version": data["base_version"],
        "version_order": data["version_order"],
        "commands": flatten_commands(data["command_groups"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="source", help="Local repeaterCommands.js instead of upstream.")
    args = ap.parse_args()

    try:
        js_source = fetch_source(args.source)
        data = extract(js_source)
    except subprocess.CalledProcessError as exc:
        print(f"node extraction failed: {exc.stderr or exc}", file=sys.stderr)
        return 1

    out = build(data)
    if not out["commands"]:
        print("no commands extracted — aborting", file=sys.stderr)
        return 1
    OUT.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(out['commands'])} commands ({len(out['version_order'])} versions) to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
