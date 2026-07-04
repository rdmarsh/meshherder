"""Advisory firmware/command compatibility catalogue.

Answers one narrow question: "was this MeshCore CLI command available in the firmware version this
node is running?" It exists purely to *warn* an operator before `mch node exec` sends a command a
node is too old to understand — it never blocks, never gates enforcement, and stays silent when it
cannot tell. The node may have been upgraded past what our vendored snapshot knows, and the operator
knows more than we do; the catalogue is only a hint (see BACKLOG "command × firmware-version
compatibility").

Data comes from ``data/repeater_commands.json``, vendored from the MeshCore Ninja cheat sheet (MIT)
by ``tools/refresh_meshcore_commands.py``. It is a cached local copy: no runtime network call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_BUNDLED = Path(__file__).parent / "data" / "repeater_commands.json"

# command_support() return values.
SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
UNKNOWN = "unknown"

# A firmware `ver` reply looks like "v1.14.1-467959c (Build: Jun 6 2026)"; we want the release only.
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*[a-z]?)")


@dataclass
class CommandCatalog:
    """A loaded, queryable command-availability catalogue.

    ``version_order`` is every tracked ``repeater-*`` release oldest -> newest; availability is
    compared by *position in that list*, not numeric parsing, so prerelease suffixes like
    ``v1.0.0a`` order correctly (matching MeshCore Ninja's own ``cmpVersion``).
    """

    version_order: list[str]
    current_version: str
    base_version: str
    commands: dict[str, dict[str, Any]]

    def _match_command(self, command: str) -> dict[str, Any] | None:
        """Return the catalogue entry for an exec command, or None if unknown.

        Matches the *longest* catalogue key the command equals or begins with (key + " "), so
        ``clock sync`` beats ``clock`` and ``set name faulkland`` resolves to ``set name`` while a
        near-miss like ``getacl`` (no word boundary) does not false-match ``get acl``.
        """
        normalised = " ".join(command.split())
        if not normalised:
            return None
        best_key: str | None = None
        for key in self.commands:
            if normalised == key or normalised.startswith(key + " "):
                if best_key is None or len(key) > len(best_key):
                    best_key = key
        return self.commands[best_key] if best_key is not None else None

    def support(self, command: str, node_ver: str | None) -> str:
        """Classify a command against a node's firmware ``ver`` as supported/unsupported/unknown.

        UNKNOWN (stay silent) whenever we cannot be confident: unrecognised command, missing or
        unparseable version, or a release not in our vendored ``version_order``.
        """
        entry = self._match_command(command)
        if entry is None:
            return UNKNOWN

        node_release = _normalise_version(node_ver)
        if node_release is None or node_release not in self.version_order:
            return UNKNOWN

        # ``added_in`` absent (null) means "present since the baseline release" -> always available.
        added_in = entry.get("added_in") or self.base_version
        if added_in not in self.version_order:
            return UNKNOWN

        if self.version_order.index(node_release) >= self.version_order.index(added_in):
            return SUPPORTED
        return UNSUPPORTED


def _normalise_version(raw: str | None) -> str | None:
    """Extract a bare ``vX.Y.Z`` release from a firmware ``ver`` reply, or None if unparseable.

    Strips the build suffix ("v1.14.1-467959c (Build: …)" -> "v1.14.1") and adds the leading ``v``
    the catalogue uses if firmware omitted it.
    """
    if not raw:
        return None
    m = _VERSION_RE.match(raw.strip())
    if not m:
        return None
    return "v" + m.group(1)


def load_command_catalog(path: Path | None = None) -> CommandCatalog:
    """Load the vendored command catalogue (bundled snapshot by default).

    Raises RuntimeError if the file is missing or malformed — callers that only want an advisory
    warning should catch it and skip the check rather than fail the operation.
    """
    src = path or _BUNDLED
    try:
        raw = json.loads(src.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"could not load command catalogue from {src}: {exc}") from exc

    version_order = raw.get("version_order")
    commands = raw.get("commands")
    if not isinstance(version_order, list) or not isinstance(commands, dict):
        raise RuntimeError(f"command catalogue {src} is malformed")

    return CommandCatalog(
        version_order=[str(v) for v in version_order],
        current_version=str(raw.get("current_version", "")),
        base_version=str(raw.get("base_version", version_order[0] if version_order else "")),
        commands=commands,
    )


def command_support(
    command: str, node_ver: str | None, catalog: CommandCatalog | None = None
) -> str:
    """Convenience wrapper: classify a single command using the bundled catalogue.

    Any load failure degrades to UNKNOWN so an advisory check can never break the caller.
    """
    try:
        cat = catalog or load_command_catalog()
    except RuntimeError as exc:
        log.debug("command catalogue unavailable, skipping compatibility check: %s", exc)
        return UNKNOWN
    return cat.support(command, node_ver)
