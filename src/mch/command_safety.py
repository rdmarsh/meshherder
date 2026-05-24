"""Safety classification for raw MeshCore CLI commands."""

from mch.command_registry import (
    blocked_command_prefixes,
    confirm_command_prefixes,
    fire_and_forget_prefixes,
    normalize_command,
)

BLOCKED_EXEC_COMMANDS = blocked_command_prefixes()
CONFIRM_EXEC_COMMANDS = confirm_command_prefixes()
FIRE_AND_FORGET_COMMANDS = fire_and_forget_prefixes()


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
