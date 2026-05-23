# Mesh Herder Backlog

This is the canonical working backlog for Mesh Herder. Use it for todo items, wishlist items,
deferred ideas, and explicit non-goals. Keep `JOURNAL.md` for dated decisions and lessons learned;
keep `DESIGN.md` for stable architecture notes.

## How to Use This File

- Prefer simple, boring solutions over clever or expansive ones.
- Treat "we could do this" as insufficient. New work should have a clear operator benefit,
  reasonable implementation cost, and low ongoing maintenance burden.
- Avoid fragile high-maintenance code. If a change depends on non-obvious ordering, firmware
  behavior, or safety constraints, leave a concise "why" comment or journal note so the reasoning is
  not rediscovered later.
- Keep speculative ideas in **Wishlist** or **Not Yet** until they solve a real problem.
- Apply the same filter to code review follow-ups: fix real bugs and maintainability risks first,
  and avoid turning nice-to-have ideas into required work.
- Add new work to **Inbox** first if priority or scope is unclear.
- Move committed near-term work to **Next**.
- Move broader improvements to **Later** or **Wishlist**.
- Move rejected or intentionally deferred ideas to **Won't Do / Not Yet** with the reason.
- When work lands, move it to **Done Recently** with the date.

## Inbox

- Decide whether `mch commission new` and `mch commission replace` should become explicit
  subcommands, with bare `mch commission` kept as an interactive wizard.
- Decide where field metadata should live for validating MeshCore `set` values: local command
  metadata, firmware docs-derived data, or a small hand-maintained schema.
- Decide whether a future config section should own status thresholds, for example
  `status.lost_after_days`, or whether this belongs under `scheduler`.

## Next

- Investigate replacement commissioning retry after partial failure.
  - Observed 2026-05-22: replacement commissioning changed some state, then timed out on
    `set radio`; immediate retry could no longer authenticate with factory credentials.
  - Try the just-applied managed password when factory login fails after a partial attempt.
  - Track commissioning stage closely enough to resume or give precise recovery guidance.
  - Distinguish a command timeout from a command that likely applied but lost its response.

- Finish retired/stale node lifecycle handling.
  - `mch prune` already lists and can delete DB ghosts.
  - Add stale/lost status for nodes not seen after a configurable threshold, with compact display
    such as `>99d ago`.

- Improve commissioning prompt semantics.
  - Keep Enter for accepting defaults.
  - Replace or clarify `-` sentinel values for "none" or "clear".
  - Ensure generated snippets never persist placeholder sentinel values.
  - Update success text to mention both inspection and action:
    `mch status <prefix>` and `mch sync <prefix>`.

- Add field-aware validation before sending values to nodes.
  - Reject non-numeric values for numeric fields.
  - Reject out-of-range numbers.
  - Reject malformed `set` payloads before they reach the mesh.
  - Apply the same rules to config/secrets validation where relevant.

- Add concurrency tests around scheduler/manual sync/exec/commission ownership.
  - Assert only one high-level gateway operation can own the radio/session at a time.
  - Cover scheduled sync, manual full sync, forced node sync, raw exec, and commissioning.

- Add lightweight per-sync outcome counters.
  - Track how many nodes were contacted in a cycle.
  - Track how many update commands were sent.
  - Track how many values were actually changed.
  - Keep this simple: useful status/log output and DB history, not a full metrics subsystem.

## Later

- Split reachability radio from desired node radio.
  - `NodeConfig.via` currently describes both how the gateway reaches a node and what radio config
    replacement commissioning should push.
  - Keep `via` for gateway/routing reachability.
  - Add a distinct desired node radio model before bridge-mediated management gets more complex.

- Add named database migrations.
  - Replace one-off `ALTER TABLE` behavior with ordered migrations and a schema version table.
  - Make upgrades deterministic and easier to reason about.

- Improve URL profile cache behavior.
  - Avoid writing to `/var/lib/meshherder/profiles.yaml` unconditionally in development/tests.
  - Derive the cache path from config or DB directory.
  - Validate downloaded YAML before replacing a known-good cache.

- Break up large modules.
  - `cli.py`: split rendering and commissioning wizard concerns.
  - `queue.py`: split scheduling, node processing, facts/settings, auth, and operations.

- Reduce duplicated issue/status assembly.
  - Extract shared issue-report construction for drift, bad-state, ghost, lost, and open-node
    reporting.
  - Test it directly.

- Add typed MeshCore contact dictionaries.
  - Introduce a `TypedDict` for the contact fields this project uses, including `public_key`,
    `adv_name`, `type`, `flags`, and radio path fields.

- Support derived desired values from observed state.
  - Concrete example: if uptime is less than 48 hours, set zero-hop advert interval to 3 hours;
    otherwise turn it off.
  - More generally, allow settings to depend on facts such as uptime, role, or other observed data.
  - Keep this separate from ordinary static desired-state enforcement.

- Continue `mch status <node>` convergence.
  - Treat `mch node <node>` wording as legacy.
  - Remove or redirect older docs/code references over time.

- Polish `mch validate` output ordering.
  - The current behavior is useful, but the display is a little awkward: the node/group count sits
    between config and secrets status, and it mentions secrets before confirming the secrets file.
  - Revisit after core validation behavior settles.

- Revisit retired-node command naming.
  - `mch remove <node>` works, but `mch prune <node>` might be a better fit as the single-node form
    of `mch prune`.
  - If changed, keep `mch remove` as an alias or compatibility path until the CLI settles.

## Wishlist

- Support alternate config/secrets locations end-to-end.
  - Let `mch validate` check a working copy in a different location, such as a home directory,
    before copying it into production.
  - Let `mchd` read config and secrets from operator-chosen paths instead of assuming `/etc/meshherder`.
  - Keep the current production layout as the default, but stop hardcoding it where flexibility
    helps safer edits.

- DM-triggered sync for already-managed nodes, using the same authorization and operation-lock
  model as existing CLI-triggered sync.
- Channel or room-server reporting for fleet status.
- Web UI with node map, using stored lat/lon facts.
- Firmware age reporting from collected `ver` facts.
- Docker image once packaging and runtime paths settle.
- Bluetooth connection support through `meshcore_py.create_ble`.
- Optional vanity public-key generation after normal commissioning and replacement are reliable.

## Research / Live-Test

- Test `tempradio` followed by `set radio` as a possible no-reboot commissioning radio switch.
  - Current confirmed behavior on Heltec V3 / MeshCore v1.15-era firmware: `set radio` returns
    `OK - reboot to apply`; commissioning should wait for that response and then send one reboot.
  - Do not change commissioning behavior until current firmware has been live-tested.

## Won't Do / Not Yet

- Do not auto-edit `config.yaml` or `secrets.yaml` from `mchd`.
  - Config and secrets are operator-managed source-of-truth files.
  - Commissioning should print paste-ready snippets only.

- Do not add a first-class `mch reset` command yet.
  - Bench reset/reflash can stay as documentation with explicit `esptool` commands.
  - Reset automation can be reconsidered only if it becomes a normal operator workflow.

- Do not broaden DM control into writes by default.
  - Current DM commands should remain read-only/status-oriented unless a clear operator need
    justifies write actions.

- Do not put dangerous identity/radio/reset commands into ordinary desired-state enforcement.
  - Keep those in deliberate commissioning or replacement flows.

- Do not implement vanity public-key generation until generated-key commissioning and replacement
  are both stable.

## Done Recently

- 2026-05-23: Added a static `mchd(8)` man page for daemon operation and troubleshooting.
- 2026-05-23: Added a static `mch(1)` man page and README install instructions.
- 2026-05-23: Added `mch remove <node>` for deleting one node's local DB state, with `--force` for
  nodes still present in config.
- 2026-05-23: Added `mch validate` to check config/secrets locally without contacting `mchd`.
- 2026-05-22: Added `mch plan` as a read-only desired-vs-known view.
- 2026-05-22: Removed config-file write access from the daemon/systemd model.
- 2026-05-22: Implemented config/secrets hot reload with last-known-good behavior.
- 2026-05-22: Live-tested fresh-flash commissioning with generated private key and identity
  verification.
- 2026-05-22: Fixed commissioning command ordering for `set radio` plus one fire-and-forget
  reboot.
- 2026-05-22: Added `mch prune` for DB nodes no longer present in `config.yaml`.
- 2026-05-22: Normalized config, secrets, and messenger pubkey prefixes to lowercase.
- 2026-05-22: Validated `secrets.yaml` prefixes like config node prefixes.
- 2026-05-22: Made background manual sync failures observable.
- 2026-05-22: Made `mch suggest` the single suggested-settings command.
- 2026-05-22: Added CI for `pytest`, `ruff check .`, and `mypy src`.
- 2026-05-21: Fixed `MCHD_SOCKET` environment variable naming.
- 2026-05-21: Fixed secret lookup to match secrets prefixes against the full node pubkey.
- 2026-05-21: Tightened command safety matching for raw `mch exec`.
- 2026-05-21: Made the default setting policy `audit`.
