# Install

This guide covers development setup, production installation, the recommended udev rule for a
stable gateway device path, and upgrades.

## Requirements

- Python 3.11+
- Linux (tested on Raspberry Pi OS / Ubuntu); macOS works for development
- A MeshCore node attached over USB serial (the gateway)

## Development

```bash
git clone https://github.com/rdmarsh/meshherder
cd meshherder
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`mchd` and `mch` are then available inside the venv.

## Production

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

### Create the system user and directories

```bash
# Dedicated system account - no home dir, no login shell
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

### Add yourself to the mch group

The `mch` CLI talks to the daemon over a Unix socket owned by the `mch` group. Any user who needs
CLI access must be in that group:

```bash
sudo usermod -aG mch <your-username>
```

Log out and back in (or run `newgrp mch`) for the membership to take effect.

### Install and start the service

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

> **Note:** the systemd unit is named `mchd` - always use `systemctl ... mchd`, not `mch`.
> The `mch start`, `mch stop`, and `mch restart` CLI commands are shortcuts to the same thing.

## udev rule

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

Use the values from the **first** (innermost) block - that is the USB device itself, not the hub.
For example:

```text
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
device (recommended - prevents the rule matching a different device of the same type):

```text
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ATTRS{serial}=="441BF66F9FDC", \
  SYMLINK+="meshcore0", GROUP="dialout", MODE="0660", \
  TAG+="systemd", ENV{SYSTEMD_WANTS}="mchd.service"
```

Or match by vendor and product alone (simpler, but matches any device of that type):

```text
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

## Upgrade

Use the same checkout and venv that were created during production install:

```bash
cd /opt/meshherder
git status --short --branch
sudo git pull --ff-only
sudo /opt/meshherder/.venv/bin/pip install .
sudo cp systemd/mchd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart mchd
mch --version
systemctl status mchd --no-pager
```

If root cannot fetch the GitHub remote, keep `/opt/meshherder` pointed at the real operator
checkout instead:

```bash
cd ~/code/meshherder
git pull --ff-only
cd /opt/meshherder
sudo git remote set-url origin ~/code/meshherder
sudo git pull --ff-only
sudo /opt/meshherder/.venv/bin/pip install .
sudo systemctl restart mchd
```

If `git status` reports `not a git repository`, stop and restore `/opt/meshherder` from the real
checkout before running `pip install .`. The install command packages whatever source tree is in
the current directory, so an old copied tree can downgrade the daemon even when the venv and
systemd unit are otherwise correct.

If `mchd` exits because `config.yaml` is invalid, the service stops instead of restart-looping.
Fix the config with `mch validate`, then restart `mchd`.

## Testing a feature branch on the bench

To try an unmerged branch (e.g. a PR you are reviewing) on the bench host, *check out the branch*
instead of fast-forward-pulling `main`. Substitute the branch name for `<branch>` below.

```bash
# 1. operator checkout: fetch from GitHub and switch to the branch
cd ~/code/meshherder
git fetch origin
git checkout <branch>          # first time; later updates on the same branch: git pull --ff-only

# 2. /opt checkout (origin points at the operator checkout): fetch + switch
cd /opt/meshherder
sudo git remote set-url origin ~/code/meshherder
sudo git fetch origin
sudo git checkout <branch>

# 3. install + restart (same as Upgrade)
sudo /opt/meshherder/.venv/bin/pip install .
sudo systemctl restart mchd
```

Verify the branch is actually deployed (a stale `/opt` tree silently downgrades the daemon - see
the Upgrade notes):

```bash
mch --version
git -C /opt/meshherder status --short --branch    # confirms the branch name
systemctl status mchd --no-pager
```

**Test safely first.** Prefer read-only and dry-run commands before anything that writes. Commands
that only read the DB or mesh (`mch status`, `mch export`, `mch plan`) are safe; `mch import`
defaults to a dry-run preview and only writes with `--yes`. `mch import`/`mch export` are local DB
operations with no mesh traffic, so they cannot strand or disturb field nodes.

**Return to `main` when done:**

```bash
cd ~/code/meshherder && git checkout main
cd /opt/meshherder  && sudo git checkout main
sudo /opt/meshherder/.venv/bin/pip install . && sudo systemctl restart mchd
```
