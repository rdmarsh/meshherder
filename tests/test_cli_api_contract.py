"""CLI ↔ daemon ↔ API contract: `mch fleet status --json` must equal the API `/status` body.

RECOMMENDATIONS-2 S3.2 (and part-1 M4 remainder): the CLI mocks `_rpc` with hand-built dicts in
`test_cli.py`, and nothing else asserts the daemon actually produces those shapes. This drives the
*real* daemon control socket with a stub gateway and a seeded DB, so a schema drift between what the
daemon emits, what the CLI prints for `--json`, and what the HTTP API serves would fail here — the
`_status_view`/`_node_detail_view` projection shims exist precisely because this has drifted before.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import MagicMock

from click.testing import CliRunner

from mch.cli import cli
from mch.config import Config, NodeConfig
from mch.daemon import _handle_client
from mch.db import Database
from mch.models import DesiredSetting, Node, NodeState, Scope, SettingPolicy
from mch.report import build_fleet_report


def _normalise(payload: object) -> object:
    """Round-trip through JSON so native vs parsed types (tuples/lists, etc.) compare equal."""
    return json.loads(json.dumps(payload, default=str))


def test_fleet_status_json_matches_api_status_over_socket(tmp_path):
    sock_path = str(tmp_path / "mchd.sock")
    db_path = tmp_path / "contract.db"
    api_body: dict[str, object] = {}
    started = threading.Event()
    stop = threading.Event()

    def run_server() -> None:
        async def main() -> None:
            db = Database(db_path)
            await db.open()
            node = Node(
                pubkey="aabbccdd" * 8,
                name="Tower",
                label="tower",
                state=NodeState.CONFIGURED,
                auth_cached=True,
            )
            await db.upsert_node(node)
            await db.set_desired_setting(DesiredSetting(
                scope_type=Scope.NODE, scope_id=node.pubkey, key="name",
                desired="New", policy=SettingPolicy.ENFORCE,
            ))
            config = Config(nodes=[NodeConfig(pubkey_prefix="aabbccdd", label="tower")])
            # The HTTP API serves this object verbatim for /status (api.py route "/status").
            api_body["body"] = await build_fleet_report(db, config)
            wq = MagicMock()  # the "fleet" command never touches the work queue
            server = await asyncio.start_unix_server(
                lambda r, w: _handle_client(r, w, wq, db, config), path=sock_path
            )
            started.set()
            while not stop.is_set():
                await asyncio.sleep(0.02)
            server.close()
            await server.wait_closed()
            await db.close()

        asyncio.run(main())

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    try:
        assert started.wait(timeout=5), "daemon socket server did not start"
        result = CliRunner().invoke(cli, ["--socket", sock_path, "fleet", "status", "--json"])
    finally:
        stop.set()
        thread.join(timeout=5)

    assert result.exit_code == 0, result.output
    cli_body = _normalise(json.loads(result.output))
    api = _normalise(api_body["body"])
    # generated_at is a per-call timestamp; the daemon and the direct call differ by microseconds.
    for body in (cli_body, api):
        assert isinstance(body, dict)
        body.pop("generated_at", None)
    assert cli_body == api
