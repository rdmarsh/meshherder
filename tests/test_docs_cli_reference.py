"""Docs-drift guard: every `mch <group> <command>` named in the hand-maintained docs must exist.

README.md, the man page, and the website each restate the command surface by hand, which is how the
post-sunset drift (RECOMMENDATIONS B2/B3) crept in. This test parses the group/command pairs out of
those docs and asserts each one resolves in the real click app (hidden deprecated aliases count), so
a renamed/removed command can't silently outlive its documentation. See RECOMMENDATIONS M5.
"""

from __future__ import annotations

import re
from pathlib import Path

import click

from mch.cli import cli

_ROOT = Path(__file__).resolve().parents[1]


def _valid_pairs() -> set[tuple[str, str]]:
    """All (group, command) pairs the CLI accepts, including hidden back-compat aliases."""
    pairs: set[tuple[str, str]] = set()
    for group_name, group in cli.commands.items():
        if isinstance(group, click.Group):
            for cmd_name in group.commands:
                pairs.add((group_name, cmd_name))
    return pairs


_GROUPS = {name for name, cmd in cli.commands.items() if isinstance(cmd, click.Group)}

# `mch <group> <command>` in prose/code (README, website). The command token stops at the first
# non-command character; option/placeholder tokens (starting with - or <) are skipped by the guard.
_MCH_RE = re.compile(r"\bmch\s+(" + "|".join(sorted(_GROUPS)) + r")\s+([A-Za-z][\w-]*)")
# `.B <group> <command>` in the roff man page.
_MAN_RE = re.compile(
    r"^\.B\s+(" + "|".join(sorted(_GROUPS)) + r")\s+([A-Za-z][\w-]*)", re.MULTILINE
)


def _pairs_in(text: str, pattern: re.Pattern[str]) -> set[tuple[str, str]]:
    return {(m.group(1), m.group(2)) for m in pattern.finditer(text)}


def _check(doc: str, pattern: re.Pattern[str]) -> None:
    text = (_ROOT / doc).read_text()
    valid = _valid_pairs()
    referenced = _pairs_in(text, pattern)
    assert referenced, f"no `mch <group> <command>` references found in {doc} — parser broke?"
    unknown = sorted(p for p in referenced if p not in valid)
    assert not unknown, (
        f"{doc} documents commands that do not exist in the CLI: "
        + ", ".join(f"mch {g} {c}" for g, c in unknown)
    )


# Words that speak implementer, not operator. They stay fine in DESIGN.md and code comments, but
# must not appear in any command's --help output (RECOMMENDATIONS-2 S1.1). \bDB\b catches the bare
# abbreviation while allowing "database".
_BANNED = re.compile(
    r"\bDB\b|source of truth|sunset|eviction|policy-only override|projection",
    re.IGNORECASE,
)


def _all_commands(
    group: click.Group, prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], click.Command]]:
    """Every (path, command) pair in the tree, groups included, deduping hidden aliases by id."""
    found: list[tuple[tuple[str, ...], click.Command]] = []
    seen: set[int] = set()
    for name, cmd in group.commands.items():
        path = (*prefix, name)
        if id(cmd) not in seen:
            seen.add(id(cmd))
            found.append((path, cmd))
        if isinstance(cmd, click.Group):
            found.extend(_all_commands(cmd, path))
    return found


def test_no_operator_jargon_in_help_output():
    """No banned implementer jargon in any command's --help output (the S1.1 rewrite)."""
    from click.testing import CliRunner

    runner = CliRunner()
    offenders: list[str] = []
    for path, _cmd in _all_commands(cli):
        result = runner.invoke(cli, [*path, "--help"])
        assert result.exit_code == 0, f"mch {' '.join(path)} --help failed"
        for match in _BANNED.finditer(result.output):
            offenders.append(f"mch {' '.join(path)}: {match.group(0)!r}")
    assert not offenders, "operator jargon leaked into --help output:\n" + "\n".join(offenders)


def test_readme_cli_references_exist():
    _check("README.md", _MCH_RE)


def test_website_cli_references_exist():
    _check("www/index.html", _MCH_RE)


def test_man_page_cli_references_exist():
    _check("docs/man/mch.1", _MAN_RE)


def test_mchd_man_page_has_no_retired_http_api():
    """The HTTP API was retired in 0.22.0; the daemon man page must not resurrect it (S5.1).

    Retired daemon surface should fail the same way retired CLI surface already does, so a
    future edit that re-documents the `api:` block or an HTTP listener trips a test.
    """
    text = (_ROOT / "docs/man/mchd.8").read_text()
    lowered = text.lower()
    # No `api:` config block and no HTTP endpoints — the enabling surface, not the phrase
    # "there is no HTTP API" (which is the correct thing to say).
    assert "api:" not in lowered
    assert "read-only api" not in lowered
    for endpoint in ("/status", "/health", "/issues", "/plan", "/logs"):
        assert endpoint not in text
