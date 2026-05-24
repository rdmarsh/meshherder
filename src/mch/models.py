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
    CONFIGURING = "configuring"  # commands in flight
    CONFIGURED  = "configured"   # all enforced settings match desired, all operations done
    PARTIAL     = "partial"      # some settings pending/failed or drifted


class SettingPolicy(Enum):
    ENFORCE = "enforce"  # check value; set it if it differs from desired
    AUDIT   = "audit"    # check value; flag drift but never set
    SKIP    = "skip"     # don't touch this setting at all


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
    drift: bool = False           # True when observed != desired and policy != SKIP


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
