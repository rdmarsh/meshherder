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

## Node identity

**The public key is the stable node identity.** Names can change. Labels are local-only (in
config.yaml, never pushed to the node). The mesh-visible name (`settings.name.desired`) is
pushed to the node hardware and visible to other mesh participants.

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

The hardcoded setting policy fallback is `audit`. A desired setting is read and compared by
default, but mchd only writes to a node when the setting, node, group, or top-level `global:`
block explicitly opts in with `policy: enforce` or `default_policy: enforce`. Desired values
resolve from `global:` to group to node, with the most specific value winning.

## Settings vs operations

**Settings** model desired state for a key that can be read back. Each sync cycle mchd sends
`get <key>`, compares the result to `desired`, and acts according to the policy (`audit`,
`enforce`, `skip`). The comparison gives convergence: if a node drifts or gets reflashed, the
next sync detects it and corrects it (under `enforce`).

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
