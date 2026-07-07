"""`mch help …` topic strings and their commands.

The topics teach the words every other command's --help assumes. Sourced from DESIGN.md
(selectors/concepts) and the NodeState docstrings (states), compressed to operator wording —
keep them free of internal jargon (see the banned-words guard test). Imported at the bottom of
``mch/cli/__init__.py`` so the commands attach to the already-defined ``help_group``.
"""

from __future__ import annotations

import click

from mch.cli import help_group

_SELECTORS_TOPIC = """\
NODE — how to name a node

Almost every command takes a NODE. You can point at one three ways:

  label          the local name you gave it (`mch node label`)
  pubkey prefix  the first few characters of its public key
  mesh name      part of the name it advertises on the mesh

What you type must match exactly one node, or mch asks you to be more
specific. The public key is the node's permanent identity — its label
and mesh name can change, its public key cannot."""

_CONCEPTS_TOPIC = """\
How mch thinks about your fleet

Desired vs reported
  You set a desired value for a setting; the node reports back the value
  it actually has. mch compares the two on every sync and, where you asked
  it to, brings the node into line.

Policy — what mch does about a setting
  enforce   write your desired value to the node
  audit     report drift, but don't change the node
  fact      just collect and show the node's current value
  skip      ignore this key

Scope — who a setting applies to
  Set a value on one node, on a group, or on the whole fleet, using the
  matching noun: `node set NODE ...`, `group set NAME ...`, `fleet set ...`.
  A node's own value beats its group's, which beats the fleet-wide value.
  (Scope is the noun; --all is different — it applies an *action* to the whole
  fleet, e.g. `node clean facts --all`.)

A sync cycle
  mch works through the nodes one at a time, reads the settings it cares
  about, and applies changes for anything under enforce. It is paced to be
  gentle on the mesh. `mch fleet sync` runs one now.

Previewing changes
  Commands that delete or change local state show a preview first and only
  act when you add --yes (`node prune`, `node clean`, `fleet import`,
  `node path --apply`). Interactive wizards (`node new`) and the
  typed-confirmation guard on destructive `node exec` commands are the
  exceptions.

Where your data lives
  mch keeps its inventory and desired settings in a local database.
  `mch fleet export` is your backup — keep it somewhere safe.

Exit codes
  0  success
  1  error
  2  issues present (`mch fleet issues`) — for cron and alerting.
     2 also = command-line usage error (bad arguments)."""

_STATES_TOPIC = """\
What the STATE column means

mch shows a state per node. The mesh is not a datacenter — one missed reply
does not mean a node is gone — so several states are just "we haven't heard
back yet", not faults.

  configured   reached, and everything you asked for matches.
  partial      reached, but some settings are still pending or failed.
  configuring  a sync is in flight — commands are on their way to the node.
  online       answered this cycle.
  offline      not seen for a while; no recent advert on the mesh.
  unreachable  mch tried to reach it on its last turn and got no answer. A
               solar node sleeping overnight shows unreachable until its next
               successful sync. `mch fleet issues` shows when it was last TRIED.
  unknown      never contacted yet — usually a freshly discovered node.
  retired      deliberately removed from management, kept for history.
               Bring it back with `mch node unretire`.

TRIES (in the drift table) counts write attempts for an enforced setting.
An unreachable node shows 0 tries because mch could not write yet; the drift
is corrected on the node's next successful sync."""


@help_group.command("selectors")
def help_selectors() -> None:
    """How to name a node on the command line."""
    click.echo(_SELECTORS_TOPIC)


@help_group.command("concepts")
def help_concepts() -> None:
    """Desired vs reported, policy, scope, sync, and exit codes."""
    click.echo(_CONCEPTS_TOPIC)


@help_group.command("states")
def help_states() -> None:
    """What each node STATE means (configured, partial, unreachable, …)."""
    click.echo(_STATES_TOPIC)
