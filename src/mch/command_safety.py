"""Safety classification for raw MeshCore CLI commands."""


BLOCKED_EXEC_COMMANDS = frozenset({
    # MeshCore docs mark `erase` as destructive factory reset.
    "erase",
    # Firmware update / identity / credential / ACL changes should go through
    # config or commissioning flows, not ad-hoc exec.
    "start ota",
    "set prv.key",
    "password",
    "set guest.password",
    "setperm",
    # Removing a blank neighbor prefix removes all neighbors.
    "neighbor.remove",
})

CONFIRM_EXEC_COMMANDS = frozenset({
    "reboot",
    "clkreboot",
    "clear stats",
    "log erase",
})

FIRE_AND_FORGET_COMMANDS = frozenset({
    "reboot",
    "clkreboot",
})


def normalize_command(command: str) -> str:
    """Return a lowercase command with collapsed whitespace for safety matching."""
    return " ".join(command.strip().lower().split())


def matched_command_prefix(command: str, candidates: frozenset[str]) -> str | None:
    """Return the candidate command matched by command, or None.

    A candidate matches either exactly or as a full-token prefix. For example,
    `set prv.key deadbeef` matches `set prv.key`, while `set prv.keyring x`
    does not.
    """
    normalized = normalize_command(command)
    for candidate in sorted(candidates, key=len, reverse=True):
        if normalized == candidate or normalized.startswith(candidate + " "):
            return candidate
    return None


def blocked_exec_command(command: str) -> str | None:
    """Return the blocked command prefix matched by a raw exec command."""
    return matched_command_prefix(command, BLOCKED_EXEC_COMMANDS)


def confirm_exec_command(command: str) -> str | None:
    """Return the confirmation-required command prefix matched by a raw exec command."""
    return matched_command_prefix(command, CONFIRM_EXEC_COMMANDS)


def fire_and_forget_command(command: str) -> str | None:
    """Return the fire-and-forget command prefix matched by a raw exec command."""
    return matched_command_prefix(command, FIRE_AND_FORGET_COMMANDS)
