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


async def _request(socket_path: str, cmd: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    except (FileNotFoundError, ConnectionRefusedError):
        click.echo(f"error: cannot connect to daemon at {socket_path}", err=True)
        click.echo("  is mchd running?  try: mchd --help", err=True)
        raise SystemExit(1)

    writer.write((json.dumps(cmd) + "\n").encode())
    await writer.drain()
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        writer.close()
        click.echo(f"error: timed out waiting for daemon response ({timeout:.0f}s)", err=True)
        raise SystemExit(1)
    writer.close()
    return json.loads(raw)


def _rpc(socket_path: str, cmd: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
    return asyncio.run(_request(socket_path, cmd, timeout=timeout))


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
@click.argument("query", required=False, default=None)
@click.pass_context
def status(ctx: click.Context, query: str | None) -> None:
    """Show daemon and fleet status."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "status"})
    data = _check(resp)

    all_nodes = data.get("nodes", [])
    nodes = _filter_nodes(all_nodes, query)
    issue_count = data.get("issues", 0)
    filtered = bool(query and len(nodes) != len(all_nodes))

    if nodes:
        def _short_ver(raw: str | None) -> str:
            if not raw:
                return "-"
            return raw.split("-")[0]  # "v1.15.0-dee3e26..." → "v1.15.0"

        state_w = max(len("STATE"),   max((len(n["state"])                        for n in nodes), default=0))
        group_w = max(len("GROUP"),   max((len(n.get("group") or "-")            for n in nodes), default=0))
        label_w = max(_wlen("LABEL"), max((_wlen(n.get("label") or "-")          for n in nodes), default=0))
        name_w  = max(_wlen("NAME"),  max((_wlen(n["name"])                       for n in nodes), default=0))
        nbrs_w  = max(len("NBRS"),    max((len(n.get("neighbors") or "-")         for n in nodes), default=0))
        ver_w   = max(len("VER"),     max((len(_short_ver(n.get("ver")))          for n in nodes), default=0))

        row = "  {:<{sw}}  {}  {:<{gw}}  {}  {:<{nw}}  {:<{vw}}  {:<16}  {}"
        click.echo(row.format("STATE", _wpad("LABEL", label_w), "GROUP", _wpad("NAME", name_w), "NBRS", "VER", "LAST SEEN", "PUBKEY", sw=state_w, gw=group_w, nw=nbrs_w, vw=ver_w))
        click.echo(row.format("-----", _wpad("-----", label_w), "-----", _wpad("----", name_w), "----", "---", "---------", "------", sw=state_w, gw=group_w, nw=nbrs_w, vw=ver_w))
        for n in nodes:
            last  = (n.get("last_seen") or "never")[:16].replace("T", " ")
            group = (n.get("group") or "-")
            nbrs  = n.get("neighbors") or "-"
            ver   = _short_ver(n.get("ver"))
            click.echo(row.format(
                n["state"],
                _wpad(n.get("label") or "-", label_w),
                group,
                _wpad(n["name"], name_w),
                nbrs, ver,
                last, n["pubkey"][:16] + "…",
                sw=state_w, gw=group_w, nw=nbrs_w, vw=ver_w,
            ))
    elif query:
        click.echo(f"  no nodes matching '{query}'")
    else:
        click.echo("  no nodes in database")

    click.echo("")
    if filtered:
        click.echo(f"{len(nodes)} of {len(all_nodes)} nodes shown")
    else:
        footer = f"{len(all_nodes)} nodes"
        if issue_count:
            issue_word = "issue" if issue_count == 1 else "issues"
            footer += f"  {issue_count} {issue_word} - run 'mch issues' for details"
        click.echo(footer)


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

    click.echo(f"label:       {data.get('label') or '-'}")
    click.echo(f"group:       {data.get('group') or '-'}")
    click.echo(f"name:        {data['name']}")
    click.echo(f"state:       {data['state']}")
    click.echo(f"last seen:   {_ts(data.get('last_seen'))}")
    click.echo(f"last conn:   {_ts(data.get('last_connected'))}")
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
        sfmt = "  {:<2}  {:<22}  {:<14}  {:<14}  {}"
        click.echo(sfmt.format("~", "KEY", "DESIRED", "OBSERVED", "POLICY"))
        click.echo(sfmt.format("-", "---", "-------", "--------", "------"))
        for s in settings:
            drift_flag = "~" if s["drift"] else " "
            click.echo(sfmt.format(
                drift_flag,
                s["key"][:22],
                (s.get("desired") or "-")[:14],
                (s.get("observed") or "-")[:14],
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
        # inline single-node sync can take many per-operation timeouts (30s each)
        resp = _rpc(ctx.obj["socket"], payload, timeout=300)
    else:
        resp = _rpc(ctx.obj["socket"], payload)
    data = _check(resp)
    click.echo(data)


# ---------------------------------------------------------------------------
# issues
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query", required=False, default=None)
@click.pass_context
def issues(ctx: click.Context, query: str | None) -> None:
    """Show all current issues: drifted settings, problem states, and config ghosts."""
    resp = _rpc(ctx.obj["socket"], {"cmd": "issues"})
    data = _check(resp) or {}
    drifted = _filter_drift(data.get("drifted", []), query)
    bad_state = _filter_nodes(data.get("bad_state", []), query)
    ghosts = _filter_ghosts(data.get("ghosts", []), query)
    open_nodes = _filter_nodes(data.get("open_nodes", []), query)

    if not drifted and not bad_state and not ghosts and not open_nodes:
        click.echo("no issues detected")
        return

    if drifted:
        click.echo("drifted settings:")
        node_w = max(len("NODE"), max((len(s.get("label") or s["node_pubkey"][:16]) for s in drifted), default=0))
        fmt = "  {:<{nw}}  {:<22}  {:<14}  {:<14}  {}"
        click.echo(fmt.format("NODE", "KEY", "DESIRED", "OBSERVED", "POLICY", nw=node_w))
        click.echo(fmt.format("----", "---", "-------", "--------", "------", nw=node_w))
        for s in drifted:
            node = s.get("label") or s["node_pubkey"][:16]
            click.echo(fmt.format(node, s["key"][:22],
                                  (s.get("desired") or "-")[:14],
                                  (s.get("observed") or "-")[:14],
                                  s["policy"], nw=node_w))
        click.echo("")

    if bad_state:
        click.echo("problem states:")
        name_w = max(_wlen("NODE"), max((_wlen(n.get("label") or n["name"]) for n in bad_state), default=0))
        fmt = "  {}  {:<12}  {:<16}  {}"
        click.echo(fmt.format(_wpad("NODE", name_w), "STATE", "LAST SEEN", "PUBKEY"))
        click.echo(fmt.format(_wpad("----", name_w), "-----", "---------", "------"))
        for n in bad_state:
            label = n.get("label") or n["name"]
            last = (n.get("last_seen") or "never")[:16].replace("T", " ")
            pubkey = n.get("pubkey", "")[:16] + "…" if n.get("pubkey") else "-"
            click.echo(fmt.format(_wpad(label, name_w), n["state"], last, pubkey))
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
        name_w = max(_wlen("NODE"), max((_wlen(n.get("label") or n["name"]) for n in open_nodes), default=0))
        fmt = "  {}  {}"
        click.echo(fmt.format(_wpad("NODE", name_w), "PUBKEY"))
        click.echo(fmt.format(_wpad("----", name_w), "------"))
        for n in open_nodes:
            label = n.get("label") or n["name"]
            pubkey = n.get("pubkey", "")[:16] + "…" if n.get("pubkey") else "-"
            click.echo(fmt.format(_wpad(label, name_w), pubkey))


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
# contacts — gateway REP nodes not yet in config.yaml
# ---------------------------------------------------------------------------

def _filter_contacts(contacts: list[dict], query: str | None) -> list[dict]:
    """Filter contacts by pubkey prefix or name substring."""
    if not query:
        return contacts
    q = query.lower()
    return [
        c for c in contacts
        if c.get("public_key", "").startswith(query)
        or q in (c.get("adv_name") or "").lower()
    ]


def _filter_nodes(nodes: list[dict], query: str | None) -> list[dict]:
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


def _filter_drift(drifted: list[dict], query: str | None) -> list[dict]:
    """Filter drifted settings by pubkey prefix or label substring."""
    if not query:
        return drifted
    q = query.lower()
    return [
        s for s in drifted
        if s["node_pubkey"].startswith(query)
        or q in (s.get("label") or "").lower()
    ]


def _filter_ghosts(ghosts: list[dict], query: str | None) -> list[dict]:
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
        click.echo(f"    group:          # assign to a group, or remove this line\n")

        click.echo("# secrets.yaml — append to the 'nodes:' section:")
        click.echo(f"  {prefix}: \"\"      # add password if required, or leave blank\n")


# ---------------------------------------------------------------------------
# hint
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query", required=False)
@click.pass_context
def hint(ctx: click.Context, query: str | None) -> None:
    """Show suggested settings based on each node's neighbour count.

    QUERY: pubkey prefix, label substring, or name substring. Omit to show all nodes.

    Prints the matching profile (e.g. rooftop, tower), suggested txdelay
    and universal settings with one-line explanations, and a ready-to-paste
    config.yaml settings: block.

    Requires 'neighbors' in the facts: list in config.yaml and at least
    one completed sync cycle to populate the neighbour count.
    """
    resp = _rpc(ctx.obj["socket"], {"cmd": "hint"})
    all_data = _check(resp)
    data = _filter_nodes(all_data, query)

    if not all_data:
        click.echo("no nodes in database")
        return

    if not data:
        click.echo(f"no nodes matching '{query}'")
        return

    if len(data) == 1:
        _hint_detail(data[0])
    else:
        _hint_table(data)


def _hint_table(data: list) -> None:
    label_w = max(_wlen("LABEL"), max((_wlen(n.get("label") or "-") for n in data), default=0))

    row = "  {}  {:<4}  {:<7}  {}"
    click.echo(row.format(_wpad("LABEL", label_w), "NBRS", "TXDELAY", "DIRECT.TXDELAY"))
    click.echo(row.format(_wpad("-----", label_w), "----", "-------", "--------------"))
    for n in data:
        count = n["neighbour_count"]
        profile = n["profile"]
        click.echo(row.format(
            _wpad(n.get("label") or "-", label_w),
            str(count) if count is not None else "-",
            profile["txdelay"] if profile else "-",
            profile["direct_txdelay"] if profile else "-",
        ))


def _hint_detail(n: dict) -> None:
    count = n["neighbour_count"]
    profile = n["profile"]

    click.echo(f"label:      {n.get('label') or '-'}")
    click.echo(f"name:       {n['name']}")
    click.echo("")

    if count is None:
        click.echo("(no neighbour data - add 'neighbors' to facts: in config.yaml and run mch sync to start collecting it)")
        click.echo("")
    else:
        click.echo(f"neighbours: {count}")
        if profile:
            click.echo(f"profile:    {profile['name']} - {profile['description']}")
        click.echo("")

    # Build the full list: profile settings (if known) first, then universal
    settings: list[tuple[str, str, str]] = []
    if profile:
        settings.append(("txdelay",        profile["txdelay"],        "Flood retransmit delay - high-reach nodes fire first"))
        settings.append(("direct.txdelay", profile["direct_txdelay"], "Direct traffic delay (lower than flood delay)"))

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
        click.echo(f"        policy: enforce")


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------

_EXEC_BLOCKED = frozenset({"erase", "start ota", "set prv.key", "password", "setperm", "set guest.password"})
_EXEC_CONFIRM = frozenset({"reboot", "clkreboot", "clear stats", "log erase"})


@cli.command(name="exec")
@click.argument("node")
@click.argument("command", nargs=-1, required=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt for destructive commands")
@click.pass_context
def exec_cmd(ctx: click.Context, node: str, command: tuple, yes: bool) -> None:
    """Run a raw MeshCore CLI command on a node.

    NODE: pubkey prefix, label substring, or name substring.
    COMMAND: MeshCore CLI command (e.g. ver, stats-radio, reboot).

    Destructive commands (reboot, clkreboot, etc.) require confirmation unless --yes is passed.
    Irreversible commands (erase, set prv.key, etc.) are blocked entirely.
    """
    cmd_str = " ".join(command)
    cmd_root = cmd_str.split()[0].lower() if cmd_str else ""

    if cmd_root in _EXEC_BLOCKED:
        click.echo(f"error: '{cmd_root}' is blocked in mch exec - use config operations instead", err=True)
        raise SystemExit(1)

    # resolve node to get pubkey for confirmation display
    node_resp = _rpc(ctx.obj["socket"], {"cmd": "node", "pubkey": node})
    if not node_resp.get("ok"):
        click.echo(f"error: {node_resp.get('error')}", err=True)
        raise SystemExit(1)
    data = node_resp["data"]
    pubkey = data["pubkey"]
    label = data.get("label") or data["name"]

    if cmd_root in _EXEC_CONFIRM and not yes:
        click.echo(f"  {cmd_str}  {label}  ({pubkey[:8]})")
        typed = click.prompt("type pubkey prefix to confirm").strip()
        if not pubkey.startswith(typed):
            click.echo("cancelled", err=True)
            raise SystemExit(1)

    resp = _rpc(ctx.obj["socket"], {"cmd": "exec", "pubkey": pubkey, "command": cmd_str}, timeout=90)
    data = _check(resp)
    click.echo(data)


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
    click.echo("Commission a MeshCore node")
    click.echo("=" * 26)
    click.echo("")

    # Search first — no point asking questions if there's no node to commission.
    gateway_radio = None
    while True:
        click.echo("Searching for unmanaged nodes in range on default factory frequency (869.618 MHz)...")
        resp = _rpc(socket, {"cmd": "commission", "mode": "scan"}, timeout=30)
        if not resp.get("ok"):
            click.echo(f"\nSearch failed: {resp.get('error', 'unknown error')}")
            choice = click.prompt("Press Enter to retry or q to cancel", default="").strip().lower()
            if choice == "q":
                return
            continue

        nodes = resp["data"]["nodes"]
        gateway_radio = resp["data"].get("gateway_radio")

        if len(nodes) == 0:
            click.echo("\nNo unmanaged nodes found. Is it powered on and in range with an antenna?")
            choice = click.prompt("Press Enter to retry or q to cancel", default="").strip().lower()
            if choice == "q":
                return
            continue

        if len(nodes) > 1:
            click.echo(f"\nFound {len(nodes)} unmanaged nodes - commission one at a time:")
            for n in nodes:
                click.echo(f"  {n['pubkey'][:12]}")
            click.echo("")
            choice = click.prompt(
                "Power off all but one, then press Enter to retry or q to cancel", default=""
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
            _commission_new_wizard(socket, gateway_radio)
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
        val = click.prompt("Frequency (MHz)", default=default).strip()
        try:
            if 400.0 <= float(val) <= 928.0:
                return val
        except ValueError:
            pass
        click.echo("  enter a frequency between 400 and 928 MHz, e.g. 915.075")


def _prompt_radio_bw(default: str = "") -> str:
    """Prompt for LoRa bandwidth (kHz); re-prompt until valid."""
    while True:
        val = click.prompt("Bandwidth (kHz)", default=default).strip()
        try:
            if float(val) in (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0):
                return val
        except ValueError:
            pass
        click.echo("  bandwidth must be one of: 7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125, 250, 500")


def _prompt_radio_sf(default: str = "") -> str:
    """Prompt for spreading factor (7-12); re-prompt until valid."""
    while True:
        val = click.prompt("Spreading factor (7-12)", default=default).strip()
        try:
            if 7 <= int(val) <= 12:
                return val
        except ValueError:
            pass
        click.echo("  spreading factor must be an integer 7-12")


def _prompt_radio_cr(default: str = "") -> str:
    """Prompt for coding rate (5-8); re-prompt until valid."""
    while True:
        val = click.prompt("Coding rate (5-8)", default=default).strip()
        try:
            if 5 <= int(val) <= 8:
                return val
        except ValueError:
            pass
        click.echo("  coding rate must be an integer 5-8")


def _commission_new_wizard(socket: str, gateway_radio: str | None = None) -> None:
    """Collect params for a new node, confirm, send RPC with retry, display result."""
    click.echo("")
    click.echo("New node commissioning")
    click.echo("")

    # Parse gateway_radio "freq,bw,sf,cr" into per-field defaults.
    gw_parts = (gateway_radio or "").split(",")
    def _gw(i: int) -> str:
        return gw_parts[i].strip() if len(gw_parts) > i else ""

    # Saved values — used as defaults when the outer edit loop iterates.
    label = ""
    group = ""
    name = ""
    freq = _gw(0)
    bw = _gw(1)
    sf = _gw(2)
    cr = _gw(3)
    admin_password = "password"
    guest_password = ""

    while True:  # outer: re-enter values loop
        if label:
            label = click.prompt("Node label", default=label).strip()
        else:
            label = click.prompt("Node label").strip()
        group = click.prompt("Group (optional, press Enter to skip)", default=group or "").strip() or None
        name_default = name or label
        # Re-prompt name until within 31 bytes.
        while True:
            name = click.prompt("Node name", default=name_default).strip()
            encoded = name.encode("utf-8")
            if len(encoded) <= 31:
                break
            truncated = encoded[:31].decode("utf-8", errors="ignore")
            click.echo(f"  name too long (max 31 bytes). Truncated: {truncated!r}")
            name_default = truncated

        freq = _prompt_radio_freq(freq)
        bw = _prompt_radio_bw(bw)
        sf = _prompt_radio_sf(sf)
        cr = _prompt_radio_cr(cr)
        target_freq = f"{freq},{bw},{sf},{cr}"

        admin_password = click.prompt("Admin password", default=admin_password).strip()
        guest_password = (
            click.prompt("Guest password (Enter to skip)", default=guest_password or "").strip() or ""
        )

        click.echo("")
        click.echo("  label:    " + label)
        click.echo("  group:    " + (group or "-"))
        click.echo("  name:     " + name)
        click.echo("  freq:     " + target_freq)
        click.echo("  password: " + admin_password)
        if guest_password:
            click.echo("  guest pw: " + guest_password)
        click.echo("")

        action = click.prompt("[y]es  [e]dit  [q]uit", default="y").strip().lower()
        if action == "q":
            click.echo("cancelled")
            return
        if action == "e":
            continue

        # Inner: commission retry loop.
        commission_ok = False
        while True:
            click.echo("Commissioning node...")
            resp = _rpc(socket, {
                "cmd": "commission",
                "mode": "new",
                "label": label,
                "group": group,
                "name": name,
                "target_freq": target_freq,
                "admin_password": admin_password,
                "guest_password": guest_password or None,
            }, timeout=60)

            if resp.get("ok"):
                commission_ok = True
                break

            click.echo(f"\nCan't complete commissioning: {resp.get('error', 'unknown error')}")
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
    click.echo(f"Node pubkey prefix: {pubkey_prefix}")
    click.echo(f"Password saved to secrets.yaml under: {pubkey_prefix}")
    click.echo("")
    click.echo("config.yaml updated. Next steps:")
    click.echo("  1. Wait 30-60s for the node to reboot on the target frequency")
    click.echo("  2. Run: mch sync")


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
        }, timeout=60)

        if resp.get("ok"):
            break

        click.echo(f"\nCan't complete commissioning: {resp.get('error', 'unknown error')}")
        click.echo("Check the node is powered on, in range, and with an antenna.")
        choice = click.prompt("[Enter] retry  [q]uit", default="").strip().lower()
        if choice == "q":
            click.echo("cancelled")
            return
        click.echo("Retrying...")

    click.echo("")
    click.echo(f"Replacement complete. '{label}' ({prefix}) is rebooting with its restored identity.")
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
