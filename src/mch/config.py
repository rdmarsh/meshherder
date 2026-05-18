"""Load and validate config.yaml and secrets.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from mch.models import SettingPolicy


@dataclass
class GatewayConfig:
    device: str = "/dev/meshcore0"
    baud: int = 115200
    name: str | None = None  # if set, mchd applies this name to the gateway on startup
    radio: "RadioConfig | None" = None  # if set, gateway is forced to this freq on startup and restored to it after every hop


@dataclass
class SchedulerConfig:
    inter_node_delay_seconds: int = 30
    sync_interval_days: float = 1.0


@dataclass
class SettingConfig:
    """Policy + desired value for one setting key."""
    desired: str | None = None
    policy: SettingPolicy | None = None  # None means "inherit from node/group default"
    poll_hours: int = 24


@dataclass
class OperationConfig:
    """A one-shot or recurring command to run on a node."""
    command: str = ""
    every_hours: int = 24
    fire_and_forget: bool = False  # if True, treat timeout/no-response as success (e.g. reboot)


@dataclass
class RadioConfig:
    """Radio parameters for a node on a non-default frequency."""
    freq: float
    bw: float
    sf: int
    cr: int


@dataclass
class NodeConfig:
    pubkey_prefix: str
    group: str | None = None
    label: str = ""
    default_policy: SettingPolicy | None = None
    settings: dict[str, SettingConfig] = field(default_factory=dict)
    operations: list[OperationConfig] = field(default_factory=list)
    radio: RadioConfig | None = None  # if set, gateway hops to this freq to manage the node


@dataclass
class GroupConfig:
    name: str
    default_policy: SettingPolicy | None = None
    # settings: desired values applied to all nodes in this group
    settings: dict[str, SettingConfig] = field(default_factory=dict)
    # setting_overrides: policy-only overrides (no desired value required)
    setting_overrides: dict[str, SettingConfig] = field(default_factory=dict)


@dataclass
class MessengerConfig:
    """Config for mesh DM control and channel announcements."""
    # pubkey prefixes allowed to issue DM commands (e.g. operator's handheld)
    allowed_prefixes: list[str] = field(default_factory=list)
    # channel slot number to post cycle summaries to (e.g. 1 for #mch)
    announce_channel: int | None = None


@dataclass
class Config:
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    nodes: list[NodeConfig] = field(default_factory=list)
    groups: list[GroupConfig] = field(default_factory=list)
    messenger: MessengerConfig = field(default_factory=MessengerConfig)
    facts: list[str] = field(default_factory=lambda: ["ver", "board", "role"])
    db_path: str = "/var/lib/mch/mch.db"
    socket_path: str = "/run/mch/mchd.sock"
    secrets: dict[str, str] = field(default_factory=dict)  # pubkey_prefix -> password


def load(config_path: Path, secrets_path: Path | None = None) -> Config:
    raw = yaml.safe_load(config_path.read_text())

    gw_raw = raw.get("gateway", {})
    gw_radio_raw = gw_raw.pop("radio", None)
    gw_radio = RadioConfig(**gw_radio_raw) if gw_radio_raw else None
    sched_raw = raw.get("scheduler", {})

    msg_raw = raw.get("messenger", {})
    cfg = Config(
        gateway=GatewayConfig(**gw_raw, radio=gw_radio),
        scheduler=SchedulerConfig(**sched_raw),
        db_path=raw.get("db_path", "/var/lib/mch/mch.db"),
        socket_path=raw.get("socket_path", "/run/mch/mchd.sock"),
        facts=raw.get("facts", ["ver", "board", "role"]),
        nodes=[_parse_node(n) for n in raw.get("nodes", [])],
        groups=[_parse_group(g) for g in raw.get("groups", [])],
        messenger=MessengerConfig(
            allowed_prefixes=msg_raw.get("allowed_prefixes", []),
            announce_channel=msg_raw.get("announce_channel"),
        ),
    )

    # Resolve secrets file: explicit arg > config key > sibling secrets.yaml
    if secrets_path is None:
        secrets_path = Path(raw.get("secrets_file", config_path.parent / "secrets.yaml"))

    if secrets_path.exists():
        _check_permissions(secrets_path)
        secrets_raw = yaml.safe_load(secrets_path.read_text()) or {}
        cfg.secrets = secrets_raw.get("nodes", {})

    return cfg


def _parse_setting(raw: dict) -> SettingConfig:
    policy = raw.get("policy")
    return SettingConfig(
        desired=raw.get("desired"),
        policy=SettingPolicy(policy) if policy else None,
        poll_hours=raw.get("poll_hours", 24),
    )


def _parse_node(raw: dict) -> NodeConfig:
    policy = raw.get("default_policy")
    settings = {
        k: _parse_setting(v if isinstance(v, dict) else {"desired": v})
        for k, v in raw.get("settings", {}).items()
    }
    operations = [
        OperationConfig(**op) if isinstance(op, dict) else OperationConfig(command=op)
        for op in raw.get("operations", [])
    ]
    radio_raw = raw.get("radio")
    radio = RadioConfig(**radio_raw) if radio_raw else None

    return NodeConfig(
        pubkey_prefix=raw["pubkey_prefix"],
        group=raw.get("group"),
        label=raw.get("label", ""),
        default_policy=SettingPolicy(policy) if policy else None,
        settings=settings,
        operations=operations,
        radio=radio,
    )


def _parse_group(raw: dict) -> GroupConfig:
    policy = raw.get("default_policy")
    settings = {
        k: _parse_setting(v if isinstance(v, dict) else {"desired": v})
        for k, v in raw.get("settings", {}).items()
    }
    overrides = {
        k: _parse_setting(v if isinstance(v, dict) else {"policy": v})
        for k, v in raw.get("setting_overrides", {}).items()
    }
    return GroupConfig(
        name=raw["name"],
        default_policy=SettingPolicy(policy) if policy else None,
        settings=settings,
        setting_overrides=overrides,
    )


def _check_permissions(path: Path) -> None:
    mode = oct(os.stat(path).st_mode)[-3:]
    if mode not in ("600", "400"):
        raise PermissionError(
            f"{path} has permissions {mode} — secrets file must be 600 or 400"
        )
