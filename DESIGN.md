# mch — Design Notes

This document explains the design decisions behind mch for contributors and operators who want
to understand why things work the way they do.

## Source of truth: managing the daemon vs. managing the mesh

There is one bright line that decides where every piece of configuration lives:

> **Managing the daemon lives in `config.yaml`. Managing the mesh lives in the DB (the CMDB side).**

- **`config.yaml`** — how *this daemon instance* runs and connects: the gateway it talks to, where
  its files are, how it paces itself, which features it exposes. Operator-owned, version-controllable,
  per-deployment. mchd never writes to it.
- **The SQLite DB** — the fleet itself: inventory, desired state (settings, directives, facts,
  operations), and the observed state / history mch records. This is the CMDB. Edited by `mch`
  commands (and, much later, possibly a web frontend), never by hand-editing YAML.
- **`secrets.yaml`** — passwords and private keys, kept a 0600 file deliberately out of the DB (any
  future read UI reads the DB; secrets must not be there). Operator-owned.

**The test for "config or DB?":** *could two different daemons managing the same fleet legitimately
hold different values?* If yes, it is per-daemon and belongs in `config.yaml` (a different gateway
device, socket path, or sync cadence). If no — it is a property of the fleet — it belongs in the DB.

| Layer | Owns | Written by |
|---|---|---|
| `config.yaml` | Daemon runtime + bootstrap: `gateway`, `scheduler`, `messenger`, paths (`db_path`/`socket_path`/`secrets_file`), `profiles_*` | Operator |
| `secrets.yaml` | Passwords, private keys (0600) | Operator |
| SQLite DB | Mesh/CMDB: inventory, desired state (settings, directives, facts, operations), observed state, history, schedule | mch / mchd |

Two consequences fall out of the line:
- **Bootstrap can't live in the DB.** The daemon reaches the DB-managed fleet *through* the gateway,
  and it must know where the DB and socket are before opening them — so gateway connection config and
  file paths are irreducibly `config.yaml`.
- **`scheduler` is the one judgement call.** Pacing ("how hard to work the mesh") is policy-ish and
  could be imagined as a DB global, but it is daemon-global behaviour, not node intent, so it stays in
  `config.yaml` to keep the line clean. (`sync_interval_days` should also become hours — see BACKLOG.)

The desired-state keys `nodes:`/`groups:`/`global:`/`facts:` are still *accepted* by the parser only
as the `mch config import` on-ramp; the daemon strict-loads and errors if any is present (empty is tolerated),
pointing at `mch config import`. Dropping them from the parser entirely is a later cleanup, once no legacy
configs remain to import.

### Historical: the earlier YAML-first model (superseded 2026-06-17)

mch originally made `config.yaml` the source of truth for *desired* state, with the DB holding only
observed state — the Ansible model (declare intent in version-controlled files; let the tool record
what it observed). The sunset (see below) moved inventory and desired state into the DB so they can be
edited by `mch` (and any future UI) rather than round-tripped through YAML. The reload machinery still applies to
the daemon settings that remain: mchd watches `config.yaml`/`secrets.yaml`, reloads shortly after a
save without dropping the gateway connection, and ignores invalid YAML (keeping the last valid config).

## Desired state in the DB (sunset complete 2026-06-17, started 2026-06-11)

We are moving desired state from config.yaml into the SQLite DB so it can be edited with `mch`
commands and, much later, possibly a web frontend. A web UI round-tripping a YAML file is
fragile; a UI writing to a DB is natural. Git history of intent is an acceptable loss here:
mch targets a single hobbyist box, not a corporate fleet, and retirement history (below) replaces
the practice of keeping old config.yaml copies.

**The problem this solves.** There were three stores that drift: config.yaml (desired), the DB
(observed + a cache of desired), and the gateway's favourited contacts. Editing YAML then waiting
to see whether the daemon read it is opaque, because the slow mesh hides whether *intent* even
landed. With desired state in the DB, `mch config set` writes synchronously, so `mch node` shows the new
desired value immediately - "intent recorded" stops being conflated with the slow "node converged".

**Stores collapse from three to two:**
- **DB** - authoritative for inventory + desired state, plus the observed state it already held.
- **Gateway favourites** - a *downstream mirror* of the DB managed set, not a source. The daemon
  favourites every managed node (eviction protection: an evicted contact cannot be addressed at
  all, and Sydney already runs ~440 repeaters) and unfavourites any contact not in the DB.
  Unfavouriting only clears the eviction pin; it does not delete the contact, and it is a local
  gateway write, not mesh traffic. Reading favourites back *as inventory* is dropped - `mch
  contacts` already covers "what is visible on the mesh".
- **secrets.yaml stays a file.** Passwords and private keys must not live in a DB that any future
  read UI would read. File mode 0600 is a
  clear secret boundary; the DB has no equivalent. "config.yaml goes away" and "secrets.yaml goes
  away" are independent decisions; the second is argued against for now.

**Schema.** Desired state is *layered* (global/group/node) but observed state is *per node only*.
Today's `settings` table conflated the two; the redesign splits them:
- `desired_settings(scope_type, scope_id, key, desired, policy, poll_hours)`, primary key
  `(scope_type, scope_id, key)`. `scope_type` is `global|group|node`; `scope_id` is `''` for global
  (a non-NULL empty string, to avoid SQLite treating NULLs in a primary key as distinct), the group
  name for group scope, or the node pubkey for node scope. `desired = NULL` is a policy-only
  override; `policy`/`poll_hours = NULL` mean "inherit".
- `observed_settings` - per node: `observed, last_checked, last_set, update_attempts, drift`
  (today's `settings` minus the desired columns).
- Scope-level attributes (`default_policy`, `name_prefix`, `via_radio`) live on the `nodes`/`groups`
  rows and a `globals` singleton for now.

**Uniformity.** Every key is settable at every scope - the `(scope_type, scope_id, key)` shape has
no per-scope whitelist, so e.g. `via_radio` on global "just works" even where it is redundant.
This deliberately reduces operator confusion. `via_radio`, `name_prefix`, and `default_policy` are
mch-internal directives (they shape resolution; they are never `get`/`set` on the node and have no
observed value), not node CLI settings. The clean end state is to store them as reserved keys in
`desired_settings` too, so there are no special scope columns at all; that is a later step.

**Resolution is unchanged.** `resolve_desired()` / `resolve_policy()` / `all_setting_keys()` keep
operating on `NodeConfig` / `GroupConfig` dataclasses. The migration builds those same dataclasses
from DB rows instead of parsed YAML, so the node > group > global precedence and the 9-step policy
cascade are untouched. During the transition, when a key exists both in the DB and still in
config.yaml, **the DB wins and the daemon warns** so the now-ignored YAML value can be cleaned up.

**Retire, don't delete.** *(Implemented 8g.)* A node has a `NodeState.RETIRED` lifecycle state,
replacing kept copies of config.yaml as the historical record. `mch node retire <node>` sets it,
unfavourites the contact (eviction-pin removal, not contact deletion), and writes a log line;
`mch node unretire <node>` reverses it (re-favourite, reset state so it reconverges). A retired node is
excluded from sync everywhere (`get_due_node`, the cycle sweep, reschedule, and the favourite-pin
step). Permanent delete stays separate and explicit: `mch node prune` hard-deletes retired nodes (history
and all); `mch node prune NODE` deletes one node, and its `--force` flag is the sole escape hatch for
deleting a still-managed node (not retired; it may re-seed). Auto-purge
is **not** triggered by a node dropping off the contact list - eviction is the normal state on a
dense mesh, so contact-list absence is a poor signal. (A dedicated `retired_at`/`retired_reason`
column pair was considered but not built; the state + log line suffice for a single-operator box and
can be added later if time-based auto-cleanup is wanted.)

**Cleanup.** `observed_settings` should only hold keys that are currently wanted, i.e.
`key in (resolved desired set + facts)`. Orphans (a key you unset, or dropped from a group) are
removed by `mch node clean settings`, the mirror of the existing `mch node clean facts`. Observed rows are
never authoritative, so deleting them is always safe - they are re-observed next cycle if still
wanted.

**Migration status / plan.**
1. [done 2026-06-11] `desired_settings` table + `set/get/delete_desired_setting` DB methods.
2. [done 2026-06-11] `overlay_db_settings()` merges DB node-scope rows onto `NodeConfig` before
   resolution (DB wins + warn); wired into `WorkQueue._process_node`, so reconcile and
   `all_setting_keys` pick up DB keys with no change to `resolve_*`.
3. [done 2026-06-11] `mch config set <node> <key> <value> [--policy ...]` and `mch config unset <node> <key>`
   write/delete a node-scope `desired_settings` row (full pubkey as `scope_id`), validating the
   key (writable settings only), value, and policy. The write is synchronous so intent is recorded
   immediately; convergence is **deliberately decoupled** - `set`/`unset` do not auto-sync (that
   would block on the mesh and conflict with traffic-minimisation), they print a `mch fleet sync <node>`
   hint instead. The slice-2 overlay reads exactly these `(NODE, pubkey)` rows, so a `set` is
   honoured on the next cycle with no resolver change.
4. [done 2026-06-11] `mch node` shows the source (`db`/`config`) per setting (a SOURCE column).
   _(Superseded 2026-06-19: the SOURCE column was removed in the sunset - the DB is the only source
   now, so it always read `db`. The report still carries a constant `source="db"` field for schema
   stability.)_
   This also closed a gap: the **report** path (`build_fleet_report`, feeding `mch node`/`mch
   status`/`mch fleet plan`/`--json`) did not previously apply `overlay_db_settings` - only the sync
   path did - so it would not have reflected a `mch config set` until the next sync wrote an observed row.
   The overlay now runs in reporting too, so DB-authored desired values (and the `mch fleet plan`
   preview) are visible immediately. Observed-only history rows have `source = None` (rendered `-`).
   Scope note: a DB desired row on a node *not* in config.yaml is not surfaced yet (the report still
   needs a config-derived `NodeConfig` to resolve against); that arrives with the config-import /
   group-global-scope slices.
5. [done 2026-06-11] `mch node clean settings` removes orphaned observed rows - the mirror of
   `mch node clean facts`. A `settings` row is orphaned when its key is not in the node's resolved
   desired set (config + DB overlay). The DB overlay is applied when computing the wanted set, and
   db-set keys are seeded directly, so a key set only via `mch config set` is never deleted (even on a node
   absent from config.yaml). Dry-run by default; `--yes` deletes; `--group`/`--all`/`--setting`
   filters mirror `clean facts`. Deletes are safe because observed rows are a re-derivable cache.
6. [done 2026-06-11] Group and global desired scopes. The schema and resolver already supported
   `scope_type` group/global; this slice wired them end-to-end. `overlay_db_group_settings` is the
   group/global analogue of `overlay_db_settings` (all DB rows land in `GroupConfig.settings`; a
   policy-only row is skipped by `resolve_desired` but drives `resolve_policy`/`all_setting_keys`).
   It is applied in `_process_node` (reconcile) and `_wanted_setting_keys` (clean) at a single
   chokepoint per path. `mch config set`/`unset` gained `--group NAME` and `--global`; node-scope wire
   format is unchanged (daemon defaults `scope` to node) so older clients keep working. `set`
   validates the group exists in config (catches typos); `unset` does not, so a row for a removed
   group can still be cleared. Group conflict warnings are config-level and go to the app log only,
   not per-node DB history. Group attribution in the clean path still needs the node in config to
   know its group (global keys are seeded unconditionally) - same limitation as node scope today.
7. Reserved-key directives as DB rows. Directives are desired state that maps to a *structured
   field* on `NodeConfig`/`GroupConfig` (resolved by `resolve_name_prefix`/`resolve_via_radio`/
   `resolve_policy`), not to a node `set <key> <value>` CLI command. They are stored as ordinary
   `desired_settings` rows; the overlay routes a directive key to its field instead of the settings
   dict via `_DIRECTIVE_PARSERS` (key -> field + parser) and `_split_directive_rows`. `settable_keys()`
   = `writable_setting_keys()` | `DIRECTIVE_KEYS` gates `mch config set`/the daemon. DB wins over a YAML
   field value with a conflict warning, exactly like a setting row.
   - [done 2026-06-12] 7a: `name_prefix` (plain string via `_coerce_name_prefix`; empty clears).
     Proven end-to-end - a DB `name_prefix` row flows through the overlay into
     `resolve_desired("name")` and the enforced `set name` write carries the prefix.
   - [done 2026-06-12] 7b: `default_policy` (enum enforce/audit/skip). Added `validate_directive_value`
     (reuses the overlay parser so set-time validation and read-time parsing cannot drift) and made
     `_directive_overrides` defensive: a malformed DB directive value is skipped with a warning
     rather than wedging the sync cycle (set-time validation prevents bad writes; this guards
     hand-edited/legacy rows).
   - [done 2026-06-12] 7c: `via_radio` (radio tuple via `_parse_radio`; empty clears). A bad tuple is
     rejected at `mch config set` and defensively skipped on read, via the same validate/parse path.
   - [done 2026-06-12] 7d: directive whitespace normalisation (`_normalize_directive_text`). Leading
     whitespace is always dropped and an all-whitespace value collapses to `""` (read as cleared/
     inherit). `name_prefix` is prepended with no separator, so a *trailing* space is meaningful
     (`" 🌞 "` -> `"🌞 "`) and kept; `default_policy`/`via_radio` strip both ends. Applied in the
     parsers (read) and at write time in the daemon (`normalize_directive_value`) so the DB stores
     the canonical value and `mch fleet status`/export stay tidy.
8. config.yaml import/export and sunset (also closes the group-attribution-needs-config limitation
   from slice 6, since import gives every desired node a resolvable identity).
   - [done 2026-06-13] 8a: `mch config import` - the migration on-ramp. Pure `config_to_desired_rows(cfg,
     resolve_pubkey)` flattens config desired state (settings + the three directives, all scopes)
     into `desired_settings` rows: global `scope_id ""`, group = group name, node = full pubkey
     resolved from `pubkey_prefix` against nodes the DB already knows. A node not yet seen is
     skipped with a warning (its full pubkey is unknowable from a prefix alone). Directives
     serialise back to their reserved-key string form (`_radio_to_str` round-trips through
     `_parse_radio`; default poll_hours 24 stores as None to match `mch config set`). Operations are *not*
     imported (not part of desired_settings). Daemon `_handle_import` builds the DB-backed resolver;
     dry-run by default, `--yes` upserts (idempotent). After import every key lives in both YAML and
     DB, so the overlay's "DB wins + warn" nudges cleanup - export + sunset (8b+) finish the job.
   - [done 2026-06-13] 8b: `mch config export` - the inverse of import. `desired_rows_to_config_dict` rebuilds
     the config.yaml structure from all `desired_settings` rows (shorthand `key: value` when only a
     desired value, mapping form with policy/poll_hours otherwise; policy-only -> group/global
     `setting_overrides` or node `settings.<key>.policy`; directives -> top-level fields; node
     scope_id -> `pubkey_prefix`). `config_dict_to_yaml` serialises it; new DB `get_all_desired_settings`
     fetches every row. Daemon `_handle_export` returns the YAML; CLI `mch config export` prints to stdout
     (`mch config export > desired.yaml`). A round-trip test proves config -> rows -> YAML -> load -> rows is
     stable. Observed state and operations are not exported.
   - [done 2026-06-17] 8c-8i (sunset): the DB is now authoritative for inventory + desired state;
     config.yaml carries only daemon settings. **Resolved open call: config.yaml errors, it is not a
     readable seed** - the daemon loads strict and refuses to start if any `nodes:`/`groups:`/`global:`
     desired-state block is still present (empty blocks tolerated), pointing the operator at
     `mch config import`. Pieces:
     - 8c: `resolve_desired_configs(db, node)` rebuilds `(NodeConfig, GroupConfig|None, global)` from
       DB rows via the empty-base overlay, closing the slice-6 group-attribution limitation.
     - 8d: every desired-state *read* path resolves from the DB resolver (clean path, fleet report,
       sync engine).
     - 8i: operations migrated to a `desired_operations` table; import/export carry them.
     - 8e: `config.load(strict=True)` for the daemon (main + reload) hard-errors on leftover desired
       state; the parser stays lenient by default for the import on-ramp. `mch config import [CONFIG_FILE]`
       takes an explicit YAML path (parsed leniently) so the on-ramp survives a strict daemon, and
       import now carries node **label/group** inventory, not just desired settings.
     - 8f: startup/reload `_reconcile_favourites` replaces config-driven seeding - seed a favourited
       repeater the DB doesn't know, pin (favourite) every managed DB node. Favourites and the managed
       set stay in step by adding in both directions; the config-vs-DB ghost/unseen concept is gone
       (every DB node is managed). `mch config set --group` validates against DB group membership.
     - 8g: `NodeState.RETIRED` + `mch node retire`/`mch node unretire` (below). `prune` hard-deletes retired
       nodes; `remove` guards a still-managed node behind `--force`.
     - 8h: `mch commission` prints an importable `nodes:` doc + `mch config import` guidance instead of a
       config.yaml snippet; secrets.yaml snippet unchanged.
   - [done 2026-06-19, post-review] **Complete backup/restore + a cleaner status table** (PR #3):
     - The DB is now the only copy of intent, so `mch config export`/`mch config import` became a full fleet
       backup, not just a desired-settings dump. Export emits every node's full pubkey, label, group,
       desired settings, and operations (even a node with no desired settings), and each node's group
       under `groups:` so the dump re-imports cleanly. Import recreates a node from its full pubkey
       even when the gateway/DB has not seen it yet (`is_full_pubkey` resolves without a DB lookup;
       short prefixes still need a known node and are skipped) - so a restore rebuilds the fleet
       up-front and the favourite mirror re-pins each node as its contact reappears.
     - Retired nodes round-trip: export tags them `retired: true` (the one inventory-state field in
       the file; observed `NodeState` values are transient and deliberately not exported). Import
       makes the operator choose `--retired keep` (restore as retired) or `--retired skip`; a write
       touching retired nodes is refused until one is chosen.
     - The per-setting SOURCE column was dropped from `mch fleet status <node>` (always `db` now).
   - Follow-up (not done): the commission **replace** and **scan/list** flows still read config
     inventory (`config.nodes`/`config.groups`) and need a DB-driven rework; tracked in BACKLOG,
     along with making `facts` a scoped union, a scheduled-backup unit, and the `0.10.0` release cut.

## Convergence Over A Constrained Mesh

mch is a slow-convergence configuration manager for MeshCore nodes over an unreliable,
low-bandwidth radio network, including low-powered solar nodes that may be intermittently
available. It is not designed around datacenter-style immediate reachability.

- Desired state should converge over repeated opportunities rather than requiring every command
  in one sync to succeed.
- Observed state is cached evidence with age; reports must make staleness and partial observation
  visible rather than treating cached values as current truth.
- Failure patterns matter: always failing, intermittently succeeding, first-command-only failure,
  and key-specific failure are different operational signals.
- Mesh traffic has a cost. Avoid redundant reads and avoid repeated commands that cannot produce
  useful new evidence, while still allowing deliberate refresh when an operator needs current
  observations.
- Recovery logic must be conservative around writes because a response can be lost after a write
  has taken effect.

## Shared internal report model

`mch` uses a single daemon-side report/action model rendered by the CLI (human tables and `--json`)
and compact mesh DM/channel summaries. `mch fleet status` and `mch fleet plan` do not separately
reconstruct desired-vs-observed meaning from raw DB records.

**The HTTP API was retired in 0.22.0.** It existed only as the read contract for a future dashboard;
with the web UI shelved and `mch ... --json` serving the identical report model over the Unix socket
(no open port, no loopback caveat, no authentication to design), the loopback HTTP server was dead
weight. If a UI is ever built, it should rebuild a read API against this same report model and the
frozen `--json` contract — not resurrect the old server. The paragraphs below keep the model's
design rationale; anywhere they say "API" read "the report model behind `--json`".

Intended split:
- `mch fleet status [LABEL_OR_PREFIX]`: current state and evidence.
- `mch fleet plan [LABEL_OR_PREFIX]`: what mchd would do next.
- `mch fleet logs [LABEL_OR_PREFIX]`: history.
- `mch fleet suggest [LABEL_OR_PREFIX]`: recommendations.
- `mch fleet sync [LABEL_OR_PREFIX]`: do work now.

The report model should classify settings as `ok`, `nominal`, `drift`, `unknown`, `ignored`, or
`blocked`, and classify next action as `none`, `read`, `write`, `report`, `ignore`, or `blocked`.
`nominal` means the observed value differs textually but is equivalent within expected firmware or
computer representation tolerance.

Implemented contract: `src/mch/report.py` builds `schema_version: 2` fleet/node reports consumed by
CLI reporting (tables and `--json`). The read commands map to the model: `fleet status`/`node show`
(state and evidence), `fleet plan` (next actions), `fleet issues` (faults only), `fleet logs`
(history). A future dashboard should consume this model rather than deriving status independently.

The report's `issues` block (`drifted`/`bad_state`/`open_nodes`) carries only *faults* — the
categories that drive alerting and the `mch fleet issues` exit code 2. Firmware currency is
deliberately a **separate** advisory field (`firmware: {latest, outdated[]}`, plus
`summary.outdated_firmware`): being behind the latest release is not a fault, so it must not set the
alert code. The full report is emitted by every CLI read command's `--json` flag, bypassing the
`_status_view`/`_node_detail_view` terminal-projection shims. `tests/test_cli_report_contract.py`
pins `mch fleet status --json` to `build_fleet_report()` over a real daemon socket, so the socket
round-trip and report shape can't silently drift. RECOMMENDATIONS-2 S3.1 removed the dead
post-sunset `ghosts`/`unseen_nodes` categories that could never populate.

`--json` currently emits the *complete* fleet report, including facts and settings for every node.
That is fine for scripting and cron. If a polled map UI is ever built, it will want a lighter fleet
summary (health/state, identity, timestamps, marker `lat`/`lon`, issue/drift counts) with a separate
per-node detail call — a shaping decision to make against real UI needs, not to pre-build now.

Any future write path (a web UI editing desired settings, selecting facts) must preserve a
read/write distinction: writes require authentication, audit records, validation, and safe
concurrency first, and must present a configuration diff for review before applying — never a
silent editor. The CLI and such a UI would share daemon-side operation logic.

Keep service diagnostics distinct from node activity. The systemd journal owns daemon startup,
process failures, and detailed transport/radio debugging. The database-backed `mch fleet logs`
view retains operator-relevant node history — writes, operations, drift, and read failures that
explain a partial sync. `mch fleet status` reports mesh state; it is not a replacement for
`systemctl status mchd`.

`mch daemon status` and `mch daemon logs` are thin, read-only wrappers over `systemctl status mchd`
and `journalctl -u mchd` so operators can reach service diagnostics without recalling the unit name.
They deliberately live under the `daemon` (process) noun, not `fleet` (nodes): `daemon logs` is the
process journal, `fleet logs` is per-node activity. Because they only read, they take no privilege
and pass systemctl's own exit code through unchanged — a stopped unit's non-zero status is normal
output, not a failure.

## Command metadata

MeshCore command knowledge should live in a small curated registry for commands mch actually uses,
not as scattered conditionals and not as a full copy of the MeshCore CLI reference. Unknown keys
must keep working with default `get <key>` / `set <key> <value>` behavior.

The registry should define read/write command shape, value validation, comparison strategy, safety
flags, reboot requirements, optional staged-write/apply commands, firmware/version notes, and
per-command timeouts. This is the foundation for safer config validation, clearer `status`/`plan`
output, and future API metadata.

`mch fleet plan` command strings are display/copy-paste helpers that intentionally mirror raw MeshCore
CLI syntax, for example `set owner.info 🦷 rdm`. Do not shell-quote values in this display unless
the underlying MeshCore tools start requiring it. Live testing with `meshcore-cli` and the phone app
showed that `owner.info` values with spaces do not need quotes; firmware settings such as
`name`/`owner.info` treat the rest of the line as the value.

The long-term registry data should live in a versioned `command_registry.json`, with Python code
limited to loading, validating, and applying fallback behavior. That keeps command knowledge easy
to update as firmware behavior is discovered without requiring code changes for every new command.

Some firmware flows may have temporary/runtime commands separate from persistent config writes.
The registry should model those explicitly rather than hiding them inside sync code, but unverified
flows must stay marked as hypotheses. On v1.15.0 repeater firmware,
`tempradio <freq,bw,sf,cr>,<minutes>` applies a temporary radio tuple, while `set radio` is the
persistent write and still requires reboot. Other examples needing hardware confirmation are
`clkreboot` and version-specific region enablement such as `region allowf`.

## CLI grammar conventions

Two conventions keep the 30-command surface consistent (RECOMMENDATIONS-2 slice 2). They are taught
to operators once in `mch help concepts`; this is the developer statement plus the conformance rule
for new commands.

- **Scope flags.** `--group NAME` / `--global` express the *scope of a desired setting*
  (`config set`/`unset`) — "set this value at global scope" reads correctly. `--all` expresses
  *applying an action fleet-wide* (`node clean facts|settings`, and any future fleet-wide action) —
  "clean facts across the fleet". The bright line is `--global` (a setting's scope) vs `--all` (an
  action's reach); no command should use `--global` for an action or `--all` for a setting.
  `--group` is shared (restrict either a setting or an action to one group).
- **Mutation convention.** Commands that delete or change local state *preview by default and execute
  with `--yes`* (`node prune`, `node clean facts|settings`, `config import`, `node path --apply`).
  The only exceptions are interactive wizards (`node new`) and the typed-confirmation guard on
  destructive `node exec` commands (that is a confirm, not a preview). `node prune [NODE]` is the
  single delete-local-state command: no argument lists/purges every retired node, a NODE argument
  prunes that one retired node, and a still-managed node is refused ("retire it first") unless
  `--force` is given. `--force` (valid only with a NODE) is the sole escape hatch for deleting a
  still-managed node; the old hidden `node remove` alias was retired in favour of it (RECOMMENDATIONS-3
  S4.1).

## Node identity

**The public key is the stable node identity.** Names can change. Labels are local-only (in
config.yaml, never pushed to the node). The mesh-visible name (`settings.name.desired`) is
pushed to the node hardware and visible to other mesh participants.

`owner.info` is also mesh-visible to operators who can read node info. Example config should use a
neutral default such as `Managed with Mesh Herder`, not a forced project URL. A clickable URL
(`Managed with https://meshherder.com`) is useful for discoverability, and URLs are clickable when
someone logs in as guest, but it can feel like advertising when written to someone else's mesh;
leave that as an explicit operator choice.

The DB stores the full 32-byte pubkey as each node's stable identity. Post-sunset the DB is the
inventory (config.yaml no longer lists nodes), so nodes enter the DB through explicit management
actions rather than a config-seeding pass.

## How nodes enter the DB

1. **Gateway favourites (bootstrap).** `_reconcile_favourites()` runs at startup and after a
   contact refresh. A favourited repeater contact the gateway knows but the DB does not is
   inserted as a managed node — a manual favourite means "manage this node". The same pass
   re-favourites ("pins") any managed DB node whose contact lost the favourite flag, so a dense
   mesh cannot evict it. It never unfavourites (that is the retire path).

2. **`mch node adopt`.** Adds a discovered running node to the DB inventory and gateway contact
   store directly, installing a route and confirming reachability.

3. **`mch config import`.** Seeds inventory + desired state from a YAML backup/desired file. A node
   given by full pubkey is created up-front (restore path); a node given by short prefix must
   already be a known DB node so its full pubkey resolves.

4. **Commissioning (`mch node new`).** After a new/replacement node reboots, the reconcile above
   seeds it from the gateway favourite; the operator then imports its desired state.

`mch gateway contacts` reads live gateway contacts and filters against the DB inventory; it is
read-only and does not itself write to the DB (it prints the `mch node adopt` command to run).

To stop managing a node, `mch node retire` (keeps history, unfavourites the contact) or, to delete
local DB state, `mch node prune` (with `--force` for a still-managed node). Because reconcile re-seeds any still-favourited
contact, retire before prune so the node is not immediately re-seeded on the next cycle.

## `mch gateway contacts` vs the DB

`mch gateway contacts` reads **live from the gateway**, and filters against the **DB inventory** (the
DB is the inventory post-sunset — see "Desired state in the DB"). It shows REP nodes the gateway
knows about whose full pubkey is not yet a managed DB node. Use it to discover what is visible on
the mesh and decide what to bring under management. It is read-only; it prints the ready-to-run
`mch node adopt` command for each contact (adopt/import/commission do the actual DB writes).

## config.yaml is operator-managed

mchd reads config.yaml (daemon settings) at startup and on hot reload. It does not modify it, and
it never edits secrets.yaml. When `mch node new` provisions a node, it prints an importable
`nodes:` desired-state doc (applied with `mch config import`) plus the secrets.yaml snippet — the
operator applies the import and adds the password, keeping both under version control.

Reasons for this constraint:
- Config files belong in version control. Auto-writes bypass review, diff, and commit history.
- The daemon runs as a restricted system user (`mch`). Writing operator-owned files from a daemon
  is a permission and ownership problem.
- YAML formatting is hard to preserve programmatically. Text-append hacks break on non-trivial
  file structures.
- mchd's role is unambiguous: reads config, never writes it.

## Work queue and rate limiting

mchd processes nodes one at a time. There is no parallel mesh traffic — the mesh is a shared
medium and hammering it degrades performance for all participants.

Between nodes, mchd waits `inter_node_delay_seconds` (configurable; default 30s). Each node
is visited at most once per `sync_interval_days`, scheduled at a random offset within the window
so daemon restarts never cause a sync storm.

The `mch fleet sync <prefix>` command bypasses the schedule for an immediate operator-triggered sync
and always streams progress because slow mesh replies are a normal operating condition. If its
client disconnects, the daemon cancels that forced sync and unwinds gateway state. `mch fleet sync`
(no arg) runs a full pass through all config nodes immediately.

Gateway-owning operations are serialized by a high-level WorkQueue operation lock. Scheduled sync,
manual full sync, forced node sync, raw `mch node exec`, and commissioning cannot overlap; manual CLI
operations return `busy: <operation>` if another workflow owns the gateway/radio.
Shutdown cancels and awaits active gateway-owning operations before disconnecting the gateway, so
a temporary management-radio switch can be restored during cancellation cleanup.
Operator-triggered single-node and full-cycle syncs write activity boundary records for start and
terminal outcome; scheduled cycles do not use these manual-run markers.

### MeshCore hop/path terminology

In MeshCore, a **hop** is traversal through a repeater in a packet path. It is not a change of
frequency, bandwidth, spreading factor, or coding rate. The upstream MeshCore FAQ documents
zero-hop versus flooded adverts, a firmware hop limit, and packet paths represented by repeater
path hashes:
<https://github.com/meshcore-dev/MeshCore/blob/main/docs/faq.md>.

For normal management traffic, a node is contacted on the gateway's current radio settings using
its stored MeshCore contact path. If effective `via_radio` specifies a different frequency,
bandwidth, or spreading factor, mchd temporarily switches the gateway before contacting the node
and restores the home settings afterward. Coding rate is included when reporting/sending a radio
configuration, but does not by itself require a switch: it controls the sender's redundancy and
can differ while radios still communicate. `via_radio` can be declared globally, on a group, or
on a node; precedence is node, then group, then global fallback. A global value should only be
used when it applies to most managed devices because otherwise it moves home-radio devices
unnecessarily. It is reachability-radio configuration, not a MeshCore path. Nodes
that have moved radio without an updated management-radio value can repeatedly time out even
though their desired settings are otherwise valid. After normal reachability probes fail, mchd
may make a bounded recovery attempt for nodes with configured radio context: profile radio
presets are tried first, then nearby spreading-factor variants of the node's effective
`via_radio` or desired `radio`. This is deliberately capped and logged so it can rescue a node
without becoming a broad scan.

The current command flow is path-capable and conservative: `send_command()` sends through the
gateway's stored MeshCore contact, whose `out_path` may already contain repeater hops. Foreground
node sync displays the effective management radio and cached contact routing mode/path metadata
before reads start; this provides diagnostic context without additional mesh traffic. When a
stored path answers but drops isolated reads, mchd spends a small per-node retry budget on the same
stored path. It does not repeat every failed read, and it does not retry writes over flood.

If a stored path fails the initial reachability probes, mchd may send one cheap flood-routed `ver`
probe to distinguish "node unavailable" from "stored path stale." A flood hit is a path-recovery
signal, not permission to run the rest of the sync over flood. The operator can then use
`mch node path <node> --discover` or `--advert` to inspect candidate paths and
`mch node path <node> --apply <path[:mode]>` to persist a confirmed replacement path on the gateway
contact. This preserves the primary traffic rule: normal management should use learned paths and
avoid hammering the mesh with flood.

By contrast, the dependency's direct-message retry helper can reset a broken path to flood after
failed attempts. Management path inspection and controlled recovery need an explicit operator
workflow before mchd can safely update stored paths or retry configuration writes over an
alternative route.

Successful routing paths should also be retained as bounded observed history. This is useful when
the gateway has fallen back to flood routing but a recently working repeater path is known, as
observed for the Acacia Gardens and QH Paterson labels in field operation. Store only a small
number of successful candidate paths with last-success time and hop count, prefer short recent
paths, and try at most a tightly limited number before a single flood probe. Long paths impose
greater latency and failure probability, so status should surface path length.

Reliability assessment should be learned primarily from observed management outcomes: rolling
successful replies, timeouts, partial syncs, command-specific failures, and path changes. Route
length and physical distance inferred from configured `lat`/`lon` may help explain fragile nodes,
but neither proves unreliability: terrain, radio settings, power budget, and repeater availability
also matter. Any eventual adaptive polling/backoff policy should first expose the underlying
score/evidence to the operator and should not increase field-node traffic merely to calculate it.

Live bench testing may deliberately exercise the same-room test node on its test radio. For the
current luna deployment that means `915.800`; avoid generating discretionary test traffic on the
`915.075` field mesh channel.

In operator-facing views, `LABEL` means mch's local identifier and `MESH NAME` means the name
advertised by the MeshCore device. Avoid using a `NODE` column heading where a local label is
being shown; operators naturally use "node" for the mesh-visible device identity.

The hardcoded setting policy fallback is `audit`. A desired setting is read and compared by
default, but mchd only writes to a node when the setting, node, group, or top-level `global:`
block explicitly opts in with `policy: enforce` or `default_policy: enforce`. Desired values
resolve from `global:` to group to node, with the most specific value winning.

## Settings vs operations

**Settings** model desired state for a key that can be read back. Each sync cycle mchd sends
`get <key>`, compares the result to `desired`, and acts according to the policy (`audit`,
`enforce`, `skip`). The comparison gives convergence: if a node drifts or gets reflashed, the
next sync detects it and corrects it (under `enforce`).

Comparison is field-aware for known numeric settings where firmware returns rounded float values.
`radio` is parsed as `freq,bw,sf,cr`: frequency and bandwidth tolerate small float roundoff, while
SF and CR are exact integers. `lat` and `lon` compare within roughly three decimal places. Status
still displays the actual observed value and marks rows within expected tolerance with `*`.

### Node name prefix

`name_prefix` prepends a string - usually an emoji marking a group on the map - to a node's desired
`name`. It is declared globally, on a group, or on a node, with the same node-then-group-then-global
precedence as other shared fields, and combines with the separately-resolved base name (so a group
prefix plus a per-node name is the common case). The prefix is prepended verbatim: no separator is
inserted, so an operator who wants a space includes it in the prefix. The prefix only applies when
a base name is configured; a prefix with no desired name has nothing to prepend onto.

Because the firmware name buffer stores at most 31 UTF-8 bytes and truncates on a raw byte boundary
(not a character boundary), `mch` validates `prefix+name` against `MAX_NODE_NAME_BYTES` at config
load and refuses to start if it would overflow. Truncation is never used: it would split a multibyte
emoji into invalid UTF-8 that `get name` reads back differently from the desired value, producing a
permanent enforcement loop. The cap is byte-based and is not reduced by a set location (bench-
confirmed); the smaller over-the-air *advertised* name budget when a location is present concerns
what other nodes display, not the stored value mch verifies, and is not enforced here.

## Facts vs settings

**Decision (2026-06-24): a fact is a fourth policy, not a separate data type.** This supersedes the
earlier stance (kept below for history) that facts and settings should be modelled separately. The
trigger was the realisation that "the set of facts to collect" is the last desired-ish knob still
living only in config.yaml after the slice-8 sunset, and that facts and settings are the *same
observation* underneath — the only difference is whether mch has an opinion (a desired value +
enforcement) about the observed value.

### One concept: an observable key with a resolved policy per scope

`SettingPolicy` becomes four values (it was `enforce` / `audit` / `skip`):

| policy    | read? | desired needed? | on drift            | use                                  |
|-----------|-------|-----------------|---------------------|--------------------------------------|
| `enforce` | yes   | yes             | write desired       | "make it so"                         |
| `audit`   | yes   | yes             | log, never write    | "tell me if it drifts"               |
| `fact`    | yes   | **no**          | nothing — just store | "record it, I have no opinion"       |
| `skip`    | no    | no              | —                   | "leave alone / parked placeholder"   |

A fact is therefore an **audit-style observation with no desired value to compare against**. Policy
resolves Node > Group > Global like every other setting — strictly more expressive than the
previously-considered `global ∪ group ∪ node` union (set `neighbors: fact` globally to collect
everywhere, then `neighbors: skip` on one node to opt it out, which a pure union cannot express).
The union plan in BACKLOG is retired in favour of this.

**Read-only constraint:** registry `kind="fact"` keys (`ver`, `board`, `role`, `neighbors`) have no
write command, so only `fact` or `skip` are legal for them; `enforce`/`audit` require a writable
key with a desired value. This is validated at the `mch config set` gate. `mch config set NODE KEY --policy fact`
(no value) declares a fact via the existing policy-only path; `mch config unset` clears it; a read-side
`mch config show KEY` (name TBD) prints a key's resolved policy/desired/observed across scopes.

### One read pass — never poll a key twice

The decisive operational win: the separate `_collect_facts` pass over `config.facts` is deleted.
A single per-node pass reads every key whose resolved policy ∈ {`enforce`, `audit`, `fact`} exactly
once, then routes the observed value by policy (`fact` → store only; `enforce`/`audit` → compare,
and write if `enforce`). Because resolution yields exactly one policy per (node, key), a key can no
longer be read once as a fact and again as a setting — the latent `name` double-poll disappears
structurally. The existing reuse of the reachability probe's `ver` reply (`queue.py`) is the same
principle and is retained; harvesting other incidental observations (a login reply, a composite
status command) is a separate bench-tested investigation (see JOURNAL/BACKLOG) because which
commands carry which values is firmware-specific.

### Storage: the single-observation invariant

There is only ever **one stored observation per (node, key)** — never two copies that could drift
apart. For now this is enforced by *routing*, reusing the two existing tables: resolved `enforce`/
`audit` → the `settings` row (desired, policy, drift, write attempts); resolved `fact` → the
`node_facts` row (value, `collected_at`). A test asserts no (node, key) ever appears in both. The
inventory/"facts" view reads observed values from **both** tables (`settings.observed` ∪
`node_facts.value`) so an enforced key (e.g. `owner`) still shows in the CMDB view — sourced from
the one observation, not a copy. Both tables already timestamp observations (`node_facts.collected_at`,
`settings.last_checked`), so freshness is retained whichever home a key lands in.

Folding the two tables into a single `observations` table (one row per node/key, `desired`/`policy`
nullable) would make divergence *structurally* impossible rather than merely disciplined, and is
the natural endpoint — but it is a larger migration and is deferred as a follow-up, not a blocker.

**Migration:** the legacy global `config.facts` default `["ver", "board", "role"]` becomes global
`policy: fact` rows for those keys, preserving today's out-of-box collection explicitly rather than
by a hidden default. A readable key with no scoped row is simply not read — facts stay opt-in. The
`default_policy` directive keeps its `enforce`/`audit`/`skip` range; a global "everything is a fact"
default is meaningless.

Configured fact keys remain checked by `mch daemon validate` against the command registry's readable keys;
unknown keys stay legal with the `get <key>` fallback but warn and suggest close names to prevent a
typo causing silent repeated reads. Status presentation can still hide cached observation rows whose
key is no longer configured, retaining the historical row until an explicit cleanup discards it.

### Slice plan (on branch `facts-as-policy`)

- **F1 (done)** — `SettingPolicy.FACT` + validation + `mch config set --policy fact` gate widening.
- **F2 (done)** — unify the read pass: deleted `_collect_facts`/`_process_settings` into one
  `_process_observations`, routing by resolved policy, one read per key.
- **F3 (done)** — migrated `config.facts` → global `policy: fact` rows (`mch config import`); strict-error
  on a leftover `facts:` block; the daemon read pass, the report's "configured facts", and
  `mch node clean facts` all follow each node's resolved fact set (`resolved_fact_keys`) instead of one
  global list. Added the single-observation invariant test (no key in both tables). The report/clean
  switch was folded in here (originally F4) to avoid a broken intermediate state.
- **F4 (done)** — added `mch config show NODE KEY`: prints a key's per-scope desired/policy rows, the
  resolved policy/desired (with the source scope), and the observed value. Read-only. (The report/
  clean rework moved into F3.) Extended in RECOMMENDATIONS-3 slice 1: `config show NODE` (no KEY) lists
  every resolving key as a table (policy/desired/FROM/observed/age), and `config show --group NAME` /
  `--global` dump a scope's stored rows, so the read side mirrors `config set`/`unset` at every scope.
  Directive transforms (name_prefix) are shown inline; an unknown KEY is rejected like `config set`.
- **F5 (separate, bench on luna)** — investigate free/batched/incidental facts; record in JOURNAL
  before turning any of it into a rule.

### Earlier stance (superseded 2026-06-24, kept for history)

The original model held that facts and settings should stay distinct data types: facts as
inventory/evidence with no desired value/policy/write-back, settings as managed values carrying
desired value, scope inheritance, policy, comparison status, write attempts, and a planned action.
The report model kept separate `facts` and `settings` collections, combined only at presentation
under an "observed values" heading. That separation already forbade duplicate mesh traffic — a key that was
both a fact and a setting was to be read once and fed to both records — which is exactly the
single-read invariant the policy unification now makes structural rather than aspirational.

**Operations** are scheduled commands with no read-back or desired-value comparison. mchd sends
the command string, records success or failure, and re-runs it after `every_hours` have elapsed
since the last successful run.

Use operations for commands that:
- have no read-back equivalent (e.g. `region put`, `region save` — there is no `get region`)
- are inherently temporal or side-effectful, not convergent (e.g. `clock sync`, `reboot`)

The region commands are the clearest example of the tension: they configure desired state but
cannot be read back, so they cannot be modelled as settings. The workaround is to put them in
operations with a very long `every_hours` (e.g. 87600 = 10 years) so they run once at first
contact and are effectively never repeated.

**Bare-command keys** are settings that don't follow the standard `get <key>` / `set <key> <value>`
pattern. `powersaving` is the current example: bare `powersaving` returns `on` or `off`, and the
write is `powersaving on` / `powersaving off` — no `get` or `set` prefix. mchd handles this via
`_BARE_COMMAND_KEYS` in `queue.py`; add new keys there as needed.

**Clock sync** is a genuine operation: `send_cmd` embeds the host's current Unix timestamp in
every packet, so the firmware on the receiving node can use that timestamp to set its clock when
it processes `clock sync`. The correct time comes from the Mac's NTP clock, not from the gateway
node's own (potentially stale) clock.

`mch fleet plan` is read-only. It compares YAML desired state with the DB's known state and shows:
planned writes for enforced drift, audit drift that will not write, and values that must be
observed by a sync before a write can be predicted. It does not send mesh commands.

## Authentication

MeshCore nodes require login before accepting CLI commands. After the first successful login, the
gateway's public key is added to the node's ACL. On subsequent syncs, mchd attempts commands
directly and only re-authenticates if a permission error is returned. This avoids a round-trip
login on every sync for nodes that have already authorised the gateway.

Passwords are stored in `secrets.yaml` per node (or a `default:` entry for fleet-wide). The
daemon never caches passwords in the DB.

Do not move passwords or node private keys into the current SQLite database simply to make a web
interface easier. The database is observational runtime/history state and can currently be
rebuilt from operator-owned configuration. Making it authoritative for secrets would make normal
database backup, export, and diagnostics security-critical. Any future
secret store needs an explicit encryption, key-management, access-control, redaction, backup,
migration, and recovery design.

Configuration import/export is compatible with a future UI without changing this ownership
model. A normal export should exclude secrets. Any secret export must be explicit and strongly
protected, and importing desired state should validate and present a reviewable diff before it is
applied. A future settings editor may manage YAML or a replacement structured format only after
those controls exist.

## Messenger control

The current messenger feature accepts read-oriented DM commands from
`messenger.allowed_prefixes` and can send cycle announcements to
`messenger.announce_channel`. A future configuration model may make inbound command handling and
outbound announcements explicitly enableable and expose a friendlier channel selection, while
retaining allow-listed sender public-key prefixes as the authorization boundary.

An unapproved device sending a command is a request for messenger access, not evidence that the
device should be trusted. It is reasonable to retain pending sender prefixes for operator review
and expose them through a command such as `mch requests`; approval should produce or apply an
explicit configuration change. These sender permissions are distinct from the MeshCore node ACL
authorization used when mchd logs into a managed repeater.

## Two-layer commissioning

`mch node new` is a wizard, not an automated flow. It:

1. Pauses the work queue so commissioning and normal sync don't contend on the gateway.
2. Hops to the factory default frequency (869.618 MHz) and discovers unmanaged nodes.
3. For new nodes, generates a MeshCore-compatible private key locally and derives the matching
   public key. For replacements, reads the existing `prv_key` from `secrets.yaml`.
4. Pushes normal settings first, then session/identity-changing settings:
   name, guest password, admin password, private key, radio.
5. Waits for `set radio` to return because v1.15-era firmware replies `OK - reboot to apply`.
6. Reboots the node fire-and-forget.
7. Discovers on the target radio and verifies that the expected public key is visible. This is
   retried because a freshly rebooted node can miss the first scan.
8. Prints paste-ready `config.yaml` and `secrets.yaml` snippets.
9. Returns the queue to normal.

The operator then adds the snippets to their config files and waits for hot reload (or sends
SIGHUP). This keeps mchd read-only with respect to config files.

Live testing on a Heltec V3 running `v1.15.0-dee3e26` confirmed the intended new-node path:
fresh flash, factory discovery, generated private key, reboot onto the test mesh frequency, and
direct serial verification that `public.key`, `prv.key`, name, and radio all matched the snippets.

### Partial-state recovery

The mesh is fragile by design — nodes lose power, drop out of range, miss packets, and reboot
mid-conversation. Commissioning must assume *any* command can time out without applying or, worse,
apply but lose its response. The architecture treats partial-state recovery as a first-class
concern rather than an edge case.

Three firmware behaviours drive the strategy:

- **Reboot gates identity and radio.** `set prv.key` and `set radio` are stored on send but only
  take effect after `reboot`. So if commissioning fails before the reboot is sent, the node still
  advertises with the old pubkey on the old radio. Re-discovery on the factory frequency will find
  it; the command sequence can be safely re-sent.
- **`set prv.key` and `set radio` are slow.** Bench-measured on v1.15-era firmware: a valid
  `set prv.key` derives the new public key (curve25519 scalar mult on the ESP32) and writes to
  flash before replying `OK, reboot to apply! New pubkey: <hex>`. The work takes longer than the
  30s default `send_command` timeout but completes inside 180s. `set radio` exhibits the same
  pattern. Commissioning passes a 180s timeout for these two commands via
  `_command_timeout` in `src/mch/commission.py`; everything else keeps the 30s default.
  This was originally misdiagnosed as session invalidation after the preceding `password <new>`;
  Run 1 of `scripts/bench_session_invalidation.py` refuted that — `set name` after a password
  change with no re-login works fine.
- **Once `password <new>` is sent, the factory password is gone.** This is not session
  invalidation, just normal admin-password change. If a later command fails for any reason and
  the wizard retries, the next factory login attempt will be rejected; recovery has to know
  about the new password.

Together these mean partial commission leaves a predictable state: factory-looking pubkey and
radio, but a *non-factory* admin password. Recovery cannot rely on the factory password alone.

The commissioning code therefore tries two passwords, in order:

- **`mch node new` new-node mode**: factory `password` first, then the user-supplied admin password from the
  wizard form. The CLI generates the private key once before the retry loop, so successive
  attempts use the same key. All mutation commands (`set name`, `set guest.password`, `password`,
  `set prv.key`, `set radio`) are idempotent under re-send with identical params, so the whole
  sequence is replayed each retry without stage tracking.
- **`mch node new` replacement mode**: factory `password` first, then the managed password from
  `secrets.yaml`. The managed identity is operator-owned and pre-existing for this mode, so the
  fallback target is always available when the prefix is in secrets.

When the fallback fires, the wizard surfaces a short progress message
(`factory login failed; trying the new admin password` or `...resuming with managed credentials`)
so the operator can tell from the live output that the recovery path was used. Both paths emit a
clear error mentioning both attempts when authentication fails entirely.

This approach is deliberately stateless: no commissioning_stage table, no resume tokens, no
opt-in flags. Idempotency plus the two-password fallback covers every observed in-session
recovery case. The remaining hole is loss of the wizard's in-memory `prv_key` if the CLI process
itself dies mid-commission; mitigation options are tracked in `BACKLOG.md`.

When both fallbacks are exhausted (the node holds a password that's neither factory nor the one
the wizard knows about), bench recovery is a direct serial command: `password password` over USB
restores the factory admin password without auth, after which normal commissioning resumes.

## Known design gaps

For the current prioritized todo list, wishlist, and explicit non-goals, see `BACKLOG.md`. This
section only records stable design gaps that affect the architecture.

- **Commission idempotency**: running `mch node new` on a node already in the DB has
  undefined behaviour. Should detect and reject.
- **Commission new ignores group defaults**: the wizard pre-fills radio settings from the gateway
  node's home radio, but does not yet inherit from the matching `config.yaml` group (or a
  `default` group) the way replacement implicitly does via the existing secrets entry. Same
  applies to a future `default:` entry in `secrets.yaml`.
- **Wizard-held `prv_key` is process-local**: the CLI generates the new node's private key once
  per wizard session and keeps it in memory across retries. If the wizard process dies mid-run,
  the key is lost while the node may still be holding it, leaving the node bricked until manual
  factory reset.
- **Limited hot-reload scope**: node/group/secrets/messenger changes reload live. Gateway device,
  socket path, and DB path changes still require a daemon restart.
- **Factory reset notes**: for bench testing and replacement drills, documentation with the exact
  `esptool` erase/write commands is enough for now. This should stay outside normal sync and
  commissioning automation.
- **Command naming**: the detail view should continue moving toward
  `mch fleet status <label-or-prefix>` as the primary interface. Older `mch node` wording should be
  treated as legacy documentation.
