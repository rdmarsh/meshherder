# Codebase Suggestions

Generated after reviewing the repository structure, docs, source, examples, systemd unit, and
test suite.

## Current Baseline

- Rechecked 2026-05-21.
- `PYTHONPATH=src pytest -q` passes: 322 tests.
- `pytest -q` fails in a fresh shell because the `src/` package is not importable unless the
  project is installed or `PYTHONPATH=src` is set.
- `ruff check .` passes.
- `mypy src` passes.
- `scripts/` is currently untracked in git; `scripts/discover_factory.py` is included by `ruff`
  because `ruff check .` scans it.

## Recheck Notes - 2026-05-21

Fixed since the previous review:

- Quality gates are mostly fixed: `ruff check .` and `mypy src` now pass cleanly.
- Secret lookup in `WorkQueue._send_authed()` now checks the full DB node pubkey against
  `secrets.yaml` prefixes, and there is a regression test for longer secret prefixes.
- `config.yaml` now rejects an empty/non-mapping top level, unknown group references, duplicate
  node labels, duplicate node prefixes, invalid label characters, odd-length/non-hex node prefixes,
  and old node-level `radio:` keys.
- The CLI socket environment variable is now `MCHD_SOCKET`.

Still open or only partially fixed:

- Bare `pytest -q` still fails unless the package is installed or `PYTHONPATH=src` is set.
- Config validation is better, but section type checks and unknown-key checks are still incomplete.
  For example, a non-mapping `gateway:`/`messenger:` or non-list `nodes:` can still fail with
  implementation errors instead of a clear config error, and unknown keys other than node-level
  `radio:` are still accepted.
- The daemon and CLI now share command-safety matching for raw `mch exec` commands. Multi-word
  blocked commands such as `set prv.key`, `set guest.password`, and `start ota` are now matched as
  full-token prefixes.
- README DM command docs still promise `sync <pubkey-prefix>`, while `Messenger._handle_command()`
  still returns `sync: not supported over DM`.
- `mch hint` has not gained a `recommend` alias, and examples still mention `mch recommend`.
- Daemon command handlers still access `wq._contacts` and `wq._gateway` directly.
- Full-cycle `mch sync` still starts `wq.run_cycle()` as an untracked background task, so scheduler
  coordination and error visibility are still weak.
- Database migrations are still ad hoc (`ALTER TABLE nodes ADD COLUMN label ...`) rather than named
  versions.
- The profile URL cache still writes to `/var/lib/mch/profiles.yaml` unconditionally.
- The systemd unit still says commissioning writes `secrets.yaml` atomically and grants
  `/usr/local/etc/mch` write access, while the design/tests say commissioning prints snippets.

New findings from this recheck:

21. Normalize or reject uppercase pubkey prefixes consistently.

    `config.py` accepts uppercase hex in `pubkey_prefix`, but the error text says lowercase-only and
    most matching uses case-sensitive `startswith()` against contact/DB pubkeys. An uppercase config
    prefix can pass validation and then fail to match a lowercase MeshCore pubkey. Either normalize
    node and secret prefixes to lowercase on load or reject uppercase with a clear error.

22. Validate `secrets.yaml` prefixes like config node prefixes.

    `_load_secrets()` accepts any `nodes:` key, including odd-length, non-hex, or uppercase prefixes.
    Bad secret prefixes will silently miss during auth lookup. Apply the same even-length hex
    validation and normalization rules used for `config.yaml`, while preserving the special
    `default` key.

23. Make background sync failures observable.

    `mch sync` without a node starts `asyncio.create_task(wq.run_cycle())` and immediately returns
    `sync started`, but no task reference or done callback records exceptions. If the cycle fails,
    operators may only see an event-loop warning. Track the task, log failures with context, and
    expose whether a cycle is already running.

24. Make commissioning own node identity from first commission.

    **2026-05-22 status:** implemented and live-tested on Heltec V3 / v1.15.0. `commission new`
    now generates a MeshCore-compatible private key, derives the public key, sends `set prv.key`,
    includes `prv_key` in the secrets snippet, and verifies that the generated public key is visible
    after reboot. Fresh flash -> commission succeeded end-to-end.

    Original finding: `mch commission new` used the factory-generated pubkey in the config snippet
    and did not include `prv_key` in the secrets snippet. That meant future replacement could not be
    done without separately retrieving the private key over USB.

    Desired flow:
    - Generate a MeshCore-compatible private key locally during `commission new`.
    - Derive the corresponding public key and use that pubkey prefix in the config snippet.
    - Send `set prv.key <generated-key>` to the factory node over the mesh.
    - Include `prv_key: <generated-key>` in the secrets snippet.
    - Reboot/apply the key change as part of commissioning.

    Official MeshCore CLI docs list `set prv.key <private_key>` as not serial-only and note that it
    requires reboot to take effect. `get prv.key` remains serial-only, so mch should avoid relying
    on key retrieval.

25. Fix commissioning fire-and-forget semantics.

    **2026-05-22 status:** fixed with an important firmware-specific nuance. `reboot` is
    fire-and-forget. `set radio` is not fire-and-forget on v1.15-era firmware; it returns
    `OK - reboot to apply`, so commissioning waits for that response before sending the no-wait
    reboot. This avoids racing the radio command and reboot.

26. Do not send redundant reboot after `set radio`.

    **2026-05-22 status:** superseded by live testing. On Heltec V3 / v1.15.0, `set radio` does not
    itself reboot; it replies `OK - reboot to apply`. The correct behavior is to wait for `set radio`
    and then send one fire-and-forget `reboot`.

30. Document the bench reset/reflash commands.

    A separate "reset node to factory" command is not needed yet. A short operator note with the
    explicit `esptool` commands is enough for repeat commissioning and replacement tests:

    ```bash
    esptool --chip esp32s3 --port /dev/cu.usbserial-0001 --baud 460800 erase-flash
    esptool --chip esp32s3 --port /dev/cu.usbserial-0001 --baud 460800 write-flash 0x0 /path/to/Heltec_v3_repeater-v1.15.0-dee3e26-merged.bin
    ```

    Keep this outside normal sync and commissioning automation.

31. Prefer `mch status <node>` over `mch node <node>`.

    Node detail UX should converge on `mch status <label|prefix>`. Existing `mch node` references
    in docs and code should either be removed, redirected, or treated as a legacy alias.

27. Split reachability radio from desired node radio.

    `NodeConfig.via` currently acts as both "how the gateway reaches this node" and "what radio
    config replacement commissioning should push to the node." That is too overloaded for
    bridge-mediated management, where the gateway may stay on one frequency while reaching a node
    on another through the mesh. Consider separate concepts:
    - `via`: gateway/routing reachability hint.
    - desired `radio`/`settings.radio`: node configuration to enforce or apply during commission.

28. Track future DM/channel/room-server control separately from fleet convergence.

    The current messenger supports small DM status/read commands, but docs still promise DM sync
    while implementation rejects it. Longer-term ideas include:
    - DM-triggered sync for already-managed nodes.
    - Read/write status to a configured channel.
    - Room-server integration for reports or command intake.

    Keep this as a separate operator interface layer so it does not complicate the core
    desired-state sync loop.

29. Consider vanity public key generation after commissioning is stable.

    Some operators like node public keys with recognizable prefixes, such as `DEADBEEF`. MeshCore
    keys can be generated until the derived public key matches a requested prefix, but this is CPU
    work and should be optional. Do not add it until normal generated-key commissioning and
    replacement mode are reliable.

## Highest Priority

1. Make the advertised quality gates actually pass.

   **2026-05-21 status:** partially fixed. `ruff check .`, `mypy src`, and
   `PYTHONPATH=src pytest -q` pass. Bare `pytest -q` still fails unless the package is installed or
   `PYTHONPATH=src` is set.

   `pyproject.toml` configures strict `mypy` and a `ruff` rule set. These now pass, but bare
   `pytest` still does not. Keep the configured checks enforceable in CI so developer reality does
   not drift from project intent.

   Good first steps:
   - Add `pythonpath = ["src"]` or use editable installs in test docs/CI so bare `pytest` works.
   - Run `ruff check --fix` for mechanical import and unused-code fixes, then handle line length and
     bugbear warnings manually.
   - Add explicit ignores for untyped third-party `meshcore` imports, or provide local stubs.

2. Fix likely type-shadowing bugs in daemon dispatch.

   **2026-05-21 status:** fixed for the reported type error. `mypy src` now passes, and the `exec`
   branch uses `cmd_result`.

   Previously, `src/mch/daemon.py` reused the same local variable name `result` across different
   command branches and `mypy` reported incompatible assignment around the `sync` and `exec`
   branches. Even though that specific finding is fixed, the broad `_dispatch` function is becoming
   hard for static analysis and humans to reason about.

   Suggested change: split each command branch into a small `_dispatch_status`, `_dispatch_exec`,
   `_dispatch_issues`, etc. helper and use specific names like `node_result` and `command_result`.

3. Fix secret lookup by full node pubkey, not config prefix direction.

   **2026-05-21 status:** fixed. `_send_authed()` now uses `node.pubkey.startswith(prefix)` and
   tests cover a secret prefix longer than the config prefix.

   Previously, `WorkQueue._send_authed()` selected per-node secrets with:

   ```python
   node_cfg.pubkey_prefix.startswith(prefix)
   ```

   That only worked when the secret key was shorter than or equal to the config prefix. Elsewhere
   the code usually checks the full node pubkey against configured prefixes. The current code now
   uses:

   ```python
   node.pubkey.startswith(prefix)
   ```

   This better supports a mix of 8, 12, or longer prefixes in `secrets.yaml`.

4. Validate `config.yaml` more aggressively.

   **2026-05-21 status:** partially fixed. Top-level mapping checks, node `radio:` rejection,
   prefix hex/even-length checks, duplicate label/prefix checks, label validation, and unknown group
   validation are present. Section type checks, unknown-key checks, minimum prefix length, and
   lowercase normalization/rejection are still open.

   `load()` now checks that the top-level YAML is a mapping and rejects old node-level `radio:`.
   More validation is still needed for section types and unknown keys.

   Suggested validation:
   - Ensure top-level config is a mapping.
   - Validate `nodes`, `groups`, `gateway`, `scheduler`, and `messenger` types.
   - Validate pubkey prefixes as even-length hex, minimum 8 chars.
   - Reject or warn on unknown keys, especially `nodes[].radio`.
   - Validate group references so typos do not silently drop inherited settings.

## Product / Operator Experience

5. Implement the known lifecycle gaps from `DESIGN.md`.

   The most useful next CLI additions are:
   - `mch forget <node>` to remove a DB ghost after a node is decommissioned.
   - Config/secrets hot reload via SIGHUP or a socket command.
   - Commissioning idempotency checks that reject nodes already present in config or DB.

6. Reconcile DM command docs with implementation.

   The README says a field operator can send `sync <pubkey>` by DM, but
   `Messenger._handle_command()` currently returns `sync: not supported over DM`. Either implement
   DM-triggered sync through the work queue with the same authorization model, or update the README
   and feature list to avoid promising it.

7. Rename or alias `mch hint` consistently.

   Docs and examples still refer to "recommend" in places (`profiles_file` comments and the module
   name), while the CLI command is `hint`. Consider adding `mch recommend` as an alias or standardize
   all docs around `hint`.

8. Fix the CLI socket environment variable name.

   **2026-05-21 status:** fixed. `src/mch/cli.py` now uses `MCHD_SOCKET`.

   `src/mch/cli.py` previously used `MCHERD_SOCKET`. It now uses `MCHD_SOCKET`.

## Robustness

9. Avoid direct access to WorkQueue internals from daemon command handlers.

   `_dispatch()` reaches into `wq._contacts` and `wq._gateway`. That makes command behavior depend
   on private state and cached contacts. Add small public methods on `WorkQueue`, such as
   `exec_command()`, `contacts()`, and `force_cycle()`, so synchronization and contact refresh rules
   live in one place.

10. Serialize manual full-cycle sync with the scheduler.

   `mch sync` without a node starts `wq.run_cycle()` in the background while the scheduler loop may
   also be active. There is a send lock in `Gateway`, but higher-level node state, radio hopping,
   and scheduling can still interleave. A queue-level operation lock would make scheduled sync,
   forced sync, exec, and commission coordination explicit.

11. Tighten command safety checks.

   **2026-05-21 status:** fixed for `mch exec`. CLI and daemon exec paths now use shared
   full-token prefix matching for blocked, confirmation-required, and fire-and-forget command
   classes. Tests cover multi-word blocked commands and multi-word confirmation prompts.

   Previously, blocked/destructive command matching used only the first word for most commands, so
   multi-word commands like `set prv.key` and `start ota` were not reliably matched by the
   daemon-side `cmd_root` check. Normalize and match command prefixes consistently in both CLI and
   daemon.

12. Add database migrations as named versions.

   `Database.open()` currently applies schema and then attempts a one-off `ALTER TABLE` for `label`.
   As more fields land, use a `schema_version` table with ordered migrations. That will keep upgrades
   deterministic and make downgrade/recovery guidance easier.

13. Improve URL profile cache behavior.

   `recommend.py` writes to `/var/lib/mch/profiles.yaml` unconditionally. That path is fine for the
   service, but awkward for development and tests. Consider deriving the cache path from config or
   the DB directory, and validate downloaded YAML before replacing a known-good cache.

## Maintainability

14. Break up the largest modules.

   `cli.py` is about 984 lines and `queue.py` is about 803 lines. Both contain several separable
   responsibilities. Suggested splits:
   - `cli_render.py` for table/detail rendering.
   - `cli_commission.py` for the interactive wizard.
   - `scheduler.py` for due-node scheduling.
   - `node_processor.py` for facts, settings, auth, and operations.

15. Reduce duplicated issue/status assembly.

   The status and issues branches in `daemon.py` duplicate drift, bad-state, ghost, and open-node
   construction. Extract a shared `build_issue_report(db, config)` helper and test it directly.

16. Make type aliases for MeshCore contact dictionaries.

   Many functions accept or return unstructured `dict[str, Any]` contacts. Add a `TypedDict` for
   the subset this project uses (`public_key`, `adv_name`, `type`, `flags`, radio path fields). This
   would remove many mypy errors and make gateway/queue boundaries clearer.

17. Keep examples aligned with implementation.

   The systemd unit comments say commissioning writes `secrets.yaml` atomically, but the design and
   current code say commissioning only prints snippets. Update the unit comment and consider removing
   `/usr/local/etc/mch` from `ReadWritePaths` if the daemon truly remains config-read-only.

## Testing Suggestions

18. Add tests for config validation failures.

   Cover empty config, list top-level config, invalid pubkey prefixes, unknown group references,
   unknown node keys, and the old `radio:` node key. This will make startup errors friendlier and
   prevent silent misconfiguration.

19. Add concurrency tests around scheduler/manual sync/commission.

   There are good unit tests for each component, but the riskier behavior is coordination: a
   scheduled node sync overlapping with manual `mch sync`, `mch exec`, or commissioning radio hops.
   Add tests that assert only one high-level gateway operation can own the radio/session at a time.

20. Add a small CI workflow.

   A minimal CI check should run:

   ```bash
   python -m pip install -e ".[dev]"
   pytest -q
   ruff check .
   mypy src
   ```

   Once CI is green, keep it required before merging. The test suite is already fast enough for this.
