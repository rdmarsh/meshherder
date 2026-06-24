"""Core data types shared across the daemon."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

# meshcore_py contact type values (CONTACT_TYPENAMES = ["NONE","CLI","REP","ROOM","SENS"])
CONTACT_TYPE_CLI       = 1
CONTACT_TYPE_REPEATER  = 2
CONTACT_TYPE_ROOM      = 3


class NodeState(Enum):
    UNKNOWN     = "unknown"      # never contacted
    ONLINE      = "online"       # contacted this cycle
    OFFLINE     = "offline"      # not seen recently
    UNREACHABLE = "unreachable"  # route/contact exists but did not answer probes
    CONFIGURING = "configuring"  # commands in flight
    CONFIGURED  = "configured"   # all enforced settings match desired, all operations done
    PARTIAL     = "partial"      # some settings pending/failed or drifted
    RETIRED     = "retired"      # deliberately removed from management; skipped + unfavourited,
    #                              kept for history (retire-don't-delete). Re-managed via unretire.


class SettingPolicy(Enum):
    ENFORCE = "enforce"  # check value; set it if it differs from desired
    AUDIT   = "audit"    # check value; flag drift but never set
    FACT    = "fact"     # read and record the observed value; no desired, no write
    SKIP    = "skip"     # don't touch this setting at all


class Scope(Enum):
    """The layer a desired setting is attached to (most specific wins at resolve time)."""
    GLOBAL = "global"  # fleet-wide default; scope_id is "" (the singleton)
    GROUP  = "group"   # applies to a group; scope_id is the group name
    NODE   = "node"    # applies to one node; scope_id is the node pubkey


class OperationStatus(Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"   # node unreachable, deferred to next cycle
    SKIPPED_FW = "skipped_fw"  # node firmware too old to support this command


@dataclass
class Node:
    pubkey: str           # stable identity — names can change
    name: str
    label: str = ""       # operator's local label from config; never pushed to hardware
    group: str | None = None
    state: NodeState = NodeState.UNKNOWN
    last_seen: datetime | None = None
    last_connected: datetime | None = None
    auth_required: bool = False
    auth_cached: bool = False  # True once we have authenticated this session
    next_sync_at: datetime | None = None  # wall-clock time for next scheduled sync


@dataclass
class NodeFact:
    """Read-only inventory data collected from a node. Never set by mchd."""
    node_pubkey: str
    key: str              # e.g. "ver", "board", "role", "repeat"
    value: str | None
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class SettingRecord:
    """
    Tracks one get/set-able setting on a node, with its resolved policy.
    Policy is resolved from configured setting overrides/defaults across node,
    group, global, then the hardcoded global default.
    """
    node_pubkey: str
    key: str                      # e.g. "owner.info", "name", "tx"
    desired: str | None           # None means audit-only, no desired value
    policy: SettingPolicy
    poll_hours: int = 24
    observed: str | None = None   # last value returned by `get <key>`
    last_checked: datetime | None = None
    last_set: datetime | None = None
    update_attempts: int = 0      # writes attempted for the current desired value
    drift: bool = False           # True when observed != desired and policy != SKIP


@dataclass
class DesiredSetting:
    """A desired value/policy for one key at one scope (global/group/node).

    The authoritative, CLI- and API-writable source of desired state. ``scope_id``
    is "" for global, the group name for group scope, and the node pubkey for node
    scope. ``policy``/``poll_hours`` of None mean "inherit" - the resolver falls back
    to a less specific scope or the built-in default.
    """
    scope_type: Scope
    scope_id: str                          # "" for global; group name; or node pubkey
    key: str                               # e.g. "name", "tx", "lat"
    desired: str | None = None             # None = policy-only override (no value)
    policy: SettingPolicy | None = None    # None = inherit
    poll_hours: int | None = None          # None = inherit / built-in default


@dataclass
class DesiredOperation:
    """A declared recurring/one-shot command for one scope (global/group/node).

    The desired-state analogue of OperationConfig, stored in the DB so operations
    sunset out of config.yaml like settings and directives (DESIGN.md sunset). Unlike
    DesiredSetting there is no policy: an operation is simply run when due. ``scope_id``
    is "" for global, the group name for group scope, the node pubkey for node scope.
    Resolution is Node > Group > Global by command.
    """
    scope_type: Scope
    scope_id: str
    command: str              # raw CLI string, e.g. "clock sync"
    every_hours: int = 24
    fire_and_forget: bool = False  # treat timeout/no-response as success (e.g. reboot)


@dataclass
class OperationRecord:
    """Tracks a one-shot command (e.g. clock sync) that runs on a schedule."""
    node_pubkey: str
    command: str              # raw CLI string, e.g. "clock sync"
    every_hours: int = 24
    status: OperationStatus = OperationStatus.PENDING
    queued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    run_at: datetime | None = None
    response: str | None = None
    id: int | None = None     # DB primary key; set when loaded from DB
