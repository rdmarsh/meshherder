# mch — Design Notes

This document explains the design decisions behind mch for contributors and operators who want
to understand why things work the way they do.

## Source of truth

**config.yaml is the source of truth for desired state.** The SQLite database reflects observed
(actual) state. mchd never writes to config.yaml or secrets.yaml — those are operator-owned files.

| Layer | Owns | Written by |
|---|---|---|
| `config.yaml` | Desired state: which nodes to manage, what settings to enforce, how to reach them | Operator |
| `secrets.yaml` | Passwords, private keys | Operator |
| SQLite DB | Observed state: last seen, facts collected, command history, sync schedule | mchd |

This matches the Ansible model: declare intent in files (version-controllable, reviewable); let
the tool record what it observed. Wiping the DB loses history but not configuration — mchd
rebuilds from config.yaml on the next startup.

mchd watches `config.yaml` and `secrets.yaml` and reloads them shortly after a file save. Reloading
keeps the existing daemon process and gateway connection alive, updates the in-memory desired
state, refreshes gateway contacts, and reseeds DB labels/groups from config. Invalid YAML is logged
and ignored; the last valid in-memory config remains active.

Future direction: keep this YAML-first model until there is a clear reason to migrate desired
state into SQLite. If that changes, add explicit import/export/backup commands rather than
silently making the DB the only copy. Secrets and private keys need a separate security decision
before they are included in any ordinary backup/export workflow.

> **Update (2026-06-11): this is being superseded.** Desired state is migrating into the SQLite DB
> so it can be edited by `mch` commands and (later) an HTTP API / web frontend. config.yaml becomes
> an import/seed source and will eventually be retired; secrets.yaml stays a file (see the
> "Desired state in the DB" section below for the full rationale and the migration plan). The
> YAML-first description above remains accurate for every scope/key not yet migrated.

## Desired state in the DB (migration in progress, started 2026-06-11)

We are moving desired state from config.yaml into the SQLite DB so it can be edited with `mch`
commands and, later, an HTTP API and a web frontend. A web UI round-tripping a YAML file is
fragile; an API/UI writing to a DB is natural. Git history of intent is an acceptable loss here:
mch targets a single hobbyist box, not a corporate fleet, and retirement history (below) replaces
the practice of keeping old config.yaml copies.

**The problem this solves.** There were three stores that drift: config.yaml (desired), the DB
(observed + a cache of desired), and the gateway's favourited contacts. Editing YAML then waiting
to see whether the daemon read it is opaque, because the slow mesh hides whether *intent* even
landed. With desired state in the DB, `mch set` writes synchronously, so `mch node` shows the new
desired value immediately - "intent recorded" stops being conflated with the slow "node converged".

**Stores collapse from three to two:**
- **DB** - authoritative for inventory + desired state, plus the observed state it already held.
- **Gateway favourites** - a *downstream mirror* of the DB managed set, not a source. The daemon
  favourites every managed node (eviction protection: an evicted contact cannot be addressed at
  all, and Sydney already runs ~440 repeaters) and unfavourites any contact not in the DB.
  Unfavouriting only clears the eviction pin; it does not delete the contact, and it is a local
  gateway write, not mesh traffic. Reading favourites back *as inventory* is dropped - `mch
  contacts` already covers "what is visible on the mesh".
- **secrets.yaml stays a file.** Passwords and private keys must not live in a DB that a (currently
  read-only, loopback-only, unauthenticated) HTTP API and web UI will read. File mode 0600 is a
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

**Retire, don't delete.** Nodes get a lifecycle (`active|retired`) plus `retired_at` and a
free-text `retired_reason`, replacing kept copies of config.yaml as the historical record. A
retired node is excluded from sync and unfavourited (same rule as the favourite mirror). Permanent
delete stays a separate, explicit operator action. Auto-purge is **not** triggered by a node
dropping off the contact list - eviction is the normal state on a dense mesh, so contact-list
absence is a poor signal; any future auto-cleanup is gated on time since retirement instead.

**Cleanup.** `observed_settings` should only hold keys that are currently wanted, i.e.
`key in (resolved desired set + facts)`. Orphans (a key you unset, or dropped from a group) are
removed by `mch clean settings`, the mirror of the existing `mch clean facts`. Observed rows are
never authoritative, so deleting them is always safe - they are re-observed next cycle if still
wanted.

**Migration status / plan.**
1. [done 2026-06-11] `desired_settings` table + `set/get/delete_desired_setting` DB methods.
2. [done 2026-06-11] `overlay_db_settings()` merges DB node-scope rows onto `NodeConfig` before
   resolution (DB wins + warn); wired into `WorkQueue._process_node`, so reconcile and
   `all_setting_keys` pick up DB keys with no change to `resolve_*`.
3. [done 2026-06-11] `mch set <node> <key> <value> [--policy ...]` and `mch unset <node> <key>`
   write/delete a node-scope `desired_settings` row (full pubkey as `scope_id`), validating the
   key (writable settings only), value, and policy. The write is synchronous so intent is recorded
   immediately; convergence is **deliberately decoupled** - `set`/`unset` do not auto-sync (that
   would block on the mesh and conflict with traffic-minimisation), they print a `mch sync <node>`
   hint instead. The slice-2 overlay reads exactly these `(NODE, pubkey)` rows, so a `set` is
   honoured on the next cycle with no resolver change.
4. [done 2026-06-11] `mch node` shows the source (`db`/`config`) per setting (new SOURCE column).
   This also closed a gap: the **report** path (`build_fleet_report`, feeding `mch node`/`mch
   status`/`mch plan`/the HTTP API) did not previously apply `overlay_db_settings` - only the sync
   path did - so it would not have reflected a `mch set` until the next sync wrote an observed row.
   The overlay now runs in reporting too, so DB-authored desired values (and the `mch plan`
   preview) are visible immediately. Observed-only history rows have `source = None` (rendered `-`).
   Scope note: a DB desired row on a node *not* in config.yaml is not surfaced yet (the report still
   needs a config-derived `NodeConfig` to resolve against); that arrives with the config-import /
   group-global-scope slices.
5. `mch clean settings` removes orphaned observed rows.
6. Later: group/global scopes, then reserved-key directives, then config.yaml import/export and
   sunset.

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

## API-ready internal model

`mch` should move toward a single daemon-side report/action model that can be rendered by the CLI,
a future HTTP/OpenAPI interface, and compact mesh DM/channel summaries. `mch status` and `mch plan`
should not separately reconstruct desired-vs-observed meaning from raw DB records.

Intended split:
- `mch status [LABEL_OR_PREFIX]`: current state and evidence.
- `mch plan [LABEL_OR_PREFIX]`: what mchd would do next.
- `mch logs [LABEL_OR_PREFIX]`: history.
- `mch suggest [LABEL_OR_PREFIX]`: recommendations.
- `mch sync [LABEL_OR_PREFIX]`: do work now.

The report model should classify settings as `ok`, `nominal`, `drift`, `unknown`, `ignored`, or
`blocked`, and classify next action as `none`, `read`, `write`, `report`, `ignore`, or `blocked`.
`nominal` means the observed value differs textually but is equivalent within expected firmware or
computer representation tolerance.

Implemented contract: `src/mch/report.py` builds `schema_version: 2` fleet/node reports consumed
by CLI reporting and the optional read-only HTTP API. HTTP is loopback-only until authentication
exists; it deliberately provides no sync, exec, or commissioning routes. A future dashboard and
map should consume this model rather than deriving status independently.
HTTP paths follow the existing read-only command vocabulary (`/status`, `/plan`, `/issues`,
`/logs`, and `/health`).

The first implementation exposes a complete fleet report from `/status`, including facts and
settings for every node. That is useful while proving the model, but it is not the intended
dashboard polling contract. Before a map UI is built, `/status` should become a lightweight fleet
summary: health/state, identity, timestamps, marker coordinates, and issue/drift counts. Keep
`lat`/`lon` in that summary so drawing the map does not require one detail request per marker.
`/status/<node>` remains the full clickable detail view. Logs, plan, and fleet issues remain
separate resources because they have different refresh and presentation needs.

Future API-driven interactions should preserve a distinction between reads and writes. The CLI
and a web UI can share daemon-side operation logic, but write endpoints require authentication,
audit records, validation, and safe concurrency behavior first. In particular, editing desired
settings or selecting new facts to collect should present a configuration diff for review before
applying it; it must not turn an unauthenticated local status API into a configuration editor.

Keep service diagnostics distinct from node activity. The systemd journal owns daemon startup,
process failures, and detailed transport/radio debugging. The database-backed `/logs` and
`mch logs` views retain operator-relevant node history, including writes, operations, drift, and
read failures that explain a partial sync. `mch status` reports mesh state; it is not a
replacement for `systemctl status mchd`.

## Command metadata

MeshCore command knowledge should live in a small curated registry for commands mch actually uses,
not as scattered conditionals and not as a full copy of the MeshCore CLI reference. Unknown keys
must keep working with default `get <key>` / `set <key> <value>` behavior.

The registry should define read/write command shape, value validation, comparison strategy, safety
flags, reboot requirements, optional staged-write/apply commands, firmware/version notes, and
per-command timeouts. This is the foundation for safer config validation, clearer `status`/`plan`
output, and future API metadata.

`mch plan` command strings are display/copy-paste helpers that intentionally mirror raw MeshCore
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

## Node identity

**The public key is the stable node identity.** Names can change. Labels are local-only (in
config.yaml, never pushed to the node). The mesh-visible name (`settings.name.desired`) is
pushed to the node hardware and visible to other mesh participants.

`owner.info` is also mesh-visible to operators who can read node info. Example config should use a
neutral default such as `Managed with Mesh Herder`, not a forced project URL. A clickable URL
(`Managed with https://meshherder.com`) is useful for discoverability, and URLs are clickable when
someone logs in as guest, but it can feel like advertising when written to someone else's mesh;
leave that as an explicit operator choice.

config.yaml stores a `pubkey_prefix` (8 hex chars) per node. The full 32-byte pubkey is
resolved at startup by cross-referencing the prefix against the gateway's stored contacts.
Once found, the full pubkey is written to the DB and used from there on.

## How nodes enter the DB

1. `_seed_from_config()` runs at startup. For each node in config.yaml, it looks for a matching
   gateway contact (by pubkey_prefix). If found and not already in the DB, it seeds a DB record.
   If not found, it logs a warning — the node is unknown to the gateway (out of range, wrong
   prefix, never advertised).

2. That is the only path into the DB. Nodes do not enter the DB by appearing in the gateway's
   contact store unless they are also in config.yaml. Discovery via `mch contacts` (which shows
   gateway contacts not in config) is purely informational — it does not write to the DB.

A node in the DB but not in config.yaml is called a **ghost**. Ghosts appear in `mch issues`.
They accumulate if you remove a node from config.yaml without purging the DB, or if a node was
previously added via an older seeding path. Use `mch prune` or `mch remove <node>` to clean up
local DB state for retired nodes.

A node in config.yaml that has no matching DB record is an **unseen config node**. This usually
means the gateway has never seen the configured prefix, or the prefix does not match the intended
node. Unseen config nodes appear separately in `mch issues` because the fix is different: correct
the config prefix, bring the node into gateway range, or wait for it to advertise.

## `mch contacts` vs the DB

`mch contacts` reads **live from the gateway**, not from the DB. It shows REP nodes the gateway
knows about that are not yet in config.yaml. Use it to discover what is visible on the mesh and
decide what to add to config. It is a read-only informational command.

## config.yaml is operator-managed

mchd reads config.yaml at startup and on hot reload. It does not modify it. When `mch new`
provisions a new node, it prints ready-to-paste YAML snippets for both config.yaml and
secrets.yaml — the operator copies them in, puts them in git, then waits for hot reload or sends
SIGHUP to mchd.

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

The `mch sync <prefix>` command bypasses the schedule for an immediate operator-triggered sync
and always streams progress because slow mesh replies are a normal operating condition. If its
client disconnects, the daemon cancels that forced sync and unwinds gateway state. `mch sync`
(no arg) runs a full pass through all config nodes immediately.

Gateway-owning operations are serialized by a high-level WorkQueue operation lock. Scheduled sync,
manual full sync, forced node sync, raw `mch exec`, and commissioning cannot overlap; manual CLI
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
`mch path <node> --discover` or `--advert` to inspect candidate paths and
`mch path <node> --apply <path[:mode]>` to persist a confirmed replacement path on the gateway
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

A value read from a setting is an observed fact in the ordinary English sense, and a future
node-detail screen may reasonably display observed facts and observed settings together. They
should not become the same data type in the management model.

**Facts** are inventory/evidence mch collects without an intended desired value, policy, or
write-back action, such as firmware version, hardware board, neighbours, or a value selected for
visibility only.

Configured fact keys are checked by `mch validate` against the command registry's known readable
facts and settings. Unknown keys remain legal and retain the ordinary `get <key>` fallback so a
new firmware value can be investigated before the registry is updated, but validation warns and
suggests close registered names to prevent silent repeated reads of a typo.

Status presentation should be able to hide cached fact rows that are no longer configured, for
example after correcting a misspelled fact key. Hiding keeps routine status output clean while
retaining the historical observation in the local database. Permanent deletion should be a
separate explicit cleanup action so operators can review stale rows before discarding them.

**Settings** are managed values with additional meaning: desired value, inheritance from
global/group/node configuration, enforcement or audit policy, comparison status, write attempts,
and a planned action. Even when the current setting is only being observed, retaining that
classification permits it to be enforced later and explains why it appears in a plan or drift
report.

The API and database should therefore retain separate `facts` and `settings` collections. UI
presentation may combine them under an "observed values" heading where useful, while exposing the
desired/policy/action fields for settings.

Separate representation does not justify duplicate mesh traffic. If a key is selected as a
collected fact and is also configured as a setting, one sync cycle should execute its read
command only once and use the single observed value for both records. This is especially
important on a low-bandwidth shared mesh. The sync implementation should eventually plan reads
by resolved command, then feed each result into fact storage and setting comparison/write logic
as required. A write prompted by drift remains a separate deliberate command.

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

`mch plan` is read-only. It compares YAML desired state with the DB's known state and shows:
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
database backup, export, diagnostics, and accidental API exposure security-critical. Any future
secret store needs an explicit encryption, key-management, access-control, redaction, backup,
migration, and recovery design.

Configuration import/export is compatible with an API-first UI without changing this ownership
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

`mch new` is a wizard, not an automated flow. It:

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

- **`mch new` new-node mode**: factory `password` first, then the user-supplied admin password from the
  wizard form. The CLI generates the private key once before the retry loop, so successive
  attempts use the same key. All mutation commands (`set name`, `set guest.password`, `password`,
  `set prv.key`, `set radio`) are idempotent under re-send with identical params, so the whole
  sequence is replayed each retry without stage tracking.
- **`mch new` replacement mode**: factory `password` first, then the managed password from
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

- **Ghost resolution is one-directional**: `mch issues` shows DB-only ghosts, while `mch prune`
  and `mch remove` delete their local DB state. Re-adopting a ghost still requires adding it back
  to config.yaml manually.
- **Commission idempotency**: running `mch new` on a node already in config/DB has
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
  `mch status <label-or-prefix>` as the primary interface. Older `mch node` wording should be
  treated as legacy documentation.
