"""Profile-based repeater settings recommendations."""

from __future__ import annotations

import json
import logging
import os
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

# Community radio presets — the same list the official MeshCore app shows (regional freq/bw/sf/cr
# presets). This is the upstream source that the meshcore.ninja network catalog is itself generated
# from, so it is the canonical "what settings does my region use" reference. Overridable via
# MCH_RADIO_PRESETS_URL for self-hosting or tests.
MESHCORE_NZ_CONFIG_URL = "https://api.meshcore.nz/api/v1/config"


def community_radio_presets_url() -> str:
    return os.environ.get("MCH_RADIO_PRESETS_URL") or MESHCORE_NZ_CONFIG_URL


# Some upstreams (e.g. Cloudflare-fronted APIs like api.meshcore.nz) reject the default
# `Python-urllib` User-Agent with 403, so send an explicit one on every outbound fetch.
_USER_AGENT = "meshherder (+https://github.com/rdmarsh/meshherder)"


def _urlopen(url: str, timeout: float):  # type: ignore[no-untyped-def]
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 (caller passes https URL)


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


@dataclass
class RadioPreset:
    name: str
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
    raw = _load_profile_data(profiles_url=profiles_url, profiles_file=profiles_file)
    if raw is None:
        raise RuntimeError("could not load profiles — bundled file missing")

    return _parse(raw)


def load_radio_presets(
    profiles_url: str | None = None,
    profiles_file: Path | None = None,
) -> list[RadioPreset]:
    """Load named radio presets from the same profile source as recommendations."""
    raw = _load_profile_data(profiles_url=profiles_url, profiles_file=profiles_file)
    if raw is None:
        raise RuntimeError("could not load profiles — bundled file missing")

    presets = [
        RadioPreset(
            name=str(p["name"]),
            value=str(p["value"]),
            description=p.get("description", ""),
        )
        for p in raw.get("radio_presets", [])
    ]
    if presets:
        return presets

    # Backward compatibility for v0/unversioned profile files.
    for item in raw.get("universal", []):
        if item.get("key") == "radio":
            return [
                RadioPreset(
                    name="Profile radio",
                    value=str(item["value"]),
                    description=item.get("description", ""),
                )
            ]
    return []


def fetch_community_radio_presets(
    url: str | None = None, timeout: float = 5.0
) -> list[RadioPreset]:
    """Fetch community radio presets from the MeshCore app config API (api.meshcore.nz).

    Maps ``config.suggested_radio_settings.entries`` to :class:`RadioPreset`, where each
    preset's value is the ``freq,bw,sf,cr`` string mch uses everywhere (e.g. an Australia entry
    becomes ``"915.800,250,10,5"``). Returns an empty list on any network or parse failure, so
    callers can fall back to the bundled presets — commissioning must work offline.
    """
    url = url or community_radio_presets_url()
    try:
        with _urlopen(url, timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("could not fetch community radio presets from %s: %s", url, exc)
        return []

    entries: Any = []
    if isinstance(data, dict):
        entries = (
            data.get("config", {})
            .get("suggested_radio_settings", {})
            .get("entries", [])
        )
    presets: list[RadioPreset] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            value = (
                f"{entry['frequency']},{entry['bandwidth']},"
                f"{entry['spreading_factor']},{entry['coding_rate']}"
            )
        except (KeyError, TypeError):
            continue  # skip an incomplete entry rather than emit a malformed radio tuple
        presets.append(RadioPreset(
            name=str(entry.get("title") or value),
            value=value,
            description=str(entry.get("description", "")),
        ))
    return presets


def _load_profile_data(
    profiles_url: str | None = None,
    profiles_file: Path | None = None,
) -> dict[str, Any] | None:
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

    return raw


def _load_yaml(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning("could not load profiles from %s: %s", path, exc)
        return None


def _fetch_url(url: str) -> dict[str, Any] | None:
    try:
        with _urlopen(url, timeout=5) as resp:
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
