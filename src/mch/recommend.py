"""Profile-based repeater settings recommendations."""

from __future__ import annotations

import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_BUNDLED = Path(__file__).parent / "data" / "profiles.yaml"
_CACHE = Path("/var/lib/meshherder/profiles.yaml")
_DEFAULT_URL = "https://raw.githubusercontent.com/rdmarsh/meshherder/main/src/mch/data/profiles.yaml"

_CACHE_MAX_AGE_SECONDS = 86400  # 24 hours


@dataclass
class Profile:
    name: str
    min_neighbours: int
    txdelay: str
    direct_txdelay: str
    description: str


@dataclass
class UniversalSetting:
    key: str
    value: str
    description: str


def parse_neighbour_count(raw: str) -> int:
    """Count neighbour entries from the `neighbors` command output."""
    return sum(
        1 for line in raw.splitlines()
        if line.strip() and line.strip() != ">"
    )


def get_profile(neighbour_count: int, profiles: list[Profile]) -> Profile:
    """Return the best-matching profile for a given neighbour count."""
    for p in sorted(profiles, key=lambda p: p.min_neighbours, reverse=True):
        if neighbour_count >= p.min_neighbours:
            return p
    return profiles[-1]


def load_profiles(
    profiles_url: str | None = None,
    profiles_file: Path | None = None,
) -> tuple[list[Profile], list[UniversalSetting]]:
    """Load profiles from file, URL cache, or bundled default.

    Resolution order:
    1. profiles_file if given (explicit operator override)
    2. Cache at /var/lib/meshherder/profiles.yaml if fresh (< 24h)
    3. profiles_url if given — fetch, write cache, use it
    4. Bundled default
    """
    raw: dict[str, Any] | None = None

    if profiles_file:
        raw = _load_yaml(profiles_file)

    if raw is None and _CACHE.exists():
        import time
        age = time.time() - _CACHE.stat().st_mtime
        if age < _CACHE_MAX_AGE_SECONDS:
            raw = _load_yaml(_CACHE)

    if raw is None and profiles_url:
        raw = _fetch_url(profiles_url)
        if raw is not None:
            try:
                _CACHE.parent.mkdir(parents=True, exist_ok=True)
                _CACHE.write_text(yaml.dump(raw))
            except OSError as exc:
                log.warning("could not write profiles cache: %s", exc)

    if raw is None:
        raw = _load_yaml(_BUNDLED)

    if raw is None:
        raise RuntimeError("could not load profiles — bundled file missing")

    return _parse(raw)


def _load_yaml(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning("could not load profiles from %s: %s", path, exc)
        return None


def _fetch_url(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = yaml.safe_load(resp.read().decode())
            return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning("could not fetch profiles from %s: %s", url, exc)
        return None


def _parse(raw: dict[str, Any]) -> tuple[list[Profile], list[UniversalSetting]]:
    profiles = [
        Profile(
            name=p["name"],
            min_neighbours=int(p["min_neighbours"]),
            txdelay=str(p["txdelay"]),
            direct_txdelay=str(p["direct_txdelay"]),
            description=p.get("description", ""),
        )
        for p in raw.get("profiles", [])
    ]
    universal = [
        UniversalSetting(
            key=u["key"],
            value=str(u["value"]),
            description=u.get("description", ""),
        )
        for u in raw.get("universal", [])
    ]
    return profiles, universal
