"""Load and validate config.yaml and secrets.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class GatewayConfig:
    device: str = "/dev/meshcore0"
    baud: int = 115200
    name: str | None = None  # if set, mcherd applies this name to the gateway on startup


@dataclass
class SchedulerConfig:
    inter_node_delay_seconds: int = 30
    sync_interval_minutes: int = 60


@dataclass
class NodeConfig:
    pubkey_prefix: str
    name: str = ""
    desired: list[str] = field(default_factory=list)  # raw CLI commands to apply


@dataclass
class Config:
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    nodes: list[NodeConfig] = field(default_factory=list)
    db_path: str = "mcherd.db"
    socket_path: str = "/run/mcherd.sock"
    secrets: dict[str, str] = field(default_factory=dict)  # pubkey_prefix -> password


def load(config_path: Path, secrets_path: Path | None = None) -> Config:
    raw = yaml.safe_load(config_path.read_text())

    gw_raw = raw.get("gateway", {})
    sched_raw = raw.get("scheduler", {})

    cfg = Config(
        gateway=GatewayConfig(**gw_raw),
        scheduler=SchedulerConfig(**sched_raw),
        db_path=raw.get("db_path", "mcherd.db"),
        socket_path=raw.get("socket_path", "/run/mcherd.sock"),
        nodes=[NodeConfig(**n) for n in raw.get("nodes", [])],
    )

    # Resolve secrets file: explicit arg > config key > sibling secrets.yaml
    if secrets_path is None:
        secrets_path = Path(raw.get("secrets_file", config_path.parent / "secrets.yaml"))

    if secrets_path.exists():
        _check_permissions(secrets_path)
        secrets_raw = yaml.safe_load(secrets_path.read_text()) or {}
        cfg.secrets = secrets_raw.get("nodes", {})

    return cfg


def _check_permissions(path: Path) -> None:
    mode = oct(os.stat(path).st_mode)[-3:]
    if mode not in ("600", "400"):
        raise PermissionError(
            f"{path} has permissions {mode} — secrets file must be 600 or 400"
        )
