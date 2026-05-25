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

## API-ready internal model

`mch` should move toward a single daemon-side report/action model that can be rendered by the CLI,
a future HTTP/OpenAPI interface, and compact mesh DM/channel summaries. `mch status` and `mch plan`
should not separately reconstruct desired-vs-observed meaning from raw DB records.

Intended split:
- `mch status [NODE]`: current state and evidence.
- `mch plan [NODE]`: what mchd would do next.
- `mch logs [NODE]`: history.
- `mch suggest [NODE]`: recommendations.
- `mch sync [NODE]`: do work now.

The report model should classify settings as `ok`, `nominal`, `drift`, `unknown`, `ignored`, or
`blocked`, and classify next action as `none`, `read`, `write`, `report`, `ignore`, or `blocked`.
`nominal` means the observed value differs textually but is equivalent within expected firmware or
computer representation tolerance.

Implemented contract: `src/mch/report.py` builds `schema_version: 1` fleet/node reports consumed
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
previously added via an older seeding path. There is currently no `mch forget` command to clean
them up — this is a known gap.

## `mch contacts` vs the DB

`mch contacts` reads **live from the gateway**, not from the DB. It shows REP nodes the gateway
knows about that are not yet in config.yaml. Use it to discover what is visible on the mesh and
decide what to add to config. It is a read-only informational command.

## config.yaml is operator-managed

mchd reads config.yaml at startup and on hot reload. It does not modify it. When `mch commission`
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

The `mch sync <prefix>` command bypasses the schedule for an immediate operator-triggered sync.
`mch sync` (no arg) runs a full pass through all config nodes immediately.

Gateway-owning operations are serialized by a high-level WorkQueue operation lock. Scheduled sync,
manual full sync, forced node sync, raw `mch exec`, and commissioning cannot overlap; manual CLI
operations return `busy: <operation>` if another workflow owns the gateway/radio.

### MeshCore hop/path terminology

In MeshCore, a **hop** is traversal through a repeater in a packet path. It is not a change of
frequency, bandwidth, spreading factor, or coding rate. The upstream MeshCore FAQ documents
zero-hop versus flooded adverts, a firmware hop limit, and packet paths represented by repeater
path hashes:
<https://github.com/meshcore-dev/MeshCore/blob/main/docs/faq.md>.

For normal management traffic, a node is contacted on the gateway's current radio tuple using its
stored MeshCore contact path. If `NodeConfig.via` is set to a different radio tuple, mchd
temporarily switches the gateway radio tuple before contacting the node and restores the home
tuple afterward. The existing `via` field is reachability-radio configuration, not a MeshCore
path; its name should eventually be made less ambiguous. Nodes that have moved radio without an
updated management-radio value can repeatedly time out even though their desired settings are
otherwise valid; recovery path inspection and historical-radio fallback remain future work.

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

## Facts vs settings

A value read from a setting is an observed fact in the ordinary English sense, and a future
node-detail screen may reasonably display observed facts and observed settings together. They
should not become the same data type in the management model.

**Facts** are inventory/evidence mch collects without an intended desired value, policy, or
write-back action, such as firmware version, hardware board, neighbours, or a value selected for
visibility only.

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

`mch commission` is a wizard, not an automated flow. It:

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

- **`commission new`**: factory `password` first, then the user-supplied admin password from the
  wizard form. The CLI generates the private key once before the retry loop, so successive
  attempts use the same key. All mutation commands (`set name`, `set guest.password`, `password`,
  `set prv.key`, `set radio`) are idempotent under re-send with identical params, so the whole
  sequence is replayed each retry without stage tracking.
- **`commission replace`**: factory `password` first, then the managed password from
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

- **No node decommissioning**: removing a node from config.yaml leaves a ghost in the DB.
  A `mch forget <prefix>` command to purge the DB record is not yet implemented.
- **Ghost resolution is one-directional**: `mch issues` shows ghosts but provides no CLI action
  to resolve them — either add to config (paste block) or remove from DB.
- **Commission idempotency**: running `mch commission` on a node already in config/DB has
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
- **Command naming**: the detail view should continue moving toward `mch status <node>` as the
  primary interface. Older `mch node <node>` wording should be treated as legacy documentation.
