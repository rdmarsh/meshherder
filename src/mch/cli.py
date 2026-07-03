"""
mch — control CLI for the mchd daemon.

Talks to the daemon over its Unix socket.
"""

from __future__ import annotations

import asyncio
import grp
import json
import os
import pwd
import re
import shlex
import subprocess
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from mch import __version__
from mch.command_registry import (
    MAX_NODE_NAME_BYTES,
    parse_radio_tuple,
    readable_observation_keys,
    validate_radio_tuple,
)
from mch.command_safety import blocked_exec_command, confirm_exec_command
from mch.config import (
    fact_name_warnings,
    sanitize_node_label,
    secrets_identity_warnings,
    settable_keys,
)
from mch.config import load as load_config
from mch.meshkey import derive_public_key, generate_private_key
from mch.recommend import load_radio_presets

DEFAULT_SOCKET = "/run/meshherder/mchd.sock"
DEFAULT_CONFIG = Path("/etc/meshherder/config.yaml")

STATE_COLORS = {
    "configured": "green",
    "online": "green",
    "partial": "yellow",
    "configuring": "yellow",
    "unknown": "yellow",
    "unreachable": "red",
    "offline": "red",
}


def _wlen(s: str) -> int:
    """Visual width of a string — wide chars (emoji, CJK) count as 2 columns."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _wpad(s: str, width: int) -> str:
    """Left-justify s in a field of visual width."""
    return s + " " * max(0, width - _wlen(s))


def _pk(pubkey: str | None, length: int = 12) -> str:
    """Short pubkey prefix for tables."""
    return (pubkey or "-")[:length]


def _sv(v: object) -> str:
    """Stringify a setting value for display — config values can be int/bool/float."""
    return str(v) if v is not None else "-"


def _state_text(state: str | None) -> str:
    text = state or "-"
    color = STATE_COLORS.get(text)
    return click.style(text, fg=color) if color else text


def _state_cell(state: str | None, width: int) -> str:
    text = state or "-"
    padded = f"{text:<{width}}"
    color = STATE_COLORS.get(text)
    return click.style(padded, fg=color) if color else padded


def _ago(iso: str | None) -> str:
    """Format an ISO datetime string as a compact relative time ('  5m ago', '  2h ago')."""
    if not iso:
        return "never"
    try:
        dt = _parse_display_datetime(iso)
        secs = int((datetime.now(UTC) - dt).total_seconds())
        if secs < 0:
            return "just now"
        if secs < 120:
            return f"{secs:>3}s ago"
        if secs < 3600:
            return f"{secs // 60:>3}m ago"
        if secs < 86400:
            return f"{secs // 3600:>3}h ago"
        return f"{secs // 86400:>3}d ago"
    except (ValueError, TypeError):
        return iso[:16].replace("T", " ")


def _parse_display_datetime(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _local_ts(iso: str | None) -> str:
    """Format an ISO timestamp in the operator's local timezone."""
    if not iso:
        return ""
    try:
        return _parse_display_datetime(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return iso[:19].replace("T", " ")


def _ago_with_ts(iso: str | None) -> str:
    """Format as 'YYYY-MM-DD HH:MM:SS (Xm ago)' for prose-style detail views."""
    if not iso:
        return "never"
    ts = _local_ts(iso)
    return f"{ts} ({_ago(iso).lstrip()})"


def _next_sync_text(iso: str | None) -> str:
    """Format the scheduled sync time as an estimate, not a guarantee."""
    if not iso:
        return "unscheduled"
    try:
        dt = _parse_display_datetime(iso)
    except (ValueError, TypeError):
        return iso[:19].replace("T", " ")
    ts = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    secs = int((dt - datetime.now(UTC)).total_seconds())
    if secs <= 0:
        return f"{ts} (due now)"
    return f"{ts} (in ~{_duration_text(secs)})"


_CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s+-\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+UTC\s*$")


def _duration_text(seconds: int) -> str:
    seconds = abs(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts[:2])


def _clock_drift_text(value: str | None, observed_at: str | None) -> str:
    if not value or not observed_at:
        return ""
    match = _CLOCK_RE.match(value)
    if not match:
        return ""
    hour, minute, day, month, year = (int(part) for part in match.groups())
    try:
        node_clock = datetime(year, month, day, hour, minute, tzinfo=UTC)
        observed = _parse_display_datetime(observed_at).astimezone(UTC)
    except ValueError:
        return ""
    drift = int((node_clock - observed).total_seconds())
    if abs(drift) < 60:
        return "within ±1m"
    direction = "fast" if drift > 0 else "slow"
    return f"{direction} by {_duration_text(drift)}"


def _log_node_pubkey(entry: dict[str, Any]) -> str:
    pubkey = entry.get("display_pubkey") or entry.get("node_pubkey")
    return pubkey[:12] if pubkey else ""


def _log_node_label(entry: dict[str, Any]) -> str:
    if not _log_node_pubkey(entry):
        return ""
    return entry.get("display_label") or entry.get("label") or "-"


async def _request(
    socket_path: str,
    cmd: dict[str, Any],
    timeout: float = 30,
    on_progress: Any = None,
) -> dict[str, Any]:
    try:
        # Contacts output can exceed asyncio's 64 KB default stream limit.
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=2**22)
    except (FileNotFoundError, ConnectionRefusedError):
        click.echo(f"error: cannot connect to daemon at {socket_path}", err=True)
        click.echo("  is mchd running?  try: mchd --help", err=True)
        _check_default_config_when_daemon_unavailable()
        raise SystemExit(1) from None

    writer.write((json.dumps(cmd) + "\n").encode())
    await writer.drain()
    try:
        while True:
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if raw == b"":
                writer.close()
                click.echo("error: daemon connection closed before a response", err=True)
                raise SystemExit(1) from None
            try:
                data: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                writer.close()
                click.echo("error: daemon returned an invalid response", err=True)
                raise SystemExit(1) from None
            if data.get("type") == "progress":
                if on_progress:
                    on_progress(data["msg"])
            else:
                writer.close()
                return data
    except TimeoutError:
        writer.close()
        click.echo(f"error: timed out waiting for daemon response ({timeout:.0f}s)", err=True)
        raise SystemExit(1) from None


def _rpc(
    socket_path: str, cmd: dict[str, Any], timeout: float = 30, on_progress: Any = None
) -> dict[str, Any]:
    return asyncio.run(_request(socket_path, cmd, timeout=timeout, on_progress=on_progress))


def _check_default_config_when_daemon_unavailable() -> None:
    """Show config health when daemon commands fail before reaching mchd."""
    config_path = _default_config_path()
    if not config_path.exists():
        click.echo(f"  default config not found: {config_path}", err=True)
        return
    click.echo(f"  checking config: {config_path}", err=True)
    try:
        cfg = load_config(config_path, load_secrets=False)
    except (PermissionError, ValueError, OSError) as exc:
        click.echo(f"  config check failed: {exc}", err=True)
        click.echo("  run: mch config validate", err=True)
        return
    click.echo(f"  config ok: {cfg.config_path}", err=True)


def _check(resp: dict[str, Any]) -> Any:
    if not resp.get("ok"):
        click.echo(f"error: {resp.get('error', 'unknown error')}", err=True)
        raise SystemExit(1)
    return resp.get("data")


def _commission_busy_hint(error: str) -> str | None:
    if "busy: commission" not in error:
        return None
    return (
        "Another commissioning operation is still finishing or cancelling.\n"
        "This usually clears after the current radio/discovery step completes.\n"
        "Wait about 10-30 seconds and retry. If it stays busy, restart mchd."
    )


def _default_config_path() -> Path:
    return DEFAULT_CONFIG


def _systemctl(action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", action, "mchd"], capture_output=True, text=True)


def _meshcore_cli_command(
    config_path: Path,
    executable: str,
    extra_args: tuple[str, ...],
) -> list[str]:
    cfg = load_config(config_path, load_secrets=False)
    return [
        executable,
        "-s",
        cfg.gateway.device,
        "-b",
        str(cfg.gateway.baud),
        *extra_args,
    ]


def _sudo_invoking_user() -> dict[str, Any]:
    """Return subprocess kwargs that drop sudo back to the invoking user."""
    if os.name != "posix" or os.geteuid() != 0:
        return {}
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_uid or not sudo_gid or not sudo_user or sudo_user == "root":
        return {}

    uid = int(sudo_uid)
    gid = int(sudo_gid)
    try:
        pwent = pwd.getpwuid(uid)
    except KeyError:
        pwent = None

    groups = [gid]
    try:
        groups.extend(
            group.gr_gid
            for group in grp.getgrall()
            if sudo_user in group.gr_mem and group.gr_gid not in groups
        )
    except OSError:
        pass

    env = dict(os.environ)
    env.update({"USER": sudo_user, "LOGNAME": sudo_user})
    if pwent is not None:
        env["HOME"] = pwent.pw_dir

    return {
        "user": uid,
        "group": gid,
        "extra_groups": groups,
        "env": env,
    }


def _run_admin_session(
    *,
    config_path: Path,
    executable: str,
    restart: bool,
    extra_args: tuple[str, ...],
) -> None:
    command = _meshcore_cli_command(config_path, executable, extra_args)
    click.echo("stopping mchd so meshcore-cli can own the gateway serial port...")
    stop_result = _systemctl("stop")
    if stop_result.returncode != 0:
        click.echo(stop_result.stderr.strip() or "systemctl stop failed", err=True)
        raise SystemExit(stop_result.returncode)

    click.echo("running: " + " ".join(shlex.quote(part) for part in command))
    try:
        try:
            result = subprocess.run(command, **_sudo_invoking_user())
        except FileNotFoundError:
            click.echo(f"error: {executable!r} not found", err=True)
            raise SystemExit(127) from None
        if result.returncode != 0:
            raise SystemExit(result.returncode)
    finally:
        if restart:
            click.echo("restarting mchd...")
            start_result = _systemctl("start")
            if start_result.returncode != 0:
                click.echo(start_result.stderr.strip() or "systemctl start failed", err=True)
                raise SystemExit(start_result.returncode)
            click.echo("mchd restarted")
        else:
            click.echo("mchd left stopped")


# ---------------------------------------------------------------------------
# group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(__version__, "-V", "--version")
@click.option(
    "--socket", "-s",
    default=DEFAULT_SOCKET,
    envvar="MCHD_SOCKET",
    help="Path to daemon Unix socket",
    show_default=True,
)
@click.option(
    "--color/--no-color",
    default=None,
    help="Control ANSI color output. Honors NO_COLOR when unset.",
)
@click.pass_context
def cli(ctx: click.Context, socket: str, color: bool | None) -> None:
    """mch — MeshCore node management CLI"""
    ctx.ensure_object(dict)
    ctx.obj["socket"] = socket
    if color is None and "NO_COLOR" in os.environ:
        color = False
    ctx.color = color


# ---------------------------------------------------------------------------
# Command groups (noun-nesting). Commands attach to one of these instead of
# the top-level `cli`, so the menu stays shallow as the tool grows. ctx.obj
# (socket) and ctx.color propagate down the parent chain automatically, so the
# group callbacks stay empty. Function names are suffixed `_group` to avoid
# shadowing the many local `config`/`node` variables used below.
# ---------------------------------------------------------------------------

@cli.group("daemon")
def daemon_group() -> None:
    """Control the mchd daemon process and gateway session."""


@cli.group("fleet")
def fleet_group() -> None:
    """Observe the whole fleet: status, sync, plan, logs."""


@cli.group("node")
def node_group() -> None:
    """Manage individual nodes: inventory, lifecycle, diagnostics."""


@cli.group("config")
def config_group() -> None:
    """Edit and back up desired state (the DB source of truth)."""


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

@config_group.command("validate")
@click.option(
    "--config", "-c",
    "config_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    show_default="/etc/meshherder/config.yaml",
    help="Path to config.yaml",
)
@click.option(
    "--secrets",
    "secrets_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to secrets.yaml. Defaults to config secrets_file or sibling secrets.yaml.",
)
def validate_config(config_path: Path | None, secrets_path: Path | None) -> None:
    """Validate config.yaml and secrets.yaml without contacting mchd."""
    config_path = config_path or _default_config_path()
    click.echo(f"checking config: {config_path}")
    if not config_path.exists():
        click.echo(f"error: config file not found: {config_path}", err=True)
        raise SystemExit(1)
    if secrets_path is not None and not secrets_path.exists():
        click.echo(f"error: secrets file not found: {secrets_path}", err=True)
        raise SystemExit(1)

    try:
        cfg = load_config(config_path, secrets_path=secrets_path)
    except PermissionError as exc:
        if getattr(exc, "errno", None) != 13:
            click.echo(f"error: config validation failed: {exc}", err=True)
            raise SystemExit(1) from None
        try:
            cfg = load_config(config_path, secrets_path=secrets_path, load_secrets=False)
        except (ValueError, PermissionError, OSError) as config_exc:
            click.echo(
                f"error: config validation failed: {config_exc}",
                err=True,
            )
            raise SystemExit(1) from None

        click.echo(f"config ok: {cfg.config_path}")
        click.echo(f"nodes: {len(cfg.nodes)}  groups: {len(cfg.groups)}  secrets: not checked")
        for warning in fact_name_warnings(cfg):
            click.echo(f"warning: {warning}", err=True)
        click.echo(
            f"warning: cannot read secrets file: {exc.filename or cfg.secrets_path or exc}",
            err=True,
        )
        click.echo("  try: sudo mch config validate", err=True)
        raise SystemExit(1) from None
    except (ValueError, OSError) as exc:
        click.echo(f"error: config validation failed: {exc}", err=True)
        raise SystemExit(1) from None

    click.echo(f"config ok: {cfg.config_path}")
    click.echo(f"nodes: {len(cfg.nodes)}  groups: {len(cfg.groups)}  secrets: {len(cfg.secrets)}")
    if cfg.secrets_path and Path(cfg.secrets_path).exists():
        click.echo(f"secrets ok: {cfg.secrets_path}")
    else:
        click.echo("secrets: none found")
    for warning in fact_name_warnings(cfg):
        click.echo(f"warning: {warning}", err=True)
    for warning in secrets_identity_warnings(cfg):
        click.echo(f"warning: {warning}", err=True)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _status_view(fleet: dict[str, Any]) -> dict[str, Any]:
    """Project the fleet API model into the existing terminal status renderer."""
    if "schema_version" not in fleet:
        return fleet
    return {
        "nodes": [
            {
                "pubkey": node["pubkey"],
                "name": node["name"],
                "label": node["label"],
                "group": node["group"],
                "state": node["state"],
                "last_seen": node["last_seen"],
                "last_connected": node["last_connected"],
                "neighbors": node["facts"].get("neighbors"),
                "ver": node["facts"].get("ver"),
            }
            for node in fleet["nodes"]
        ],
        **fleet["issues"],
    }


def _node_detail_view(node: dict[str, Any]) -> dict[str, Any]:
    """Project a canonical node API response into the existing terminal detail renderer."""
    if "auth" not in node:
        return node
    facts = {key: "???" for key in node.get("fact_keys", [])}
    facts.update(node["facts"])
    fact_times = {key: None for key in facts}
    fact_times.update(node.get("facts_collected_at", {}))
    return {
        "pubkey": node["pubkey"],
        "name": node["name"],
        "label": node["label"],
        "group": node["group"],
        "state": node["state"],
        "last_seen": node["last_seen"],
        "last_connected": node["last_connected"],
        "next_sync_at": node.get("next_sync_at"),
        "auth_cached": node["auth"]["cached"],
        "passwd_known": node["auth"]["password_known"],
        "management_path": node.get("management_path"),
        "facts": facts,
        "facts_collected_at": fact_times,
        "settings": [
            {
                "key": setting["key"],
                "desired": setting["desired"],
                "observed": setting["observed"],
                "policy": setting["policy"],
                "source": setting.get("source"),
                "update_attempts": setting["update_attempts"],
                "drift": setting["status"] == "drift",
                "approximate": setting["status"] == "nominal",
            }
            for setting in node["settings"]
            if setting["status"] != "ignored"
        ],
        "logs": node.get("logs", []),
    }


def _plan_view(fleet: dict[str, Any]) -> dict[str, Any]:
    """Read plan data from the fleet API while accepting legacy test/daemon payloads."""
    return fleet["plan"] if "plan" in fleet else fleet


@fleet_group.command("status")
@click.argument("query", required=False, default=None)
@click.pass_context
def status(ctx: click.Context, query: str | None) -> None:
    """Show daemon and fleet status, or detail for a single matched node."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "fleet"})
    fleet = _check(resp)
    data = _status_view(fleet)

    all_nodes = data.get("nodes", [])
    nodes = _filter_nodes(all_nodes, query)
    drifted = _filter_drift(data.get("drifted", []), query)
    bad_state = _filter_nodes(data.get("bad_state", []), query)
    ghosts = _filter_ghosts(data.get("ghosts", []), query)
    unseen_nodes = _filter_unseen_nodes(data.get("unseen_nodes", []), query)
    open_nodes = _filter_nodes(data.get("open_nodes", []), query)

    filtered = bool(query and len(nodes) != len(all_nodes))

    # Single match → detail view
    if query and len(nodes) == 1:
        pubkey = nodes[0]["pubkey"]
        detail_resp = _rpc(ctx.obj["socket"], {"cmd": "fleet_node", "query": pubkey})
        detail = _node_detail_view(_check(detail_resp))
        _render_node_detail(detail)
        return

    # Zero matches with a query
    if query and not nodes:
        click.echo(f"  no nodes matching '{query}'")
        return

    # Fleet table
    if nodes:
        def _short_ver(raw: str | None) -> str:
            if not raw:
                return "-"
            return raw.split("-")[0]  # "v1.15.0-dee3e26..." → "v1.15.0"

        state_w = max(len("STATE"), max((len(n["state"]) for n in nodes), default=0))
        group_w = max(len("GROUP"), max((len(n.get("group") or "-") for n in nodes), default=0))
        label_w = max(_wlen("LABEL"), max((_wlen(n.get("label") or "-") for n in nodes), default=0))
        name_w = max(_wlen("MESH NAME"), max((_wlen(n["name"]) for n in nodes), default=0))
        nbrs_w = max(len("NBRS"), max((len(n.get("neighbors") or "-") for n in nodes), default=0))
        ver_w = max(len("VER"), max((len(_short_ver(n.get("ver"))) for n in nodes), default=0))

        row = "  {:<12}  {}  {}  {:<{gw}}  {}  {:<{nw}}  {:<{vw}}  {}"
        click.echo(row.format(
            "PUBKEY", f"{'STATE':<{state_w}}", _wpad("LABEL", label_w),
            "GROUP", _wpad("MESH NAME", name_w),
            "NBRS", "VER", "LAST SEEN",
            gw=group_w, nw=nbrs_w, vw=ver_w,
        ))
        click.echo(row.format(
            "------", f"{'-----':<{state_w}}", _wpad("-----", label_w),
            "-----", _wpad("---------", name_w),
            "----", "---", "---------",
            gw=group_w, nw=nbrs_w, vw=ver_w,
        ))
        for n in nodes:
            last  = _ago(n.get("last_seen"))
            group = (n.get("group") or "-")
            nbrs  = n.get("neighbors") or "-"
            ver   = _short_ver(n.get("ver"))
            click.echo(row.format(
                _pk(n["pubkey"]),
                _state_cell(n.get("state"), state_w),
                _wpad(n.get("label") or "-", label_w),
                group,
                _wpad(n["name"], name_w),
                nbrs, ver,
                last,
                gw=group_w, nw=nbrs_w, vw=ver_w,
            ))
    else:
        click.echo("  no nodes in database")

    click.echo("")
    if filtered:
        click.echo(f"{len(nodes)} of {len(all_nodes)} nodes shown")
    else:
        click.echo(f"{len(all_nodes)} nodes")

    _render_issue_sections(drifted, bad_state, ghosts, unseen_nodes, open_nodes)


@node_group.command("path")
@click.argument("node")
@click.option(
    "--discover",
    is_flag=True,
    help="Ask the mesh for a fresh path-discovery candidate without applying it.",
)
@click.option(
    "--advert",
    is_flag=True,
    help="Show the gateway's cached advert path candidate without applying it.",
)
@click.option(
    "--apply",
    "apply_path",
    metavar="PATH[:MODE]",
    help="Apply an operator-approved replacement path to the gateway contact.",
)
@click.option("--yes", "-y", is_flag=True, help="Apply without an interactive confirmation.")
@click.pass_context
def path(
    ctx: click.Context,
    node: str,
    discover: bool,
    advert: bool,
    apply_path: str | None,
    yes: bool,
) -> None:
    """Inspect or explicitly repair the cached management path for NODE.

    NODE is a label or pubkey prefix. With no option, this prints the gateway's
    cached contact route only; it does not probe the mesh. A flood route is
    marked "path repair needed" because normal management will avoid full syncs
    over flood.

    Repair flow: run --discover or --advert to print a candidate, then apply an
    operator-approved value with --apply PATH[:MODE].
    """
    selected = sum(1 for value in (discover, advert, apply_path is not None) if value)
    if selected > 1:
        raise click.UsageError("choose only one of --discover, --advert, or --apply")

    if discover or advert:
        source = "advert" if advert else "discovery"
        resp = _rpc(
            ctx.obj["socket"],
            {"cmd": "path_discover", "query": node, "source": source},
            timeout=90,
        )
        data = _check(resp)
        _render_path_candidate(data)
        return

    if apply_path is not None:
        if not yes:
            click.confirm(
                f"Apply management path {apply_path!r} to {node!r} on the gateway contact?",
                abort=True,
            )
        resp = _rpc(
            ctx.obj["socket"],
            {"cmd": "path_apply", "query": node, "path": apply_path},
            timeout=90,
        )
        data = _check(resp)
        click.echo("updated cached management path")
        _render_path_status(data)
        return

    resp = _rpc(ctx.obj["socket"], {"cmd": "path", "query": node})
    data = _check(resp)
    _render_path_status(data)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

@fleet_group.command("sync")
@click.argument("node", required=False)
@click.pass_context
def sync(ctx: click.Context, node: str | None) -> None:
    """Trigger an immediate sync cycle, or sync a single node.

    NODE: pubkey prefix, label substring, or name substring. Omit to sync all nodes.
    """
    payload: dict[str, Any] = {"cmd": "sync"}
    if node:
        payload["prefix"] = node
        # inline single-node sync can take many per-operation timeouts (30s each)
        resp = _rpc(
            ctx.obj["socket"],
            payload,
            timeout=300,
            on_progress=lambda msg: click.echo(f"  {msg}"),
        )
    else:
        resp = _rpc(ctx.obj["socket"], payload)
    data = _check(resp)
    click.echo(data)


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

@fleet_group.command("logs")
@click.argument("pubkey", required=False)
@click.option("--limit", "-n", default=50, show_default=True, help="Max log lines")
@click.pass_context
def logs(ctx: click.Context, pubkey: str | None, limit: int) -> None:
    """Show recent per-node activity log (optionally filtered to one node by pubkey prefix)."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "logs", "pubkey": pubkey, "limit": limit})
    data = _check(resp)

    if not data:
        click.echo("no logs")
        return

    level_w = max(len(entry.get("level") or "") for entry in data)
    age_w = max((len(_ago(entry.get("timestamp")).lstrip()) for entry in data), default=0)
    pubkey_w = max((_wlen(_log_node_pubkey(entry)) for entry in data), default=0)
    label_w = max((_wlen(_log_node_label(entry)) for entry in data), default=0)

    for entry in reversed(data):
        ts = _local_ts(entry.get("timestamp"))
        age = _ago(entry.get("timestamp")).lstrip()
        level = entry.get("level") or ""
        pubkey = _log_node_pubkey(entry)
        label = _log_node_label(entry)
        if pubkey:
            node_label = f" [{_wpad(pubkey, pubkey_w)}] [{_wpad(label, label_w)}]"
        else:
            node_label = ""
        click.echo(f"{ts} ({age:>{age_w}}) [{level:<{level_w}}]{node_label} {entry['message']}")


# ---------------------------------------------------------------------------
# node detail and issue section renderers
# ---------------------------------------------------------------------------

def _render_node_detail(data: dict[str, Any]) -> None:
    """Render the full per-node detail view."""
    click.echo(f"label:       {data.get('label') or '-'}")
    click.echo(f"group:       {data.get('group') or '-'}")
    click.echo(f"mesh name:   {data['name']}")
    click.echo(f"state:       {_state_text(data.get('state'))}")
    click.echo(f"last seen:   {_ago_with_ts(data.get('last_seen'))}")
    click.echo(f"last conn:   {_ago_with_ts(data.get('last_connected'))}")
    click.echo(f"next sync:   {_next_sync_text(data.get('next_sync_at'))}")
    click.echo(f"pubkey:      {_pk(data.get('pubkey'))}")
    click.echo(f"pubkey full: {data['pubkey']}")
    path = data.get("management_path")
    if path:
        path_text = path.get("cached_path") or "unknown"
        if path.get("path_repair_needed"):
            path_text = f"{path_text} (path repair needed)"
        click.echo(f"known path:  {path_text}")
    click.echo(f"authed:      {'yes' if data.get('auth_cached') else 'no'}")
    click.echo(f"passwd:      {'yes' if data.get('passwd_known') else 'no'}")
    click.echo("")

    facts = data.get("facts", {})
    if facts:
        fact_times = data.get("facts_collected_at", {})
        # Match settings table behavior: pad to visual width, not char count.
        key_w = max(_wlen("KEY"), max((_wlen(k) for k in facts), default=0))
        value_w = max(_wlen("VALUE"), max((_wlen(_sv(v)) for v in facts.values()), default=0))
        observed_w = max(
            _wlen("OBSERVED"),
            max((_wlen(_ago_with_ts(ts)) for ts in fact_times.values() if ts), default=0),
        )
        click.echo("facts:")
        row = "  {}  {}  {}  {}"
        click.echo(row.format(
            _wpad("KEY", key_w),
            _wpad("VALUE", value_w),
            _wpad("OBSERVED", observed_w),
            "NOTE",
        ))
        click.echo(row.format(
            _wpad("---", key_w),
            _wpad("-----", value_w),
            _wpad("--------", observed_w),
            "----",
        ))
        for k, v in sorted(facts.items()):
            observed_at = fact_times.get(k)
            observed = _ago_with_ts(observed_at) if observed_at else "-"
            note = _clock_drift_text(v, observed_at) if k == "clock" else ""
            click.echo(row.format(
                _wpad(k, key_w),
                _wpad(_sv(v), value_w),
                _wpad(observed, observed_w),
                note,
            ).rstrip())
        click.echo("")

    settings = data.get("settings", [])
    if settings:
        def _tries(setting: dict[str, Any]) -> str:
            return (
                str(setting.get("update_attempts", 0))
                if setting["policy"] == "enforce"
                else "-"
            )

        click.echo("settings:")
        # column widths based on content display width, not char count
        col_pol = max(6, max(len(s["policy"]) for s in settings))
        col_key = max(3, max(_wlen(s["key"]) for s in settings))
        col_obs = max(8, max(_wlen(_sv(s.get("observed"))) for s in settings))
        col_des = max(7, max(_wlen(_sv(s.get("desired"))) for s in settings))
        col_tries = max(
            len("TRIES"), max(len(_tries(s)) for s in settings)
        )
        # DESIRED before OBSERVED, matching the fleet drift table (desired-vs-actual reading).
        # No SOURCE column: the DB is the only desired-state source now (sunset).
        def _srow(
            policy: str, key: str, desired: str, observed: str,
            tries: str, status: str,
        ) -> str:
            return (
                f"  {_wpad(policy, col_pol)}  {_wpad(key, col_key)}  {_wpad(desired, col_des)}"
                f"  {_wpad(observed, col_obs)}  {tries:>{col_tries}}  {status}"
            )
        click.echo(_srow("POLICY", "KEY", "DESIRED", "OBSERVED", "TRIES", "STATUS"))
        click.echo(_srow("------", "---", "-------", "--------", "-----", "------"))
        for s in settings:
            if s.get("observed") is None:
                status = click.style("not observed", fg="yellow")
            elif s["drift"]:
                status = click.style("drifted", fg="red")
            elif s.get("approximate"):
                status = click.style("nominal", fg="green")
            else:
                status = click.style("ok", fg="green")
            click.echo(_srow(
                s["policy"],
                s["key"],
                _sv(s.get("desired")),
                _sv(s.get("observed")),
                _tries(s),
                status,
            ))
        click.echo("")

    logs = data.get("logs", [])
    if logs:
        visible_logs = logs[:10]
        level_w = max(len(entry.get("level") or "") for entry in visible_logs)
        age_w = max(
            (len(_ago(entry.get("timestamp")).lstrip()) for entry in visible_logs),
            default=0,
        )
        click.echo("recent activity:")
        for entry in reversed(visible_logs):
            ts = _local_ts(entry.get("timestamp"))
            level = entry.get("level") or ""
            age = _ago(entry.get("timestamp")).lstrip()
            click.echo(f"  {ts} ({age:>{age_w}}) [{level:<{level_w}}] {entry['message']}")


def _render_path_status(data: dict[str, Any]) -> None:
    label = data.get("label") or "-"
    name = data.get("name") or "-"
    click.echo(f"{data['pubkey'][:12]}  {label}  {name}")
    click.echo(f"cached path: {data.get('cached_path') or 'unknown'}")

    route = data.get("route") or "unknown"
    if data.get("path_repair_needed"):
        click.echo(f"route: {route} (path repair needed)")
    else:
        click.echo(f"route: {route}")

    if not data.get("contact_present"):
        return

    click.echo("gateway contact:")
    contact_name = data.get("contact_name") or "-"
    contact_type = data.get("contact_type")
    contact_flags = data.get("contact_flags")
    click.echo(f"  name: {contact_name}")
    click.echo(f"  type: {contact_type if contact_type is not None else '-'}")
    click.echo(f"  flags: {contact_flags if contact_flags is not None else '-'}")

    path_len = data.get("out_path_len")
    hash_mode = data.get("out_path_hash_mode")
    click.echo(f"  out_path: {data.get('out_path') or '-'}")
    click.echo(f"  out_path_len: {path_len if path_len is not None else '-'}")
    click.echo(f"  out_path_hash_mode: {hash_mode if hash_mode is not None else '-'}")


def _render_path_candidate(data: dict[str, Any]) -> None:
    label = data.get("label") or "-"
    name = data.get("name") or "-"
    source = data.get("source") or "candidate"
    click.echo(f"{data['pubkey'][:12]}  {label}  {name}")
    click.echo(f"{source} path candidate:")
    click.echo(f"  path: {data.get('path') or '-'}")
    click.echo(f"  path_len: {data.get('path_len') if data.get('path_len') is not None else '-'}")
    click.echo(f"  path_hash_mode: {data.get('path_hash_mode')}")
    if data.get("apply_value"):
        click.echo(
            f"  apply with: mch node path {data['pubkey'][:12]} --apply {data['apply_value']}"
        )


def _render_discovery(data: dict[str, Any]) -> None:
    radio = data.get("radio", {})
    radio_text = radio.get("text") or "unknown"
    dwell = data.get("dwell")
    nodes = data.get("nodes", [])
    click.echo(f"discovery radio: {radio_text}")
    click.echo(f"dwell: {dwell:g}s" if isinstance(dwell, int | float) else f"dwell: {dwell}")

    if not nodes:
        click.echo("no nodes discovered")
        return

    type_w = max(len("TYPE"), max(len(str(node.get("node_type", "-"))) for node in nodes))
    type_w = max(type_w, max(len(_discovery_type(node.get("node_type"))) for node in nodes))
    snr_w = max(len("SNR"), max(len(_discovery_snr(node.get("SNR_in"))) for node in nodes))
    status_w = max(len("STATUS"), max(len(str(node.get("status") or "-")) for node in nodes))
    label_w = max(_wlen("LABEL"), max(_wlen(str(node.get("label") or "-")) for node in nodes))
    row = "  {:<12}  {:<64}  {:<{tw}}  {:>{sw}}  {:<{stw}}  {}"
    click.echo(row.format(
        "PREFIX", "PUBKEY", "TYPE", "SNR", "STATUS", _wpad("LABEL", label_w),
        tw=type_w, sw=snr_w, stw=status_w,
    ))
    click.echo(row.format(
        "------", "------", "----", "---", "------", _wpad("-----", label_w),
        tw=type_w, sw=snr_w, stw=status_w,
    ))
    for node in nodes:
        pubkey = str(node.get("pubkey") or "")
        click.echo(row.format(
            pubkey[:12] or "-",
            pubkey or "-",
            _discovery_type(node.get("node_type")),
            _discovery_snr(node.get("SNR_in")),
            str(node.get("status") or "-"),
            _wpad(str(node.get("label") or "-"), label_w),
            tw=type_w,
            sw=snr_w,
            stw=status_w,
        ))


def _discovery_type(value: object) -> str:
    names = {1: "cli", 2: "repeater", 3: "room"}
    try:
        return names.get(int(value), str(value))
    except (TypeError, ValueError):
        return "-"


def _discovery_snr(value: object) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "-"


def _render_adopt_result(data: dict[str, Any]) -> None:
    click.echo("adopted node")
    click.echo(f"  pubkey: {data.get('pubkey')}")
    click.echo(f"  label: {data.get('label') or '-'}")
    click.echo(f"  group: {data.get('group') or '-'}")
    radio = data.get("radio") or {}
    click.echo(f"  discovered radio: {radio.get('text') or '-'}")
    click.echo(f"  matched config: {'yes' if data.get('matched_config') else 'no'}")
    click.echo(f"  gateway contact: {'added' if data.get('contact_added') else 'already present'}")
    click.echo(f"  favourited: {'yes' if data.get('favourited') else 'already/no'}")
    route = data.get("route") or "unknown"
    if data.get("path_repair_needed"):
        click.echo(f"  route: {route} (path repair needed)")
    else:
        click.echo(f"  route: {route}")
    confirm = data.get("confirm") or {}
    if confirm.get("sent"):
        if confirm.get("ok"):
            click.echo(f"  confirm probe: ok - directed ver -> {confirm.get('response') or '-'}")
        else:
            click.echo(f"  confirm probe: FAILED - {confirm.get('response') or 'no response'}")
            click.echo("    (direct route did not carry a directed command on this radio)")
    click.echo("")
    click.echo(f"next: mch fleet status {(data.get('label') or data.get('pubkey') or '')[:12]}")


def _render_issue_sections(
    drifted: list[dict[str, Any]],
    bad_state: list[dict[str, Any]],
    ghosts: list[dict[str, Any]],
    unseen_nodes: list[dict[str, Any]],
    open_nodes: list[dict[str, Any]],
) -> None:
    """Render issue sections below the fleet table. No-ops if all lists are empty."""
    if not drifted and not bad_state and not ghosts and not unseen_nodes and not open_nodes:
        return

    click.echo("")

    if drifted:
        def _tries(setting: dict[str, Any]) -> str:
            return (
                str(setting.get("update_attempts", 0))
                if setting["policy"] == "enforce"
                else "-"
            )

        click.echo("drifted settings:")
        node_w = max(
            len("LABEL"),
            max((len(s.get("label") or "-") for s in drifted), default=0),
        )
        key_w = max(len("KEY"), max(len(s["key"]) for s in drifted))
        des_w = max(len("DESIRED"), max(_wlen(_sv(s.get("desired"))) for s in drifted))
        obs_w = max(len("OBSERVED"), max(_wlen(_sv(s.get("observed"))) for s in drifted))
        tries_w = max(len("TRIES"), max(len(_tries(s)) for s in drifted))
        def _drow(
            pubkey: str, node: str, key: str, desired: str, observed: str, tries: str, policy: str
        ) -> str:
            return (
                f"  {pubkey:<12}  {node:<{node_w}}  {key:<{key_w}}  {_wpad(desired, des_w)}"
                f"  {_wpad(observed, obs_w)}  {tries:>{tries_w}}  {policy}"
            )
        click.echo(_drow("PUBKEY", "LABEL", "KEY", "DESIRED", "OBSERVED", "TRIES", "POLICY"))
        click.echo(_drow("------", "-----", "---", "-------", "--------", "-----", "------"))
        for s in drifted:
            node = s.get("label") or "-"
            click.echo(_drow(_pk(s["node_pubkey"]), node, s["key"],
                             _sv(s.get("desired")), _sv(s.get("observed")),
                             _tries(s),
                             s["policy"]))
        click.echo("")

    if bad_state:
        click.echo("problem states:")
        name_w = max(
            _wlen("LABEL"),
            max((_wlen(n.get("label") or "-") for n in bad_state), default=0),
        )
        fmt = "  {:<12}  {}  {}  {:<16}"
        click.echo(fmt.format("PUBKEY", _wpad("LABEL", name_w), f"{'STATE':<12}", "LAST SEEN"))
        click.echo(fmt.format("------", _wpad("-----", name_w), f"{'-----':<12}", "---------"))
        for n in bad_state:
            label = n.get("label") or "-"
            last = _ago(n.get("last_seen"))
            click.echo(fmt.format(
                _pk(n.get("pubkey")),
                _wpad(label, name_w),
                _state_cell(n.get("state"), 12),
                last,
            ))
        click.echo("")

    if ghosts:
        click.echo("ghost nodes (in local DB but not config.yaml):")
        label_w = max(
            _wlen("LABEL"),
            max((_wlen(g.get("label") or "-") for g in ghosts), default=0),
        )
        name_w = max(
            _wlen("MESH NAME"),
            max((_wlen(g.get("name") or "-") for g in ghosts), default=0),
        )
        fmt = "  {:<12}  {}  {}  {:<12}"
        click.echo(fmt.format(
            "PUBKEY",
            _wpad("LABEL", label_w),
            _wpad("MESH NAME", name_w),
            "LAST SEEN",
        ))
        click.echo(fmt.format(
            "------",
            _wpad("-----", label_w),
            _wpad("---------", name_w),
            "---------",
        ))
        for g in ghosts:
            click.echo(fmt.format(
                _pk(g.get("pubkey")),
                _wpad(g.get("label") or "-", label_w),
                _wpad(g.get("name") or "-", name_w),
                _ago(g.get("last_seen")),
            ))
        click.echo("")

    if unseen_nodes:
        click.echo("unseen config nodes (in config.yaml but not known to gateway/DB):")
        label_w = max(
            _wlen("LABEL"),
            max((_wlen(n.get("label") or "-") for n in unseen_nodes), default=0),
        )
        fmt = "  {:<8}  {}"
        click.echo(fmt.format("PREFIX", _wpad("LABEL", label_w)))
        click.echo(fmt.format("------", _wpad("-----", label_w)))
        for n in unseen_nodes:
            click.echo(fmt.format(n["pubkey_prefix"][:8], _wpad(n.get("label") or "-", label_w)))
        click.echo("")

    if open_nodes:
        click.echo("open nodes (no ACL - anyone on the mesh can send commands):")
        name_w = max(
            _wlen("LABEL"),
            max((_wlen(n.get("label") or "-") for n in open_nodes), default=0),
        )
        fmt = "  {:<12}  {}"
        click.echo(fmt.format("PUBKEY", _wpad("LABEL", name_w)))
        click.echo(fmt.format("------", _wpad("-----", name_w)))
        for n in open_nodes:
            label = n.get("label") or "-"
            click.echo(fmt.format(_pk(n.get("pubkey")), _wpad(label, name_w)))


# ---------------------------------------------------------------------------
# contacts — gateway REP nodes not yet in config.yaml
# ---------------------------------------------------------------------------

def _filter_contacts(contacts: list[dict[str, Any]], query: str | None) -> list[dict[str, Any]]:
    """Filter contacts by pubkey prefix or name substring."""
    if not query:
        return contacts
    q = query.lower()
    return [
        c for c in contacts
        if c.get("public_key", "").startswith(query)
        or q in (c.get("adv_name") or "").lower()
    ]


def _filter_nodes(nodes: list[dict[str, Any]], query: str | None) -> list[dict[str, Any]]:
    """Filter nodes by pubkey prefix, label substring, or name substring."""
    if not query:
        return nodes
    q = query.lower()
    return [
        n for n in nodes
        if n["pubkey"].startswith(query)
        or q in (n.get("label") or "").lower()
        or q in n["name"].lower()
    ]


def _filter_drift(drifted: list[dict[str, Any]], query: str | None) -> list[dict[str, Any]]:
    """Filter drifted settings by pubkey prefix or label substring."""
    if not query:
        return drifted
    q = query.lower()
    return [
        s for s in drifted
        if s["node_pubkey"].startswith(query)
        or q in (s.get("label") or "").lower()
    ]


def _filter_plan_items(items: list[dict[str, Any]], query: str | None) -> list[dict[str, Any]]:
    """Filter plan items by pubkey prefix, label, mesh name, or setting key."""
    if not query:
        return items
    q = query.lower()
    return [
        item for item in items
        if (item.get("pubkey") or "").startswith(query)
        or q in (item.get("label") or "").lower()
        or q in (item.get("mesh_name") or "").lower()
        or q in (item.get("key") or "").lower()
    ]


def _filter_ghosts(ghosts: list[dict[str, Any]], query: str | None) -> list[dict[str, Any]]:
    """Filter DB-only ghost nodes by pubkey, label, or mesh name."""
    if not query:
        return ghosts
    q = query.lower()
    return [
        g for g in ghosts
        if (g.get("pubkey") or "").startswith(query)
        or q in (g.get("label") or "").lower()
        or q in (g.get("name") or "").lower()
    ]


def _filter_unseen_nodes(nodes: list[dict[str, Any]], query: str | None) -> list[dict[str, Any]]:
    """Filter config-only unseen nodes by pubkey_prefix or label substring."""
    if not query:
        return nodes
    q = query.lower()
    return [
        n for n in nodes
        if n["pubkey_prefix"].startswith(query)
        or q in (n.get("label") or "").lower()
    ]


@node_group.command("contacts")
@click.argument("query", required=False, default=None)
@click.pass_context
def contacts(ctx: click.Context, query: str | None) -> None:
    """Show gateway repeater contacts not yet in your config.yaml.

    Optionally filter by pubkey prefix or name substring:

      mch node contacts 4e       # pubkeys starting with 4e
      mch node contacts mill     # names containing "mill"

    Prints ready-to-paste YAML snippets. Review and append to the nodes:
    section of your config.yaml for any nodes you want mch to manage.
    """
    resp = _rpc(ctx.obj["socket"], {"cmd": "contacts"})
    data = _check(resp)
    data = _filter_contacts(data or [], query)

    if not data:
        if query:
            click.echo(f"no contacts matching '{query}' outside your config.yaml")
        else:
            click.echo("no repeater contacts found outside your config.yaml")
        return

    if query:
        click.echo(f"# Repeater contacts matching '{query}' not in your config.yaml.")
    else:
        click.echo("# Repeater contacts visible to the gateway but not in your config.yaml.")
    click.echo("# Review each one, then paste the snippets into your config files.\n")

    for c in data:
        pubkey = c.get("public_key", "")
        prefix = pubkey[:8]
        name = c.get("adv_name", "") or "unknown"

        # pubkey line is always 79 chars — use that as the fixed row width
        width = len("# full pubkey: ") + len(pubkey)  # always 79
        sep = "─" * max(0, width - len("# ── ") - len(name) - 1)

        click.echo("#" * width)
        click.echo(f"# ── {name} {sep}")
        click.echo(f"# full pubkey: {pubkey}\n")

        click.echo("# config.yaml — append to the 'nodes:' section:")
        click.echo(f"  - pubkey_prefix: {prefix}")
        click.echo(f'    label: "{name}"')
        click.echo("    group:          # assign to a group, or remove this line\n")

        click.echo("# secrets.yaml — append to the 'nodes:' section:")
        click.echo(f"  {prefix}: \"\"      # add password if required, or leave blank\n")


@node_group.command("discover")
@click.option(
    "--radio",
    required=True,
    metavar="FREQ,BW,SF,CR",
    help="Temporary gateway radio for active discovery, e.g. 915.800,250,10,5.",
)
@click.option(
    "--dwell",
    default=5.0,
    show_default=True,
    type=click.FloatRange(min=0.1, max=30.0),
    help="Seconds to collect discovery responses after sending the request.",
)
@click.option(
    "--unknown-only",
    is_flag=True,
    help="Hide fully managed nodes; keep config-only, DB-only, and unmanaged results.",
)
@click.pass_context
def discover(ctx: click.Context, radio: str, dwell: float, unknown_only: bool) -> None:
    """Actively discover MeshCore nodes on a temporary gateway radio.

    This sends a bounded discovery request, restores the gateway radio, and
    prints responses. It does not add nodes to the DB or gateway contacts.
    """
    error = validate_radio_tuple(radio)
    if error:
        raise click.UsageError(f"--radio {error}")
    parsed = parse_radio_tuple(radio)
    assert parsed is not None
    freq, bw, sf, cr = parsed
    resp = _rpc(
        ctx.obj["socket"],
        {"cmd": "discover", "radio": [float(freq), float(bw), int(sf), int(cr)], "dwell": dwell},
        timeout=dwell + 30,
        on_progress=lambda msg: click.echo(f"  {msg}"),
    )
    data = _check(resp)
    if unknown_only:
        data = dict(data)
        data["nodes"] = [
            node for node in data.get("nodes", [])
            if node.get("status") != "managed"
        ]
    _render_discovery(data)


@node_group.command("adopt")
@click.argument("pubkey")
@click.option(
    "--radio",
    required=True,
    metavar="FREQ,BW,SF,CR",
    help="Radio where this node was discovered, e.g. 915.800,250,11,5.",
)
@click.option("--label", default=None, help="Local label to store in the DB.")
@click.option("--yes", "-y", is_flag=True, help="Adopt without interactive confirmation.")
@click.pass_context
def adopt(ctx: click.Context, pubkey: str, radio: str, label: str | None, yes: bool) -> None:
    """Adopt an already-running discovered repeater into local inventory.

    This adds a DB node and a gateway contact, installs a direct (zero-hop)
    route on that contact, then sends one directed `ver` on the discovered
    radio to confirm the route carries a command. It does not edit config.yaml,
    secrets.yaml, or hardware settings.
    """
    error = validate_radio_tuple(radio)
    if error:
        raise click.UsageError(f"--radio {error}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", pubkey):
        raise click.UsageError("PUBKEY must be the full 64-character public key")
    parsed = parse_radio_tuple(radio)
    assert parsed is not None
    freq, bw, sf, cr = parsed
    clean_label = sanitize_node_label(label.strip()) if label else None
    if label and clean_label != label.strip():
        click.echo(f"local label must be config-safe; using {clean_label!r}")
    if not yes:
        click.echo("about to adopt discovered node")
        click.echo(f"  pubkey: {pubkey.lower()}")
        click.echo(f"  label:  {clean_label or '-'}")
        click.echo(f"  radio:  {radio}")
        click.echo("")
        click.echo("This adds local DB state, a gateway contact with a direct route,")
        click.echo("and sends one directed `ver` on the radio above to confirm it.")
        click.echo("It does not prove the physical device identity beyond the pubkey.")
        click.confirm("Adopt this node?", abort=True)
    resp = _rpc(
        ctx.obj["socket"],
        {
            "cmd": "adopt",
            "pubkey": pubkey.lower(),
            "radio": [float(freq), float(bw), int(sf), int(cr)],
            "label": clean_label,
        },
        on_progress=lambda msg: click.echo(f"  {msg}"),
        timeout=60,
    )
    data = _check(resp)
    _render_adopt_result(data)


def _render_probe_result(data: dict[str, Any]) -> None:
    label = data.get("label") or data.get("name") or (data.get("pubkey") or "")[:12]
    radio = data.get("radio") or {}
    click.echo("probe result")
    click.echo(f"  node:  {label} ({(data.get('pubkey') or '')[:12]})")
    click.echo(f"  radio: {radio.get('text') or '-'}")
    click.echo(f"  route: {data.get('route') or 'unknown'}")
    click.echo(f"  advert sent: {'yes' if data.get('advert_sent') else 'no'}")
    if data.get("login_attempted"):
        click.echo(f"  login: {'ok' if data.get('login_ok') else 'FAILED'}")
    if data.get("ok"):
        click.echo(f"  ver:   ok -> {data.get('response') or '-'}")
    else:
        click.echo(f"  ver:   FAILED - {data.get('response') or 'no response'}")


@node_group.command("probe")
@click.argument("node")
@click.option(
    "--radio",
    required=True,
    metavar="FREQ,BW,SF,CR",
    help="Radio to probe on, e.g. 915.800,250,11,5.",
)
@click.option(
    "--advert",
    is_flag=True,
    help="Send a gateway zero-hop advert on the radio before probing, so the node "
    "can learn a direct route back to the gateway.",
)
@click.option(
    "--login",
    is_flag=True,
    help="Log in with the node's configured password before probing. Auth-required "
    "nodes silently drop unauthenticated commands, so a bare `ver` times out.",
)
@click.pass_context
def probe(ctx: click.Context, node: str, radio: str, advert: bool, login: bool) -> None:
    """Send a directed `ver` to NODE on a given radio to test reachability.

    Temporarily switches the gateway radio, optionally sends a zero-hop advert
    and/or logs in first, sends one directed `ver`, restores the radio, and
    reports the result. Investigation only - it does not change the contact
    route, config, secrets, or the node.
    """
    error = validate_radio_tuple(radio)
    if error:
        raise click.UsageError(f"--radio {error}")
    parsed = parse_radio_tuple(radio)
    assert parsed is not None
    freq, bw, sf, cr = parsed
    resp = _rpc(
        ctx.obj["socket"],
        {
            "cmd": "probe",
            "query": node,
            "radio": [float(freq), float(bw), int(sf), int(cr)],
            "advert": advert,
            "login": login,
        },
        on_progress=lambda msg: click.echo(f"  {msg}"),
        timeout=60,
    )
    data = _check(resp)
    _render_probe_result(data)


@node_group.command("prune")
@click.option("--yes", "-y", is_flag=True, help="Delete the listed local DB state.")
@click.pass_context
def prune(ctx: click.Context, yes: bool) -> None:
    """List or hard-delete retired nodes (the purge step after `mch node retire`)."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "prune", "dry_run": not yes})
    data = _check(resp)
    candidates = data.get("candidates", [])

    if not candidates:
        click.echo("no retired nodes to prune")
        click.echo(
            "retire a node first with `mch node retire <node>`; secrets.yaml is left unchanged"
        )
        return

    label_w = max(
        _wlen("LABEL"),
        max((_wlen(n.get("label") or "-") for n in candidates), default=0),
    )
    name_w = max(
        _wlen("MESH NAME"),
        max((_wlen(n.get("name") or "-") for n in candidates), default=0),
    )
    row = "  {:<12}  {}  {}  {:<12}  {:<12}"
    click.echo("retired nodes (will be hard-deleted, history and all):")
    click.echo(row.format(
        "PUBKEY",
        _wpad("LABEL", label_w),
        _wpad("MESH NAME", name_w),
        "STATE",
        "LAST SEEN",
    ))
    click.echo(row.format(
        "------",
        _wpad("-----", label_w),
        _wpad("---------", name_w),
        "-----",
        "---------",
    ))
    for n in candidates:
        click.echo(row.format(
            _pk(n["pubkey"]),
            _wpad(n.get("label") or "-", label_w),
            _wpad(n.get("name") or "-", name_w),
            n.get("state") or "-",
            _ago(n.get("last_seen")),
        ))

    if yes:
        click.echo("")
        click.echo(f"deleted {data.get('deleted', 0)} retired node(s) from local DB state")
    else:
        click.echo("")
        click.echo("run 'mch node prune --yes' to delete these local DB rows")
    click.echo("secrets.yaml entries are left unchanged")


@node_group.group("clean")
def clean() -> None:
    """Clean reviewed stale local DB state."""


def _render_stale_table(
    data: dict[str, Any],
    *,
    noun_plural: str,
    noun_singular: str,
    key_header: str,
    yes: bool,
) -> None:
    """Render the candidate table shared by `clean facts` and `clean settings`.

    Both list orphaned, re-derivable observation rows (PUBKEY/LABEL/KEY/VALUE/OBSERVED)
    and behave identically apart from the noun wording and the KEY column header.
    """
    candidates = data.get("candidates", [])
    if not candidates:
        click.echo(f"no stale {noun_plural}")
        return

    label_w = max(
        _wlen("LABEL"),
        max((_wlen(item.get("label") or "-") for item in candidates), default=0),
    )
    key_w = max(
        len(key_header), max((len(item.get("key") or "") for item in candidates), default=0)
    )
    value_w = max(
        _wlen("VALUE"),
        max((_wlen(_sv(item.get("value"))) for item in candidates), default=0),
    )
    observed_w = max(
        _wlen("OBSERVED"),
        max((_wlen(_ago_with_ts(item.get("collected_at"))) for item in candidates), default=0),
    )
    row = "  {:<12}  {}  {:<{kw}}  {}  {}"
    click.echo(f"stale {noun_plural}:")
    click.echo(row.format(
        "PUBKEY",
        _wpad("LABEL", label_w),
        key_header,
        _wpad("VALUE", value_w),
        _wpad("OBSERVED", observed_w),
        kw=key_w,
    ))
    click.echo(row.format(
        "------",
        _wpad("-----", label_w),
        "-" * len(key_header),
        _wpad("-----", value_w),
        _wpad("--------", observed_w),
        kw=key_w,
    ))
    for item in candidates:
        click.echo(row.format(
            _pk(item.get("pubkey")),
            _wpad(item.get("label") or "-", label_w),
            item.get("key") or "",
            _wpad(_sv(item.get("value")), value_w),
            _wpad(_ago_with_ts(item.get("collected_at")), observed_w),
            kw=key_w,
        ))

    click.echo("")
    if yes:
        click.echo(f"deleted {data.get('deleted', 0)} stale {noun_singular}(s)")
    else:
        click.echo(f"run again with --yes to delete these stale {noun_singular} rows")


@clean.command("facts")
@click.argument("node", required=False)
@click.option("--group", "group_name", help="Clean stale facts for nodes in this group.")
@click.option("--all", "all_nodes", is_flag=True, help="Clean stale facts across the fleet.")
@click.option("--fact", "fact_key", help="Only consider one fact key, e.g. powersavings.")
@click.option("--yes", "-y", is_flag=True, help="Delete the listed stale facts.")
@click.pass_context
def clean_facts(
    ctx: click.Context,
    node: str | None,
    group_name: str | None,
    all_nodes: bool,
    fact_key: str | None,
    yes: bool,
) -> None:
    """List or delete cached facts no longer present in facts: config."""
    scopes = sum(1 for value in (node, group_name, all_nodes) if value)
    if scopes != 1:
        raise click.UsageError("choose exactly one scope: NODE, --group, or --all")

    resp = _rpc(
        ctx.obj["socket"],
        {
            "cmd": "clean_facts",
            "query": node,
            "group": group_name,
            "all": all_nodes,
            "fact": fact_key,
            "dry_run": not yes,
        },
    )
    data = _check(resp)
    _render_stale_table(
        data, noun_plural="facts", noun_singular="fact", key_header="FACT", yes=yes
    )


@clean.command("settings")
@click.argument("node", required=False)
@click.option("--group", "group_name", help="Clean orphaned settings for nodes in this group.")
@click.option("--all", "all_nodes", is_flag=True, help="Clean orphaned settings across the fleet.")
@click.option("--setting", "setting_key", help="Only consider one setting key, e.g. txdelay.")
@click.option("--yes", "-y", is_flag=True, help="Delete the listed orphaned settings.")
@click.pass_context
def clean_settings(
    ctx: click.Context,
    node: str | None,
    group_name: str | None,
    all_nodes: bool,
    setting_key: str | None,
    yes: bool,
) -> None:
    """List or delete observed settings whose key is no longer desired.

    Orphans appear when a key is unset, dropped from a group/global, or the node
    leaves config. Observed rows are a cache, so deleting them is safe - they are
    re-observed next sync if still wanted. Desired state (mch config set) is untouched.
    """
    scopes = sum(1 for value in (node, group_name, all_nodes) if value)
    if scopes != 1:
        raise click.UsageError("choose exactly one scope: NODE, --group, or --all")

    resp = _rpc(
        ctx.obj["socket"],
        {
            "cmd": "clean_settings",
            "query": node,
            "group": group_name,
            "all": all_nodes,
            "setting": setting_key,
            "dry_run": not yes,
        },
    )
    data = _check(resp)
    _render_stale_table(
        data, noun_plural="settings", noun_singular="setting", key_header="SETTING", yes=yes
    )


@node_group.command("remove")
@click.argument("node")
@click.option("--yes", "-y", is_flag=True, help="Delete without prompting.")
@click.option(
    "--force",
    is_flag=True,
    help="Allow deleting local DB state for a node that is still managed (not retired).",
)
@click.pass_context
def remove_node(ctx: click.Context, node: str, yes: bool, force: bool) -> None:
    """Delete one node's local DB state (prefer `mch node retire` to stop managing a node)."""
    resp = _rpc(
        ctx.obj["socket"],
        {"cmd": "remove", "query": node, "dry_run": True, "force": force},
    )
    data = _check(resp)

    label = data.get("label") or "-"
    name = data.get("name") or "-"
    click.echo("local DB node state:")
    click.echo(f"  pubkey:    {_pk(data.get('pubkey'))}")
    click.echo(f"  label:     {label}")
    click.echo(f"  mesh name: {name}")
    click.echo(f"  state:     {data.get('state') or '-'}")
    click.echo(f"  last seen: {_ago(data.get('last_seen'))}")
    if not data.get("retired"):
        click.echo("  note:      still managed (not retired) — forced delete; it may re-seed")
    click.echo("")
    click.echo("This deletes local facts, settings, operations, logs, and the DB node row.")
    click.echo("secrets.yaml is left unchanged.")

    if not yes and not click.confirm("Delete this local DB state?"):
        click.echo("not deleted")
        return

    delete_resp = _rpc(
        ctx.obj["socket"],
        {"cmd": "remove", "query": data["pubkey"], "dry_run": False, "force": force},
    )
    deleted = _check(delete_resp).get("deleted", 0)
    click.echo(f"deleted {deleted} node(s) from local DB state")


@node_group.command("retire")
@click.argument("node")
@click.pass_context
def retire_node(ctx: click.Context, node: str) -> None:
    """Stop managing a node: exclude it from sync and unfavourite it, keeping its history.

    Retire-don't-delete. Re-manage later with `mch node unretire`, or purge the history with
    `mch node prune`.
    """
    data = _check(_rpc(ctx.obj["socket"], {"cmd": "retire", "query": node}))
    ident = data.get("label") or data.get("name") or _pk(data.get("pubkey"))
    click.echo(f"retired {ident} ({_pk(data.get('pubkey'))}) — state now {data.get('state')}")
    click.echo("excluded from sync and unfavourited; history kept. `mch node unretire` to undo.")


@node_group.command("unretire")
@click.argument("node")
@click.pass_context
def unretire_node(ctx: click.Context, node: str) -> None:
    """Return a retired node to management: re-favourite it and schedule it for sync."""
    data = _check(_rpc(ctx.obj["socket"], {"cmd": "unretire", "query": node}))
    ident = data.get("label") or data.get("name") or _pk(data.get("pubkey"))
    click.echo(f"unretired {ident} ({_pk(data.get('pubkey'))}) — state now {data.get('state')}")
    click.echo("re-favourited and queued for sync.")


# ---------------------------------------------------------------------------
# suggest
# ---------------------------------------------------------------------------

@config_group.command("suggest")
@click.argument("query", required=False)
@click.pass_context
def suggest(ctx: click.Context, query: str | None) -> None:
    """Show suggested settings based on each node's neighbour count.

    QUERY: pubkey prefix, label substring, or name substring. Omit to show all nodes.

    Prints the matching profile (e.g. rooftop, tower), suggested txdelay
    and universal settings with one-line explanations, and a ready-to-paste
    config.yaml settings: block.

    Requires 'neighbors' in the facts: list in config.yaml and at least
    one completed sync cycle to populate the neighbour count.
    """
    _show_suggestions(ctx, query)


def _show_suggestions(ctx: click.Context, query: str | None) -> None:
    resp = _rpc(ctx.obj["socket"], {"cmd": "suggest"})
    all_data = _check(resp)
    data = _filter_nodes(all_data, query)

    if not all_data:
        click.echo("no nodes in database")
        return

    if not data:
        click.echo(f"no nodes matching '{query}'")
        return

    if len(data) == 1:
        _suggest_detail(data[0])
    else:
        _suggest_table(data)


def _suggest_table(data: list[dict[str, Any]]) -> None:
    label_w = max(_wlen("LABEL"), max((_wlen(n.get("label") or "-") for n in data), default=0))

    row = "  {:<12}  {}  {:<4}  {:<7}  {}"
    click.echo(row.format("PUBKEY", _wpad("LABEL", label_w), "NBRS", "TXDELAY", "DIRECT.TXDELAY"))
    click.echo(row.format("------", _wpad("-----", label_w), "----", "-------", "--------------"))
    for n in data:
        count = n["neighbour_count"]
        profile = n["profile"]
        click.echo(row.format(
            _pk(n.get("pubkey")),
            _wpad(n.get("label") or "-", label_w),
            str(count) if count is not None else "-",
            profile["txdelay"] if profile else "-",
            profile["direct_txdelay"] if profile else "-",
        ))


def _suggest_detail(n: dict[str, Any]) -> None:
    count = n["neighbour_count"]
    profile = n["profile"]

    click.echo(f"label:      {n.get('label') or '-'}")
    click.echo(f"mesh name:  {n['name']}")
    click.echo("")

    if count is None:
        click.echo(
            "(no neighbour data - add 'neighbors' to facts: in config.yaml"
            " and run mch fleet sync to start collecting it)"
        )
        click.echo("")
    else:
        click.echo(f"neighbours: {count}")
        if profile:
            click.echo(f"profile:    {profile['name']} - {profile['description']}")
        click.echo("")

    # Build the full list: profile settings (if known) first, then universal
    settings: list[tuple[str, str, str]] = []
    if profile:
        settings.append((
            "txdelay", profile["txdelay"], "Flood retransmit delay - high-reach nodes fire first",
        ))
        settings.append((
            "direct.txdelay",
            profile["direct_txdelay"],
            "Direct traffic delay (lower than flood delay)",
        ))

    for u in n.get("universal", []):
        settings.append((u["key"], u["value"], u["description"]))

    if not settings:
        return

    key_w = max(len(s[0]) for s in settings)
    val_w = max(len(s[1]) for s in settings)

    click.echo("Suggested settings:")
    for key, val, desc in settings:
        click.echo(f"  {key:<{key_w}}  {val:<{val_w}}  {desc}")

    click.echo("")
    click.echo("To apply, paste into this node's settings: block in config.yaml:")
    for key, val, _ in settings:
        click.echo(f"      {key}:")
        click.echo(f'        desired: "{val}"')
        click.echo("        policy: enforce")


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------

@node_group.command(name="exec")
@click.argument("node")
@click.argument("command", nargs=-1, required=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt for destructive commands")
@click.pass_context
def exec_cmd(ctx: click.Context, node: str, command: tuple[str, ...], yes: bool) -> None:
    """Run a raw MeshCore CLI command on a node.

    NODE: pubkey prefix, label substring, or name substring.
    COMMAND: MeshCore CLI command (e.g. ver, stats-radio, reboot).

    Destructive commands (reboot, clkreboot, etc.) require confirmation unless --yes is passed.
    Irreversible commands (erase, set prv.key, etc.) are blocked entirely.
    """
    cmd_str = " ".join(command)
    blocked = blocked_exec_command(cmd_str)

    if blocked:
        click.echo(
            f"error: '{blocked}' is blocked in mch node exec - use config operations instead",
            err=True,
        )
        raise SystemExit(1)

    # resolve node to get pubkey for confirmation display
    node_resp = _rpc(ctx.obj["socket"], {"cmd": "node", "pubkey": node})
    if not node_resp.get("ok"):
        click.echo(f"error: {node_resp.get('error')}", err=True)
        raise SystemExit(1)
    data = node_resp["data"]
    pubkey = data["pubkey"]
    label = data.get("label") or data["name"]

    if confirm_exec_command(cmd_str) and not yes:
        click.echo(f"  {cmd_str}  {label}  ({pubkey[:8]})")
        typed = click.prompt("type pubkey prefix to confirm").strip()
        if not pubkey.startswith(typed):
            click.echo("cancelled", err=True)
            raise SystemExit(1)

    resp = _rpc(
        ctx.obj["socket"], {"cmd": "exec", "pubkey": pubkey, "command": cmd_str}, timeout=90
    )
    data = _check(resp)
    click.echo(data)


# ---------------------------------------------------------------------------
# set / unset (DB-authored desired state)
# ---------------------------------------------------------------------------

def _scope_from_flags(group_name: str | None, global_scope: bool) -> str:
    """Validate the --group/--global flags and return the scope name."""
    if group_name and global_scope:
        raise click.UsageError("choose at most one of --group / --global")
    if global_scope:
        return "global"
    if group_name:
        return "group"
    return "node"


def _scoped_target_echo(verb: str, key: str, detail: str, data: dict[str, Any]) -> None:
    """Print the 'set/unset KEY ... on <target>' line for any scope."""
    scope = data.get("scope", "node")
    suffix = f" {detail}" if detail else ""
    if scope == "group":
        click.echo(f"{verb} {key}{suffix}  on group {data['group']!r}")
    elif scope == "global":
        click.echo(f"{verb} {key}{suffix}  on all managed nodes")
    else:
        label = data.get("label") or data["name"]
        click.echo(f"{verb} {key}{suffix}  on {label} ({data['pubkey'][:8]})")


@config_group.command("set")
@click.argument("args", nargs=-1, required=True)
@click.option("--group", "group_name", default=None,
              help="Set at group scope: applies to every node in the group.")
@click.option("--global", "global_scope", is_flag=True,
              help="Set at global scope: applies to every managed node.")
@click.option(
    "--policy",
    type=click.Choice(["enforce", "audit", "fact", "skip"]),
    default=None,
    help="Override this setting's policy (default: inherit). 'fact' collects a "
         "read-only observation with no desired value.",
)
@click.pass_context
def set_setting(
    ctx: click.Context,
    args: tuple[str, ...],
    group_name: str | None,
    global_scope: bool,
    policy: str | None,
) -> None:
    """Record a desired setting in the DB (the source of truth).

    \b
    Node scope:   mch config set NODE KEY [VALUE]
    Group scope:  mch config set --group NAME KEY [VALUE]
    Global scope: mch config set --global KEY [VALUE]

    NODE: pubkey prefix, label substring, or name substring.
    KEY:  a writable setting (e.g. name, txdelay), or a read-only fact key
          (e.g. ver, board, neighbors) when collecting it with --policy fact.
    VALUE: the desired value (omit for a policy-only override with --policy;
           --policy fact/skip take no value).
    Node wins over group wins over global at resolution time.

    Writing is synchronous, so `mch fleet status` shows the new intent immediately. Nodes
    converge on the next sync cycle, or run `mch fleet sync` to converge now.
    """
    scope = _scope_from_flags(group_name, global_scope)
    # With nargs the positionals shift: group/global drop the NODE arg, so KEY is first.
    if scope == "node":
        if not 2 <= len(args) <= 3:
            raise click.UsageError("usage: mch config set NODE KEY [VALUE]")
        node, key, value = args[0], args[1], (args[2] if len(args) == 3 else None)
    else:
        if not 1 <= len(args) <= 2:
            raise click.UsageError(
                "usage: mch config set --group NAME KEY [VALUE]  (or --global KEY [VALUE])"
            )
        node, key, value = None, args[0], (args[1] if len(args) == 2 else None)

    if value is None and policy is None:
        raise click.UsageError("provide a VALUE, --policy, or both")
    # A read-only fact key (e.g. ver) is not a writable setting but may be collected
    # with --policy fact/skip; let it through and let the daemon enforce the precise
    # rules (no value, only fact/skip). A bare unknown key is still a usage error here.
    if key not in settable_keys() | readable_observation_keys():
        valid = ", ".join(sorted(settable_keys()))
        raise click.UsageError(f"{key!r} is not a writable setting (choose from: {valid})")

    req: dict[str, Any] = {"cmd": "set_setting", "key": key, "value": value, "policy": policy}
    if scope == "group":
        req.update({"scope": "group", "group": group_name})
    elif scope == "global":
        req["scope"] = "global"
    else:
        req["query"] = node

    data = _check(_rpc(ctx.obj["socket"], req))
    parts = []
    if data.get("value") is not None:
        parts.append(f"= {data['value']!r}")
    if data.get("policy"):
        parts.append(f"policy={data['policy']}")
    detail = " ".join(parts) or "(no change)"
    _scoped_target_echo("set", key, detail, data)
    if data.get("scope", "node") == "node":
        click.echo(f"run: mch fleet sync {data['pubkey'][:8]}  to converge now")
    else:
        click.echo("run: mch fleet sync  to converge the fleet now")


@config_group.command("unset")
@click.argument("args", nargs=-1, required=True)
@click.option("--group", "group_name", default=None,
              help="Unset at group scope.")
@click.option("--global", "global_scope", is_flag=True,
              help="Unset at global scope.")
@click.pass_context
def unset_setting(
    ctx: click.Context,
    args: tuple[str, ...],
    group_name: str | None,
    global_scope: bool,
) -> None:
    """Remove a DB-set desired setting (leaves observed state alone).

    \b
    Node scope:   mch config unset NODE KEY
    Group scope:  mch config unset --group NAME KEY
    Global scope: mch config unset --global KEY
    """
    scope = _scope_from_flags(group_name, global_scope)
    if scope == "node":
        if len(args) != 2:
            raise click.UsageError("usage: mch config unset NODE KEY")
        node, key = args[0], args[1]
    else:
        if len(args) != 1:
            raise click.UsageError(
                "usage: mch config unset --group NAME KEY  (or --global KEY)"
            )
        node, key = None, args[0]

    req: dict[str, Any] = {"cmd": "unset_setting", "key": key}
    if scope == "group":
        req.update({"scope": "group", "group": group_name})
    elif scope == "global":
        req["scope"] = "global"
    else:
        req["query"] = node

    data = _check(_rpc(ctx.obj["socket"], req))
    _scoped_target_echo("unset", key, "", data)


@config_group.command("show")
@click.argument("node")
@click.argument("key")
@click.pass_context
def show_setting(ctx: click.Context, node: str, key: str) -> None:
    """Explain one KEY for one NODE: per-scope config, what resolves, what's observed.

    \b
    mch config show NODE KEY

    Shows the desired/policy row at each scope (global, the node's group, the node),
    the resolved policy and desired value (Node > Group > Global), and the value mch
    has observed. Read-only: it sends no mesh traffic.
    """
    data = _check(_rpc(ctx.obj["socket"], {"cmd": "show_setting", "query": node, "key": key}))
    target = data.get("label") or data.get("name") or data["pubkey"][:8]
    resolved = data["resolved"]

    click.echo(f"{key}  on  {target}  ({data['pubkey'][:12]})")
    if not data["configured"]:
        click.echo("  not configured for this node "
                   "(no desired/policy row resolves at any scope)")
    desired = resolved["desired"]
    desired_str = repr(desired) if desired is not None else "-"
    src = f"  (from {resolved['desired_source']})" if resolved["desired_source"] else ""
    click.echo(f"  resolved:  policy={resolved['policy']}  desired={desired_str}{src}")
    obs = resolved["observed"]
    if obs is None:
        click.echo("  observed:  (never read)")
    else:
        when = f"  [{resolved['observed_at']}]" if resolved["observed_at"] else ""
        click.echo(f"  observed:  {obs!r}{when}")
    if data["scopes"]:
        click.echo("  scopes:")
        for s in data["scopes"]:
            label = s["scope"] if s["scope"] != "group" else f"group {s['scope_id']!r}"
            sd = repr(s["desired"]) if s["desired"] is not None else "-"
            sp = s["policy"] or "(inherit)"
            click.echo(f"    {label:<16}  policy={sp:<9}  desired={sd}")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

@fleet_group.command("plan")
@click.argument("query", required=False, default=None)
@click.pass_context
def plan(ctx: click.Context, query: str | None) -> None:
    """Show desired-vs-known changes without sending mesh commands."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "fleet"})
    data = _plan_view(_check(resp))
    writes = _filter_plan_items(data.get("writes", []), query)
    audits = _filter_plan_items(data.get("audits", []), query)
    unknown = _filter_plan_items(data.get("unknown", []), query)

    if not writes and not audits and not unknown:
        click.echo(f"no planned changes matching '{query}'" if query else "no planned writes")
        return

    def _value(value: Any) -> str:
        if value is None or value == "":
            return "-"
        return str(value)

    def _label(item: dict[str, Any]) -> str:
        return item.get("label") or item.get("mesh_name") or "-"

    def _label_width(items: list[dict[str, Any]]) -> int:
        return max(_wlen("LABEL"), max((_wlen(_label(item)) for item in items), default=0))

    def _col_width(items: list[dict[str, Any]], header: str, field: str) -> int:
        return max(
            _wlen(header),
            max((_wlen(_value(item.get(field))) for item in items), default=0),
        )

    if writes:
        click.echo("planned writes (next sync would enforce):")
        node_w = _label_width(writes)
        key_w = _col_width(writes, "KEY", "key")
        known_w = _col_width(writes, "KNOWN", "known")
        desired_w = _col_width(writes, "DESIRED", "desired")

        def _wrow(pubkey: str, label: str, key: str, known: str, desired: str, command: str) -> str:
            return (
                f"  {pubkey:<12}  {_wpad(label, node_w)}  {_wpad(key, key_w)}  "
                f"{_wpad(known, known_w)}  {_wpad(desired, desired_w)}  {command}"
            )

        click.echo(_wrow("PUBKEY", "LABEL", "KEY", "KNOWN", "DESIRED", "COMMAND"))
        click.echo(_wrow("------", "-----", "---", "-----", "-------", "-------"))
        for item in writes:
            click.echo(_wrow(
                _pk(item.get("pubkey")),
                _label(item),
                item["key"],
                _value(item.get("known")),
                _value(item.get("desired")),
                item.get("command") or "-",
            ))
        click.echo("")

    if audits:
        click.echo("audit drift (no write):")
        node_w = _label_width(audits)
        key_w = _col_width(audits, "KEY", "key")
        known_w = _col_width(audits, "KNOWN", "known")
        desired_w = _col_width(audits, "DESIRED", "desired")

        def _arow(pubkey: str, label: str, key: str, known: str, desired: str) -> str:
            return (
                f"  {pubkey:<12}  {_wpad(label, node_w)}  {_wpad(key, key_w)}  "
                f"{_wpad(known, known_w)}  {_wpad(desired, desired_w)}"
            )

        click.echo(_arow("PUBKEY", "LABEL", "KEY", "KNOWN", "DESIRED"))
        click.echo(_arow("------", "-----", "---", "-----", "-------"))
        for item in audits:
            click.echo(_arow(
                _pk(item.get("pubkey")),
                _label(item),
                item["key"],
                _value(item.get("known")),
                _value(item.get("desired")),
            ))
        click.echo("")

    if unknown:
        click.echo("needs observation (no known value yet):")
        node_w = _label_width(unknown)
        key_w = _col_width(unknown, "KEY", "key")
        desired_w = _col_width(unknown, "DESIRED", "desired")

        def _urow(pubkey: str, label: str, key: str, desired: str, reason: str) -> str:
            return (
                f"  {pubkey:<12}  {_wpad(label, node_w)}  {_wpad(key, key_w)}  "
                f"{_wpad(desired, desired_w)}  {reason}"
            )

        click.echo(_urow("PUBKEY", "LABEL", "KEY", "DESIRED", "REASON"))
        click.echo(_urow("------", "-----", "---", "-------", "------"))
        for item in unknown:
            click.echo(_urow(
                _pk(item.get("pubkey")),
                _label(item),
                item.get("key") or "-",
                _value(item.get("desired")),
                item.get("reason") or "-",
            ))


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

@config_group.command("import")
@click.argument(
    "config_file",
    required=False,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--yes", "-y", is_flag=True, help="Write the imported rows to the DB.")
@click.option(
    "--retired",
    type=click.Choice(["keep", "skip"]),
    default=None,
    help="For a backup that contains retired nodes: keep (restore them as retired) or skip "
    "(omit them). Required to write when the backup has retired nodes.",
)
@click.pass_context
def import_config(
    ctx: click.Context, config_file: Path | None, yes: bool, retired: str | None
) -> None:
    """Seed the DB's desired state from a config.yaml (the migration on-ramp).

    Flattens config.yaml settings and directives (name_prefix/default_policy/via_radio) at
    node/group/global scope into desired_settings rows. Dry-run by default; --yes writes.
    Idempotent - re-running upserts the same rows. A node not yet seen by the gateway is
    skipped with a warning until it appears in the DB.

    Pass an explicit CONFIG_FILE (a legacy or `mch config export`-ed file) to import from it instead
    of the daemon's loaded config. This is the migration path once config.yaml no longer holds
    desired state — the daemon process must be able to read the file. A full `mch config export`
    backup also restores nodes the gateway has not seen yet; use --retired to choose what
    happens to any retired nodes it carries.
    """
    cmd: dict[str, Any] = {"cmd": "import", "dry_run": not yes}
    if config_file is not None:
        # Resolve here: the daemon runs from a different cwd, so it needs an absolute path.
        cmd["path"] = str(config_file.resolve())
    if retired is not None:
        cmd["retired"] = retired
    data = _check(_rpc(ctx.obj["socket"], cmd))
    counts = data["counts"]
    total = data["total"]
    operations = data.get("operations", 0)
    nodes_created = data.get("nodes_created", 0)
    retired_in_backup = data.get("retired_in_backup", 0)
    retired_mode = data.get("retired_mode")
    if yes:
        click.echo(
            f"imported {data['written']} desired item(s) into the DB "
            f"(global {counts['global']}, groups {counts['group']}, nodes {counts['node']}, "
            f"operations {operations})"
        )
        if nodes_created:
            click.echo(f"created {nodes_created} new node(s) from full pubkeys (restore)")
        if retired_in_backup:
            verb = "restored as retired" if retired_mode == "keep" else "skipped"
            click.echo(f"{retired_in_backup} retired node(s) in backup: {verb}")
    else:
        click.echo("import preview (dry run):")
        click.echo(f"  global:     {counts['global']}")
        click.echo(f"  groups:     {counts['group']}")
        click.echo(f"  nodes:      {counts['node']}")
        click.echo(f"  operations: {operations}")
        click.echo(f"  total:      {total}")
        if nodes_created:
            click.echo(f"  new nodes:  {nodes_created} (created from full pubkeys on restore)")
    for warning in data.get("warnings", []):
        click.echo(click.style(f"  warning: {warning}", fg="yellow"))
    if retired_in_backup and retired_mode is None:
        click.echo(click.style(
            f"  note: backup has {retired_in_backup} retired node(s) — add --retired keep "
            "to restore them as retired, or --retired skip to omit them",
            fg="yellow",
        ))
    if not yes and total:
        click.echo("run again with --yes to write these to the DB")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@config_group.command("export")
@click.pass_context
def export_config(ctx: click.Context) -> None:
    """Dump the DB's desired state as config.yaml-shaped YAML (the inverse of import).

    Prints to stdout - redirect to save (`mch config export > desired.yaml`). Round-trips with
    `mch config import`. Observed state and operations are not included.
    """
    data = _check(_rpc(ctx.obj["socket"], {"cmd": "export"}))
    if not data.get("total"):
        click.echo("no desired state in the DB to export")
        return
    click.echo(data["yaml"], nl=False)


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------

@node_group.command(name="new")
@click.pass_context
def new(ctx: click.Context) -> None:
    """Interactively add a fresh or replacement MeshCore node.

    The gateway will switch to the factory frequency (869.618 MHz) and search
    for unmanaged nodes. Make sure only one new or replacement node is powered
    on and within range before proceeding.
    """
    socket = ctx.obj["socket"]
    _run_commission_wizard(socket)


def _run_commission_wizard(socket: str) -> None:
    """Interactive wizard: scans for nodes, asks mode, collects params, sends RPC, prints result."""
    click.echo("")
    click.echo("Add a MeshCore Node")
    click.echo("===================")
    click.echo("")

    # Search first — no point asking questions if there's no node to commission.
    gateway_radio = None
    radio_options: list[dict[str, str]] = []
    group_options: list[str] = []
    while True:
        click.echo(
            "Searching for unmanaged nodes in range on default factory frequency (869.618 MHz)..."
        )
        resp = _rpc(socket, {"cmd": "commission", "mode": "scan"}, timeout=30)
        if not resp.get("ok"):
            error = resp.get("error", "unknown error")
            click.echo(f"\nSearch failed: {error}")
            hint = _commission_busy_hint(error)
            if hint:
                click.echo(hint)
            click.echo("")
            choice = click.prompt(
                "Press Enter to retry or q to cancel", default="", show_default=False
            ).strip().lower()
            if choice in ("q", "quit", "cancel"):
                return
            continue

        nodes = resp["data"]["nodes"]
        gateway_radio = resp["data"].get("gateway_radio")
        radio_options = resp["data"].get("radio_options", [])
        group_options = resp["data"].get("group_options", [])

        if len(nodes) == 0:
            click.echo("\nNo unmanaged nodes found. Is it powered on and in range with an antenna?")
            click.echo("")
            choice = click.prompt(
                "Press Enter to retry or q to cancel", default="", show_default=False
            ).strip().lower()
            if choice in ("q", "quit", "cancel"):
                return
            continue

        if len(nodes) > 1:
            click.echo(f"\nFound {len(nodes)} unmanaged nodes - commission one at a time:")
            for n in nodes:
                click.echo(f"  {n['pubkey'][:12]}")
            click.echo("")
            choice = click.prompt(
                "Power off all but one, then press Enter to retry or q to cancel",
                default="",
                show_default=False,
            ).strip().lower()
            if choice in ("q", "quit", "cancel"):
                return
            continue

        found_pubkey = nodes[0]["pubkey"][:12]
        click.echo(f"\nFound 1 unmanaged node: {found_pubkey}")
        break

    click.echo("")
    click.echo("  n. new node       brand new hardware, generate fresh identity")
    click.echo("  r. replace node   restore a known node's identity from secrets.yaml")
    click.echo("  q. cancel")
    click.echo("")

    while True:
        mode_raw = click.prompt("Mode", default="q").strip().lower()
        if mode_raw in ("n", "new"):
            _commission_new_wizard(socket, gateway_radio, radio_options, group_options)
            return
        if mode_raw in ("r", "replace"):
            _commission_replace_wizard(socket)
            return
        if mode_raw in ("q", "quit", "cancel", ""):
            click.echo("cancelled")
            return
        click.echo("  please enter n, r, or q")


def _prompt_radio_freq(default: str = "") -> str:
    """Prompt for a LoRa frequency in MHz; re-prompt until valid."""
    while True:
        val: str = click.prompt("Frequency (MHz)", default=default).strip()
        try:
            if 400.0 <= float(val) <= 928.0:
                return val
        except ValueError:
            pass
        click.echo("  enter a frequency between 400 and 928 MHz, e.g. 915.075")


def _prompt_radio_bw(default: str = "") -> str:
    """Prompt for LoRa bandwidth (kHz); re-prompt until valid."""
    while True:
        val: str = click.prompt("Bandwidth (kHz)", default=default).strip()
        try:
            if float(val) in (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0):
                return val
        except ValueError:
            pass
        click.echo(
            "  bandwidth must be one of: 7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125, 250, 500"
        )


def _prompt_radio_sf(default: str = "") -> str:
    """Prompt for spreading factor (7-12); re-prompt until valid."""
    while True:
        val: str = click.prompt("Spreading factor (7-12)", default=default).strip()
        try:
            if 7 <= int(val) <= 12:
                return val
        except ValueError:
            pass
        click.echo("  spreading factor must be an integer 7-12")


def _prompt_radio_cr(default: str = "") -> str:
    """Prompt for coding rate (5-8); re-prompt until valid."""
    while True:
        val: str = click.prompt("Coding rate (5-8)", default=default).strip()
        try:
            if 5 <= int(val) <= 8:
                return val
        except ValueError:
            pass
        click.echo("  coding rate must be an integer 5-8")


def _validate_radio_tuple(raw: str) -> tuple[str, str, str, str] | None:
    parsed = parse_radio_tuple(raw)
    if parsed is None:
        return None
    if validate_radio_tuple(raw) is not None:
        return None
    return parsed


def _profile_radio_options() -> list[tuple[str, str]]:
    try:
        presets = load_radio_presets()
    except RuntimeError:
        return []
    return [
        (preset.name, preset.value)
        for preset in presets
        if _validate_radio_tuple(preset.value)
    ]


def _prompt_radio_all(
    freq: str,
    bw: str,
    sf: str,
    cr: str,
    gateway_radio: str | None,
    config_radio_options: list[dict[str, str]],
) -> tuple[str, str, str, str] | None:
    """Prompt for radio settings with named choices plus paste/custom escape hatches."""
    current_radio = f"{freq},{bw},{sf},{cr}" if all((freq, bw, sf, cr)) else ""
    options: list[tuple[str, str, str]] = []
    selected_key: str | None = None

    def _add_option(label: str, value: str | None) -> None:
        if value is None:
            return
        is_new = all(existing != value for _, existing, _ in options)
        if is_new and _validate_radio_tuple(value):
            options.append((str(len(options) + 1), value, label))

    _add_option("gateway/home radio", gateway_radio)
    for option in config_radio_options:
        _add_option(option.get("label", "config radio"), option.get("value"))
    for name, value in _profile_radio_options():
        _add_option(name, value)
    _add_option("current value", current_radio)

    if options:
        if current_radio:
            for key, value, label in options:
                if value == current_radio:
                    options = [(key, value, label)] + [
                        option for option in options if option[1] != current_radio
                    ]
                    options = [
                        (str(idx), option_value, option_label)
                        for idx, (_, option_value, option_label) in enumerate(options, 1)
                    ]
                    selected_key = "1"
                    break
        click.echo("Radio settings:")
        for key, value, label in options:
            suffix = " (selected)" if key == selected_key else ""
            click.echo(f"  {key}. {value:<18} {label}{suffix}")
            if key == selected_key and len(options) > 1:
                click.echo("")
        click.echo("  p. paste freq,bw,sf,cr")
        click.echo("  c. custom fields")
        click.echo("  q. cancel")
        default_choice = selected_key or options[0][0]
        while True:
            choice = click.prompt("Radio", default=default_choice).strip().lower()
            if choice in ("q", "quit", "cancel", "exit"):
                return None
            for key, value, _ in options:
                if choice == key:
                    parsed = _validate_radio_tuple(value)
                    if parsed is not None:
                        return parsed
            if choice == "p":
                raw = click.prompt(
                    "Radio tuple (freq,bw,sf,cr)",
                    default="",
                    show_default=False,
                ).strip()
                if not raw:
                    continue
                if raw.lower() in ("q", "quit", "cancel", "exit"):
                    return None
                parsed = _validate_radio_tuple(raw)
                if parsed is not None:
                    return parsed
                click.echo("  enter radio as freq,bw,sf,cr, e.g. 915.075,125,9,5")
                continue
            if choice == "c":
                break
            pasted = _validate_radio_tuple(choice)
            if pasted is not None:
                return pasted
            click.echo("  choose a number, p to paste, c for custom fields, or q to cancel")

    click.echo("")
    freq = _prompt_radio_freq(freq)
    bw = _prompt_radio_bw(bw)
    sf = _prompt_radio_sf(sf)
    cr = _prompt_radio_cr(cr)
    return freq, bw, sf, cr


def _default_commission_label(pubkey: str | None) -> str:
    return pubkey[:12] if pubkey else "node01"


def _prompt_commission_group(
    current: str | None,
    group_options: list[str],
) -> str | None:
    """Prompt for a config group using numbered choices when groups are known."""
    if not group_options:
        while True:
            raw_group: str = click.prompt("Group label (none configured)", default="-").strip()
            if raw_group in ("", "-"):
                return None
            click.echo("  no groups are configured; enter - for none")

    default_group = current if current in group_options else group_options[0]
    choices = [default_group] + [name for name in group_options if name != default_group]
    default_choice = "1"

    click.echo("Groups:")
    for idx, name in enumerate(choices, 1):
        suffix = " (selected)" if name == default_group else ""
        click.echo(f"  {idx}. {name}{suffix}")
        if idx == 1 and len(choices) > 1:
            click.echo("")
    click.echo("  n. none")

    while True:
        choice: str = click.prompt("Group", default=default_choice).strip()
        lowered = choice.lower()
        if lowered in ("n", "none", "-"):
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        except ValueError:
            pass
        if choice in group_options:
            return choice
        click.echo("  choose a configured group number or n for none")


def _commission_new_wizard(
    socket: str,
    gateway_radio: str | None = None,
    radio_options: list[dict[str, str]] | None = None,
    group_options: list[str] | None = None,
) -> None:
    """Collect params for a new node, confirm, send RPC with retry, display result."""
    click.echo("")
    click.echo("New node")
    click.echo("--------")
    radio_options = radio_options or []
    group_options = group_options or []

    # Parse gateway_radio "freq,bw,sf,cr" into per-field defaults.
    gw_parts = (gateway_radio or "").split(",")
    def _gw(i: int) -> str:
        return gw_parts[i].strip() if len(gw_parts) > i else ""

    prv_key = generate_private_key()
    pubkey = derive_public_key(prv_key)

    # Saved values — used as defaults when the outer edit loop iterates.
    label = _default_commission_label(pubkey)
    if "default" in group_options:
        group: str | None = "default"
    elif group_options:
        group = group_options[0]
    else:
        group = None
    name = ""
    admin_password = "password"
    guest_password = "guest"
    freq = _gw(0)
    bw = _gw(1)
    sf = _gw(2)
    cr = _gw(3)

    while True:  # outer: re-enter values loop
        # --- identity ---
        requested_label = click.prompt("Node label ", default=label).strip()
        label = sanitize_node_label(requested_label)
        if label != requested_label:
            click.echo(f"  local label must be config-safe; using {label!r}")
        # Re-prompt name until within the MeshCore firmware byte cap.
        name_default = name or requested_label or label
        while True:
            name = click.prompt("Node name  ", default=name_default).strip()
            encoded = name.encode("utf-8")
            if len(encoded) <= MAX_NODE_NAME_BYTES:
                break
            truncated = encoded[:MAX_NODE_NAME_BYTES].decode("utf-8", errors="ignore")
            click.echo(
                f"  name too long (max {MAX_NODE_NAME_BYTES} bytes). Truncated: {truncated!r}"
            )
            name_default = truncated
        group = _prompt_commission_group(group, group_options)

        # --- security ---
        click.echo("")
        admin_password = click.prompt("Admin password", default=admin_password).strip()
        raw_guest = click.prompt("Guest password", default=guest_password or "-").strip()
        guest_password = "" if raw_guest == "-" else raw_guest

        # --- radio ---
        click.echo("")
        radio = _prompt_radio_all(
            freq, bw, sf, cr, gateway_radio, radio_options
        )
        if radio is None:
            click.echo("cancelled")
            return
        freq, bw, sf, cr = radio
        target_freq = f"{freq},{bw},{sf},{cr}"

        click.echo("")
        click.echo("  label:    " + label)
        click.echo("  name:     " + name)
        click.echo("  group:    " + (group or "-"))
        click.echo("  password: " + admin_password)
        click.echo("  guest pw: " + (guest_password or "-"))
        click.echo("  radio:    " + target_freq)
        click.echo("")

        while True:
            action = click.prompt(
                "Proceed? [y]es  [e]dit  [q]cancel", default="y"
            ).strip().lower()
            if action in ("", "y", "yes"):
                break
            if action in ("e", "edit"):
                break
            if action in ("q", "quit", "cancel", "n", "no"):
                break
            click.echo("  please enter y, e, or q")

        if action in ("q", "quit", "cancel", "n", "no"):
            click.echo("cancelled")
            return
        if action in ("e", "edit"):
            continue

        # Inner: commission retry loop.
        commission_ok = False
        while True:
            click.echo("Commissioning node...")
            click.echo("")
            resp = _rpc(socket, {
                "cmd": "commission",
                "mode": "new",
                "label": label,
                "group": group,
                "name": name,
                "target_freq": target_freq,
                "admin_password": admin_password,
                "guest_password": guest_password or None,
                "prv_key": prv_key,
            }, timeout=120, on_progress=lambda msg: click.echo(f"  {msg}"))

            if resp.get("ok"):
                commission_ok = True
                break

            click.echo(f"\nCan't complete commissioning: {resp.get('error', 'unknown error')}")
            hint = _commission_busy_hint(resp.get("error", ""))
            if hint:
                click.echo(hint)
            click.echo("Check the node is powered on, in range, and with an antenna.")
            choice = click.prompt(
                "[Enter] retry  [e]dit  [q]cancel", default=""
            ).strip().lower()
            if choice in ("q", "quit", "cancel"):
                click.echo("cancelled")
                return
            if choice == "e":
                break  # back to outer loop (re-enter values)
            click.echo("Retrying...")

        if commission_ok:
            break  # exit outer loop

    d = resp["data"]
    pubkey_prefix = d.get("pubkey_prefix", "?")

    click.echo("")
    click.echo("Node commissioned successfully.")
    click.echo("")
    click.echo("Save this to a file (e.g. new-node.yaml) and apply it with `mch config import`:")
    click.echo("")
    click.echo(d.get("import_yaml", ""))
    click.echo("")
    click.echo("Add to secrets.yaml under nodes:")
    click.echo("")
    click.echo(d.get("secrets_snippet", ""))
    click.echo("")
    click.echo("Next steps:")
    click.echo("  1. Add the secrets entry to secrets.yaml")
    click.echo("  2. Reload mchd so it seeds the new node from gateway favourites")
    click.echo("  3. Run: mch config import new-node.yaml   (then again with --yes to write)")
    click.echo(f"  4. Run: mch fleet status {pubkey_prefix}")
    click.echo(f"  5. Optional: mch fleet sync {pubkey_prefix}")


def _commission_replace_wizard(socket: str) -> None:
    """List replaceable nodes, ask operator to pick one, confirm, send RPC with retry."""
    resp = _rpc(socket, {"cmd": "commission", "mode": "list_replace"}, timeout=10)
    if not resp.get("ok"):
        click.echo(f"error: {resp.get('error', 'unknown error')}", err=True)
        raise SystemExit(1)

    candidates = resp["data"].get("replaceable", [])
    if not candidates:
        click.echo("error: no nodes have a prv_key in secrets.yaml - nothing to replace", err=True)
        raise SystemExit(1)

    click.echo("")
    click.echo("Nodes eligible for replacement (have prv_key in secrets.yaml):")
    click.echo("")
    for i, c in enumerate(candidates, 1):
        click.echo(f"  {i}. {c['pubkey_prefix']}  {c.get('label') or '(no label)'}")
    click.echo("")

    while True:
        try:
            idx = int(click.prompt(f"Select node [1-{len(candidates)}]")) - 1
            if 0 <= idx < len(candidates):
                break
            click.echo(f"  please enter a number between 1 and {len(candidates)}")
        except (ValueError, click.Abort):
            click.echo("cancelled")
            return

    chosen = candidates[idx]
    prefix = chosen["pubkey_prefix"]
    label = chosen.get("label") or prefix

    click.echo("")
    click.echo(f"About to replace hardware for '{label}' ({prefix}).")
    click.echo("The new hardware should be in factory state (fresh flash, default frequency).")
    click.echo("")

    if not click.confirm("Is the replacement hardware ready?", default=False):
        click.echo("cancelled")
        return

    click.echo("")

    while True:  # commission retry loop
        click.echo("Commissioning node...")
        resp = _rpc(socket, {
            "cmd": "commission",
            "mode": "replace",
            "pubkey_prefix": prefix,
        }, timeout=120, on_progress=lambda msg: click.echo(f"  {msg}"))

        if resp.get("ok"):
            break

        click.echo(f"\nCan't complete commissioning: {resp.get('error', 'unknown error')}")
        click.echo("Check the node is powered on, in range, and with an antenna.")
        choice = click.prompt(
            "[Enter] retry  [q]cancel", default="", show_default=False
        ).strip().lower()
        if choice in ("q", "quit", "cancel"):
            click.echo("cancelled")
            return
        click.echo("Retrying...")

    click.echo("")
    click.echo(
        f"Replacement complete. '{label}' ({prefix}) is rebooting with its restored identity."
    )
    click.echo(f"Once it comes back up, run: mch fleet sync {prefix}")


# ---------------------------------------------------------------------------
# start / stop (via systemd)
# ---------------------------------------------------------------------------

@daemon_group.command("start")
def start() -> None:
    """Start the mchd daemon via systemd."""
    result = _systemctl("start")
    if result.returncode != 0:
        click.echo(result.stderr.strip() or "systemctl start failed", err=True)
        raise SystemExit(result.returncode)
    click.echo("mchd started")


@daemon_group.command("stop")
def stop() -> None:
    """Stop the mchd daemon via systemd."""
    click.echo("stopping current sync, this may take some time...")
    result = _systemctl("stop")
    if result.returncode != 0:
        click.echo(result.stderr.strip() or "systemctl stop failed", err=True)
        raise SystemExit(result.returncode)
    click.echo("mchd stopped")


@daemon_group.command("restart")
def restart() -> None:
    """Restart the mchd daemon via systemd."""
    click.echo("stopping current sync, this may take some time...")
    result = _systemctl("restart")
    if result.returncode != 0:
        click.echo(result.stderr.strip() or "systemctl restart failed", err=True)
        raise SystemExit(result.returncode)
    click.echo("mchd restarted")


@daemon_group.command("admin", context_settings={"ignore_unknown_options": True})
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_CONFIG,
    show_default=True,
    help="Config file to read gateway.device and gateway.baud from.",
)
@click.option(
    "--cli",
    "executable",
    default="meshcore-cli",
    show_default=True,
    help="meshcore-cli executable to launch.",
)
@click.option(
    "--restart/--no-restart",
    default=True,
    show_default=True,
    help="Restart mchd after meshcore-cli exits.",
)
@click.argument("meshcore_args", nargs=-1, type=click.UNPROCESSED)
def admin(
    config_path: Path,
    executable: str,
    restart: bool,
    meshcore_args: tuple[str, ...],
) -> None:
    """Stop mchd, run meshcore-cli on the configured gateway, then restart mchd."""
    _run_admin_session(
        config_path=config_path,
        executable=executable,
        restart=restart,
        extra_args=meshcore_args,
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cli()


if __name__ == "__main__":
    main()
