"""
mch — control CLI for the mchd daemon.

Talks to the daemon over its Unix socket.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import click


DEFAULT_SOCKET = "/run/mch/mchd.sock"


def _wpad(s: str, width: int) -> str:
    """Left-justify s in a field of visual width, accounting for wide chars (emoji, CJK)."""
    visual = sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)
    return s + " " * max(0, width - visual)


async def _request(socket_path: str, cmd: dict[str, Any]) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    except (FileNotFoundError, ConnectionRefusedError):
        click.echo(f"error: cannot connect to daemon at {socket_path}", err=True)
        click.echo("  is mchd running?  try: mchd --help", err=True)
        raise SystemExit(1)

    writer.write((json.dumps(cmd) + "\n").encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.readline(), timeout=30)
    writer.close()
    return json.loads(raw)


def _rpc(socket_path: str, cmd: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(_request(socket_path, cmd))


def _check(resp: dict[str, Any]) -> Any:
    if not resp.get("ok"):
        click.echo(f"error: {resp.get('error', 'unknown error')}", err=True)
        raise SystemExit(1)
    return resp.get("data")


# ---------------------------------------------------------------------------
# group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name="mch")
@click.option(
    "--socket", "-s",
    default=DEFAULT_SOCKET,
    envvar="MCHERD_SOCKET",
    help="Path to daemon Unix socket",
    show_default=True,
)
@click.pass_context
def cli(ctx: click.Context, socket: str) -> None:
    """mch — MeshCore node management CLI"""
    ctx.ensure_object(dict)
    ctx.obj["socket"] = socket


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show daemon and fleet status."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "status"})
    data = _check(resp)

    nodes = data.get("nodes", [])
    drifted = data.get("drifted_settings", 0)

    click.echo(f"nodes: {len(nodes)}  drifted settings: {drifted}")
    click.echo("")

    if not nodes:
        click.echo("  (no nodes in database yet)")
        return

    row = "  {:<16}  {}  {}"
    click.echo(row.format("STATE", _wpad("NAME", 22), "PUBKEY"))
    click.echo(row.format("-----", _wpad("----", 22), "------"))
    for n in nodes:
        click.echo(row.format(n["state"], _wpad(n["name"][:22], 22), n["pubkey"][:16] + "…"))


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def nodes(ctx: click.Context) -> None:
    """List all known nodes."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "nodes"})
    data = _check(resp)

    if not data:
        click.echo("no nodes in database")
        return

    row = "  {:<12}  {}  {}  {:<10}  {:<16}  {}"
    click.echo(row.format("STATE", _wpad("LABEL", 22), _wpad("NAME", 20), "GROUP", "LAST SEEN", "PUBKEY"))
    click.echo(row.format("-----", _wpad("-----", 22), _wpad("----", 20), "-----", "---------", "------"))
    for n in data:
        last = (n.get("last_seen") or "never")[:16]
        group = (n.get("group") or "—")[:10]
        click.echo(row.format(
            n["state"],
            _wpad((n.get("label") or "—")[:22], 22),
            _wpad(n["name"][:20], 20),
            group, last, n["pubkey"][:16] + "…",
        ))


# ---------------------------------------------------------------------------
# node
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("pubkey")
@click.pass_context
def node(ctx: click.Context, pubkey: str) -> None:
    """Show details for a single node (partial pubkey match ok)."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "node", "pubkey": pubkey})
    data = _check(resp)

    click.echo(f"name:        {data['name']}")
    click.echo(f"pubkey:      {data['pubkey']}")
    click.echo(f"group:       {data.get('group') or '—'}")
    click.echo(f"state:       {data['state']}")
    click.echo(f"last seen:   {data.get('last_seen') or 'never'}")
    click.echo(f"last conn:   {data.get('last_connected') or 'never'}")
    click.echo(f"auth cached: {data.get('auth_cached', False)}")
    click.echo("")

    facts = data.get("facts", {})
    if facts:
        click.echo("facts:")
        for k, v in sorted(facts.items()):
            click.echo(f"  {k}: {v}")
        click.echo("")

    settings = data.get("settings", [])
    if settings:
        click.echo("settings:")
        sfmt = "  {:<2}  {:<22}  {:<14}  {:<14}  {}"
        click.echo(sfmt.format("!", "KEY", "DESIRED", "OBSERVED", "POLICY"))
        for s in settings:
            drift_flag = "*" if s["drift"] else " "
            click.echo(sfmt.format(
                drift_flag,
                s["key"][:22],
                (s.get("desired") or "—")[:14],
                (s.get("observed") or "—")[:14],
                s["policy"],
            ))
        click.echo("")

    logs = data.get("logs", [])
    if logs:
        click.echo("recent activity:")
        for entry in logs[:10]:
            ts = (entry.get("timestamp") or "")[:19].replace("T", " ")
            level = entry["level"]
            click.echo(f"  [{level:<5}] {ts}  {entry['message']}")


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
    payload: dict = {"cmd": "sync"}
    if node:
        payload["prefix"] = node
    resp = _rpc(ctx.obj["socket"], payload)
    data = _check(resp)
    click.echo(data)


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def drift(ctx: click.Context) -> None:
    """Show all settings currently drifted from desired."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "drift"})
    data = _check(resp)

    if not data:
        click.echo("no drift detected")
        return

    fmt = "  {:<16}  {:<22}  {:<14}  {:<14}  {}"
    click.echo(fmt.format("NODE", "KEY", "DESIRED", "OBSERVED", "POLICY"))
    click.echo(fmt.format("----", "---", "-------", "--------", "------"))
    for s in data:
        click.echo(fmt.format(
            s["node_pubkey"][:16],
            s["key"][:22],
            (s.get("desired") or "—")[:14],
            (s.get("observed") or "—")[:14],
            s["policy"],
        ))


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

    for entry in data:
        ts = (entry.get("timestamp") or "")[:19].replace("T", " ")
        node_label = f" [{entry['node_pubkey'][:12]}]" if entry.get("node_pubkey") else ""
        click.echo(f"{ts} [{entry['level']:<5}]{node_label} {entry['message']}")


# ---------------------------------------------------------------------------
# contacts — gateway REP nodes not yet in config.yaml
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def contacts(ctx: click.Context) -> None:
    """Show gateway repeater contacts not yet in your config.yaml.

    Prints ready-to-paste YAML snippets. Review and append to the nodes:
    section of your config.yaml for any nodes you want mch to manage.
    """
    resp = _rpc(ctx.obj["socket"], {"cmd": "contacts"})
    data = _check(resp)

    if not data:
        click.echo("no unfiled repeater contacts found")
        return

    click.echo("# Repeater contacts visible to the gateway but not in your config.yaml.")
    click.echo("# Review each one, then paste the snippets into your config files.\n")

    for c in data:
        pubkey = c.get("public_key", "")
        prefix = pubkey[:8]
        name = c.get("adv_name", "") or "unknown"
        sep = "─" * max(0, 60 - len(name))

        click.echo(f"# ── {name} {sep}")
        click.echo(f"# full pubkey: {pubkey}\n")

        click.echo("# config.yaml — append to the 'nodes:' section:")
        click.echo(f"  - pubkey_prefix: {prefix}")
        click.echo(f'    label: "{name}"')
        click.echo(f"    group:           # assign to a group, or remove this line\n")

        click.echo("# secrets.yaml — append to the 'nodes:' section:")
        click.echo(f"  {prefix}: \"\"     # add password if required, or leave blank\n")


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
