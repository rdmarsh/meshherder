# Recommendations Part 4 — scope-as-noun CLI redesign

A work plan for the CLI redesign agreed in discussion (rdm + AI, 2026-07-06), written — like
parts 1–3 — for another AI model or human to execute item by item. Baseline: **v0.22.0** line,
`main` at the commit carrying this file; `pytest` 998 passing, `ruff check .` clean, `mypy src`
clean.

**Problem being solved.** `mch config set/unset/show` is the odd one out. Post-sunset,
`config.yaml` holds only daemon settings — desired node/group/global state lives in the DB — so
`config set NODE …` writes to the DB while its name says "config file". Group scope is a
second-class `--group NAME` flag next to a node positional, even though the underlying scope
machinery is identical. And `daemon` is jargon.

**Core idea.** Make the desired-state *scope* the noun. The resolution hierarchy
Node > Group > Fleet maps onto three nouns, each with the same verbs:

```
              overview/detail    issues            desired state           sync           extras
fleet         fleet status       fleet issues      fleet set KEY VAL       fleet sync     export / import
group NAME    group status [N]   group issues N    group set N KEY VAL     group sync N   create / rename / delete
node  NAME    node  status  N    node  issues  N   node  set  N KEY VAL    node  sync  N  retire/prune/exec/probe/path/adopt/discover/label/move/new/…
```

Resolution reads straight off the grid: node beats group beats fleet. Resulting noun set:
`fleet` / `group` / `node` (scope, uniform verbs) + `gateway` + `service` + `help`.
The `config` noun is **removed entirely**.

## How to use this document (instructions for the executing agent)

- **Read `CLAUDE.md` first** and follow it: TDD (failing test first, confirm it fails for the
  expected reason), small verified slices, preserve debug logging and comments, record rationale
  in `JOURNAL.md`/`DESIGN.md`.
- **Branch policy** (operator preference): each slice is a branch + PR merged `--no-ff`; within
  a slice keep commits small and gates green.
- **Gates** for every PR: `pytest` (998 passing at baseline), `ruff check .`, `mypy src`. The
  help-text/banned-jargon/docs-drift guard tests must be *migrated*, never weakened.
- **No mesh traffic needed** for any slice. Everything here is CLI/daemon/DB surface.
- **Alpha rule (part 3 ground rule 1) applies in full:** when a command moves or renames,
  **delete the old path in the same change** — no deprecation aliases, no dual grammars. The
  single deliberate exception is S6's hidden `global` synonym, which is an operator-preference
  synonym recorded in DESIGN.md, not a compat shim.
- **Decisions below are made, not open.** If a decision proves wrong during implementation,
  stop and ask the operator; do not silently choose differently.
- **Coordination:** the facts-as-policy work (BACKLOG "Make `facts` a fourth policy", noted as
  active on a branch) touches the same CLI/config surface. Before starting S2+, ask the operator
  whether that branch should land first or be rebased after; do not race it.
- **RECOMMENDATIONS-2 slice 4** (`mch node add` onboarding pipeline) is still awaiting operator
  review and is *not* superseded here. Note that `node add` stays **reserved** for it — that is
  one reason the membership verb below is `node move`, not `node add`.
- This is a breaking release: next version is **0.23.0** when the sweep ships.

## Locked decisions (rdm + AI, 2026-07-06)

1. **Widest scope noun: `fleet`** (not `global`). `global` is an adjective and the grid only
   teaches "node beats group beats fleet" if the same three nouns appear in the grammar and the
   resolution rule. `fleet` already carries the flagship commands.
   **Deliberate non-change:** the DB keeps `scope_type = 'global'` in
   `desired_settings`/`desired_operations`. This rename is CLI surface only — do NOT migrate the
   stored enum value to `'fleet'`; that would be a data migration for zero operator benefit.
   (The word "global" in the schema is in fact where the synonym below comes from.)
2. **Hidden synonym: `mch global …` ≡ `mch fleet …`** (operator preference; a deliberate,
   recorded exception to the no-hidden-commands rule). Implement as a root-group `get_command`
   mapping (`"global"` resolves to the fleet group object), so every fleet verb aliases for
   free and it never appears in `list_commands`/`--help`/README. Record in DESIGN.md; pin with
   a test.
3. **Display verb: `status`**, one verb across all scopes (`fleet status`, `group status [N]`,
   `node status N [KEY]`). `node show` is renamed; `show` disappears from the vocabulary.
4. **Scope-status rule (uniform):** `<scope> status` = that scope's own desired rows + a rollup
   of its children. So `fleet status` = fleet-scope desired rows (compact section, rendered only
   when non-empty) + the node table + issues footer; `group status NAME` = the group's desired
   rows + member table; bare `group status` lists all groups with member counts (makes typo
   groups visible); `node status NODE [KEY]` absorbs `config show NODE [KEY]` and `node show`.
5. **Groups become first-class.** New `groups` registry table (named migration; seeded from
   existing `nodes.node_group` values ∪ group-scoped `desired_settings`/`desired_operations`
   scope_ids). `group create NAME` is required before `group set`/`node move` can target it —
   targeting an unknown group errors with a hint to `mch group create`. The commission wizard
   may still accept a new group name and create it (interactive confirmation is inherent there).
6. **`group delete NAME` cascades, hard.** Preview by default (list the member nodes and the
   group's desired rows), `--yes` to act: members become ungrouped (`node_group` NULL), the
   group's desired rows are deleted, the registry row is deleted. Hard delete, not retire-style
   soft delete: groups carry no observation history of their own (that is per-node);
   `fleet export` snapshots are the recovery path. Ungrouped is a fully supported state.
   `group rename OLD NEW` updates the registry row, `nodes.node_group`, and the `scope_id` of
   group-scoped desired rows in one transaction.
7. **Membership verb: `node move NODE GROUP`**, with the sentinel **`-`** to ungroup
   (`node move corbin -`), matching the commission wizard's existing "`-` for none". Node-side,
   not `group add GROUP NODE`: a node belongs to at most one group, so it is move semantics,
   not set membership — and `node add` is reserved (see above). Replaces `mch node group`;
   omitting the argument no longer clears the group (silent-ungroup footgun removed).
8. **Backup verbs: `fleet export` / `fleet import`** (not backup/restore). The verbs stay
   mechanics-honest — export prints YAML to stdout, import takes a file argument — and scripts /
   muscle memory only change the noun. Per-group/per-node export was considered and rejected
   (not worth the code). Import keeps preview-by-default + `--yes` and its preview must stay
   loud about what is overwritten (desired state + inventory metadata, never observations).
9. **Lifecycle noun: `service`** (`daemon` is jargon). `daemon validate` becomes
   **`service configtest`** (the nginx `-t` pattern — its reason to exist is testing the
   *edited file before reload* so a typo can't take the running daemon down). The daemon also
   **auto-validates config on start and on SIGHUP reload** (on invalid config: keep last valid
   config on reload, refuse to start on boot, log loudly).
10. **`fleet set` teaches resolution in its own output**: on success echo
    `fleet default KEY = VAL (groups and nodes may override)` — one line that re-teaches the
    model on every use, addressing the "does this force every node now?" misreading.
11. **Directives** (`name_prefix` / `default_policy` / `via_radio`) ride along unchanged:
    `group set deployed name_prefix "🦔 "` — same `validate_directive_value` validation, pinned
    by tests at their new spellings.
12. **Top-level shortcuts `mch status` / `mch sync` stay**, mapped to fleet scope. They are the
    most-used commands.
13. **S4.2 (the cli.py package split, deferred from part 3) happens AFTER this redesign**, along
    the final noun set — see S8. Do not split into `config_cmds.py`/`daemon_cmds.py` modules the
    redesign would immediately delete.

## Old → new command mapping (delete old in the same change)

| Old | New |
| --- | --- |
| `config set --global KEY VAL` | `fleet set KEY VAL` |
| `config set --group NAME KEY VAL` | `group set NAME KEY VAL` |
| `config set NODE KEY VAL` | `node set NODE KEY VAL` |
| `config unset …` (all three scopes) | `fleet unset` / `group unset NAME` / `node unset NODE` |
| `config show --global` | fleet-scope section of `fleet status` |
| `config show --group NAME` | `group status NAME` |
| `config show NODE [KEY]` | `node status NODE [KEY]` |
| `node show NODE` | `node status NODE` |
| `config export` / `config import` | `fleet export` / `fleet import` |
| `node group NODE [GROUP]` | `node move NODE GROUP` / `node move NODE -` |
| `daemon start/stop/restart/status/logs` | `service start/stop/restart/status/logs` |
| `daemon validate` | `service configtest` |
| `config` noun | removed entirely |
| — (new) | `group create NAME`, `group delete NAME`, `group rename OLD NEW`, `group status`, `group issues NAME`, `group sync NAME`, `node issues NODE` |

## Slices

### Slice 1 — groups become first-class (DB + daemon, no CLI renames yet)

1. Named migration adding `groups(name TEXT PRIMARY KEY, created_at TEXT)`; seed from
   `SELECT DISTINCT node_group FROM nodes WHERE node_group IS NOT NULL` ∪ distinct group
   `scope_id`s in `desired_settings`/`desired_operations`.
2. DB layer: `create_group`, `delete_group` (the cascade from decision 6, one transaction),
   `rename_group` (decision 6), `list_groups` (with member counts), `group_exists`.
3. Daemon verbs for the CLI to call; enforcement: setting desired state at group scope or
   assigning a node to a group **fails with a create hint** when the group is not registered.
   The commission wizard path may create-on-confirm.
4. Existing commands (`node group`, `config set --group`) gain the enforcement now, at their
   current spellings — behavior first, renames later. Tests: migration seeding (incl. a legacy
   DB fixture), cascade delete, rename transaction, unknown-group errors.

### Slice 2 — mechanical renames: `service`, `node status`, `fleet export/import`, `node move`

Pure renames, old paths deleted in the same change: `daemon`→`service` (all five verbs),
`daemon validate`→`service configtest`, `node show`→`node status`, `config export/import`→
`fleet export/import`, `node group`→`node move` with the `-` sentinel (and the omitted-argument
clears-group behavior removed). Update every in-app hint that names them (the daemon-down hint
at `cli.py:232-233`, `mch help concepts`, wizard epilogues at `cli.py:3463/3474`, `--help`
epilogs). Migrate the guard tests. No behavior changes beyond the sentinel.

### Slice 3 — auto-validate on start and reload

Daemon validates config on startup (refuse to start, loud error) and on SIGHUP reload (keep
last valid config, log the rejection) — the safety half of retiring `daemon validate`'s old
role; `service configtest` remains the pre-flight for an edited file. Tests for both paths.

### Slice 4 — the scope grammar: `set`/`unset` move to the scope nouns

`fleet set/unset KEY [VAL]`, `group set/unset NAME KEY [VAL]`, `node set/unset NODE KEY [VAL]`;
`config set`/`config unset` deleted; `_scope_from_flags` and the `--group`/`--global` flags on
these commands die. `fleet set` echoes the decision-10 line. Directives validate unchanged
(decision 11, pin with tests). RPC verbs may rename freely (alpha rule); migrate the socket
contract tests.

### Slice 5 — the status views + grid verbs

1. `node status NODE [KEY]` absorbs `config show NODE [KEY]` (desired rows fold into the node
   detail view; one view of everything about the node).
2. `group status` (bare = list groups with member counts; `NAME` = desired rows + members +
   rollup), replacing `config show --group`.
3. Fleet-scope desired rows render as a compact section of `fleet status`, only when non-empty,
   replacing `config show --global`. `--json` carries them in the report model (JSON-first rule
   from part 3 applies: one documented shape, renderer a pure function of it, `--json` verbatim).
4. Grid completions: `group sync NAME` (rate-limited like everything else — it is a filtered
   fleet sync), `group issues NAME`, `node issues NODE` (filtered views of the existing report).
5. `config show` and then the empty `config` group deleted — the noun is gone.

### Slice 6 — hidden `global` synonym

Root-group `get_command` maps `"global"` → the fleet group; absent from `list_commands`, help,
README, man page. One pinning test (`mch global status` ≡ `mch fleet status`; not listed in
`mch --help`). DESIGN.md records it as a deliberate exception to the no-hidden-commands rule,
with the operator-preference rationale.

### Slice 7 — shipped-artifact and docs sweep

1. `systemd/mch-backup.sh` (calls `mch config export` at line 23; restore hint in comments) —
   a stale script on a deployed Pi silently stops snapshotting after upgrade, so this is not
   optional.
2. README (add the scope × verb grid — it makes the model obvious), INSTALL, MANUAL_TESTS,
   DESIGN, ROADMAP, `mch.1` man page, `examples/`, CLAUDE.md command examples.
   JOURNAL/CHANGELOG history stays at point-in-time syntax.
3. `mch help concepts`/`selectors`/`states` re-read end-to-end for the new vocabulary.
4. Small addition while sweeping DESIGN.md: a short "Vendored catalogues" section recording the
   meshcore.ninja sources and licences (upstream `github.com/meshcore-cz/meshcore-index`;
   CC0 `data.json` → `src/mch/data/networks.yaml`; MIT `src/lib/repeaterCommands.js` →
   `src/mch/data/repeater_commands.json`; refresh via `tools/refresh_meshcore_*.py`; vendor
   locally, never fetch at runtime). Today this rationale lives only in BACKLOG and the tools'
   docstrings.
5. CHANGELOG the whole redesign under Unreleased; release as **0.23.0** when the operator says.

### Slice 8 — cli.py package split (the part-3 S4.2, re-scoped to the final nouns)

`src/mch/cli.py` (~3.7k lines) → `src/mch/cli/` package: `render.py`, `fleet.py`, `group.py`,
`node.py`, `service.py`, `gateway.py`, `wizard.py`, `help_topics.py`. Pure move, its own PR;
diff `mch --help` (all nouns) before/after; keep the help-text/banned-jargon/docs-drift guards
green.
