"""
mch — control CLI for the mchd daemon.

Talks to the daemon over its Unix socket.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from mch.command_safety import blocked_exec_command, confirm_exec_command
from mch.config import load as load_config
from mch.config import secrets_identity_warnings
from mch.meshkey import derive_public_key, generate_private_key
from mch.recommend import load_radio_presets

DEFAULT_SOCKET = "/run/meshherder/mchd.sock"
DEFAULT_CONFIG = Path("/etc/meshherder/config.yaml")


def _wlen(s: str) -> int:
    """Visual width of a string — wide chars (emoji, CJK) count as 2 columns."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _wpad(s: str, width: int) -> str:
    """Left-justify s in a field of visual width."""
    return s + " " * max(0, width - _wlen(s))


def _pk(pubkey: str | None, length: int = 12) -> str:
    """Short pubkey prefix for tables."""
    return (pubkey or "-")[:length]


def _ago(iso: str | None) -> str:
    """Format an ISO datetime string as a compact relative time ('  5m ago', '  2h ago')."""
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
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


def _ago_with_ts(iso: str | None) -> str:
    """Format as 'YYYY-MM-DD HH:MM:SS (  Xm ago)' for detail views."""
    if not iso:
        return "never"
    ts = iso[:19].replace("T", " ")
    return f"{ts} ({_ago(iso)})"


async def _request(
    socket_path: str,
    cmd: dict[str, Any],
    timeout: float = 30,
    on_progress: Any = None,
) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=2**22)  # 4MB — contacts response can exceed the 64KB default
    except (FileNotFoundError, ConnectionRefusedError):
        click.echo(f"error: cannot connect to daemon at {socket_path}", err=True)
        click.echo("  is mchd running?  try: mchd --help", err=True)
        raise SystemExit(1) from None

    writer.write((json.dumps(cmd) + "\n").encode())
    await writer.drain()
    try:
        while True:
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
            data: dict[str, Any] = json.loads(raw)
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


# ---------------------------------------------------------------------------
# group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name="meshherder")
@click.option(
    "--socket", "-s",
    default=DEFAULT_SOCKET,
    envvar="MCHD_SOCKET",
    help="Path to daemon Unix socket",
    show_default=True,
)
@click.pass_context
def cli(ctx: click.Context, socket: str) -> None:
    """mch — MeshCore node management CLI"""
    ctx.ensure_object(dict)
    ctx.obj["socket"] = socket


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

@cli.command("validate")
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
        click.echo(
            f"warning: cannot read secrets file: {exc.filename or cfg.secrets_path or exc}",
            err=True,
        )
        click.echo("  try: sudo mch validate", err=True)
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
    for warning in secrets_identity_warnings(cfg):
        click.echo(f"warning: {warning}", err=True)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query", required=False, default=None)
@click.pass_context
def status(ctx: click.Context, query: str | None) -> None:
    """Show daemon and fleet status, or detail for a single matched node."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "status"})
    data = _check(resp)

    all_nodes = data.get("nodes", [])
    nodes = _filter_nodes(all_nodes, query)
    drifted = _filter_drift(data.get("drifted", []), query)
    bad_state = _filter_nodes(data.get("bad_state", []), query)
    ghosts = _filter_ghosts(data.get("ghosts", []), query)
    open_nodes = _filter_nodes(data.get("open_nodes", []), query)

    filtered = bool(query and len(nodes) != len(all_nodes))

    # Single match → detail view
    if query and len(nodes) == 1:
        pubkey = nodes[0]["pubkey"]
        detail_resp = _rpc(ctx.obj["socket"], {"cmd": "node", "pubkey": pubkey})
        detail = _check(detail_resp)
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

        row = "  {:<12}  {:<{sw}}  {}  {:<{gw}}  {}  {:<{nw}}  {:<{vw}}  {:<16}"
        click.echo(row.format(
            "PUBKEY", "STATE", _wpad("LABEL", label_w), "GROUP", _wpad("MESH NAME", name_w),
            "NBRS", "VER", "LAST SEEN",
            sw=state_w, gw=group_w, nw=nbrs_w, vw=ver_w,
        ))
        click.echo(row.format(
            "------", "-----", _wpad("-----", label_w), "-----", _wpad("---------", name_w),
            "----", "---", "---------",
            sw=state_w, gw=group_w, nw=nbrs_w, vw=ver_w,
        ))
        for n in nodes:
            last  = _ago(n.get("last_seen"))
            group = (n.get("group") or "-")
            nbrs  = n.get("neighbors") or "-"
            ver   = _short_ver(n.get("ver"))
            click.echo(row.format(
                _pk(n["pubkey"]),
                n["state"],
                _wpad(n.get("label") or "-", label_w),
                group,
                _wpad(n["name"], name_w),
                nbrs, ver,
                last,
                sw=state_w, gw=group_w, nw=nbrs_w, vw=ver_w,
            ))
    else:
        click.echo("  no nodes in database")

    click.echo("")
    if filtered:
        click.echo(f"{len(nodes)} of {len(all_nodes)} nodes shown")
    else:
        click.echo(f"{len(all_nodes)} nodes")

    _render_issue_sections(drifted, bad_state, ghosts, open_nodes)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("node", required=False)
@click.pass_context
def sync(ctx: click.Context, node: str | None) -> None:
    """Trigger an immediate sync cycle, or sync a single node by pubkey prefix.

    NODE: even-length pubkey prefix (2, 4, 6, 8 … chars). Omit to sync all nodes.
    """
    payload: dict[str, Any] = {"cmd": "sync"}
    if node:
        payload["prefix"] = node
        # inline single-node sync can take many per-operation timeouts (30s each)
        resp = _rpc(ctx.obj["socket"], payload, timeout=300)
    else:
        resp = _rpc(ctx.obj["socket"], payload)
    data = _check(resp)
    click.echo(data)


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

@cli.command()
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

    for entry in reversed(data):
        ts = (entry.get("timestamp") or "")[:19].replace("T", " ")
        node_label = f" [{entry['node_pubkey'][:12]}]" if entry.get("node_pubkey") else ""
        click.echo(f"{ts} [{entry['level']:<5}]{node_label} {entry['message']}")


# ---------------------------------------------------------------------------
# node detail and issue section renderers
# ---------------------------------------------------------------------------

def _render_node_detail(data: dict[str, Any]) -> None:
    """Render the full per-node detail view."""
    click.echo(f"label:       {data.get('label') or '-'}")
    click.echo(f"group:       {data.get('group') or '-'}")
    click.echo(f"mesh name:   {data['name']}")
    click.echo(f"state:       {data['state']}")
    click.echo(f"last seen:   {_ago_with_ts(data.get('last_seen'))}")
    click.echo(f"last conn:   {_ago_with_ts(data.get('last_connected'))}")
    click.echo(f"pubkey:      {data['pubkey']}")
    click.echo(f"authed:      {'yes' if data.get('auth_cached') else 'no'}")
    click.echo(f"passwd:      {'yes' if data.get('passwd_known') else 'no'}")

    facts = data.get("facts", {})
    if facts:
        key_w = max(len(k) for k in facts) + 1  # +1 for the colon
        click.echo("facts:")
        for k, v in sorted(facts.items()):
            click.echo(f"  {(k + ':'):<{key_w}}  {v}")
        click.echo("")

    settings = data.get("settings", [])
    if settings:
        any_drift = any(s["drift"] for s in settings)
        legend = "  (~ = drifted from desired)" if any_drift else ""
        click.echo(f"settings:{legend}")
        # column widths based on content display width, not char count
        col_key = max(3, max(_wlen(s["key"]) for s in settings))
        col_des = max(7, max(_wlen(s.get("desired") or "-") for s in settings))
        col_obs = max(8, max(_wlen(s.get("observed") or "-") for s in settings))
        def _srow(flag: str, key: str, desired: str, observed: str, policy: str) -> str:
            return f"  {_wpad(flag, 2)}  {_wpad(key, col_key)}  {_wpad(desired, col_des)}  {_wpad(observed, col_obs)}  {policy}"
        drift_hdr = "~" if any_drift else " "
        click.echo(_srow(drift_hdr, "KEY", "DESIRED", "OBSERVED", "POLICY"))
        click.echo(_srow("-", "---", "-------", "--------", "------"))
        for s in settings:
            drift_flag = "~" if s["drift"] else " "
            click.echo(_srow(
                drift_flag,
                s["key"],
                s.get("desired") or "-",
                s.get("observed") or "-",
                s["policy"],
            ))
        click.echo("")

    logs = data.get("logs", [])
    if logs:
        click.echo("recent activity:")
        for entry in reversed(logs[:10]):
            ts = (entry.get("timestamp") or "")[:19].replace("T", " ")
            level = entry["level"]
            click.echo(f"  [{level:<5}] {ts}  {entry['message']}")


def _render_issue_sections(
    drifted: list[dict[str, Any]],
    bad_state: list[dict[str, Any]],
    ghosts: list[dict[str, Any]],
    open_nodes: list[dict[str, Any]],
) -> None:
    """Render issue sections below the fleet table. No-ops if all lists are empty."""
    if not drifted and not bad_state and not ghosts and not open_nodes:
        return

    click.echo("")

    if drifted:
        click.echo("drifted settings:")
        node_w = max(
            len("NODE"),
            max((len(s.get("label") or s["node_pubkey"][:16]) for s in drifted), default=0),
        )
        fmt = "  {:<12}  {:<{nw}}  {:<22}  {:<14}  {:<14}  {}"
        click.echo(fmt.format("PUBKEY", "NODE", "KEY", "DESIRED", "OBSERVED", "POLICY", nw=node_w))
        click.echo(fmt.format("------", "----", "---", "-------", "--------", "------", nw=node_w))
        for s in drifted:
            node = s.get("label") or s["node_pubkey"][:16]
            click.echo(fmt.format(_pk(s["node_pubkey"]), node, s["key"][:22],
                                  (s.get("desired") or "-")[:14],
                                  (s.get("observed") or "-")[:14],
                                  s["policy"], nw=node_w))
        click.echo("")

    if bad_state:
        click.echo("problem states:")
        name_w = max(
            _wlen("NODE"),
            max((_wlen(n.get("label") or n["name"]) for n in bad_state), default=0),
        )
        fmt = "  {:<12}  {}  {:<12}  {:<16}"
        click.echo(fmt.format("PUBKEY", _wpad("NODE", name_w), "STATE", "LAST SEEN"))
        click.echo(fmt.format("------", _wpad("----", name_w), "-----", "---------"))
        for n in bad_state:
            label = n.get("label") or n["name"]
            last = _ago(n.get("last_seen"))
            click.echo(fmt.format(_pk(n.get("pubkey")), _wpad(label, name_w), n["state"], last))
        click.echo("")

    if ghosts:
        click.echo("config ghosts (in config.yaml but not visible to gateway):")
        label_w = max(len("LABEL"), max((len(g.get("label") or "-") for g in ghosts), default=0))
        fmt = "  {:<8}  {}"
        click.echo(fmt.format("PREFIX", _wpad("LABEL", label_w)))
        click.echo(fmt.format("------", _wpad("-----", label_w)))
        for g in ghosts:
            click.echo(fmt.format(g["pubkey_prefix"][:8], g.get("label") or "-"))
        click.echo("")

    if open_nodes:
        click.echo("open nodes (no ACL - anyone on the mesh can send commands):")
        name_w = max(
            _wlen("NODE"),
            max((_wlen(n.get("label") or n["name"]) for n in open_nodes), default=0),
        )
        fmt = "  {:<12}  {}"
        click.echo(fmt.format("PUBKEY", _wpad("NODE", name_w)))
        click.echo(fmt.format("------", _wpad("----", name_w)))
        for n in open_nodes:
            label = n.get("label") or n["name"]
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
    """Filter ghost config entries by pubkey_prefix or label substring."""
    if not query:
        return ghosts
    q = query.lower()
    return [
        g for g in ghosts
        if g["pubkey_prefix"].startswith(query)
        or q in (g.get("label") or "").lower()
    ]


@cli.command()
@click.argument("query", required=False, default=None)
@click.pass_context
def contacts(ctx: click.Context, query: str | None) -> None:
    """Show gateway repeater contacts not yet in your config.yaml.

    Optionally filter by pubkey prefix or name substring:

      mch contacts 4e       # pubkeys starting with 4e
      mch contacts mill     # names containing "mill"

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


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Delete the listed local DB state.")
@click.pass_context
def prune(ctx: click.Context, yes: bool) -> None:
    """List or delete DB nodes that are no longer in config.yaml."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "prune", "dry_run": not yes})
    data = _check(resp)
    candidates = data.get("candidates", [])

    if not candidates:
        click.echo("no DB nodes outside config.yaml")
        click.echo("secrets.yaml entries and gateway favourites are left unchanged")
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
    click.echo("DB nodes outside config.yaml:")
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
        click.echo(f"deleted {data.get('deleted', 0)} node(s) from local DB state")
    else:
        click.echo("")
        click.echo("run 'mch prune --yes' to delete these local DB rows")
    click.echo("secrets.yaml entries and gateway favourites are left unchanged")


@cli.command("remove")
@click.argument("node")
@click.option("--yes", "-y", is_flag=True, help="Delete without prompting.")
@click.option(
    "--force",
    is_flag=True,
    help="Allow deleting local DB state for a node still present in config.yaml.",
)
@click.pass_context
def remove_node(ctx: click.Context, node: str, yes: bool, force: bool) -> None:
    """Delete one node's local DB state."""
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
    if data.get("configured"):
        click.echo("  config:    still present in config.yaml (--force used)")
    click.echo("")
    click.echo("This deletes local facts, settings, operations, logs, and the DB node row.")
    click.echo("config.yaml, secrets.yaml, and gateway favourites are left unchanged.")

    if not yes and not click.confirm("Delete this local DB state?"):
        click.echo("not deleted")
        return

    delete_resp = _rpc(
        ctx.obj["socket"],
        {"cmd": "remove", "query": data["pubkey"], "dry_run": False, "force": force},
    )
    deleted = _check(delete_resp).get("deleted", 0)
    click.echo(f"deleted {deleted} node(s) from local DB state")


# ---------------------------------------------------------------------------
# suggest
# ---------------------------------------------------------------------------

@cli.command()
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
            " and run mch sync to start collecting it)"
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

@cli.command(name="exec")
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
            f"error: '{blocked}' is blocked in mch exec - use config operations instead",
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
# plan
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query", required=False, default=None)
@click.pass_context
def plan(ctx: click.Context, query: str | None) -> None:
    """Show desired-vs-known changes without sending mesh commands."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "plan"})
    data = _check(resp)
    writes = _filter_plan_items(data.get("writes", []), query)
    audits = _filter_plan_items(data.get("audits", []), query)
    unknown = _filter_plan_items(data.get("unknown", []), query)

    if not writes and not audits and not unknown:
        click.echo(f"no planned changes matching '{query}'" if query else "no planned writes")
        return

    def _value(value: Any, width: int = 18) -> str:
        if value is None or value == "":
            return "-"
        text = str(value)
        return text if _wlen(text) <= width else text[: max(0, width - 1)] + "…"

    def _label(item: dict[str, Any]) -> str:
        return item.get("label") or item.get("mesh_name") or "-"

    def _label_width(items: list[dict[str, Any]]) -> int:
        return max(_wlen("LABEL"), max((_wlen(_label(item)) for item in items), default=0))

    if writes:
        click.echo("planned writes (next sync would enforce):")
        node_w = _label_width(writes)
        fmt = "  {:<12}  {}  {:<22}  {:<18}  {:<18}  {}"
        click.echo(fmt.format(
            "PUBKEY", _wpad("LABEL", node_w), "KEY", "KNOWN", "DESIRED", "COMMAND"
        ))
        click.echo(fmt.format(
            "------", _wpad("-----", node_w), "---", "-----", "-------", "-------"
        ))
        for item in writes:
            click.echo(fmt.format(
                _pk(item.get("pubkey")),
                _wpad(_label(item), node_w),
                item["key"][:22],
                _value(item.get("known")),
                _value(item.get("desired")),
                item.get("command") or "-",
            ))
        click.echo("")

    if audits:
        click.echo("audit drift (no write):")
        node_w = _label_width(audits)
        fmt = "  {:<12}  {}  {:<22}  {:<18}  {:<18}"
        click.echo(fmt.format("PUBKEY", _wpad("LABEL", node_w), "KEY", "KNOWN", "DESIRED"))
        click.echo(fmt.format("------", _wpad("-----", node_w), "---", "-----", "-------"))
        for item in audits:
            click.echo(fmt.format(
                _pk(item.get("pubkey")),
                _wpad(_label(item), node_w),
                item["key"][:22],
                _value(item.get("known")),
                _value(item.get("desired")),
            ))
        click.echo("")

    if unknown:
        click.echo("needs observation (no known value yet):")
        node_w = _label_width(unknown)
        fmt = "  {:<12}  {}  {:<22}  {:<18}  {}"
        click.echo(fmt.format("PUBKEY", _wpad("LABEL", node_w), "KEY", "DESIRED", "REASON"))
        click.echo(fmt.format("------", _wpad("-----", node_w), "---", "-------", "------"))
        for item in unknown:
            click.echo(fmt.format(
                _pk(item.get("pubkey")),
                _wpad(_label(item), node_w),
                (item.get("key") or "-")[:22],
                _value(item.get("desired")),
                item.get("reason") or "-",
            ))


# ---------------------------------------------------------------------------
# commission
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def commission(ctx: click.Context) -> None:
    """Interactively commission a new or replacement MeshCore node.

    The gateway will hop to the factory frequency (869.618 MHz) and search
    for unmanaged nodes. Make sure only one new or replacement node is powered
    on and within range before proceeding.
    """
    socket = ctx.obj["socket"]
    _run_commission_wizard(socket)


def _run_commission_wizard(socket: str) -> None:
    """Interactive wizard: scans for nodes, asks mode, collects params, sends RPC, prints result."""
    click.echo("")
    click.echo("Commission a MeshCore Node")
    click.echo("=" * 26)
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
            if choice == "q":
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
            if choice == "q":
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
            if choice == "q":
                return
            continue

        found_pubkey = nodes[0]["pubkey"][:12]
        click.echo(f"\nFound 1 unmanaged node: {found_pubkey}")
        break

    click.echo("")
    click.echo("  (n) New node     - brand new hardware, generate fresh identity")
    click.echo("  (r) Replace node - restore a known node's identity from secrets.yaml")
    click.echo("  (q) Cancel")
    click.echo("")

    while True:
        mode_raw = click.prompt("Mode", default="q").strip().lower()
        if mode_raw in ("n", "new"):
            _commission_new_wizard(socket, gateway_radio, radio_options, group_options)
            return
        if mode_raw in ("r", "replace"):
            _commission_replace_wizard(socket)
            return
        if mode_raw in ("q", "quit", ""):
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
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        return None
    freq, bw, sf, cr = parts
    try:
        if not 400.0 <= float(freq) <= 928.0:
            return None
        if float(bw) not in (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0):
            return None
        if not 7 <= int(sf) <= 12:
            return None
        if not 5 <= int(cr) <= 8:
            return None
    except ValueError:
        return None
    return freq, bw, sf, cr


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
        click.echo("Radio settings:")
        for key, value, label in options:
            click.echo(f"  {key}. {value:<18} {label}")
        click.echo("  p. paste freq,bw,sf,cr")
        click.echo("  c. custom fields")
        click.echo("  q. cancel")
        default_choice = options[0][0]
        while True:
            choice = click.prompt("Radio", default=default_choice).strip().lower()
            if choice in ("q", "quit", "n", "no", "exit"):
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
                if raw.lower() in ("q", "quit", "n", "no", "exit"):
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
        raw_group: str = click.prompt("Group label", default=current or "-").strip()
        return None if raw_group == "-" else raw_group

    default_group = current if current in group_options else group_options[0]
    default_choice = str(group_options.index(default_group) + 1)

    click.echo("Groups:")
    for idx, name in enumerate(group_options, 1):
        click.echo(f"  {idx}. {name}")
    click.echo("  n. none")
    click.echo("  c. custom")

    while True:
        choice: str = click.prompt("Group", default=default_choice).strip()
        lowered = choice.lower()
        if lowered in ("n", "none", "-"):
            return None
        if lowered == "c":
            raw_group = click.prompt("Group label", default=current or "-").strip()
            return None if raw_group == "-" else raw_group
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(group_options):
                return group_options[idx]
        except ValueError:
            pass
        if choice in group_options:
            return choice
        click.echo("  choose a number, n for none, or c for custom")


def _commission_new_wizard(
    socket: str,
    gateway_radio: str | None = None,
    radio_options: list[dict[str, str]] | None = None,
    group_options: list[str] | None = None,
) -> None:
    """Collect params for a new node, confirm, send RPC with retry, display result."""
    click.echo("")
    click.echo("New node commissioning")
    click.echo("")
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
        group = "default"
    name = ""
    admin_password = "password"
    guest_password = "guest"
    freq = _gw(0)
    bw = _gw(1)
    sf = _gw(2)
    cr = _gw(3)

    while True:  # outer: re-enter values loop
        # --- identity ---
        label = click.prompt("Node label ", default=label).strip()
        # Re-prompt name until within 31 bytes (MeshCore firmware cap).
        name_default = name or label
        while True:
            name = click.prompt("Node name  ", default=name_default).strip()
            encoded = name.encode("utf-8")
            if len(encoded) <= 31:
                break
            truncated = encoded[:31].decode("utf-8", errors="ignore")
            click.echo(f"  name too long (max 31 bytes). Truncated: {truncated!r}")
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
            action = click.prompt("Proceed? [y]es  [e]dit  [q]uit", default="y").strip().lower()
            if action in ("", "y", "yes"):
                break
            if action in ("e", "edit"):
                break
            if action in ("q", "quit", "n", "no"):
                break
            click.echo("  please enter y, e, or q")

        if action in ("q", "quit", "n", "no"):
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
                "[Enter] retry  [e]dit  [q]uit", default=""
            ).strip().lower()
            if choice == "q":
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
    click.echo("Add to config.yaml under nodes:")
    click.echo("")
    click.echo(d.get("config_snippet", ""))
    click.echo("")
    click.echo("Add to secrets.yaml under nodes:")
    click.echo("")
    click.echo(d.get("secrets_snippet", ""))
    click.echo("")
    click.echo("Next steps:")
    click.echo("  1. Add both entries to your config files")
    click.echo("  2. Restart or reload mchd so it reads the new entries")
    click.echo(f"  3. Run: mch status {pubkey_prefix}")
    click.echo(f"  4. Optional: mch sync {pubkey_prefix}")


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
            "[Enter] retry  [q]uit", default="", show_default=False
        ).strip().lower()
        if choice == "q":
            click.echo("cancelled")
            return
        click.echo("Retrying...")

    click.echo("")
    click.echo(
        f"Replacement complete. '{label}' ({prefix}) is rebooting with its restored identity."
    )
    click.echo(f"Once it comes back up, run: mch sync {prefix}")


# ---------------------------------------------------------------------------
# start / stop (via systemd)
# ---------------------------------------------------------------------------

@cli.command()
def start() -> None:
    """Start the mchd daemon via systemd."""
    result = subprocess.run(["systemctl", "start", "mchd"], capture_output=True, text=True)
    if result.returncode != 0:
        click.echo(result.stderr.strip() or "systemctl start failed", err=True)
        raise SystemExit(result.returncode)
    click.echo("mchd started")


@cli.command()
def stop() -> None:
    """Stop the mchd daemon via systemd."""
    result = subprocess.run(["systemctl", "stop", "mchd"], capture_output=True, text=True)
    if result.returncode != 0:
        click.echo(result.stderr.strip() or "systemctl stop failed", err=True)
        raise SystemExit(result.returncode)
    click.echo("mchd stopped")


@cli.command()
def restart() -> None:
    """Restart the mchd daemon via systemd."""
    result = subprocess.run(["systemctl", "restart", "mchd"], capture_output=True, text=True)
    if result.returncode != 0:
        click.echo(result.stderr.strip() or "systemctl restart failed", err=True)
        raise SystemExit(result.returncode)
    click.echo("mchd restarted")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cli()


if __name__ == "__main__":
    main()
