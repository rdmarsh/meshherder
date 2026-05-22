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
# pubkey_prefix must be even-length hex (full bytes only); values are normalized to lowercase.
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_MIN_PREFIX_LEN = 8

_TOP_LEVEL_KEYS = {
    "gateway",
    "scheduler",
    "db_path",
    "socket_path",
    "secrets_file",
    "facts",
    "profiles_url",
    "profiles_file",
    "messenger",
    "groups",
    "nodes",
}
_GATEWAY_KEYS = {"device", "baud", "name", "radio", "advert_on_start"}
_SCHEDULER_KEYS = {"inter_node_delay_seconds", "sync_interval_days"}
_MESSENGER_KEYS = {"allowed_prefixes", "announce_channel"}
_NODE_KEYS = {"pubkey_prefix", "group", "label", "default_policy", "settings", "operations", "via"}
_GROUP_KEYS = {"name", "default_policy", "settings", "setting_overrides"}
_SETTING_KEYS = {"desired", "policy", "poll_hours"}
_OPERATION_KEYS = {"command", "every_hours", "fire_and_forget"}
_RADIO_KEYS = {"freq", "bw", "sf", "cr"}


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
    db_path: str = "/var/lib/meshherder/mch.db"
    socket_path: str = "/run/meshherder/mchd.sock"
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
    _reject_unknown_keys("config", raw, _TOP_LEVEL_KEYS)

    gw_raw = _mapping(raw.get("gateway", {}), "gateway")
    _reject_unknown_keys("gateway", gw_raw, _GATEWAY_KEYS)
    gw_radio = _parse_radio(gw_raw.pop("radio", None))
    sched_raw = _mapping(raw.get("scheduler", {}), "scheduler")
    _reject_unknown_keys("scheduler", sched_raw, _SCHEDULER_KEYS)
    msg_raw = _mapping(raw.get("messenger", {}), "messenger")
    _reject_unknown_keys("messenger", msg_raw, _MESSENGER_KEYS)
    nodes_raw = _list(raw.get("nodes", []), "nodes")
    groups_raw = _list(raw.get("groups", []), "groups")
    facts_raw = _list(raw.get("facts", ["ver", "board", "role"]), "facts")

    cfg = Config(
        gateway=GatewayConfig(**gw_raw, radio=gw_radio),
        scheduler=SchedulerConfig(**sched_raw),
        db_path=raw.get("db_path", "/var/lib/meshherder/mch.db"),
        socket_path=raw.get("socket_path", "/run/meshherder/mchd.sock"),
        facts=[str(f) for f in facts_raw],
        nodes=[_parse_node(n, f"nodes[{i}]") for i, n in enumerate(nodes_raw)],
        groups=[_parse_group(g, f"groups[{i}]") for i, g in enumerate(groups_raw)],
        messenger=MessengerConfig(
            allowed_prefixes=[
                _normalize_pubkey_prefix(p, f"messenger.allowed_prefixes[{i}]")
                for i, p in enumerate(
                    _list(msg_raw.get("allowed_prefixes", []), "messenger.allowed_prefixes")
                )
            ],
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


def _mapping(raw: Any, context: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a YAML mapping, got {type(raw).__name__}")
    return dict(raw)


def _list(raw: Any, context: str) -> list[Any]:
    if not isinstance(raw, list):
        raise ValueError(f"{context} must be a YAML list, got {type(raw).__name__}")
    return raw


def _reject_unknown_keys(context: str, raw: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(raw) - allowed
    if unknown:
        key = sorted(unknown)[0]
        noun = "node key" if context.startswith("nodes[") else f"{context} key"
        raise ValueError(f"unknown {noun} {key!r}")


def _normalize_pubkey_prefix(raw: Any, context: str) -> str:
    prefix = str(raw).lower()
    if not _HEX_RE.match(prefix):
        raise ValueError(
            f"{context} {prefix!r} contains non-hex characters"
            " - must be hex digits only"
        )
    if len(prefix) < _MIN_PREFIX_LEN:
        raise ValueError(
            f"{context} {prefix!r} is too short"
            f" - use at least {_MIN_PREFIX_LEN} hex characters"
        )
    if len(prefix) % 2 != 0:
        raise ValueError(
            f"{context} {prefix!r} has odd length ({len(prefix)} chars)"
            " - prefix must represent whole bytes (even number of hex chars)"
        )
    return prefix


def _parse_setting(raw: dict[str, Any], context: str = "setting") -> SettingConfig:
    raw = _mapping(raw, context)
    _reject_unknown_keys(context, raw, _SETTING_KEYS)
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
    raw = _mapping(raw, "radio")
    _reject_unknown_keys("radio", raw, _RADIO_KEYS)
    return RadioConfig(
        freq=round(float(raw["freq"]), 3), **{k: v for k, v in raw.items() if k != "freq"}
    )


def _parse_node(raw: Any, context: str = "node") -> NodeConfig:
    raw = _mapping(raw, context)
    if "radio" in raw:
        raise ValueError(
            f"node {raw.get('pubkey_prefix')!r}: 'radio:' is no longer supported"
            " - use 'via:' instead (same format)"
        )
    _reject_unknown_keys(context, raw, _NODE_KEYS)

    pubkey_prefix = _normalize_pubkey_prefix(raw["pubkey_prefix"], "pubkey_prefix")

    policy = raw.get("default_policy")
    settings_raw = _mapping(raw.get("settings", {}), f"{context}.settings")
    settings = {
        k: _parse_setting(v if isinstance(v, dict) else {"desired": v}, f"{context}.settings.{k}")
        for k, v in settings_raw.items()
    }
    operations_raw = _list(raw.get("operations", []), f"{context}.operations")
    operations = [
        _parse_operation(op, f"{context}.operations[{i}]")
        for i, op in enumerate(operations_raw)
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


def _parse_operation(raw: Any, context: str) -> OperationConfig:
    if not isinstance(raw, dict):
        return OperationConfig(command=str(raw))
    raw = _mapping(raw, context)
    _reject_unknown_keys(context, raw, _OPERATION_KEYS)
    return OperationConfig(**raw)


def _parse_group(raw: Any, context: str = "group") -> GroupConfig:
    raw = _mapping(raw, context)
    _reject_unknown_keys(context, raw, _GROUP_KEYS)
    policy = raw.get("default_policy")
    settings_raw = _mapping(raw.get("settings", {}), f"{context}.settings")
    settings = {
        k: _parse_setting(v if isinstance(v, dict) else {"desired": v}, f"{context}.settings.{k}")
        for k, v in settings_raw.items()
    }
    overrides_raw = _mapping(raw.get("setting_overrides", {}), f"{context}.setting_overrides")
    overrides = {
        k: _parse_setting(
            v if isinstance(v, dict) else {"policy": v},
            f"{context}.setting_overrides.{k}",
        )
        for k, v in overrides_raw.items()
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
        prefix = (
            str(k)
            if str(k) == "default"
            else _normalize_pubkey_prefix(k, f"secrets.nodes.{k}")
        )
        if isinstance(v, str):
            # old flat format: migrate to NodeSecrets
            result[prefix] = NodeSecrets(password=v)
        elif isinstance(v, dict):
            result[prefix] = NodeSecrets(
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
