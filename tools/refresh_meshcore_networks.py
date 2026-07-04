#!/usr/bin/env python3
"""Regenerate src/mch/data/networks.yaml from the MeshCore Ninja catalogue.

The catalogue at https://meshcore.ninja publishes a compiled ``data.json`` "for reuse" and
dedicates its data to the public domain (CC0 1.0). We vendor a small slice of it — the official
regional *radio presets* — into ``src/mch/data/networks.yaml`` so ``mch node new`` can offer them
at the radio prompt **without any runtime network call**. mch never fetches the catalogue itself;
a maintainer runs this tool occasionally and commits the refreshed file (same pattern as
profiles.yaml).

Usage:
    python tools/refresh_meshcore_networks.py            # fetch upstream, rewrite the vendored file
    python tools/refresh_meshcore_networks.py --from data.json   # use a local data.json instead

Upstream: https://github.com/meshcore-cz/meshcore-index (data/networks/<id>/network.yaml).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

DATA_JSON_URL = "https://meshcore.ninja/data.json"
OUT = Path(__file__).resolve().parents[1] / "src" / "mch" / "data" / "networks.yaml"
# Honest, descriptive identification — not a bot workaround.
_USER_AGENT = "meshherder-refresh (+https://github.com/rdmarsh/meshherder)"


def _num(value: object) -> str:
    """Render a number without a trailing .0 (250.0 -> '250', 62.5 -> '62.5')."""
    f = float(value)  # type: ignore[arg-type]
    return f"{f:g}"


def _coding_rate_denominator(raw: object) -> str:
    """MeshCore CLI wants the CR denominator digit; the catalogue stores '4/5'."""
    text = str(raw).strip()
    return text.split("/")[-1] if "/" in text else text


def _preset(network: dict) -> dict | None:
    radio = network.get("radio") or {}
    # Only the curated official app presets carry app_preset; skip community-specific networks.
    if not radio.get("app_preset"):
        return None
    try:
        freq = float(radio["frequency_mhz"])
        bw = _num(radio["bandwidth_khz"])
        sf = int(radio["spreading_factor"])
        cr = _coding_rate_denominator(radio["coding_rate"])
    except (KeyError, TypeError, ValueError):
        return None
    value = f"{freq:.3f},{bw},{sf},{cr}"
    countries = list((network.get("coverage") or {}).get("countries") or [])
    return {
        "name": str(radio["app_preset"]),
        "value": value,
        "countries": countries,
        "description": f"{freq:.3f} MHz / BW{bw} / SF{sf} / CR{radio['coding_rate']}",
    }


def load_data(source: str | None) -> dict:
    if source:
        return json.loads(Path(source).read_text())
    request = urllib.request.Request(DATA_JSON_URL, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310 (fixed https URL)
        return json.loads(resp.read().decode())


def render(presets: list[dict], schema_version: object) -> str:
    lines = [
        "# Regional radio presets for `mch node new`.",
        "#",
        "# Vendored from the MeshCore Ninja catalogue (https://meshcore.ninja/data.json), whose",
        "# data is dedicated to the public domain under CC0 1.0. Upstream source of truth:",
        "# https://github.com/meshcore-cz/meshcore-index (data/networks/<id>/network.yaml).",
        "#",
        "# This is a CACHED LOCAL COPY so mch never calls an API at runtime. Do not hand-edit —",
        "# regenerate with `python tools/refresh_meshcore_networks.py`; fixes belong upstream.",
        f"source_url: {DATA_JSON_URL}",
        f"upstream_schema_version: {schema_version}",
        f"generated_at: \"{datetime.now(UTC).replace(microsecond=0).isoformat()}\"",
        "presets:",
    ]
    for p in sorted(presets, key=lambda p: p["name"]):
        countries = "[" + ", ".join(p["countries"]) + "]" if p["countries"] else "[]"
        lines.append(f"  - name: \"{p['name']}\"")
        lines.append(f"    value: \"{p['value']}\"")
        lines.append(f"    countries: {countries}")
        lines.append(f"    description: \"{p['description']}\"")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="source", help="Local data.json instead of fetching upstream.")
    args = ap.parse_args()

    data = load_data(args.source)
    presets = [p for n in data.get("networks", []) if (p := _preset(n))]
    if not presets:
        print("no presets found in data.json — aborting", file=sys.stderr)
        return 1
    OUT.write_text(render(presets, data.get("schemaVersion", "?")))
    print(f"wrote {len(presets)} presets to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
