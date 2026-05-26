# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

mch (Mesh Herder) is a tool for managing fleets of MeshCore repeater nodes — think Ansible/Salt but over the mesh itself. The daemon is `mchd`; the control CLI is `mch`. A locally attached node (USB serial) — called the **gateway** — connects mchd to the mesh. mchd sends remote CLI commands across the mesh to bring administered nodes into a declared desired state, then leaves them alone. ("Companion node" is meshcore_py's internal term for this; mchd calls it the gateway everywhere.)

The mesh is unreliable and bandwidth-constrained; managed nodes may be low-powered solar devices
with intermittent availability. Design for gradual convergence, cached observations with visible
staleness, and useful failure-pattern history while minimizing unnecessary radio traffic. Do not
assume a datacenter-style network where one timeout means a node is down.

Target audience: operators with dozens of nodes who need consistent, verifiable configuration without logging into each one individually.

**Core design constraints:**
- Must not hammer the mesh — work is rate-limited, one node at a time, with configurable inter-operation delay.
- Lightweight enough for a headless Raspberry Pi with low RAM, or a Docker container.
- The public key is the stable node identity — names can change.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Common commands

```bash
# Run tests
pytest

# Run a single test
pytest tests/test_clock.py::test_sync_clock

# Lint
ruff check .

# Type check
mypy src/

# Run daemon (dev)
mchd --config config.yaml

# Control CLI
mch status
mch start
mch stop
mch sync          # trigger immediate sync cycle
mch node <name>   # show detail for one node
```

## Testing

Use local unit tests for normal development. Live mesh testing needs a host with a USB-attached
gateway node and a private `config.yaml`/`secrets.yaml`; do not commit operator config or generated
node secrets.

## Architecture

```
mchd (daemon)                      mch (CLI)
    ↕ Unix socket (/run/meshherder/mchd.sock)  ↕
    ↕
meshcore_py (async serial, /dev/meshcore0)
    ↕
Local gateway node (USB)
    ↕ mesh
Managed repeater nodes
```

### Component responsibilities

**`mchd` daemon**
- Holds the single `meshcore_py` connection to the gateway node.
- Runs a rate-limited work queue: one node operation at a time, configurable delay between ops.
- On startup: reads favourited contacts from the gateway node via `get_contacts()`, seeds any new ones into the database.
- Exposes a Unix socket for `mch` CLI commands.
- Writes to the SQLite database (actual state, history, logs).

**`mch` CLI**
- Thin client: sends commands to `mchd` over the Unix socket and prints the response.
- `mch status` — fleet overview: 8-column table (STATE, LABEL, GROUP, NAME, NBRS, VER, LAST SEEN, PUBKEY) + issue count; footer when issues > 0.
- `mch start` / `mch stop` — lifecycle (for use outside systemd; systemd uses `systemctl`).
- `mch node <name-or-pubkey-prefix>` — per-node detail with command history.

**Work queue**
- Each managed node has a queue of *desired commands* (from config). The daemon works through nodes sequentially, applying pending commands and recording results.
- Never marks a command successful unless the response confirms it.
- Tracks: command, timestamp, response, success/failure.

### Data layer

**SQLite** (`/var/lib/meshherder/mch.db`) — actual state and history:
- `nodes` table: pubkey (PK), name, last_seen, last_connected, auth_required, auth_cached, config_state, deployment_state.
- `commands` table: node_pubkey, command, queued_at, run_at, response, success.
- `logs` table: node_pubkey, timestamp, level, message.

**YAML** (`config.yaml`) — desired state and daemon settings:
```yaml
gateway:
  device: /dev/meshcore0
  baud: 115200

scheduler:
  inter_node_delay_seconds: 30   # pause between nodes
  sync_interval_minutes: 60      # how often to run a full cycle

nodes:
  - pubkey_prefix: abc123        # stable identifier
    name: NodeA                  # informational only
    desired:
      - "set name NodeA"
      - "clock sync"
```

**`secrets.yaml`** (mode 0600, never committed) — passwords per node:
```yaml
nodes:
  abc123: "adminpassword"
```

### Initialisation from gateway favourites

On first run (or `mch discover`), mchd calls `get_contacts()` on the gateway node, filters for favourited repeaters, and inserts any unknown pubkeys into the `nodes` table. This bootstraps the inventory without manual entry.

### meshcore_py patterns

```python
# Connect to gateway
mc = await MeshCore.create_serial("/dev/meshcore0", 115200)

# Get contacts (flags & 1 = favourited/managed nodes)
contacts_event = await mc.commands.get_contacts()
managed = [c for c in contacts_event.payload.values() if c.get("flags", 0) & 1]

# Send remote CLI command and fetch response — CORRECT FLOW:
# send_cmd() does NOT push CONTACT_MSG_RECV spontaneously.
# The node queues its reply; MESSAGES_WAITING signals it's ready; get_msg() fetches it.
result = await mc.commands.send_cmd(contact, "ver")
notif = await mc.dispatcher.wait_for_event(EventType.MESSAGES_WAITING, timeout=30)
if notif:
    while True:
        msg = await mc.commands.get_msg()
        if msg.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
            break
        if (msg.type == EventType.CONTACT_MSG_RECV
                and msg.payload.get("pubkey_prefix") == contact["public_key"][:12]):
            response_text = msg.payload.get("text", "")
            break

# Login (only needed once — gateway pubkey is added to node ACL after first success)
login = await mc.commands.send_login_sync(contact, password, timeout=30, min_timeout=30)
# After this, subsequent sessions can skip login and attempt commands directly.
# Only re-login if a command returns a permission error.
```

## MeshCore CLI commands (sent via `send_cmd`)

| Command | Effect |
|---|---|
| `clock sync` | Sync time from remote device |
| `time <epoch_seconds>` | Set node clock to Unix timestamp |
| `clock` | Get current UTC time |
| `ver` | Firmware version |
| `board` | Hardware name |
| `get name` / `set name <name>` | Node name — max 31 chars (confirmed on v1.14.1 Seeed Xiao-nrf52: 32 sent → 31 stored) |
| `get role` | Node role |
| `get public.key` | Node identity |
| `neighbors` | List 8 most recent peer adverts (repeater only) |
| `stats-core` | Battery, uptime, queue (direct serial only) |
| `reboot` | Restart node |

Full reference: https://docs.meshcore.io/cli_commands

## systemd

Install the service unit to `/etc/systemd/system/mchd.service`. The `mch start/stop` commands are aliases for `systemctl start/stop mchd` when running under systemd; the daemon detects this from the environment.

## Future scope (not in current design)

- **Firmware age reporting**: `ver` is already collected as a fact; add a report flagging nodes below a configurable minimum firmware version, or older than the newest version seen across the fleet.
- Web UI showing node map (lat/long already stored per node from `get/set lat` and `get/set lon` commands).
- Write status updates to a MeshCore room server.
- Broadcast status onto a configurable mesh channel.
- Docker image (already low-RAM; should be straightforward once arch is stable).
- Bluetooth connection support (meshcore_py supports `create_ble`).

## Development workflow

- TDD: write the failing test first, confirm it fails for the expected reason, then implement.
- Keep changes small and verify at each step.
- Prefer low-maintenance code over fragile cleverness. Avoid designs that require future readers to
  remember hidden context, ordering assumptions, or bench-specific behavior.
- Leave concise comments explaining why something non-obvious is done, especially when the reason
  comes from live MeshCore behavior, safety constraints, or a decision not to take an obvious path.
- Preserve the rationale and supporting evidence for operational constants and safety decisions
  (for example calibrated timeouts, retry limits, radio behavior, or reboot choices) in
  `JOURNAL.md` while investigating and in `DESIGN.md` once it is stable. Do not leave a magic
  value in code without a traceable reason.
- Preserve all debug logging and comments — they are intentional.
- Preserve untriaged notes, backlog items, and investigation evidence until each item has an
  explicit destination: active backlog, journal/design history, changelog, or deliberate rejection.
  Do not silently compress away distinct future work.
- When a mistake reveals a missing rule, update this file and the project journal.

## Live testing

- `luna` has a USB-attached MeshCore test node available for live/bench verification. Remember
  this when a change depends on real firmware timing, serial behavior, radio behavior, or mesh
  response handling; do not rely only on mocks where the hardware can answer the question.
- Keep potentially disruptive live writes deliberate: use the attached node for approved testing
  and record observed behavior in `JOURNAL.md` before turning it into a rule or constant.

## Code reviews

- Prefer simple, boring fixes over clever or expansive changes.
- Review for real operator impact: bugs, regressions, confusing behavior, missing validation, and
  missing tests.
- Treat high-maintenance or fragile code as a review risk even when it currently works.
- Ask for "why" comments when code depends on non-obvious firmware behavior, ordering constraints,
  safety tradeoffs, or previous debugging lessons.
- Keep speculative improvements out of review findings unless they block correctness or
  maintainability.
- Do not turn "we could also..." ideas into required work without a clear benefit and low
  maintenance cost.
