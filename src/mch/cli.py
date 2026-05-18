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


def _wlen(s: str) -> int:
    """Visual width of a string — wide chars (emoji, CJK) count as 2 columns."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _wpad(s: str, width: int) -> str:
    """Left-justify s in a field of visual width."""
    return s + " " * max(0, width - _wlen(s))


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

    state_w = max(len("STATE"), max((len(n["state"])        for n in nodes), default=0))
    name_w  = max(_wlen("NAME"), max((_wlen(n["name"][:22]) for n in nodes), default=0))

    row = "  {:<{sw}}  {}  {}"
    click.echo(row.format("STATE", _wpad("NAME", name_w), "PUBKEY", sw=state_w))
    click.echo(row.format("-----", _wpad("----", name_w), "------", sw=state_w))
    for n in nodes:
        click.echo(row.format(n["state"], _wpad(n["name"][:22], name_w), n["pubkey"][:16] + "…", sw=state_w))


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

    def _short_ver(raw: str | None) -> str:
        if not raw:
            return "—"
        return raw.split("-")[0]  # "v1.15.0-dee3e26..." → "v1.15.0"

    group_w = max(len("GROUP"),   max((len(n.get("group") or "—")         for n in data), default=0))
    label_w = max(_wlen("LABEL"), max((_wlen(n.get("label") or "—")       for n in data), default=0))
    name_w  = max(_wlen("NAME"),  max((_wlen(n["name"])                    for n in data), default=0))
    nbrs_w  = max(len("NBRS"),    max((len(n.get("neighbors") or "—")      for n in data), default=0))
    ver_w   = max(len("VER"),     max((len(_short_ver(n.get("ver")))       for n in data), default=0))

    row = "  {:<10}  {}  {:<{gw}}  {}  {:<{nw}}  {:<{vw}}  {:<16}  {}"
    click.echo(row.format("STATE", _wpad("LABEL", label_w), "GROUP", _wpad("NAME", name_w), "NBRS", "VER", "LAST SEEN", "PUBKEY", gw=group_w, nw=nbrs_w, vw=ver_w))
    click.echo(row.format("-----", _wpad("-----", label_w), "-----", _wpad("----", name_w), "----", "---", "---------", "------", gw=group_w, nw=nbrs_w, vw=ver_w))
    for n in data:
        last  = (n.get("last_seen") or "never")[:16].replace("T", " ")
        group = (n.get("group") or "—")
        nbrs  = n.get("neighbors") or "—"
        ver   = _short_ver(n.get("ver"))
        click.echo(row.format(
            n["state"],
            _wpad(n.get("label") or "—", label_w),
            group,
            _wpad(n["name"], name_w),
            nbrs, ver,
            last, n["pubkey"][:16] + "…",
            gw=group_w, nw=nbrs_w, vw=ver_w,
        ))


# ---------------------------------------------------------------------------
# node
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("pubkey")
@click.pass_context
def node(ctx: click.Context, pubkey: str) -> None:
    """Show details for a single node.

    PUBKEY: pubkey prefix or label (case-insensitive).
    """
    resp = _rpc(ctx.obj["socket"], {"cmd": "node", "pubkey": pubkey})
    data = _check(resp)

    def _ts(v: str | None) -> str:
        return (v or "")[:19].replace("T", " ") or "never"

    click.echo(f"label:       {data.get('label') or '—'}")
    click.echo(f"group:       {data.get('group') or '—'}")
    click.echo(f"name:        {data['name']}")
    click.echo(f"state:       {data['state']}")
    click.echo(f"last seen:   {_ts(data.get('last_seen'))}")
    click.echo(f"last conn:   {_ts(data.get('last_connected'))}")
    click.echo(f"pubkey:      {data['pubkey']}")
    click.echo(f"auth cached: {data.get('auth_cached', False)}")

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
        sfmt = "  {:<2}  {:<22}  {:<14}  {:<14}  {}"
        click.echo(sfmt.format("~", "KEY", "DESIRED", "OBSERVED", "POLICY"))
        click.echo(sfmt.format("-", "---", "-------", "--------", "------"))
        for s in settings:
            drift_flag = "~" if s["drift"] else " "
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
# recommend
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("pubkey", required=False)
@click.pass_context
def recommend(ctx: click.Context, pubkey: str | None) -> None:
    """Show recommended settings based on each node's neighbour count.

    PUBKEY: pubkey prefix or label (case-insensitive). Omit to show all nodes.

    Prints the matching profile (e.g. rooftop, tower), recommended txdelay
    and universal settings with one-line explanations, and a ready-to-paste
    config.yaml settings: block.

    Requires 'neighbors' in the facts: list in config.yaml and at least
    one completed sync cycle to populate the neighbour count.
    """
    resp = _rpc(ctx.obj["socket"], {"cmd": "recommend", "pubkey": pubkey})
    data = _check(resp)

    if not data:
        click.echo("no nodes in database")
        return

    if pubkey:
        _recommend_detail(data[0])
    else:
        _recommend_table(data)


def _recommend_table(data: list) -> None:
    label_w = max(_wlen("LABEL"), max((_wlen(n.get("label") or "—") for n in data), default=0))

    row = "  {}  {:<4}  {:<10}  {:<7}  {}"
    click.echo(row.format(_wpad("LABEL", label_w), "NBRS", "PROFILE", "TXDELAY", "DIRECT.TXDELAY"))
    click.echo(row.format(_wpad("-----", label_w), "----", "-------", "-------", "--------------"))
    for n in data:
        count = n["neighbour_count"]
        profile = n["profile"]
        click.echo(row.format(
            _wpad(n.get("label") or "—", label_w),
            str(count) if count is not None else "—",
            profile["name"] if profile else "(no data)",
            profile["txdelay"] if profile else "—",
            profile["direct_txdelay"] if profile else "—",
        ))


def _recommend_detail(n: dict) -> None:
    count = n["neighbour_count"]
    profile = n["profile"]

    click.echo(f"label:      {n.get('label') or '—'}")
    click.echo(f"name:       {n['name']}")

    if count is None:
        click.echo("")
        click.echo("(no neighbour data — add 'neighbors' to facts: in config.yaml and run mch sync)")
        return

    click.echo(f"neighbours: {count}")
    if profile:
        click.echo(f"profile:    {profile['name']} — {profile['description']}")

    click.echo("")

    # Build the full list: profile settings first, then universal
    settings: list[tuple[str, str, str]] = []
    if profile:
        settings.append(("txdelay",        profile["txdelay"],        "Flood retransmit delay — high-reach nodes fire first"))
        settings.append(("direct.txdelay", profile["direct_txdelay"], "Direct traffic delay (lower than flood delay)"))

    for u in n.get("universal", []):
        settings.append((u["key"], u["value"], u["description"]))

    key_w = max(len(s[0]) for s in settings)
    val_w = max(len(s[1]) for s in settings)

    click.echo("Recommended settings:")
    for key, val, desc in settings:
        click.echo(f"  {key:<{key_w}}  {val:<{val_w}}  {desc}")

    click.echo("")
    click.echo("Paste into config.yaml settings: block for this node:")
    for key, val, _ in settings:
        click.echo(f"    {key}:")
        click.echo(f'      desired: "{val}"')
        click.echo(f"      policy: enforce")


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
