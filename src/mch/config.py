"""Load and validate config.yaml and secrets.yaml."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field, replace
from difflib import get_close_matches
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import yaml

from mch.command_registry import (
    MAX_NODE_NAME_BYTES,
    readable_observation_keys,
    validate_value,
    writable_setting_keys,
)
from mch.meshkey import derive_public_key
from mch.models import DesiredSetting, Scope, SettingPolicy

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
    "api",
    "global",
    "groups",
    "nodes",
}
_GATEWAY_KEYS = {"device", "baud", "name", "radio", "advert_on_start"}
_SCHEDULER_KEYS = {"inter_node_delay_seconds", "sync_interval_days"}
_MESSENGER_KEYS = {"allowed_prefixes", "announce_channel"}
_API_KEYS = {"enabled", "host", "port"}
_NODE_KEYS = {
    "pubkey_prefix",
    "group",
    "label",
    "default_policy",
    "settings",
    "operations",
    "via_radio",
    "name_prefix",
}
_GROUP_KEYS = {
    "name",
    "default_policy",
    "settings",
    "setting_overrides",
    "operations",
    "via_radio",
    "name_prefix",
}
_GLOBAL_KEYS = {
    "default_policy",
    "settings",
    "setting_overrides",
    "operations",
    "via_radio",
    "name_prefix",
}
_SETTING_KEYS = {"desired", "policy", "poll_hours"}
_OPERATION_KEYS = {"command", "every_hours", "fire_and_forget"}
_RADIO_KEYS = {"freq", "bw", "sf", "cr"}


def sanitize_node_label(label: str) -> str:
    """Convert operator input into a valid local node label."""
    return re.sub(r"[^a-zA-Z0-9_\-.]", "-", label).strip("-") or "my-node"


def validate_node_label(label: str) -> None:
    """Reject local node labels that cannot be used in config and CLI lookup."""
    if label and not _LABEL_RE.match(label):
        suggested = sanitize_node_label(label)
        raise ValueError(
            f"node label {label!r} contains invalid characters - "
            f"use letters, digits, dashes, underscores, or dots only "
            f"(e.g. {suggested!r})"
        )


class _NoDuplicateKeysLoader(yaml.SafeLoader):
    """YAML loader that raises ValueError on duplicate mapping keys.

    yaml.safe_load silently overwrites duplicate keys with the last value.
    Operators who accidentally paste the same prefix twice would lose one entry silently.
    """
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        if isinstance(node, yaml.MappingNode):
            seen: dict[str, int] = {}
            for key_node, _ in node.value:
                key = key_node.value  # raw string before type coercion
                if key in seen:
                    first_line = seen[key]
                    duplicate_line = key_node.start_mark.line + 1
                    raise ValueError(
                        f"duplicate YAML key {key!r} at line {duplicate_line} "
                        f"(first defined at line {first_line})"
                    )
                seen[key] = key_node.start_mark.line + 1
        return super().construct_mapping(node, deep=deep)


def _yaml_error(kind: str, exc: yaml.YAMLError) -> ValueError:
    """Format parse failures with an actionable hint for common YAML mistakes."""
    detail = str(exc)
    if "\\t" in detail or "tab" in detail.lower():
        return ValueError(
            f"{kind} file contains a tab character in YAML indentation; "
            "replace tabs with spaces"
        )
    return ValueError(f"{kind} file is not valid YAML: {detail}")

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
    # If set, forced to the home radio on startup and restored after temporary switches.
    radio: RadioConfig | None = None
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
    # Radio tuple used to contact this node; overrides the group value when set.
    via_radio: RadioConfig | None = None
    # Prepended to the desired name (no auto-space); overrides group/global when set.
    name_prefix: str | None = None


@dataclass
class GroupConfig:
    name: str
    default_policy: SettingPolicy | None = None
    # Radio tuple used to contact members that do not set their own value.
    via_radio: RadioConfig | None = None
    # settings: desired values applied to all nodes in this group
    settings: dict[str, SettingConfig] = field(default_factory=dict)
    # setting_overrides: policy-only overrides (no desired value required)
    setting_overrides: dict[str, SettingConfig] = field(default_factory=dict)
    operations: list[OperationConfig] = field(default_factory=list)
    # Prepended to members' desired names (no auto-space); usually an emoji.
    name_prefix: str | None = None


@dataclass
class MessengerConfig:
    """Config for mesh DM control and channel announcements."""
    # pubkey prefixes allowed to issue DM commands (e.g. operator's handheld)
    allowed_prefixes: list[str] = field(default_factory=list)
    # channel slot number to post cycle summaries to (e.g. 1 for #mch)
    announce_channel: int | None = None


def resolve_via_radio(
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
    global_cfg: GroupConfig | None = None,
) -> RadioConfig | None:
    """Return the radio tuple on which a node is reachable, if configured."""
    return (
        node_cfg.via_radio
        or (group_cfg.via_radio if group_cfg else None)
        or (global_cfg.via_radio if global_cfg else None)
    )


def resolve_name_prefix(
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
    global_cfg: GroupConfig | None = None,
) -> str | None:
    """Return the name prefix for a node with Node > Group > Global precedence."""
    return (
        node_cfg.name_prefix
        or (group_cfg.name_prefix if group_cfg else None)
        or (global_cfg.name_prefix if global_cfg else None)
    )


def resolve_desired(
    key: str,
    node_cfg: NodeConfig,
    group_cfg: GroupConfig | None,
    global_cfg: GroupConfig | None = None,
) -> str | None:
    """Return the desired value for a setting, node overrides group overrides global.

    For the ``name`` key the resolved ``name_prefix`` (Node > Group > Global) is
    prepended verbatim - no separator is inserted, so an operator who wants a space
    puts it in the prefix. The prefix only applies when a base name is configured;
    a prefix with no desired name has nothing to prepend onto and yields None.
    """
    node_setting = node_cfg.settings.get(key)
    if node_setting and node_setting.desired is not None:
        desired = node_setting.desired
    elif group_cfg and (gs := group_cfg.settings.get(key)) and gs.desired is not None:
        desired = gs.desired
    elif global_cfg and (xs := global_cfg.settings.get(key)) and xs.desired is not None:
        desired = xs.desired
    else:
        desired = None

    if key == "name" and desired is not None:
        prefix = resolve_name_prefix(node_cfg, group_cfg, global_cfg)
        if prefix:
            return prefix + desired
    return desired


def _normalize_directive_text(key: str, raw: Any) -> str:
    """Canonical string form of a directive value (see normalize_directive_value).

    Leading whitespace is never meaningful, so it is always dropped. ``name_prefix`` is
    prepended to the name with no separator, so a *trailing* space is meaningful (e.g.
    ``"🌞 "``) and is kept; the other directives strip both ends. An all-whitespace value
    collapses to ``""``, which every directive parser reads as "cleared / inherit".
    """
    if raw is None:
        return ""
    text = str(raw)
    return text.lstrip() if key == "name_prefix" else text.strip()


def normalize_directive_value(key: str, value: str) -> str:
    """Public hook so the daemon stores the canonical directive value (clean DB/display).

    Read-time parsers normalise too, so this is belt-and-suspenders, but storing the
    normalised form keeps `mch status`/export tidy and matches what the operator meant.
    """
    return _normalize_directive_text(key, value)


def _coerce_name_prefix(raw: Any) -> str | None:
    """Stringify a name_prefix; drop leading space, keep trailing, empty -> no prefix."""
    return _normalize_directive_text("name_prefix", raw) or None


def _parse_policy_directive(raw: Any) -> SettingPolicy | None:
    """Parse a default_policy directive value; empty/all-space clears (inherit)."""
    text = _normalize_directive_text("default_policy", raw)
    return SettingPolicy(text) if text else None


def _parse_radio_directive(raw: Any) -> RadioConfig | None:
    """Parse a via_radio directive value; empty/all-space clears (inherit).

    Delegates to _parse_radio (defined later, resolved at call time), which raises
    on a malformed 'freq,bw,sf,cr' tuple - that drives both set-time rejection and
    the defensive skip in _directive_overrides.
    """
    text = _normalize_directive_text("via_radio", raw)
    return _parse_radio(text) if text else None


# Directive keys are desired state that maps to a *structured field* on NodeConfig/
# GroupConfig (resolved by resolve_name_prefix / resolve_via_radio / resolve_policy),
# not to a node `set <key> <value>` CLI command. Stored as desired_settings rows like
# any other key, but the overlay routes them to the field instead of the settings dict.
# Each entry maps the reserved key to (config field name, parser from the row's string).
_DIRECTIVE_PARSERS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "name_prefix": ("name_prefix", _coerce_name_prefix),
    "default_policy": ("default_policy", _parse_policy_directive),
    "via_radio": ("via_radio", _parse_radio_directive),
}
# Human hint shown when a directive value fails validation at set-time. name_prefix
# accepts any string, so it needs no hint.
_DIRECTIVE_VALUE_HINTS = {
    "default_policy": "must be enforce, audit, or skip",
    "via_radio": "must be a radio tuple 'freq,bw,sf,cr'",
}
DIRECTIVE_KEYS = frozenset(_DIRECTIVE_PARSERS)


def settable_keys() -> frozenset[str]:
    """Keys an operator may write via ``mch set``: node CLI settings plus directives.

    Directives (name_prefix, default_policy, via_radio) are not node CLI ``set``
    commands but are operator-authored desired state stored as desired_settings rows.
    """
    return writable_setting_keys() | DIRECTIVE_KEYS


def validate_directive_value(key: str, value: str) -> str | None:
    """Validate an operator-supplied directive value at set-time; None means ok.

    Reuses the overlay parser so set-time validation and read-time parsing cannot
    drift: a value that fails here is exactly one the overlay would have to skip.
    """
    entry = _DIRECTIVE_PARSERS.get(key)
    if entry is None:
        return None
    _, parse = entry
    try:
        parse(value)
    except (ValueError, TypeError):
        return _DIRECTIVE_VALUE_HINTS.get(key, "is invalid")
    return None


def _directive_overrides(
    directive_rows: list[DesiredSetting],
    current: NodeConfig | GroupConfig,
    ident: str,
    scope_noun: str,
) -> tuple[dict[str, Any], list[str]]:
    """Field overrides (for ``replace``) from directive rows, plus conflict warnings.

    A directive row sets the corresponding structured field rather than a settings
    entry. The DB wins over a YAML value for the same field, with a warning so the
    operator can drop the now-ignored YAML line - mirroring _merge_desired_rows.
    """
    overrides: dict[str, Any] = {}
    warnings: list[str] = []
    for row in directive_rows:
        field, parse = _DIRECTIVE_PARSERS[row.key]
        try:
            parsed = parse(row.desired)
        except (ValueError, TypeError):
            # set-time validation rejects bad values, so this only fires on a hand-edited
            # or legacy DB row. Skip it (fall back to YAML/inherit) rather than wedge the
            # whole sync cycle on one malformed directive.
            warnings.append(
                f"{scope_noun} {ident!r}: ignoring invalid {row.key} value "
                f"{row.desired!r} in the DB"
            )
            continue
        yaml_val = getattr(current, field)
        if yaml_val is not None:
            warnings.append(
                f"{scope_noun} {ident!r}: {row.key} is set in both config.yaml "
                f"({yaml_val!r}) and the DB ({row.desired!r}); "
                "DB wins - remove it from config.yaml"
            )
        overrides[field] = parsed
    return overrides, warnings


def _split_directive_rows(
    db_rows: list[DesiredSetting],
) -> tuple[list[DesiredSetting], list[DesiredSetting]]:
    """Partition DB rows into (setting rows, directive rows)."""
    settings_rows = [r for r in db_rows if r.key not in DIRECTIVE_KEYS]
    directive_rows = [r for r in db_rows if r.key in DIRECTIVE_KEYS]
    return settings_rows, directive_rows


def _merge_desired_rows(
    base_settings: dict[str, SettingConfig],
    db_rows: list[DesiredSetting],
    ident: str,
    scope_noun: str,
    extra_conflict: dict[str, SettingConfig] | None = None,
) -> tuple[dict[str, SettingConfig], list[str]]:
    """Layer DB desired rows over a config-derived settings dict (DB wins, warn on conflict).

    Shared by the node and group/global overlays. ``extra_conflict`` lets the group
    overlay also flag a DB key that shadows a policy-only YAML ``setting_override``.
    A None poll_hours on the DB row means "inherit"; we materialise the SettingConfig
    default (24h) since SettingConfig carries a concrete int.
    """
    merged = dict(base_settings)
    warnings: list[str] = []
    for row in db_rows:
        yaml_setting = merged.get(row.key)
        if yaml_setting is None and extra_conflict is not None:
            yaml_setting = extra_conflict.get(row.key)
        if yaml_setting is not None:
            warnings.append(
                f"{scope_noun} {ident!r}: setting {row.key!r} is set in both config.yaml "
                f"({yaml_setting.desired!r}) and the DB ({row.desired!r}); "
                "DB wins - remove it from config.yaml"
            )
        merged[row.key] = SettingConfig(
            desired=row.desired,
            policy=row.policy,
            poll_hours=row.poll_hours if row.poll_hours is not None else 24,
        )
    return merged, warnings


def overlay_db_settings(
    node_cfg: NodeConfig,
    db_rows: list[DesiredSetting],
) -> tuple[NodeConfig, list[str]]:
    """Merge DB node-scope desired settings onto a config-derived NodeConfig.

    The DB is the authoritative source of desired state (see DESIGN.md "Desired state
    in the DB"). Each DB row replaces the same-key setting from config.yaml, so the
    existing resolve_desired/resolve_policy/all_setting_keys path picks up DB values
    without any change to the resolver itself.

    During the config.yaml -> DB transition a key may still appear in both places.
    The DB wins, and we return a warning for every key present in both so the operator
    can remove the now-ignored YAML entry.

    Returns a *new* NodeConfig (the shared config-loaded object is never mutated) and
    the list of conflict warnings. With no DB rows the original object is returned
    unchanged so callers pay nothing on the common path.
    """
    if not db_rows:
        return node_cfg, []
    ident = node_cfg.label or node_cfg.pubkey_prefix
    setting_rows, directive_rows = _split_directive_rows(db_rows)
    merged, warnings = _merge_desired_rows(node_cfg.settings, setting_rows, ident, "node")
    overrides, dir_warnings = _directive_overrides(directive_rows, node_cfg, ident, "node")
    return replace(node_cfg, settings=merged, **overrides), warnings + dir_warnings


def overlay_db_group_settings(
    group_cfg: GroupConfig,
    db_rows: list[DesiredSetting],
) -> tuple[GroupConfig, list[str]]:
    """Merge DB group- or global-scope desired settings onto a config-derived GroupConfig.

    The group/global analogue of overlay_db_settings. All DB rows are placed in
    ``settings`` (never ``setting_overrides``): a row with a desired value drives
    resolve_desired, and a policy-only row (desired=None) is skipped by resolve_desired
    but still consulted by resolve_policy/all_setting_keys - exactly as a node-scope
    overlay row behaves. A conflict warning is raised when the key already appears in
    the group's YAML ``settings`` or its policy-only ``setting_overrides``.

    Returns a *new* GroupConfig; the shared config-loaded object is never mutated.
    """
    if not db_rows:
        return group_cfg, []
    setting_rows, directive_rows = _split_directive_rows(db_rows)
    merged, warnings = _merge_desired_rows(
        group_cfg.settings, setting_rows, group_cfg.name, "group",
        extra_conflict=group_cfg.setting_overrides,
    )
    overrides, dir_warnings = _directive_overrides(
        directive_rows, group_cfg, group_cfg.name, "group"
    )
    return replace(group_cfg, settings=merged, **overrides), warnings + dir_warnings


def _radio_to_str(radio: RadioConfig) -> str:
    """Serialise a RadioConfig to the 'freq,bw,sf,cr' string `_parse_radio` accepts.

    Integral floats render without a trailing '.0' (bw 250.0 -> '250') so the result
    matches how operators write it in config.yaml and round-trips through `_parse_radio`.
    """
    def num(x: float) -> str:
        return str(int(x)) if float(x).is_integer() else str(x)
    return f"{num(radio.freq)},{num(radio.bw)},{radio.sf},{radio.cr}"


def _scope_rows(
    scope: Scope,
    scope_id: str,
    cfg: NodeConfig | GroupConfig,
) -> list[DesiredSetting]:
    """All desired_settings rows for one scope object (settings + directives).

    A default poll_hours (24) stores as None (inherit) to match `mch set` and keep rows
    tidy; a non-default value is preserved. Policy-only group/global ``setting_overrides``
    become desired=None rows. The three directives serialise to their reserved keys.
    """
    rows: list[DesiredSetting] = []
    settings = dict(cfg.settings)
    settings.update(getattr(cfg, "setting_overrides", {}))  # group/global only
    for key, sc in settings.items():
        rows.append(DesiredSetting(
            scope, scope_id, key,
            desired=sc.desired,
            policy=sc.policy,
            poll_hours=None if sc.poll_hours == 24 else sc.poll_hours,
        ))
    if cfg.name_prefix is not None:
        rows.append(DesiredSetting(scope, scope_id, "name_prefix", desired=cfg.name_prefix))
    if cfg.default_policy is not None:
        rows.append(
            DesiredSetting(scope, scope_id, "default_policy", desired=cfg.default_policy.value)
        )
    if cfg.via_radio is not None:
        rows.append(
            DesiredSetting(scope, scope_id, "via_radio", desired=_radio_to_str(cfg.via_radio))
        )
    return rows


def config_to_desired_rows(
    cfg: Config,
    resolve_pubkey: Callable[[str], str | None],
) -> tuple[list[DesiredSetting], list[str]]:
    """Flatten a loaded Config's desired state into desired_settings rows (for `mch import`).

    Emits one row per setting and per set directive at each scope: global (scope_id ``""``),
    each group (scope_id = group name), and each node (scope_id = full pubkey, resolved from
    its ``pubkey_prefix`` via ``resolve_pubkey``). A node whose prefix cannot be resolved -
    not yet seen by the gateway/DB - is skipped with a warning. Operations are *not* imported;
    they are not part of desired_settings.
    """
    rows: list[DesiredSetting] = []
    warnings: list[str] = []

    rows += _scope_rows(Scope.GLOBAL, "", cfg.global_settings)
    for group in cfg.groups:
        rows += _scope_rows(Scope.GROUP, group.name, group)
    for node in cfg.nodes:
        pubkey = resolve_pubkey(node.pubkey_prefix)
        if pubkey is None:
            ident = node.label or node.pubkey_prefix
            warnings.append(
                f"node {ident!r} ({node.pubkey_prefix}) is not known to the DB yet - "
                "skipped; it will import once the gateway has seen it"
            )
            continue
        rows += _scope_rows(Scope.NODE, pubkey, node)

    return rows, warnings


@dataclass
class ApiConfig:
    """Read-only HTTP API configuration for local dashboard and integration clients."""
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8743

    def __post_init__(self) -> None:
        try:
            loopback = self.host == "localhost" or ip_address(self.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError(
                "api.host must be a loopback address (127.0.0.1, ::1, or localhost)"
                " until HTTP API authentication is implemented"
            )
        if not 0 <= self.port <= 65535:
            raise ValueError("api.port must be between 0 and 65535")


@dataclass
class Config:
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    nodes: list[NodeConfig] = field(default_factory=list)
    global_settings: GroupConfig = field(default_factory=lambda: GroupConfig(name="global"))
    groups: list[GroupConfig] = field(default_factory=list)
    messenger: MessengerConfig = field(default_factory=MessengerConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
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


def load(
    config_path: Path,
    secrets_path: Path | None = None,
    *,
    load_secrets: bool = True,
) -> Config:
    config_path = config_path.resolve()
    try:
        raw = yaml.load(config_path.read_text(), Loader=_NoDuplicateKeysLoader)
    except yaml.YAMLError as exc:
        raise _yaml_error("config", exc) from exc

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
    api_raw = _mapping(raw.get("api", {}), "api")
    _reject_unknown_keys("api", api_raw, _API_KEYS)
    nodes_raw = _list(raw.get("nodes", []), "nodes")
    global_raw = _mapping(raw.get("global", {}), "global")
    _reject_unknown_keys("global", global_raw, _GLOBAL_KEYS)
    groups_raw = _list(raw.get("groups", []), "groups")
    facts_raw = _list(raw.get("facts", ["ver", "board", "role"]), "facts")

    cfg = Config(
        gateway=GatewayConfig(**gw_raw, radio=gw_radio),
        scheduler=SchedulerConfig(**sched_raw),
        db_path=raw.get("db_path", "/var/lib/meshherder/mch.db"),
        socket_path=raw.get("socket_path", "/run/meshherder/mchd.sock"),
        facts=[str(f) for f in facts_raw],
        nodes=[_parse_node(n, f"nodes[{i}]") for i, n in enumerate(nodes_raw)],
        global_settings=_parse_global(global_raw),
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
        api=ApiConfig(**api_raw),
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
    unknown_group_errors: list[str] = []
    for n in cfg.nodes:
        if n.group and n.group not in group_names:
            suggestion = get_close_matches(n.group, group_names, n=1, cutoff=0.45)
            hint = f" (did you mean {suggestion[0]!r}?)" if suggestion else ""
            unknown_group_errors.append(
                f"node {n.pubkey_prefix!r} references unknown group {n.group!r}{hint}"
            )
    if unknown_group_errors:
        defined = sorted(group_names) or "(none)"
        raise ValueError(
            "; ".join(unknown_group_errors)
            + f" - defined groups: {defined}"
        )

    # Reject any node whose prefixed desired name exceeds the firmware byte cap.
    # Truncation corrupts multibyte chars and loops forever (see MAX_NODE_NAME_BYTES),
    # so fail at load rather than ship a name that can never converge.
    groups_by_name = {g.name: g for g in cfg.groups}
    for n in cfg.nodes:
        group_cfg = groups_by_name.get(n.group) if n.group else None
        effective_name = resolve_desired("name", n, group_cfg, cfg.global_settings)
        if effective_name is None:
            continue
        nbytes = len(effective_name.encode("utf-8"))
        if nbytes > MAX_NODE_NAME_BYTES:
            ident = n.label or n.pubkey_prefix
            prefix = resolve_name_prefix(n, group_cfg, cfg.global_settings)
            prefix_note = f" (name_prefix {prefix!r} + name)" if prefix else ""
            raise ValueError(
                f"node {ident!r}: desired name {effective_name!r}{prefix_note} is "
                f"{nbytes} bytes, over the {MAX_NODE_NAME_BYTES}-byte firmware limit"
                " - shorten the name or prefix (the node would truncate and never converge)"
            )

    # Resolve secrets file: explicit arg > config key > sibling secrets.yaml
    if secrets_path is None:
        secrets_path = Path(raw.get("secrets_file", config_path.parent / "secrets.yaml"))
    if not secrets_path.is_absolute():
        secrets_path = config_path.parent / secrets_path

    cfg.secrets = _load_secrets(secrets_path) if load_secrets else {}
    cfg.config_path = str(config_path)
    cfg.secrets_path = str(secrets_path.resolve())

    return cfg


def secrets_identity_warnings(cfg: Config) -> list[str]:
    """Return warnings for secrets private keys that do not match config prefixes."""
    node_prefixes = {n.pubkey_prefix for n in cfg.nodes}
    warnings: list[str] = []

    for prefix, secrets in sorted(cfg.secrets.items()):
        if prefix == "default" or not secrets.prv_key:
            continue
        if prefix not in node_prefixes:
            continue
        try:
            pubkey = derive_public_key(str(secrets.prv_key))
        except (TypeError, ValueError) as exc:
            warnings.append(f"secrets.nodes.{prefix}.prv_key is invalid: {exc}")
            continue

        if not pubkey.startswith(prefix):
            suggested = pubkey[: max(len(prefix), _MIN_PREFIX_LEN)]
            warnings.append(
                f"secrets.nodes.{prefix}.prv_key derives public key {pubkey[:12]}..., "
                f"which does not match config pubkey_prefix {prefix!r}; "
                f"expected prefix would start with {suggested!r}"
            )

    return warnings


def fact_name_warnings(cfg: Config) -> list[str]:
    """Return warnings for fact reads not represented in command metadata."""
    known = readable_observation_keys()
    allowed = ", ".join(sorted(known))
    warnings: list[str] = []

    for key in getattr(cfg, "facts", []):
        if key in known:
            continue
        suggestion = get_close_matches(key, known, n=1, cutoff=0.6)
        hint = f"; did you mean {suggestion[0]!r}?" if suggestion else ""
        warnings.append(
            f"facts contains unrecognised key {key!r}{hint} "
            f"Known readable facts/settings: {allowed}. "
            f"Unknown keys remain permitted and will be read as 'get {key}'."
        )
    return warnings


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


def _coerce_setting_value(v: Any) -> str | None:
    """Stringify a setting value from YAML.

    YAML 1.1 parses on/off/yes/no/true/false (all case variants) as Python bools,
    losing the original spelling. MeshCore CLI uses 'on'/'off' for boolean settings
    (e.g. 'powersaving on'), so coerce True→'on' and False→'off'. Ints and floats
    are stringified directly; strings pass through unchanged.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v)


def _parse_setting(raw: dict[str, Any], context: str = "setting") -> SettingConfig:
    raw = _mapping(raw, context)
    _reject_unknown_keys(context, raw, _SETTING_KEYS)
    policy = raw.get("policy")
    return SettingConfig(
        desired=_coerce_setting_value(raw.get("desired")),
        policy=SettingPolicy(policy) if policy else None,
        poll_hours=raw.get("poll_hours", 24),
    )


def _validate_settings(settings: dict[str, SettingConfig], context: str) -> None:
    for key, setting in settings.items():
        if setting.desired is None:
            continue
        error = validate_value(key, setting.desired)
        if error:
            raise ValueError(f"{context}.{key}.desired {error}: {setting.desired!r}")


def _parse_radio(raw: Any) -> RadioConfig | None:
    """Parse a radio value: either a mapping or a "freq,bw,sf,cr" string.

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
            " - use 'via_radio:' instead (same format)"
        )
    _reject_unknown_keys(context, raw, _NODE_KEYS)

    pubkey_prefix = _normalize_pubkey_prefix(raw["pubkey_prefix"], "pubkey_prefix")

    policy = raw.get("default_policy")
    settings_raw = _mapping(raw.get("settings", {}), f"{context}.settings")
    settings = {
        k: _parse_setting(v if isinstance(v, dict) else {"desired": v}, f"{context}.settings.{k}")
        for k, v in settings_raw.items()
    }
    _validate_settings(settings, f"{context}.settings")
    operations_raw = _list(raw.get("operations", []), f"{context}.operations")
    operations = [
        _parse_operation(op, f"{context}.operations[{i}]")
        for i, op in enumerate(operations_raw)
    ]
    via_radio = _parse_radio(raw.get("via_radio"))

    label = raw.get("label", "")
    validate_node_label(label)

    return NodeConfig(
        pubkey_prefix=pubkey_prefix,
        group=raw.get("group"),
        label=label,
        default_policy=SettingPolicy(policy) if policy else None,
        settings=settings,
        operations=operations,
        via_radio=via_radio,
        name_prefix=_coerce_name_prefix(raw.get("name_prefix")),
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
    _validate_settings(settings, f"{context}.settings")
    overrides_raw = _mapping(raw.get("setting_overrides", {}), f"{context}.setting_overrides")
    overrides = {
        k: _parse_setting(
            v if isinstance(v, dict) else {"policy": v},
            f"{context}.setting_overrides.{k}",
        )
        for k, v in overrides_raw.items()
    }
    _validate_settings(overrides, f"{context}.setting_overrides")
    operations_raw = _list(raw.get("operations", []), f"{context}.operations")
    operations = [
        _parse_operation(op, f"{context}.operations[{i}]")
        for i, op in enumerate(operations_raw)
    ]
    return GroupConfig(
        name=raw["name"],
        default_policy=SettingPolicy(policy) if policy else None,
        via_radio=_parse_radio(raw.get("via_radio")),
        settings=settings,
        setting_overrides=overrides,
        operations=operations,
        name_prefix=_coerce_name_prefix(raw.get("name_prefix")),
    )


def _parse_global(raw: dict[str, Any]) -> GroupConfig:
    policy = raw.get("default_policy")
    settings_raw = _mapping(raw.get("settings", {}), "global.settings")
    settings = {
        k: _parse_setting(v if isinstance(v, dict) else {"desired": v}, f"global.settings.{k}")
        for k, v in settings_raw.items()
    }
    _validate_settings(settings, "global.settings")
    overrides_raw = _mapping(raw.get("setting_overrides", {}), "global.setting_overrides")
    overrides = {
        k: _parse_setting(
            v if isinstance(v, dict) else {"policy": v},
            f"global.setting_overrides.{k}",
        )
        for k, v in overrides_raw.items()
    }
    _validate_settings(overrides, "global.setting_overrides")
    operations_raw = _list(raw.get("operations", []), "global.operations")
    operations = [
        _parse_operation(op, f"global.operations[{i}]")
        for i, op in enumerate(operations_raw)
    ]
    return GroupConfig(
        name="global",
        default_policy=SettingPolicy(policy) if policy else None,
        via_radio=_parse_radio(raw.get("via_radio")),
        settings=settings,
        setting_overrides=overrides,
        operations=operations,
        name_prefix=_coerce_name_prefix(raw.get("name_prefix")),
    )


def _load_secrets(path: Path) -> dict[str, NodeSecrets]:
    if not path.exists():
        return {}
    _check_permissions(path)
    try:
        raw = yaml.load(path.read_text(), Loader=_NoDuplicateKeysLoader) or {}
    except yaml.YAMLError as exc:
        raise _yaml_error("secrets", exc) from exc
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
