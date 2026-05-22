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

## Known design gaps

- **No node decommissioning**: removing a node from config.yaml leaves a ghost in the DB.
  A `mch forget <prefix>` command to purge the DB record is not yet implemented.
- **Ghost resolution is one-directional**: `mch issues` shows ghosts but provides no CLI action
  to resolve them — either add to config (paste block) or remove from DB.
- **Commission idempotency**: running `mch commission` on a node already in config/DB has
  undefined behaviour. Should detect and reject.
- **Limited hot-reload scope**: node/group/secrets/messenger changes reload live. Gateway device,
  socket path, and DB path changes still require a daemon restart.
- **Factory reset notes**: for bench testing and replacement drills, documentation with the exact
  `esptool` erase/write commands is enough for now. This should stay outside normal sync and
  commissioning automation.
- **Command naming**: the detail view should continue moving toward `mch status <node>` as the
  primary interface. Older `mch node <node>` wording should be treated as legacy documentation.
