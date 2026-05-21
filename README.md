# mch — Mesh Config Herder

Herding config across MeshCore nodes.

mch manages fleets of [MeshCore](https://meshcore.io) repeater nodes from a single host. You declare
the desired state for each node in a YAML config file; mchd (the daemon) connects to a
locally-attached gateway node over USB serial, walks the mesh, and brings each managed node into
compliance — one node at a time, without hammering the mesh.

Think Ansible for your nodes, running continuously in the background.

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
- **DM commands** — You can send commands to mchd over the mesh from your MeshCore device. Useful
  in the field: just replaced a node and want to bring it into compliance immediately? Send `sync
  <pubkey>` as a direct message to the gateway from wherever you are on the mesh. Note: the
  replacement node must already be in the gateway's contact store — a brand-new node won't be
  reachable until its advertisement has been received and it has been added to config.
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
  the sync queue, hops to the factory default frequency, discovers the node, pushes password/name/
  and radio config, then prints ready-to-paste snippets for `config.yaml` and `secrets.yaml`.
  No need to stop mchd.
- **`mch` CLI** — check daemon status, list nodes, force a sync, see nodes not yet in config.

## Requirements

- Python 3.11+
- Linux (tested on Raspberry Pi OS / Ubuntu); macOS works for development
- A MeshCore node attached over USB serial (the gateway)

## Install

### Development

```bash
git clone https://github.com/rdmarsh/mch
cd mch
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`mchd` and `mch` are then available inside the venv.

### Production (server / Raspberry Pi)

Clone to a permanent location, create a venv, and install into it:

```bash
sudo git clone https://github.com/rdmarsh/mch /opt/mch
sudo python3 -m venv /opt/mch/.venv
sudo /opt/mch/.venv/bin/pip install /opt/mch
```

Symlink the binaries onto the system PATH so the service file works as-is:

```bash
sudo ln -sf /opt/mch/.venv/bin/mchd /usr/local/bin/mchd
sudo ln -sf /opt/mch/.venv/bin/mch  /usr/local/bin/mch
```

#### Create the system user and directories

```bash
# Dedicated system account — no home dir, no login shell
sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
    --comment "Mesh Config Herder" mch
# Grant serial port access
sudo usermod -aG dialout mch

# Config directory
sudo install -d -o root -g mch -m 750 /usr/local/etc/mch

# Config and secrets
sudo install -o mch -g mch -m 640 examples/config.example.yaml /usr/local/etc/mch/config.yaml
sudo install -o mch -g mch -m 600 examples/secrets.example.yaml /usr/local/etc/mch/secrets.yaml

# Database directory
sudo install -d -o mch -g mch -m 755 /var/lib/mch
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

Set the `device` path in `/usr/local/etc/mch/config.yaml` to `/dev/meshcore0`.

## Configuration

Copy `examples/config.example.yaml` to `/usr/local/etc/mch/config.yaml` and edit it. The example file
documents every available option with comments.

A minimal working config:

```yaml
gateway:
  device: /dev/meshcore0
  baud: 115200
  radio:
    freq: 915.800
    bw: 250
    sf: 10
    cr: 5

scheduler:
  sync_interval_days: 1
  inter_node_delay_seconds: 120

nodes:
  - pubkey_prefix: aabbccdd
    label: MyRepeater
    default_policy: enforce
    settings:
      owner.info: "Managed by mch"
```

Run `mch contacts` to list repeater nodes visible to the gateway that are not yet in your config,
with pubkey prefixes ready to paste in.

**Passwords:** nodes that require a login go in `secrets.yaml` (mode 0600):

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

### Node label vs. mesh-visible name

`label:` is mch's **local label** for the node — it only ever appears on the host running mchd.
Labels must use only letters, digits, dashes, underscores, and dots (`[a-zA-Z0-9_\-.]+`) because
they are used directly in CLI commands (`mch status east-hill-cab3`). Invalid labels or duplicate
labels cause mchd to refuse to start with a clear error.

`settings.name.desired:` is what gets **enforced on the hardware** and broadcast across the mesh —
visible to anyone scanning. If you don't want your deployment location appearing on the mesh, set a
neutral name here and keep the descriptive one in `label:`:

```yaml
nodes:
  - pubkey_prefix: aabbccdd
    label: east-hill-cab3               # local only, never leaves this machine
    default_policy: enforce
    settings:
      name:
        desired: "📡 Millbrook"          # pushed to hardware; visible across the mesh
      owner.info: "Managed by mch"
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

3. Copy the snippets you want into your config files, fill in any passwords, then restart mchd:
   `sudo systemctl restart mchd`
4. Run `mch status` to confirm the node is now known, then `mch sync <prefix>` to sync it
   immediately without waiting for the scheduler.

### Commissioning a new node

Use `mch commission` to take a freshly-flashed node from zero config to fully managed without
stopping mchd. The wizard supports two modes:

**New node** — node starts with a factory-generated identity:

1. Power on the fresh node near the gateway (factory default frequency: 869.618 MHz, 62.5 kHz, SF8, CR5).
2. Run `mch commission` and select **n** (new).
3. Enter the label, group, mesh-visible name, target frequency, and admin password.
4. The daemon discovers the node, generates and sets a new private key, sets the password, name,
   and radio, then reboots the node.
5. `mch commission` prints ready-to-paste snippets for both `config.yaml` and `secrets.yaml`.
   Copy them into your config files.
6. Wait ~30 seconds for the node to reboot on the target frequency, then run `mch sync`.

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
mch hint                    # suggested txdelay for every node based on neighbour count
mch hint <label|prefix>     # full suggestions + paste-ready config block
mch contacts                # nodes visible to the gateway but not yet in config
```

### Forcing a sync

```
mch sync <prefix>  # sync one node right now (CLI waits for the result)
mch sync           # sweep all nodes in the background
```

Use `mch sync <prefix>` after editing a node's desired settings in config — it applies the changes
immediately rather than waiting for the next scheduled cycle.

### Adding a node in the field

If you have replaced a node or commissioned a new one in the field, send a DM to the gateway from
your MeshCore device: `sync <pubkey-prefix>`. mchd will sync that node immediately. The node must
already be in your config and visible to the gateway; brand-new nodes need to have advertised first.

### Changing a setting fleet-wide

Add the setting to a group in `config.yaml`, assign nodes to the group, restart mchd, then run
`mch sync` to push the change to all nodes without waiting for the scheduler.

## CLI reference

```
mch status                    Fleet overview: state, label, group, firmware, neighbours, last seen
mch status <label|prefix>     One match → node detail; multiple matches → filtered table + issues
mch hint                      Suggested txdelay for all nodes based on neighbour count
mch hint <label|prefix>       Full suggestions + paste-ready config block for one node
mch logs                      Recent per-node activity across all nodes
mch logs <prefix>             Activity for one node
mch contacts                  Repeater nodes visible to the gateway not yet in config
mch sync                      Trigger an immediate sync cycle (respects rate limiting)
mch sync <prefix>             Sync one node immediately; CLI waits for the result
mch exec <node> <command>     Run a raw MeshCore CLI command on a node (e.g. ver, stats-radio)
mch commission                Commission a fresh or replacement MeshCore node (interactive wizard)
mch start                     Start mchd via systemd (alias: sudo systemctl start mchd)
mch stop                      Stop mchd via systemd
mch restart                   Restart mchd via systemd
```

`mch sync <prefix>` runs that node immediately and waits — the CLI returns once the sync is
complete. It collects facts, checks and enforces settings, runs any due operations, then
reschedules the node back into normal rotation. Use this when you need a result right now:

```
mch sync aabbccdd    # sync the node whose pubkey starts with aabbccdd; CLI waits for the result
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
mchd: gateway connected: name='🤖 mch-gw' freq=915.800 bw=250 sf=10 cr=5
mchd: work queue started (interval=1.0d)
mchd: processing node '📡 Millbrook' (aabbccdd...)
mchd: hopping to 916.575/62.5/SF7 for '📡 Millbrook'
mchd: hopping radio: 915.800/250/SF10 → 916.575/62.5/SF7
mchd: radio restored: 915.800/250/SF10
mchd: '📡 Millbrook' done — facts:5 settings:1/1 enforced drift:0 ops:0/0 failed
```

Per-node activity (settings changes, operation results, errors) is also stored in the local
database and shown by `mch node` and `mch logs`:

```
mch node aabbccdd

  state:       configured
  ...
  recent activity:
    [INFO ] 2026-05-18 09:27  set owner.info='Managed by mch' (was '')
    [INFO ] 2026-05-18 09:27  operation ok: 'clock sync'
```

```
mch logs               # recent activity across all nodes
mch logs aabbccdd      # filtered to one node (prefix ok)
mch logs -n 100        # last 100 entries
```

If a node shows as `partial`, `mch logs <prefix>` will show what failed:

```
2026-05-17 14:38 [ERROR] operation failed: 'reboot': timeout — no response from node
```

In this case `reboot` timed out because the node rebooted before it could reply — mark it
`fire_and_forget: true` in your config and it will be treated as success.

## Upgrading

```bash
cd /opt/mch
sudo git pull
source .venv/bin/activate
pip install .
deactivate
sudo systemctl restart mchd
```
