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

- Bare `pytest` now imports from `src` via pytest config.
- Config validation now catches section type errors, unknown keys, short prefixes, and normalizes
  uppercase config/secrets prefixes.
- The daemon and CLI now share command-safety matching for raw `mch exec` commands. Multi-word
  blocked commands such as `set prv.key`, `set guest.password`, and `start ota` are now matched as
  full-token prefixes.
- README DM command docs now describe status/read commands instead of promising DM-triggered sync.
- `mch suggest` is the single command for suggested settings.
- Daemon dispatch and commissioning no longer access `wq._contacts` or `wq._gateway` directly.
- Gateway-owning CLI operations now use a shared high-level operation lock and return `busy: ...`
  when sync/exec/commission would overlap.
- Database migrations are still ad hoc (`ALTER TABLE nodes ADD COLUMN label ...`) rather than named
  versions.
- The profile URL cache still writes to `/var/lib/meshherder/profiles.yaml` unconditionally.
- The systemd unit now keeps `/etc/meshherder` read-only and says config files are
  operator-owned.

New findings from this recheck:

21. Normalize or reject uppercase pubkey prefixes consistently.

    **2026-05-22 status:** fixed. Config node prefixes, secrets prefixes, and messenger allow-list
    prefixes are normalized to lowercase on load.

22. Validate `secrets.yaml` prefixes like config node prefixes.

    **2026-05-22 status:** fixed. `secrets.yaml` preserves the special `default` key, validates
    per-node prefixes as even-length hex with at least 8 characters, and normalizes them to
    lowercase.

23. Make background sync failures observable.

    **2026-05-22 status:** fixed. `mch sync` without a node now calls `WorkQueue.start_cycle()`,
    which owns the task, rejects duplicate manual cycles, logs completion, and logs failures with
    context.

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

33. Investigate replacement retry after partial commissioning failure.

    **Observed on luna, 2026-05-22 19:57:** `mch commission` replacement found factory node
    `fa5a4515607e`, logged in with the factory password, sent password/key setup commands, then
    timed out waiting for `set radio 915.800,250.0,10,5`. Retrying immediately found the same node
    but failed factory authentication:

    ```text
    Can't complete commissioning: commission failed at 'set radio 915.800,250.0,10,5': timeout
    ...
    Retrying...
    Can't complete commissioning: could not authenticate to factory node
    ```

    Likely hypothesis: the first attempt partially succeeded and changed the node's admin password
    and/or private key before timing out on radio. The retry still treats the visible node as
    factory-fresh and tries the default password, so authentication fails. Investigate whether retry
    should:

    - Try the just-applied managed password when factory login fails after a partial attempt.
    - Track the commissioning stage and resume from the failed command instead of restarting from
      factory assumptions.
    - Distinguish `set radio` timeout caused by no response from a command that actually applied.
    - Give the operator explicit recovery guidance: reboot/wait, retry with managed credentials, or
      erase/reflash for a true factory reset.

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

32. Clean up commissioning CLI text and consider explicit subcommands.

    Live testing showed `mch commission` works, but the wizard copy is still a little confusing.
    Add a focused UX pass before broadening replacement testing:

    - Consider `mch commission new` and `mch commission replace` so operators can choose the
      workflow before discovery/menu prompts.
    - Keep bare `mch commission` as the interactive wizard if useful.
    - Avoid top-level `mch new` / `mch replace` unless the project is comfortable making
      commissioning the dominant command namespace.
    - Rename menu labels from `New node` / `Replace node` to clearer action phrases such as
      `Commission fresh node` and `Restore existing identity`.
    - Make sentinel prompts explicit, e.g. `Group label [- for none]`.
    - Update success next steps to mention both inspection and action:
      `mch status <prefix>` to inspect, `mch sync <prefix>` to force convergence.

27. Split reachability radio from desired node radio.

    `NodeConfig.via` currently acts as both "how the gateway reaches this node" and "what radio
    config replacement commissioning should push to the node." That is too overloaded for
    bridge-mediated management, where the gateway may stay on one frequency while reaching a node
    on another through the mesh. Consider separate concepts:
    - `via`: gateway/routing reachability path.
    - desired `radio`/`settings.radio`: node configuration to enforce or apply during commission.

28. Track future DM/channel/room-server control separately from fleet convergence.

    The current messenger supports small DM status/read commands. Longer-term ideas include:
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

   **2026-05-22 status:** fixed. `pytest` imports from `src` via pytest config, and CI runs the
   advertised checks.

   `pyproject.toml` configures strict `mypy`, `ruff`, and pytest source-path behavior. CI keeps
   these checks enforceable.

   Checks:
   - `pytest`
   - `ruff check .`
   - `mypy src`

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

   **2026-05-22 status:** fixed for current schema. Top-level mapping checks, section type checks,
   unknown-key rejection, node `radio:` rejection, prefix hex/even-length/minimum-length checks,
   duplicate label/prefix checks, label validation, lowercase prefix normalization, secrets prefix
   validation, and unknown group validation are present.

   `load()` now fails fast with clear `ValueError` messages for malformed YAML structure and common
   typos.

## Product / Operator Experience

5. Make read-only/audit the default safety posture before wider use.

   Done:
   - `resolve_policy()` now falls back to `AUDIT` when a setting has no explicit policy and no
     node/group default.
   - Desired settings without an explicit enforce policy now report drift but do not write.

   Remaining direction:
   - Keep dangerous identity/radio/reset commands outside normal settings unless they are part of a
     deliberate commissioning or replacement flow.

   Done:
   - Added `mch plan` as a read-only desired-vs-known view. It shows enforced writes, audit drift,
     and values that need observation before a write can be predicted.

   This fits the desired-vs-known model: YAML declares desired state, SQLite records known/observed
   state, and writes should only happen where the operator opted in.

6. Implement the known lifecycle gaps from `DESIGN.md`.

   The most useful next CLI additions are:
   - `mch forget <node>` to remove a DB ghost after a node is decommissioned.
   - Commissioning idempotency checks that reject nodes already present in config or DB.

   Done:
   - Config/secrets hot reload via file polling and SIGHUP. A socket/CLI `mch reload` command may
     still be useful later for operator feedback, but daemon restart is no longer required for
     normal node/group/secrets edits.

7. Keep DM control read-only unless there is a clear operator need.

   **2026-05-22 status:** README now documents the implemented read-only/status commands:
   `status`, `nodes`, `node <name-or-prefix>`, `issues`, and `logs [n]`.

   DM-triggered sync can be added later through the work queue with the same authorization and
   scheduler coordination model, but it should stay out until manual/scheduled sync ownership is
   clearer.

8. Rename or alias `mch suggest` consistently.

   **2026-05-22 status:** done. `mch suggest` is the single command for suggested settings.

8. Fix the CLI socket environment variable name.

   **2026-05-21 status:** fixed. `src/mch/cli.py` now uses `MCHD_SOCKET`.

   `src/mch/cli.py` previously used `MCHERD_SOCKET`. It now uses `MCHD_SOCKET`.

## Robustness

9. Avoid direct access to WorkQueue internals from daemon command handlers.

   **2026-05-22 status:** fixed for daemon dispatch. Raw exec now goes through
   `WorkQueue.exec_command()`, including contact lookup, operation locking, gateway send, and
   fire-and-forget handling. Contacts lookup now goes through `WorkQueue.unfiled_contacts()`.

10. Serialize manual full-cycle sync with the scheduler.

   **2026-05-22 status:** fixed. Scheduled node sync, manual full-cycle sync, forced
   single-node sync, raw exec, and commissioning now share a high-level WorkQueue operation lock.
   CLI-owned operations return `busy: <operation>` rather than silently overlapping.

   Commissioning enters the gateway through `WorkQueue.commission_session()`, which pauses scheduled
   work while the commissioning workflow owns the gateway.

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

   `recommend.py` writes to `/var/lib/meshherder/profiles.yaml` unconditionally. That path is fine for the
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

   Done:
   - The systemd unit no longer grants write access to `/etc/meshherder`.
   - Comments now state that mchd reads config files but never writes `config.yaml` or
     `secrets.yaml`.

## Testing Suggestions

18. Add tests for config validation failures.

   **2026-05-22 status:** done for current schema.

   Cover empty config, list top-level config, invalid pubkey prefixes, unknown group references,
   unknown node keys, and the old `radio:` node key. This will make startup errors friendlier and
   prevent silent misconfiguration.

19. Add concurrency tests around scheduler/manual sync/commission.

   There are good unit tests for each component, but the riskier behavior is coordination: a
   scheduled node sync overlapping with manual `mch sync`, `mch exec`, or commissioning radio hops.
   Add tests that assert only one high-level gateway operation can own the radio/session at a time.

20. Add a small CI workflow.

   **2026-05-22 status:** done.

   A minimal CI check should run:

   ```bash
   python -m pip install -e ".[dev]"
   pytest -q
   ruff check .
   mypy src
   ```

   Once CI is green, keep it required before merging. The test suite is already fast enough for this.
