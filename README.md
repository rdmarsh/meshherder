# mch — Mesh Herder

Herding MeshCore nodes.

mch manages fleets of [MeshCore](https://meshcore.io) repeater nodes from a single host. You declare
the desired state for each node in a YAML config file; mchd (the daemon) connects to a
locally-attached gateway node over USB serial, walks the mesh, and brings each managed node into
compliance — one node at a time, without hammering the mesh.

Think Ansible for your nodes, running continuously in the background.

## Project Docs

| File | Role |
|---|---|
| [`README.md`](README.md) | User-facing install, configuration, and command reference. |
| [`BACKLOG.md`](BACKLOG.md) | Canonical active todo list, wishlist, deferred ideas, and non-goals. |
| [`DESIGN.md`](DESIGN.md) | Stable architecture notes and design rationale. |
| [`DEVELOP.md`](DEVELOP.md) | Repeatable bench and development procedures. |
| [`JOURNAL.md`](JOURNAL.md) | Dated implementation decisions and lessons learned. |
| [`SUGGEST.md`](SUGGEST.md) | Historical codebase review notes; move active items to `BACKLOG.md`. |

## Features

- **Desired-state config** — declare `owner.info`, `powersaving`, TX power, and any other
  `get`/`set` CLI setting. mchd enforces, audits, or ignores each key according to a configurable
  policy.
- **Read-only facts** — a configurable list of read-only data (firmware version, hardware name,
  role, and more) collected each sync cycle and stored in a local SQLite database.
- **Multi-frequency support** — nodes on different radio settings? Add `via:` to the node entry;
  mchd hops the gateway, manages the node, and restores the home frequency when done.
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
  configured, no login cached). Flagged in the daemon log and in `mch issues` so you know which
  nodes anyone on the mesh can freely command.
- **`mch exec`** — run one-off MeshCore CLI commands against any managed node without adding them
  to config. Destructive commands (reboot, clkreboot) require confirmation; irreversible commands
  (erase, set prv.key) are blocked.
- **`mch commission`** — guided wizard to commission a fresh or replacement MeshCore node. Pauses
  the sync queue, hops to the factory default frequency, discovers the node, generates or restores
  a managed private key, pushes name/password/radio config, verifies the expected public key after
  reboot, then prints ready-to-paste snippets for `config.yaml` and `secrets.yaml`. No need to
  stop mchd.
- **`mch` CLI** — check daemon status, list nodes, force a sync, see nodes not yet in config.
- **Read-only fleet API** — optional loopback HTTP JSON API with normalized node health, settings
  actions, facts, issues, plan data, and logs, ready for a later dashboard/map UI.

## Requirements

- Python 3.11+
- Linux (tested on Raspberry Pi OS / Ubuntu); macOS works for development
- A MeshCore node attached over USB serial (the gateway)

## Install

### Development

```bash
git clone https://github.com/rdmarsh/meshherder
cd meshherder
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`mchd` and `mch` are then available inside the venv.

### Production

Use this for a personal server, Raspberry Pi, or other always-on host with a MeshCore gateway node
permanently attached over USB. Clone to a permanent location, create a venv, and install into it:

```bash
sudo git clone https://github.com/rdmarsh/meshherder /opt/meshherder
sudo python3 -m venv /opt/meshherder/.venv
sudo /opt/meshherder/.venv/bin/pip install /opt/meshherder
```

Symlink the binaries onto the system PATH so the service file works as-is:

```bash
sudo ln -sf /opt/meshherder/.venv/bin/mchd /usr/local/bin/mchd
sudo ln -sf /opt/meshherder/.venv/bin/mch  /usr/local/bin/mch
```

Install the manual pages:

```bash
sudo install -D -m 644 docs/man/mch.1 /usr/local/share/man/man1/mch.1
sudo install -D -m 644 docs/man/mchd.8 /usr/local/share/man/man8/mchd.8
```

#### Create the system user and directories

```bash
# Dedicated system account — no home dir, no login shell
sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
    --comment "Mesh Herder" mch
# Grant serial port access
sudo usermod -aG dialout mch

# Config directory
sudo install -d -o root -g mch -m 750 /etc/meshherder

# Config and secrets
sudo install -o root -g mch -m 640 examples/config.example.yaml /etc/meshherder/config.yaml
sudo install -o mch -g mch -m 600 examples/secrets.example.yaml /etc/meshherder/secrets.yaml

# Database directory
sudo install -d -o mch -g mch -m 755 /var/lib/meshherder
```

#### Add yourself to the mch group

The `mch` CLI talks to the daemon over a Unix socket owned by the `mch` group. Any user who needs
CLI access must be in that group:

```bash
sudo usermod -aG mch <your-username>
```

Log out and back in (or run `newgrp mch`) for the membership to take effect.

#### Install and start the service

```bash
sudo cp systemd/mchd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mchd
```

Check it started:

```bash
sudo systemctl status mchd
mch status
```

> **Note:** the systemd unit is named `mchd` — always use `systemctl ... mchd`, not `mch`.
> The `mch start`, `mch stop`, and `mch restart` CLI commands are shortcuts to the same thing.

## udev rule (recommended)

By default a MeshCore node appears as `/dev/ttyACM0` or similar, and that path can change between
reboots if you have other USB serial devices. A udev rule creates a stable symlink at
`/dev/meshcore0` so your config never needs updating, and lets systemd start mchd automatically
when the gateway is plugged in.

**Find your device's vendor ID, product ID, and serial number:**

Plug in the gateway node, then run:

```bash
udevadm info --name=/dev/ttyACM0 --attribute-walk \
    | grep -E 'idVendor|idProduct|serial' | head -8
```

Use the values from the **first** (innermost) block — that is the USB device itself, not the hub.
For example:

```
ATTRS{idVendor}=="303a"
ATTRS{idProduct}=="0002"
ATTRS{serial}=="441BF66F9FDC"
```

Common vendor/product IDs for MeshCore hardware:

| Chip | idVendor | idProduct |
|---|---|---|
| ESP32-S3 (most recent MeshCore hardware) | `303a` | `0002` |
| Seeed XIAO (nrf52) | `2886` | `0045` |
| CP2102 | `10c4` | `ea60` |
| CH340 | `1a86` | `7523` |

**Create the rule:**

Create `/etc/udev/rules.d/99-meshcore.rules`. If you know the serial number of your gateway
device (recommended — prevents the rule matching a different device of the same type):

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ATTRS{serial}=="441BF66F9FDC", \
  SYMLINK+="meshcore0", GROUP="dialout", MODE="0660", \
  TAG+="systemd", ENV{SYSTEMD_WANTS}="mchd.service"
```

Or match by vendor and product alone (simpler, but matches any device of that type):

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="0002", \
  SYMLINK+="meshcore0", GROUP="dialout", MODE="0660", \
  TAG+="systemd", ENV{SYSTEMD_WANTS}="mchd.service"
```

Replace the `idVendor`, `idProduct`, and `serial` values with the ones from your hardware.

The `TAG+="systemd"` line makes the device visible as a systemd device unit
(`dev-meshcore0.device`), which is what `mchd.service` waits on at boot.

**Reload udev and test:**

```bash
sudo udevadm control --reload-rules
# Unplug and replug the gateway, then verify:
ls -l /dev/meshcore0
```

Set the `device` path in `/etc/meshherder/config.yaml` to `/dev/meshcore0`.

## Configuration

Copy `examples/config.example.yaml` to `/etc/meshherder/config.yaml` and edit it. The example file
documents every available option with comments.

`config.yaml` and `secrets.yaml` are the source of truth for desired state. The SQLite database is
observed state: last seen times, facts, command history, and sync schedule. `mchd` watches both YAML
files and reloads them automatically shortly after they change. You can also send `SIGHUP` to the
daemon to force a reload. Invalid YAML is rejected and the previous in-memory config stays active.
`mchd` never edits `config.yaml` or `secrets.yaml`; commands such as `mch contacts` and
`mch commission` print snippets for the operator to review and paste.

A minimal working config:

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

global:
  settings:
    owner.info: "Managed with Mesh Herder"

nodes:
  - pubkey_prefix: aabbccdd
    label: MyRepeater
    settings:
      name:
        desired: "My Repeater"
        policy: enforce
```

Run `mch contacts` to list repeater nodes visible to the gateway that are not yet in your config,
with pubkey prefixes ready to paste in.

Settings default to `audit`: mchd reads and reports drift, but does not write to a node unless the
setting, node, group, or top-level `global:` block explicitly says `policy: enforce` or
`default_policy: enforce`. Desired values resolve from `global:` to group to node, with the most
specific value winning. Prefer per-setting `policy: enforce` while bringing a node under management;
use `default_policy: enforce` only where most declared settings should be written.

**Passwords:** nodes that require a login go in `secrets.yaml` (owned by `mch:mch`, mode 0600):

```yaml
nodes:
  aabbccdd:
    password: "hunter2"
    guest_password: "guest"    # optional
    prv_key: "deadbeef..."     # generated by mch commission; used for node replacement
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
4. Run `mch sync <prefix>` and confirm it can still authenticate.

Avoid relying only on `nodes.default.password` during a rotation. A per-node password entry gives
you an explicit record of the credential that node should accept and avoids accidentally applying a
fleet-wide password assumption to hardware that has drifted.

As a rule of thumb: `config.yaml` is for ordinary desired state that mchd may audit or enforce;
`secrets.yaml` is for credentials and identity material mchd can use to authenticate or replace
hardware. Secrets are not treated as normal drift-corrected settings. The exception is
commissioning/replacement, where mchd intentionally sends the stored password or `prv_key` to
blank hardware as mesh commands as part of taking ownership of that node. It still does not edit
the YAML files.

`mch validate` checks that each configured node's stored `prv_key`, when present, derives a public
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
they are used directly in CLI commands (`mch status east-hill-cab3`). Invalid labels or duplicate
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
2. Run `mch contacts` to list repeater nodes the gateway can already see but that aren't in your
   config yet. Each entry prints ready-to-paste snippets:

```
# ── MySite-East ─────────────────────────────────────────────────
# full pubkey: aabbccdd1122...

# config.yaml — append to the 'nodes:' section:
  - pubkey_prefix: aabbccdd
    label: "MySite-East"
    group:           # assign to a group, or remove this line

# secrets.yaml — append to the 'nodes:' section:
  aabbccdd: ""     # add password if required, or leave blank
```

3. Copy the snippets you want into your config files and fill in any passwords. mchd hot-reloads
   the files shortly after save; you can also send `SIGHUP` or restart mchd to force a reload.
4. Run `mch status` to confirm the node is now known, then `mch sync <prefix>` to sync it
   immediately without waiting for the scheduler.

### Commissioning a new node

Use `mch commission` to take a freshly-flashed node from zero config to fully managed without
stopping mchd. The wizard supports two modes:

**New node** — node starts with a factory-generated identity:

1. Power on the fresh node near the gateway (factory default frequency: 869.618 MHz, 62.5 kHz, SF8, CR5).
2. Run `mch commission` and select **n** (new).
3. Enter the label, group, mesh-visible name, target frequency, and admin password.
4. The daemon discovers the node, generates and sets a new private key, sets the name, password,
   and radio, then reboots the node.
5. `mch commission` prints ready-to-paste snippets for both `config.yaml` and `secrets.yaml`.
   Copy them into your config files.
6. Commissioning verifies that the generated public key is visible on the target frequency before
   returning success.
7. Commit the config/secrets edits, restart mchd, then run `mch sync <prefix>` if you want to sync
   immediately.

**Replacement node** — restoring a known identity onto new hardware:

Prerequisites: `prv_key` must already be in `secrets.yaml` for the node you are replacing. For
nodes commissioned by mch, this was printed in the original `secrets.yaml` snippet.

1. Power on the blank hardware near the gateway.
2. Run `mch commission` and select **r** (replace).
3. Select the node from the list of replaceable nodes.
4. The daemon discovers the blank hardware, restores the private key (restoring the original mesh
   identity), sets the password and radio frequency, then reboots.
5. After reboot the node comes back with the original pubkey — mchd will sync it on the next cycle
   (or use `mch sync <prefix>` to sync immediately).

> **Note:** During commissioning the daemon discovers unmanaged nodes on the factory default
> frequency (869.618 MHz). If more than one unmanaged node is in range, commissioning is aborted —
> commission one node at a time.

### Building a node on the bench

Power the node on near the gateway. MeshCore nodes broadcast advertisements on startup; within a
minute or two the gateway will have stored the contact. Run `mch contacts` again to see it, copy the
snippet, add it to config, and restart mchd. If the node needs a login password, add it to
`secrets.yaml` before syncing.

### Day-to-day checks

```
mch status                  # fleet view: state, label, group, firmware, neighbours, last seen
mch status <label|prefix>   # one match → detail view; multiple matches → filtered table
mch validate                # validate /etc/meshherder/config.yaml
mch suggest                    # suggested txdelay for every node based on neighbour count
mch suggest <label|prefix>     # full suggestions + paste-ready config block
mch contacts                # nodes visible to the gateway but not yet in config
```

### Forcing a sync

```
mch sync <prefix>            # sync one node right now (CLI waits for the result)
mch sync --verbose <prefix>  # sync one node and print each read/write wait as it runs
mch sync                     # sweep all nodes in the background
```

Use `mch sync <prefix>` after editing a node's desired settings in config — it applies the changes
immediately rather than waiting for the next scheduled cycle.

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

### Read-only fleet API

The optional HTTP API exposes the same normalized report model used by CLI status and plan views.
Its paths mirror the read-only `mch` commands. It is disabled by default and is intentionally
restricted to loopback addresses until an authentication design is added.

To use the examples below, add this block to the active `config.yaml` on the host running
`mchd`, then restart the daemon:

```yaml
api:
  enabled: true
  host: 127.0.0.1
  port: 8743
```

```bash
sudo systemctl restart mchd
```

`8743` is the default local API port chosen by Mesh Herder; it can be changed to any available
loopback TCP port in `config.yaml`. Changing the API listener configuration requires restarting
`mchd`.

Endpoints:

```text
GET /health
GET /status
GET /status/<label-or-pubkey-prefix>
GET /issues
GET /plan
GET /logs?node=<label-or-prefix>&limit=50
```

Example queries:

```bash
# Confirm the local API is responding.
curl -fsS http://127.0.0.1:8743/health

# Fetch the fleet report, including summary counts, nodes, issues, and planned work.
curl -fsS http://127.0.0.1:8743/status

# Fetch one node by label or public-key prefix.
curl -fsS http://127.0.0.1:8743/status/SiteA-Rooftop
curl -fsS http://127.0.0.1:8743/status/aabbccdd

# Fetch issue and action views directly.
curl -fsS http://127.0.0.1:8743/issues
curl -fsS http://127.0.0.1:8743/plan

# Fetch the most recent logs, or logs for one node.
curl -fsS 'http://127.0.0.1:8743/logs?limit=20'
curl -fsS 'http://127.0.0.1:8743/logs?node=SiteA-Rooftop&limit=20'
```

Add `| jq` to any example for readable formatted output when `jq` is installed:

```bash
curl -fsS http://127.0.0.1:8743/status | jq
curl -fsS http://127.0.0.1:8743/status/SiteA-Rooftop | jq '.health, .facts, .settings'
```

`/status` returns `schema_version: 1`, a health summary, nodes, unresolved configured
nodes, issues, and planned work. Each node includes `health`, configured `fact_keys`, collected `facts` such as
`lat`/`lon`, and settings classified with `status` (`ok`, `nominal`, `drift`, `unknown`,
`ignored`) and `action` (`none`, `read`, `write`, `report`, `ignore`). This is the contract a
future green/red map dashboard should consume; the API never contacts nodes or makes changes.

### Changing a setting fleet-wide

Add the setting to a group in `config.yaml`, assign nodes to the group, restart mchd, then run
`mch sync` to push the change to all nodes without waiting for the scheduler.

## CLI reference

The same command summary is available as a manual page after install. The daemon
has its own operational page:

```bash
man mch
man mchd
```

```
mch status                    Fleet overview: state, label, group, firmware, neighbours, last seen
mch status <label|prefix>     One match → node detail; multiple matches → filtered table + issues
                              Detail view: facts, all desired settings (config + group, including
                              pending ones not yet synced), last seen/last conn with full timestamp
                              and relative time, recent activity logs
mch validate                  Validate /etc/meshherder/config.yaml
mch validate --config <path>  Validate an explicit config file
mch plan                      Desired-vs-known changes; no mesh traffic and no writes
mch plan <label|prefix>       Filter planned writes/audit drift/unknown values
mch suggest                      Suggested txdelay for all nodes based on neighbour count
mch suggest <label|prefix>       Full suggestions + paste-ready config block for one node
mch logs                      Recent per-node activity across all nodes
mch logs <prefix>             Activity for one node
mch contacts                  Repeater nodes visible to the gateway not yet in config
mch prune                     List DB nodes no longer present in config.yaml
mch prune --yes               Delete listed local DB state for retired nodes
mch remove <label|prefix>     Delete one node's local DB state
mch sync                      Trigger an immediate sync cycle (respects rate limiting)
mch sync <prefix>             Sync one node immediately; CLI waits for the result
mch sync --verbose <prefix>   Sync one node and stream live mesh-operation progress
mch exec <node> <command>     Run a raw MeshCore CLI command on a node (e.g. ver, stats-radio)
mch commission                Commission a fresh or replacement MeshCore node (interactive wizard)
mch start                     Start mchd via systemd (alias: sudo systemctl start mchd)
mch stop                      Stop mchd via systemd
mch restart                   Restart mchd via systemd
```

`mch validate` checks whatever it can read. On a production install, an ordinary operator account
can validate the group-readable config file but may need `sudo mch validate` to validate the
mode-600 secrets file as well.

`mch prune` is deliberately conservative: removing a node from `config.yaml` stops future syncs,
but does not automatically delete historical DB state. Run `mch prune` to review retired DB nodes
and `mch prune --yes` to delete their local facts, settings, operations, and logs. It does not edit
`config.yaml`, `secrets.yaml`, or gateway favourites. Secrets entries without matching config nodes
are allowed; operators may keep passwords or private keys there for replacement hardware.
Use `mch remove <label|prefix>` to delete one retired node's local DB state. It refuses nodes still
present in `config.yaml` unless `--force` is supplied.

`mch sync <prefix>` runs that node immediately and waits — the CLI returns once the sync is
complete. It collects facts, checks and enforces settings, runs any due operations, then
reschedules the node back into normal rotation. Use this when you need a result right now:

```
mch sync aabbccdd    # sync the node whose pubkey starts with aabbccdd; CLI waits for the result
mch sync --verbose qh-wold   # print reads, timeouts, writes, and operations while waiting
```

`mch sync` (no argument) starts a full sweep of every node in the background — the CLI returns
"sync started" immediately and mchd works through the list in config order, pausing
`inter_node_delay_seconds` between each node, exactly as a scheduled cycle would. With many nodes
and a long delay this can take a while; check `mch status` to watch progress.

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
mchd: hopping to 916.575/62.5/SF7 for '📡 Millbrook'
mchd: hopping radio: 915.075/125/SF9 → 916.575/62.5/SF7
mchd: radio restored: 915.075/125/SF9
mchd: '📡 Millbrook' done — facts:5 settings:1/1 enforced drift:0 ops:0/0 failed
```

Per-node activity (settings changes, operation results, and fact/setting read failures) is stored in the local
database and shown by `mch status <prefix>` and `mch logs`:

```
mch status aabbccdd

  state:       configured
  ...
  recent activity:
    [INFO ] 2026-05-18 09:27  set owner.info='Managed with Mesh Herder' (was '')
    [INFO ] 2026-05-18 09:27  operation ok: 'clock sync'
```

```
mch logs               # recent activity across all nodes
mch logs aabbccdd      # filtered to one node (prefix ok)
mch logs -n 100        # last 100 entries
```

If a node shows as `partial`, `mch logs <prefix>` will show what failed:

```
2026-05-25 17:03 [WARNING] read advert.interval failed: timeout - no response from node
```

`TRIES` in status/issue output is write-attempt count, not read-timeout count. A timed-out
enforced setting read cannot be compared or written and therefore does not increment `TRIES`;
it appears in `mch logs` and in the final partial-sync result instead.

The systemd journal remains the daemon/service diagnostic log, including startup, service
failure, and radio/session debugging. `mch logs` is the node activity history; `mch status`
reports mesh state. For daemon process health use `systemctl status mchd --no-pager`.

## Upgrade

Use the same checkout and venv that were created during production install:

```bash
cd /opt/meshherder
sudo git pull --ff-only
sudo /opt/meshherder/.venv/bin/pip install .
sudo cp systemd/mchd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart mchd
mch --version
systemctl status mchd --no-pager
```

If `mchd` exits because `config.yaml` is invalid, the service stops instead of restart-looping.
Fix the config with `mch validate`, then restart `mchd`.
