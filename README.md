# mch — Mesh Herder

Herding MeshCore nodes.

mch manages fleets of [MeshCore](https://meshcore.io) repeater nodes from a single host. You declare
the desired state for each node in a YAML config file; mchd (the daemon) connects to a
locally-attached gateway node over USB serial, walks the mesh, and brings each managed node into
compliance — one node at a time, without hammering the mesh.

Think Ansible for your nodes, running continuously in the background.

<!--ts-->
   * [Project Docs](#project-docs)
   * [Features](#features)
   * [Requirements](#requirements)
   * [Install](#install)
   * [Configuration](#configuration)
      * [Bench reset / reflash](#bench-reset--reflash)
      * [Node label vs. mesh-visible name](#node-label-vs-mesh-visible-name)
   * [Typical workflow](#typical-workflow)
      * [First run — discover and add nodes](#first-run--discover-and-add-nodes)
      * [Commissioning a new node](#commissioning-a-new-node)
      * [Building a node on the bench](#building-a-node-on-the-bench)
      * [Day-to-day checks](#day-to-day-checks)
      * [Forcing a sync](#forcing-a-sync)
      * [Inspecting and repairing management paths](#inspecting-and-repairing-management-paths)
      * [Active discovery on another radio](#active-discovery-on-another-radio)
      * [Investigating reachability with mch node probe](#investigating-reachability-with-mch-probe)
      * [Checking status in the field](#checking-status-in-the-field)
      * [Scripting with --json](#scripting-with---json)
      * [Changing a setting fleet-wide](#changing-a-setting-fleet-wide)
   * [CLI reference](#cli-reference)
   * [Logs](#logs)
<!--te-->

## Project Docs

| File | Role |
|---|---|
| [`README.md`](README.md) | Project overview, features, configuration, and command reference. |
| [`INSTALL.md`](INSTALL.md) | Development and production install, service setup, udev, and upgrades. |
| [`BACKLOG.md`](BACKLOG.md) | Canonical active todo list, wishlist, deferred ideas, and non-goals. |
| [`CHANGELOG.md`](CHANGELOG.md) | Released version history and operator-visible changes. |
| [`DESIGN.md`](DESIGN.md) | Stable architecture notes and design rationale. |
| [`DEVELOP.md`](DEVELOP.md) | Repeatable bench and development procedures. |
| [`MANUAL_TESTS.md`](MANUAL_TESTS.md) | Luna live-test worksheet with commands and result fields. |
| [`JOURNAL.md`](JOURNAL.md) | Dated implementation decisions and lessons learned. |
| [`tools/`](tools/README.md) | Standalone MeshCore protocol test tools. |

## Features

- **Desired-state config** — declare `owner.info`, `powersaving`, TX power, and any other
  `get`/`set` CLI setting. mchd enforces, audits, or ignores each key according to a configurable
  policy.
- **Read-only facts** — a configurable list of read-only data (firmware version, hardware name,
  role, and more) collected each sync cycle and stored in a local SQLite database.
- **Multi-frequency support** — nodes on different radio settings? Add `via_radio:` globally, to
  a group, or to a node. Node values override group values, which override the global fallback.
  mchd temporarily switches the gateway radio tuple, manages the node, and restores the home
  radio tuple when done. This is not a MeshCore routing hop.
- **Group name prefix** — tag a group on the map with a `name_prefix:` (usually an emoji) that is
  prepended to each member's desired name. Set it globally, on a group, or on a node; the most
  specific wins. No space is added (put one in the prefix if you want it), and mchd refuses to
  start if a `prefix+name` would overflow the 31-byte firmware name limit.
- **Staggered scheduler** — nodes are spread randomly across the sync window so mchd restarts never
  cause a sync surge.
- **DM commands** — authorised MeshCore devices can ask mchd for field status over the mesh:
  `status`, `nodes`, `node <name-or-prefix>`, `issues`, and `logs [n]`. Optional channel
  announcements can post a short cycle summary after each sync.
- **Rate limiting** — mchd never talks to more than one node at a time. After finishing each node
  it waits `inter_node_delay_seconds` before starting the next (skipped if there is only one node
  in the run). Each node is visited at most once per `sync_interval_days`, spread randomly across
  the window.
- **Open-node detection** — mchd warns when a node syncs with no ACL protection (no password
  configured, no login cached). Flagged in the daemon log and in `mch fleet status` so you know
  which nodes anyone on the mesh can freely command.
- **`mch node exec`** — run one-off MeshCore CLI commands against any managed node without adding them
  to config. Destructive commands (reboot, clkreboot) require confirmation; irreversible commands
  (erase, set prv.key) are blocked.
- **`mch node new`** — guided wizard to add a fresh or replacement MeshCore node. Pauses
  the sync queue, switches to the factory default frequency, discovers the node, generates or restores
  a managed private key, pushes name/password/radio config, verifies the expected public key after
  reboot, then prints an importable `nodes:` doc (apply with `mch fleet import`) and the
  `secrets.yaml` snippet. No need to stop mchd.
- **`mch` CLI** — check daemon status, list nodes, force a sync, see gateway contacts not yet managed.
- **`--json` scripting surface** — every read command (`fleet status/issues/plan/logs`,
  `node status`) emits the normalized report model as JSON over the Unix socket, for cron and scripts.

## Requirements

- Python 3.11+
- Linux (tested on Raspberry Pi OS / Ubuntu); macOS works for development
- A MeshCore node attached over USB serial (the gateway)

## Install

See [`INSTALL.md`](INSTALL.md) for development setup, production installation, service setup, the
recommended udev rule, and upgrades.

## Configuration

There are two kinds of state, and they live in two different places:

- **Daemon settings** — the gateway device, scheduler, messenger, and file paths — live in
  `config.yaml`. Copy `examples/config.example.yaml` to `/etc/meshherder/config.yaml` and edit it;
  the example documents every option with comments. `mchd` watches `config.yaml` and `secrets.yaml`
  and reloads them shortly after they change (or on `SIGHUP`). Invalid YAML is rejected and the
  previous in-memory config stays active.
- **Desired state and inventory** — which nodes are managed, their groups and labels, the global and
  per-group defaults, the facts collected, and every desired setting — live in the **SQLite
  database**. You edit them with `mch node set` / `mch node unset` (see *Editing desired state in
  the database* below), and back them up with `mch fleet export`.

The daemon **refuses to start** if `config.yaml` still carries a `nodes:`, `groups:`, `global:`, or
`facts:` desired-state block — those moved to the DB. If you have an older config, migrate it once
with `mch fleet import` (see *Migrating an existing `config.yaml`* below), then strip those blocks.
`mchd` never edits `config.yaml` or `secrets.yaml`.

A minimal working `config.yaml` (daemon settings only):

```yaml
gateway:
  device: /dev/meshcore0
  baud: 115200
  radio:
    freq: 915.075
    bw: 125
    sf: 9
    cr: 5

scheduler:
  sync_interval_days: 1
  inter_node_delay_seconds: 120
```

With the daemon running, bring nodes under management and set their desired state in the DB — see
*Editing desired state in the database* and *Commissioning a new node* below. In short:
`mch node adopt` / `mch node new` add a node to the inventory, then `mch node set` records desired
settings. Run `mch gateway contacts` to list repeater nodes visible to the gateway that are not yet
managed.

Settings default to `audit`: mchd reads and reports drift, but does not write to a node unless the
setting, node, group, or global scope explicitly has `policy: enforce` (or `default_policy: enforce`).
Desired values resolve from global to group to node, with the most specific value winning. Prefer
per-setting `--policy enforce` while bringing a node under management; use `default_policy enforce`
only where most declared settings should be written.

**Passwords:** nodes that require a login go in `secrets.yaml` (owned by `mch:mch`, mode 0600):

```yaml
nodes:
  aabbccdd:
    password: "hunter2"
    guest_password: "guest"    # optional
    prv_key: "deadbeef..."     # generated by mch node new; used for node replacement
```

A fleet-wide default password is supported with the `default` key:

```yaml
nodes:
  default:
    password: "fleetpassword"  # fallback for any node without an explicit entry
  aabbccdd:
    password: "overridepassword"  # takes priority over default
```

mchd only uses the password when it gets a permission error from a node — it does not send it on
every connection. After the first successful login the gateway's public key is added to the node's
ACL, so subsequent syncs proceed without a password. Keep the entry in `secrets.yaml` anyway: if
the ACL is cleared (without a factory reset), mchd can re-enrol automatically using the configured
password. After a factory reset the node's password reverts to unknown, so manual re-enrolment
is required — mchd will mark the node offline until the password is corrected.

**Changing admin passwords:** do not put `password <new>` in recurring operations or treat the
admin password as a normal desired setting. mchd stores one current admin password per node (or the
fleet-wide default) and uses that value when it needs to log in. If a node currently has `hunter2`
and you want it to become `monkey6`, rotate it as an operator action:

1. Make sure you can reach the node and that `secrets.yaml` still contains the current password.
2. Change the password with the commissioning flow for new/replacement hardware, or from a trusted
   direct session where you can recover locally.
3. Immediately edit that node's `secrets.yaml` entry to the new password and reload mchd.
4. Run `mch fleet sync <prefix>` and confirm it can still authenticate.

Avoid relying only on `nodes.default.password` during a rotation. A per-node password entry gives
you an explicit record of the credential that node should accept and avoids accidentally applying a
fleet-wide password assumption to hardware that has drifted.

As a rule of thumb: desired state (audited or enforced by mchd) lives in the DB, edited with
`mch node set`; `secrets.yaml` is for credentials and identity material mchd can use to
authenticate or replace hardware. Secrets are not treated as normal drift-corrected settings. The exception is
commissioning/replacement, where mchd intentionally sends the stored password or `prv_key` to
blank hardware as mesh commands as part of taking ownership of that node. It still does not edit
the YAML files.

`mch service configtest` checks that each configured node's stored `prv_key`, when present, derives a public
key matching the node's `pubkey_prefix`. A mismatch is reported as a warning with the prefix that
the private key actually implies. Do not run replacement commissioning with a mismatched key unless
you intend the replacement hardware to come back under that derived identity.

### Bench reset / reflash

For bench tests and replacement drills, use the explicit `esptool` erase/write flow:

```bash
esptool --chip esp32s3 --port /dev/cu.usbserial-0001 --baud 460800 erase-flash
esptool --chip esp32s3 --port /dev/cu.usbserial-0001 --baud 460800 write-flash 0x0 /path/to/Heltec_v3_repeater-v1.15.0-dee3e26-merged.bin
```

Erase first when you want a real factory reset; writing the merged image alone may preserve old
state outside the written image range. See `DEVELOP.md` for a full example transcript.

### Node label vs. mesh-visible name

`label:` is mch's **local label** for the node — it only ever appears on the host running mchd.
Labels must use only letters, digits, dashes, underscores, and dots (`[a-zA-Z0-9_\-.]+`) because
they are used directly in CLI commands (`mch fleet status east-hill-cab3`). Invalid labels or duplicate
labels cause mchd to refuse to start with a clear error.

`settings.name.desired:` is what gets **enforced on the hardware** and broadcast across the mesh —
visible to anyone scanning. If you don't want your deployment location appearing on the mesh, set a
neutral name here and keep the descriptive one in `label:`. The example below opts in to writes with
`default_policy: enforce`:

```yaml
nodes:
  - pubkey_prefix: aabbccdd
    label: east-hill-cab3               # local only, never leaves this machine
    default_policy: enforce
    settings:
      name:
        desired: "📡 Millbrook"          # pushed to hardware; visible across the mesh
      owner.info: "Managed with Mesh Herder"
```

## Typical workflow

### First run — discover and add nodes

1. Plug in the gateway node and start mchd.
2. Run `mch gateway contacts` to list repeater nodes the gateway can already see but that aren't yet
   managed in the DB. Each entry shows the full pubkey and the ready-to-run `mch node adopt`
   command.
3. Bring a node under management directly (no file editing):

```
mch node adopt aabbccdd1122... --radio 915.075,125,9,5 --label MySite-East
```

   `adopt` adds the node to the DB inventory, installs a direct route, and confirms it answers.
   If the node needs a login password, add it to `secrets.yaml` keyed by its pubkey prefix.
4. Record any desired settings with `mch node set` (see *Editing desired state in the database*),
   for example `mch node set MySite-East name "My Repeater" --policy enforce`.
5. Run `mch fleet status` to confirm the node is now known, then `mch fleet sync <prefix>` to sync it
   immediately without waiting for the scheduler.

   To onboard many nodes at once, write them into an `examples/desired.example.yaml`-style file and
   seed the DB with `mch fleet import <file> --yes` instead of adopting each one.

### Commissioning a new node

Use `mch node new` to take a freshly-flashed node from zero config to fully managed without
stopping mchd. The wizard supports two modes:

**New node** — node starts with a factory-generated identity:

1. Power on the fresh node near the gateway (factory default frequency: 869.618 MHz, 62.5 kHz, SF8, CR5).
2. Run `mch node new` and select **n** (new).
3. Enter the label, group, mesh-visible name, target frequency, and admin password. The radio
   prompt offers the official regional presets (Australia, NZ, EU/UK, USA/Canada, …), vendored
   offline from the community [MeshCore Ninja](https://meshcore.ninja) catalogue (CC0) — no network
   call. Maintainers refresh them with `python tools/refresh_meshcore_networks.py`.
4. The daemon discovers the node, generates and sets a new private key, sets the name, password,
   and radio, then reboots the node.
5. `mch node new` prints an importable `nodes:`/`groups:` YAML doc (save it to a file) and the
   `secrets.yaml` snippet.
6. Commissioning verifies that the generated public key is visible on the target frequency before
   returning success.
7. Add the password to `secrets.yaml`, reload mchd so it seeds the new node into the DB from gateway
   favourites, then `mch fleet import <file> --yes` to record the desired state. Run
   `mch fleet sync <prefix>` if you want to sync immediately.

**Replacement node** — restoring a known identity onto new hardware:

Prerequisites: `prv_key` must already be in `secrets.yaml` for the node you are replacing. For
nodes commissioned by mch, this was printed in the original `secrets.yaml` snippet.

1. Power on the blank hardware near the gateway.
2. Run `mch node new` and select **r** (replace).
3. Select the node from the list of replaceable nodes.
4. The daemon discovers the blank hardware, restores the private key (restoring the original mesh
   identity), sets the password and radio frequency, then reboots.
5. After reboot the node comes back with the original pubkey — mchd will sync it on the next cycle
   (or use `mch fleet sync <prefix>` to sync immediately).

> **Note:** During commissioning the daemon discovers unmanaged nodes on the factory default
> frequency (869.618 MHz). If more than one unmanaged node is in range, commissioning is aborted —
> commission one node at a time.

### Building a node on the bench

Power the node on near the gateway. MeshCore nodes broadcast advertisements on startup; within a
minute or two the gateway will have stored the contact. Run `mch gateway contacts` again to see it,
then bring it under management with the printed `mch node adopt` command. If the node needs a login
password, add it to `secrets.yaml` before syncing.

### Day-to-day checks

```
mch fleet status                  # fleet view: state, label, group, firmware, neighbours, last seen
mch fleet status <label|prefix>   # filtered table (single match hints `mch node status`)
mch node status <label|prefix>      # full detail for one node (facts, settings, schedule, activity)
mch service configtest                # validate the daemon's /etc/meshherder/config.yaml
mch fleet suggest                    # suggested txdelay for every node based on neighbour count
mch fleet suggest <label|prefix>     # full suggestions + the `mch node set` commands to apply them
mch gateway contacts                # gateway contacts not yet managed in the DB
mch node discover --radio F,B,S,C  # active bounded scan on a temporary gateway radio
```

`mch service configtest` also warns about unrecognised names in `facts:` and suggests close known
readable values, for example `powersavings` to `powersaving`. Unknown fact keys are still allowed
so newly added firmware reads can be evaluated before mch metadata catches up.

### Editing desired state in the database

Desired-state **scope** is the noun. The resolution hierarchy node > group > fleet maps onto three
nouns that share the same verbs, so `mch`'s vocabulary reads straight off this grid:

| scope | overview / detail | issues | desired state | sync | extras |
|---|---|---|---|---|---|
| **fleet** | `fleet status` | `fleet issues` | `fleet set KEY VAL` | `fleet sync` | `export` / `import` |
| **group NAME** | `group status [NAME]` | `group issues NAME` | `group set NAME KEY VAL` | `group sync NAME` | `create` / `rename` / `delete` |
| **node NODE** | `node status NODE` | `node issues NODE` | `node set NODE KEY VAL` | `node sync NODE` | `move` / `retire` / `exec` / … |

A node's own value beats its group's, which beats the fleet-wide default. (`service` and `gateway`
round out the noun set; the old `config` noun is gone.)

Desired state and inventory live in the SQLite database; `config.yaml` carries only daemon settings
(gateway, scheduler, messenger, api, paths, profiles). **The daemon refuses to start if
`config.yaml` still contains a `nodes:`, `groups:`, `global:`, or `facts:` desired-state block** — it points
you at `mch fleet import`. The DB is edited directly by `mch`. Writes
are synchronous, so `mch fleet status <node>` shows the new intent immediately; convergence still waits for
the next sync (or `mch fleet sync <node>`).

```
mch node set <prefix> txdelay 14            # node scope
mch group set solar txdelay 20              # group scope
mch fleet set default_policy audit          # fleet-wide default (groups and nodes may override)
mch fleet set ver --policy fact             # collect a read-only fact (no value); policy: enforce/audit/fact/skip
mch node set <prefix> name_prefix "🌞 "      # directives: name_prefix, default_policy, via_radio
mch node unset <prefix> txdelay             # remove a DB-set value (observed state untouched)
mch node status <prefix>                    # node detail: facts, desired settings, schedule, activity
mch node status <prefix> txdelay            # trace one key: per-scope rows, resolved value, observed
mch group status deployed                   # a group's desired rows + member nodes (bare lists all groups)
mch fleet import                           # preview seeding the DB from the daemon's config (dry run)
mch fleet import legacy.yaml --yes         # seed from an explicit file (settings + label/group inventory)
mch fleet export > backup.yaml             # complete fleet backup (inventory + desired state)
mch node clean settings <prefix>          # drop observed rows for keys no longer desired (dry run)
```

**Migrating an existing `config.yaml`:** on the running daemon, `mch fleet import` (then `--yes`) to seed
the DB, `mch fleet export` to verify, then delete the `nodes:`/`groups:`/`global:`/`facts:` blocks from
`config.yaml` (keep the daemon settings) and restart. Import folds a legacy `facts:` list into global
`policy: fact` rows. When importing a *legacy* config (nodes given
by short `pubkey_prefix`), a node must already be in the DB — seen as a favourited gateway contact —
for import to resolve its full pubkey; unseen nodes are skipped with a warning until they appear.

**Backup and restore.** Because the DB is now the only copy of desired state, keep periodic
`mch fleet export > backup.yaml` snapshots (a daily cron/systemd timer is ideal). The export is a complete
backup — every node's full public key, label, group, desired settings, and operations, with retired
nodes tagged `retired: true`. To restore after a server or gateway replacement, point a fresh daemon
at a clean `config.yaml` (daemon settings only) and run `mch fleet import backup.yaml --yes`: because the
backup carries full public keys, import recreates every node up-front even before the new gateway has
favourited them, and the favourite mirror re-pins each node as its contact reappears on the mesh.
If the backup contains retired nodes, import makes you choose: `--retired keep` restores them as
retired, `--retired skip` omits them (a write is refused until you pick one). A plain dry-run
preview just warns that they are present.

`mch fleet status <prefix>` settings table reads `POLICY / KEY / DESIRED / OBSERVED / TRIES / STATUS`
(no source column — the DB is the only source now).

### Forcing a sync

```
mch fleet sync <prefix>            # sync one node and print progress while waiting for the result
mch fleet sync                     # sweep all nodes in the background
```

Use `mch fleet sync <prefix>` after editing a node's desired settings in config — it applies the changes
immediately rather than waiting for the next scheduled cycle. MeshCore replies can be slow during
normal operation, so foreground node sync always prints the effective management radio, cached
contact path, and each read/write wait as it runs. The path line is cached gateway information,
not a fresh reachability test. Manual sync start, completion, failure, and abort are also retained
in `mch fleet logs`.

Each sync starts with a cheap reachability probe (a bare `ver` read, retried once). If the node
answers neither attempt it is marked unreachable and the rest of the pass is skipped, rather than
timing out on every fact and setting. When the node answers, that `ver` reply is reused as the
`ver` fact. Independent fact and setting reads are then issued in a randomized order each cycle, so
a marginal link does not keep losing the same trailing observations.

Reachability and path behavior is deliberately conservative:

- mch uses the gateway's cached MeshCore contact path for normal management. If that contact has a
  stored repeater path, reads and writes use that path. If the contact is flood-routed, mch sends
  only a reachability probe and defers the full sync until the path is repaired.
- On a stored path, isolated fact/setting read misses get a small retry budget for that node pass.
  This helps lossy links without retrying every command or increasing traffic without bound.
- Enforced writes are not retried over flood after a stored-path failure. A missed write remains a
  failed write for that cycle so the operator can see it and avoid duplicate side effects.
- If both initial `ver` probes fail on a stored path, mch sends one flood-routed `ver` probe as a
  recovery signal. If flood reaches the node, mch fleet logs that the stored path needs repair, but it
  does not continue the full sync over flood.
- If normal reachability fails for a node with configured radio context, including a flood-routed
  contact that does not answer on the gateway's current radio, mch may try up to three radio
  recovery candidates from configured radios. The exact effective `via_radio` or desired `radio`
  is tried before nearby spreading-factor variants. The gateway radio is restored afterward.
- Successful enforced setting writes are recorded in the local `setting_history` table with the
  previous observed value and the command used, giving a rollback breadcrumb for radio and other
  managed settings.
- Scheduled syncs use the same rules as manual syncs. A node due once per `sync_interval_days`
  can therefore get one flood `ver` recovery probe per scheduled attempt when its stored path is
  failing, plus any manual syncs the operator explicitly requests.
- Path repair is explicit: use `mch node path <node> --discover` or `--advert` to learn a candidate,
  then `mch node path <node> --apply <path[:mode]>` to persist an operator-approved stored path on the
  gateway contact. Rerun `mch fleet sync <node>` after applying the path.

### Inspecting and repairing management paths

`mch node path NODE` is the read-only route inspection tool. `NODE` is required and may be a local
label, mesh name substring, or pubkey prefix:

```bash
mch node path wip06
mch node path c041a906d246
```

The command reads the gateway's cached MeshCore contact metadata. It does not send a probe to the
node and it does not prove the route works right now.

Example output:

```text
c041a906d246  wip06  wip 06
cached path: flood
route: flood (path repair needed)
gateway contact:
  name: wip 06
  type: 2
  flags: 1
  out_path: -
  out_path_len: -1
  out_path_hash_mode: -1
```

Interpret the fields as follows:

- `cached path`: human-readable summary of the gateway contact route.
- `route`: `direct`, `stored`, `flood`, `missing`, or `unknown`.
- `path repair needed`: normal management will not run a full sync over this cached route.
- `out_path`: repeater path hash(es) used for a stored route; `-` means none is stored.
- `out_path_len`: `0` means direct, a positive number means stored repeater hops, and `-1` means
  flood routing.
- `out_path_hash_mode`: MeshCore path hash mode to preserve when applying a replacement path.

For a flood-routed or stale route, inspect candidates first:

```bash
mch node path wip06 --discover
mch node path wip06 --advert
```

Both commands print a candidate and an `apply with:` line when they find one. Review the candidate,
then persist it only if it is the route you want the gateway to use:

```bash
mch node path wip06 --apply 2fd9:1
mch fleet sync wip06
```

Use `--apply` carefully: it changes the gateway's cached contact path. A wrong path can make normal
management less reachable until repaired again.

### Active discovery on another radio

`mch node discover --radio FREQ,BW,SF,CR` sends a bounded MeshCore discovery request on a temporary
gateway radio and restores the gateway radio afterward:

```bash
mch node discover --radio 915.800,250,10,5 --dwell 5
```

This is not an advert wait and it does not edit `config.yaml`, seed the DB, or add gateway
contacts. It only proves which nodes answer on that radio and prints their full public keys, type,
and SNR. Use it before a future adopt/contact-seeding step, or when checking whether a node is
alive on a non-home radio.

Discovery also annotates each result:

- `managed`: known in both DB and config.
- `db-only`: known in DB but not config.
- `config-only`: matches a configured prefix but is not in DB yet.
- `unmanaged`: not known in DB or config.

Use `--unknown-only` to hide fully managed nodes while keeping `config-only`, `db-only`, and
`unmanaged` results:

```bash
mch node discover --radio 915.800,250,11,5 --unknown-only
```

After identifying the full public key, `mch node adopt` can add a running repeater to local inventory
and the gateway contact store without recommissioning it:

```bash
mch node adopt 8e4a0d73ab612e8cc08cec29aa0df62e46c76614ab0449901de8e0dd3b5fef95 \
  --label faulkland \
  --radio 915.800,250,11,5
```

Adopt creates or reuses the gateway contact, installs a direct (zero-hop) route on it, then sends
one directed `ver` on the discovered radio to confirm the route carries a command (restoring the
gateway radio afterwards). Path/radio repair may still be needed before normal management is
reliable; the confirm-probe result is shown in the output and recorded in `mch fleet logs`.

### Investigating reachability with `mch node probe`

`mch node probe NODE --radio FREQ,BW,SF,CR` is a read-only reachability check: it temporarily switches
the gateway radio, sends one directed `ver`, restores the radio, and reports the result. It changes
nothing on the node, the contact route, config, or secrets.

```bash
mch node probe faulkland --radio 915.800,250,11,5
mch node probe faulkland --radio 915.800,250,11,5 --login    # auth-required nodes
mch node probe faulkland --radio 915.800,250,11,5 --advert   # announce the gateway first
```

`--login` authenticates with the node's configured password before probing (an auth-required node
silently drops unauthenticated commands, so a bare `ver` times out). `--advert` sends a gateway
zero-hop advert on the radio first, so the node can learn a route back.

### Checking status in the field

Authorised MeshCore devices can send a DM to the gateway for lightweight status while away from the
host:

```text
status
nodes
node <name-or-prefix>
issues
logs 10
```

Configure authorised sender prefixes under `messenger.allowed_prefixes` in `config.yaml`.

### Scripting with `--json`

There is no HTTP API — the read commands emit the normalized report model as JSON with `--json`,
which is the whole scripting/integration surface. It goes over the same Unix socket as every other
command, so there's no open port, no loopback caveat, and no authentication to design. Available on
`fleet status`, `fleet issues`, `fleet plan`, `fleet logs`, and `node status`.

```bash
# Nodes not seen in the last 24 hours (labels), straight from the CLI:
mch fleet status --json | jq -r --arg cutoff "$(date -u -d '24 hours ago' +%FT%T)" \
  '.nodes[] | select((.last_seen // "0") < $cutoff) | .label'

# Which nodes are behind the latest firmware the catalogue knows:
mch fleet status --json | jq '.firmware'
```

`mch fleet status --json` returns `schema_version: 2`, a health summary, the managed nodes, issues,
an advisory `firmware` block (which nodes trail the latest known release), and planned work. Each
node includes `health`, configured `fact_keys`, collected `facts` such as `lat`/`lon`, and settings
classified with `status` (`ok`, `nominal`, `drift`, `unknown`, `ignored`) and `action` (`none`,
`read`, `write`, `report`, `ignore`). It never contacts nodes or makes changes. (If a web UI is ever
built, it should consume this same model — rebuilt as a read API against the stable `--json`
contract, not the retired loopback server.)

```bash
# Nodes not seen in the last 24 hours (labels), straight from the CLI:
mch fleet status --json | jq -r --arg cutoff "$(date -u -d '24 hours ago' +%FT%T)" \
  '.nodes[] | select((.last_seen // "0") < $cutoff) | .label'

# Which nodes are behind the latest firmware the catalogue knows:
mch fleet status --json | jq '.firmware'
```

`mch fleet issues --json` keeps the exit codes below (machine-readable output, machine-readable
code): it exits `2` when any issue is present. Firmware age is **advisory** — it appears in the
`firmware` block and as a footer line, but never as an issue, so it never sets exit `2`. In the
`mch fleet status` table an outdated node is flagged with a trailing `!` in the VER column.

### Changing a setting fleet-wide

Set the value at group or fleet scope in the DB — `mch group set repeaters powersaving on
--policy enforce`, or `mch fleet set …` for the whole fleet — then run `mch fleet sync` to
push the change to all nodes without waiting for the scheduler. (Assign a node to a group with
`mch fleet import` or during commissioning.)

## CLI reference

The same command summary is available as a manual page after install. The daemon
has its own operational page:

```bash
man mch
man mchd
```

```
mch status                          Shortcut for `mch fleet status` (the daily loop)
mch sync                            Shortcut for `mch fleet sync`
mch help selectors                  How to name a node on the command line
mch help concepts                   Desired vs reported, policy, scope, sync, exit codes
mch help states                     What each node STATE means (configured, partial, unreachable, …)
mch fleet status                    Fleet overview: state, label, group, firmware (VER, ! = outdated), neighbours, last seen
mch fleet status <label|prefix>     Filtered table; a single match hints `mch node status`
                              (add --json to fleet status/issues/plan/logs, node status for scripting)
mch node status <label|prefix>        Full detail for one node: facts, all desired settings (config +
                              group, including pending ones not yet synced), last seen/last conn
                              and approximate next sync with timestamp + relative time, activity logs
mch service configtest                  Validate /etc/meshherder/config.yaml
mch service configtest --config <path>  Validate an explicit config file
mch fleet plan                      Desired-vs-known changes; no mesh traffic and no writes
mch fleet plan <label|prefix>       Filter planned writes/audit drift/unknown values
mch fleet issues [label|prefix]     Just the issue sections; exits 2 when any issue is present
mch node status <label|prefix>        Full detail for one node (facts, settings, schedule, activity)
mch node move <label|prefix> GROUP    Move a node into GROUP (must exist); `-` as GROUP ungroups it
mch node label <label|prefix> [TEXT]   Set (or, TEXT omitted, clear) a node's host-only label
mch node path <label|prefix>       Show cached gateway management route metadata; no mesh probe
mch node path <node> --discover    Learn a fresh path-discovery candidate without applying it
mch node path <node> --advert      Show the gateway's cached advert path candidate
mch node path <node> --apply P[:M] Apply an operator-approved replacement path to the gateway contact
mch node discover --radio R        Actively discover nodes on a temporary radio; no DB/contact writes
mch node discover --radio R --unknown-only  Hide fully managed discovery results
mch node adopt PUBKEY --radio R    Add a discovered running node to DB + gateway contact (direct route)
mch node probe <node> --radio R [--login] [--advert]  Read-only reachability check on a given radio
mch fleet suggest                      Suggested txdelay for all nodes based on neighbour count
mch fleet suggest <label|prefix>       Full suggestions + the `mch node set` commands for one node
mch fleet logs                      Recent per-node activity across all nodes
mch fleet logs <prefix>             Activity for one node
mch gateway contacts                  Repeater nodes visible to the gateway not yet managed in the DB
mch fleet import [FILE]             Seed DB inventory + desired state from config.yaml or a FILE (dry run)
mch fleet import [FILE] --yes       Write it (idempotent; full-pubkey nodes created even if unseen)
mch fleet export > backup.yaml      Complete fleet backup as YAML (inventory + desired; inverse of import)
mch node retire <label|prefix>     Stop managing a node: skip sync + unfavourite, keep history
mch node unretire <label|prefix>   Return a retired node to management (re-favourite + reconverge)
mch node prune                     List all retired nodes (hard-delete candidates)
mch node prune --yes               Hard-delete every retired node (facts, settings, history)
mch node prune <label|prefix>      Preview deleting one retired node (--yes to delete; refuses if still managed)
mch node clean facts --all         List cached facts no longer configured as fact-policy keys
mch node clean facts NODE --fact K Delete one stale fact key with --yes
mch fleet sync                      Trigger an immediate sync cycle (respects rate limiting)
mch fleet sync <prefix>             Sync one node; stream progress while waiting for the result
mch node exec <label|prefix> <command>  Run one raw remote command through mchd over the mesh
mch gateway admin                     Stop mchd, open local serial meshcore-cli, then restart mchd
mch node new                       Add a fresh or replacement MeshCore node (interactive wizard)
mch service start                     Start mchd via systemd (alias: sudo systemctl start mchd)
mch service stop                      Stop mchd via systemd
mch service restart                   Restart mchd via systemd
mch service status                    Show service status (mirror of systemctl status mchd)
mch service logs [-n N] [-f]          Show the daemon process log from journald (not per-node activity)
```

New to the vocabulary? `mch help selectors` explains how to name a node, and `mch help concepts`
covers desired-vs-reported values, policy (enforce/audit/fact/skip), scope precedence, what a sync
cycle does, and the exit codes below.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | error (bad usage, daemon unreachable, command failed) |
| `2` | issues present — **`mch fleet issues` only** |

`mch fleet issues` exits `2` when any issue (drift, a problem state, an open node) is present, so a
cron job can alert on it: `mch fleet issues >/dev/null || notify-me`. `mch fleet status` is a
viewing command and always exits `0` — use `mch fleet issues` for health checks.

`mch service configtest` checks whatever it can read. On a production install, an ordinary operator account
can validate the group-readable config file but may need `sudo mch service configtest` to validate the
mode-600 secrets file as well.

`mch gateway admin` may need privilege to stop/start `mchd`. When run through `sudo`, it drops the
`meshcore-cli` process back to the invoking user so the interactive serial session is not run as
root.

To stop managing a node, `mch node retire <label|prefix>`: it sets the node's state to `retired`,
unfavourites its gateway contact (clears the eviction pin; the contact itself is kept), and excludes
it from sync — while keeping its history. `mch node unretire` reverses it (re-favourite and reconverge).
Retire-don't-delete is the normal way to decommission a node.

`mch node prune` then hard-deletes retired nodes (their local facts, settings, operations, and logs) —
the deliberate purge step. It previews first; `mch node prune --yes` deletes. With no argument it
acts on every retired node; give it a `<label|prefix>` to delete just that one. A node that is still
managed is refused ("retire it first") — an un-retired node is re-seeded from gateway favourites on
the next restart, so retire it before pruning. It does not edit `secrets.yaml`, so operators may keep
passwords or private keys there for replacement hardware.

`mch node clean facts` is also dry-run first. Use it after changing `facts:` to review cached fact rows
that are no longer collected, then add `--yes` to delete them. It can be scoped to one node,
`--group <name>`, or `--all`, and `--fact <key>` narrows the cleanup to a single key such as a
misspelled `powersavings`.

`mch fleet sync <prefix>` runs that node immediately and waits, streaming progress because slow command
replies are normal on a mesh network. It collects facts, checks and enforces settings, runs any
due operations, then reschedules the node back into normal rotation. Use this when you need a
result right now:

```
mch fleet sync aabbccdd    # print radio/path context, reads, timeouts, writes, and operations
```

`mch fleet sync` (no argument) starts a full sweep of every node in the background — the CLI returns
"sync started" immediately and mchd works through the list in config order, pausing
`inter_node_delay_seconds` between each node, exactly as a scheduled cycle would. With many nodes
and a long delay this can take a while; check `mch fleet status` to watch progress.

## Logs

mchd logs to the systemd journal. Follow live output with:

```bash
journalctl -u mchd -f
```

A normal startup and sync looks like this:

```
mchd: gateway connected: name='🤖 mch-gw' freq=915.075 bw=125 sf=9 cr=5
mchd: work queue started (interval=1.0d)
mchd: processing node '📡 Millbrook' (aabbccdd...)
mchd: management radio: 916.575/62.5/SF7/CR8 (via_radio temporary switch)
mchd: cached management path: flood
mchd: switching gateway radio to 916.575/62.5/SF7/CR8 for '📡 Millbrook'
mchd: switching gateway radio: 915.075/125/SF9/CR5 -> 916.575/62.5/SF7/CR8
mchd: radio restored: 915.075/125/SF9/CR5
mchd: '📡 Millbrook' done — facts:5 settings:1/1 enforced drift:0 ops:0/0 failed
```

Per-node activity (settings changes, operation results, and fact/setting read failures) is stored in the local
database and shown by `mch fleet status <prefix>` and `mch fleet logs`:

```
mch fleet status aabbccdd

  state:       configured
  ...
  recent activity:
    [INFO ] 2026-05-18 09:27  set owner.info='Managed with Mesh Herder' (was '')
    [INFO ] 2026-05-18 09:27  operation ok: 'clock sync'
```

```
mch fleet logs               # recent activity across all nodes
mch fleet logs aabbccdd      # filtered to one node (prefix ok)
mch fleet logs -n 100        # last 100 entries
```

If a node shows as `partial`, `mch fleet logs <prefix>` will show what failed:

```
2026-05-25 17:03 [WARNING] read advert.interval failed: timeout - no response from node
```

`TRIES` in status/issue output is write-attempt count, not read-timeout count. A timed-out
enforced setting read cannot be compared or written and therefore does not increment `TRIES`;
it appears in `mch fleet logs` and in the final partial-sync result instead.

Operator-triggered syncs add activity markers around this output: `mch fleet sync <prefix>` records
start and completion, failure, or abort on the node, while `mch fleet sync` records equivalent
fleet-level markers. This distinguishes manual investigation from scheduled sync history.

The systemd journal remains the daemon/service diagnostic log, including startup, service
failure, and radio/session debugging. `mch fleet logs` is the node activity history; `mch fleet status`
reports mesh state. For daemon process health use `systemctl status mchd --no-pager`.
