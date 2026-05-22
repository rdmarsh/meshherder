"""Load and validate config.yaml and secrets.yaml."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Hashable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mch.models import SettingPolicy

# Labels are node identifiers used in CLI commands — no spaces, emoji, or shell-hostile chars.
_LABEL_RE = re.compile(r"^[a-zA-Z0-9_\-.]+$")
# pubkey_prefix must be even-length lowercase hex (full bytes only).
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


class _NoDuplicateKeysLoader(yaml.SafeLoader):
    """YAML loader that raises ValueError on duplicate mapping keys.

    yaml.safe_load silently overwrites duplicate keys with the last value.
    Operators who accidentally paste the same prefix twice would lose one entry silently.
    """
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        if isinstance(node, yaml.MappingNode):
            seen: set[str] = set()
            for key_node, _ in node.value:
                key = key_node.value  # raw string before type coercion
                if key in seen:
                    raise ValueError(f"duplicate key {key!r} in secrets file")
                seen.add(key)
        return super().construct_mapping(node, deep=deep)

log = logging.getLogger(__name__)


@dataclass
class NodeSecrets:
    password: str | None = None
    guest_password: str | None = None
    prv_key: str | None = None


@dataclass
class GatewayConfig:
    device: str = "/dev/meshcore0"
    baud: int = 115200
    name: str | None = None  # if set, mchd applies this name to the gateway on startup
    radio: RadioConfig | None = None  # if set, forced to home freq on startup; restored per hop
    advert_on_start: str = "never"  # "never" | "zerohop" — send advert after daemon restart

    def __post_init__(self) -> None:
        _VALID = {"never", "zerohop"}
        if self.advert_on_start not in _VALID:
            raise ValueError(
                f"gateway.advert_on_start must be one of {sorted(_VALID)!r},"
                f" got {self.advert_on_start!r}"
            )


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
    via: RadioConfig | None = None  # if set, gateway hops to this freq to manage the node


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
    secrets: dict[str, NodeSecrets] = field(default_factory=dict)  # pubkey_prefix -> NodeSecrets
    # profiles_url: fetch profiles.yaml from this URL on startup (cached 24h)
    profiles_url: str | None = None
    # profiles_file: use this local file instead of the bundled default
    profiles_file: str | None = None
    # Absolute paths used by mchd for hot-reload watches.
    config_path: str | None = None
    secrets_path: str | None = None


def load(config_path: Path, secrets_path: Path | None = None) -> Config:
    config_path = config_path.resolve()
    raw = yaml.safe_load(config_path.read_text())

    if not isinstance(raw, dict):
        kind = type(raw).__name__ if raw is not None else "empty file"
        raise ValueError(f"config file must be a YAML mapping, got {kind}")

    gw_raw = raw.get("gateway", {})
    gw_radio = _parse_radio(gw_raw.pop("radio", None))
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
        profiles_url=raw.get("profiles_url"),
        profiles_file=raw.get("profiles_file"),
    )

    labels = [n.label for n in cfg.nodes if n.label]
    dup_labels = {lbl for lbl in labels if labels.count(lbl) > 1}
    if dup_labels:
        raise ValueError(
            f"duplicate node labels in config: {', '.join(sorted(dup_labels))}"
            " - each label must be unique"
        )

    prefixes = [n.pubkey_prefix for n in cfg.nodes]
    dup_prefixes = {p for p in prefixes if prefixes.count(p) > 1}
    if dup_prefixes:
        raise ValueError(
            f"duplicate pubkey_prefix in config: {', '.join(sorted(dup_prefixes))}"
            " - each node must be unique"
        )

    group_names = {g.name for g in cfg.groups}
    for n in cfg.nodes:
        if n.group and n.group not in group_names:
            raise ValueError(
                f"node {n.pubkey_prefix!r} references unknown group {n.group!r}"
                f" - defined groups: {sorted(group_names) or '(none)'}"
            )

    # Resolve secrets file: explicit arg > config key > sibling secrets.yaml
    if secrets_path is None:
        secrets_path = Path(raw.get("secrets_file", config_path.parent / "secrets.yaml"))
    if not secrets_path.is_absolute():
        secrets_path = config_path.parent / secrets_path

    cfg.secrets = _load_secrets(secrets_path)
    cfg.config_path = str(config_path)
    cfg.secrets_path = str(secrets_path.resolve())

    return cfg


def _parse_setting(raw: dict[str, Any]) -> SettingConfig:
    policy = raw.get("policy")
    return SettingConfig(
        desired=raw.get("desired"),
        policy=SettingPolicy(policy) if policy else None,
        poll_hours=raw.get("poll_hours", 24),
    )


def _parse_radio(raw: Any) -> RadioConfig | None:
    """Parse a via/radio value: either a mapping or a "freq,bw,sf,cr" string.

    Frequencies are rounded to 3 decimal places (1 kHz resolution) so that
    firmware float noise (e.g. 915.7999877 for a set value of 915.800) does
    not cause spurious drift comparisons.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        freq, bw, sf, cr = raw.split(",")
        return RadioConfig(freq=round(float(freq), 3), bw=float(bw), sf=int(sf), cr=int(cr))
    return RadioConfig(
        freq=round(float(raw["freq"]), 3), **{k: v for k, v in raw.items() if k != "freq"}
    )


def _parse_node(raw: dict[str, Any]) -> NodeConfig:
    if "radio" in raw:
        raise ValueError(
            f"node {raw.get('pubkey_prefix')!r}: 'radio:' is no longer supported"
            " - use 'via:' instead (same format)"
        )

    pubkey_prefix = str(raw["pubkey_prefix"])
    if not _HEX_RE.match(pubkey_prefix):
        raise ValueError(
            f"pubkey_prefix {pubkey_prefix!r} contains non-hex characters"
            " - must be lowercase hex digits only"
        )
    if len(pubkey_prefix) % 2 != 0:
        raise ValueError(
            f"pubkey_prefix {pubkey_prefix!r} has odd length ({len(pubkey_prefix)} chars)"
            " - prefix must represent whole bytes (even number of hex chars)"
        )

    policy = raw.get("default_policy")
    settings = {
        k: _parse_setting(v if isinstance(v, dict) else {"desired": v})
        for k, v in raw.get("settings", {}).items()
    }
    operations = [
        OperationConfig(**op) if isinstance(op, dict) else OperationConfig(command=op)
        for op in raw.get("operations", [])
    ]
    via = _parse_radio(raw.get("via"))

    label = raw.get("label", "")
    if label and not _LABEL_RE.match(label):
        suggested = re.sub(r"[^a-zA-Z0-9_\-.]", "-", label).strip("-") or "my-node"
        raise ValueError(
            f"node label {label!r} contains invalid characters - "
            f"use letters, digits, dashes, underscores, or dots only "
            f"(e.g. {suggested!r})"
        )

    return NodeConfig(
        pubkey_prefix=pubkey_prefix,
        group=raw.get("group"),
        label=label,
        default_policy=SettingPolicy(policy) if policy else None,
        settings=settings,
        operations=operations,
        via=via,
    )


def _parse_group(raw: dict[str, Any]) -> GroupConfig:
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


def _load_secrets(path: Path) -> dict[str, NodeSecrets]:
    if not path.exists():
        return {}
    _check_permissions(path)
    try:
        raw = yaml.load(path.read_text(), Loader=_NoDuplicateKeysLoader) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"secrets file is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"secrets file must be a YAML mapping, got {type(raw).__name__}"
        )
    nodes = raw.get("nodes", {})
    if not isinstance(nodes, dict):
        raise ValueError("secrets.nodes must be a mapping of pubkey_prefix: password")
    result: dict[str, NodeSecrets] = {}
    for k, v in nodes.items():
        if isinstance(v, str):
            # old flat format: migrate to NodeSecrets
            result[str(k)] = NodeSecrets(password=v)
        elif isinstance(v, dict):
            result[str(k)] = NodeSecrets(
                password=v.get("password"),
                guest_password=v.get("guest_password"),
                prv_key=v.get("prv_key"),
            )
        else:
            raise ValueError(
                f"secrets.nodes.{k} must be a string (password) or mapping, got {type(v).__name__}"
            )
    return result


def _check_permissions(path: Path) -> None:
    mode = oct(os.stat(path).st_mode)[-3:]
    if mode not in ("600", "400"):
        raise PermissionError(
            f"{path} has permissions {mode} — secrets file must be 600 or 400"
        )
