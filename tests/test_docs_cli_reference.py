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


def test_readme_cli_references_exist():
    _check("README.md", _MCH_RE)


def test_website_cli_references_exist():
    _check("www/index.html", _MCH_RE)


def test_man_page_cli_references_exist():
    _check("docs/man/mch.1", _MAN_RE)
