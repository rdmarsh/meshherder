# Project Journal

This journal records project decisions and lessons learned without preserving private bench
details, node identities, generated private keys, local hostnames, or operator passwords.
Use `BACKLOG.md` for active todo items, wishlist items, deferred ideas, and explicit non-goals.

## 2026-05-21 - Commissioning Identity Model

- `mch commission new` should not keep a factory-generated node identity.
- Mesh Herder should generate a MeshCore-compatible private key locally, derive the public key,
  send `set prv.key <generated-key>` during commissioning, and print:
  - `config.yaml`: derived `pubkey_prefix`.
  - `secrets.yaml`: generated `prv_key`.
- This avoids later USB-only private-key retrieval and makes replacement mode viable.
- Vanity public-key generation is parked until normal commissioning and replacement are stable.

## 2026-05-21 - Commissioning Command Ordering

- Live tests on Heltec V3 / MeshCore v1.15-era firmware showed that session-invalidating changes
  should be sent late in the commissioning flow.
- Commissioning now treats CLI-looking error responses as failures and redacts passwords/private
  keys from errors.
- `set radio` returns a normal response indicating that reboot is needed; it should not be
  fire-and-forget.
- The final `reboot` remains fire-and-forget.

## 2026-05-21 - Commissioning Verification

- `set prv.key` can appear to succeed before the new identity is visible over the mesh.
- `commission new` verifies the expected generated public key after reboot/radio change before
  returning success.
- Discovery verification needs retry logic because the rebooted node may not be visible
  immediately and another node on the mesh can answer first.

## 2026-05-22 - Fresh Flash Commission Success

- Fresh-flash commissioning was live-tested end-to-end on Heltec V3 / MeshCore v1.15-era firmware.
- The successful flow:
  - Discover factory node.
  - Login with factory credentials.
  - Set normal node settings.
  - Set admin password and generated private key late.
  - Set target radio, reboot, and verify generated identity after retry.
- The resulting snippets are printed for operator review rather than written by the daemon.

## 2026-05-22 - Post-Commission Documentation Decisions

- `config.yaml` and `secrets.yaml` remain operator-managed source-of-truth files.
- `mch commission new` prints paste-ready snippets instead of editing files automatically.
- A separate `mch reset` command is not needed yet; bench reflash commands can stay as
  development documentation.
- Prefer `mch status <node>` for node detail views. Treat `mch node <node>` wording as legacy.

## 2026-05-22 - Commission Wizard UX Notes

- `mch commission` works, but the copy needs a pass before wider use.
- Candidate CLI shape:
  - Keep bare `mch commission` as the interactive wizard.
  - Add explicit subcommands later: `mch commission new` and `mch commission replace`.
- Suggested wording:
  - "New node" -> "Commission fresh node".
  - "Replace node" -> "Restore existing identity".
  - Use explicit sentinel prompts such as `Group label [- for none]`.
  - After successful commissioning, say: add snippets, restart/reload `mchd`, then run
    `mch status <prefix>` to inspect or `mch sync <prefix>` to force convergence.

## 2026-05-22 - Config Reload Direction

- YAML remains desired state.
- SQLite remains observed/cache/history state.
- `mchd` detects YAML file changes and reloads desired state without an operator restart.
- On valid reload, the daemon updates the in-memory config, messenger settings, gateway contacts,
  and DB label/group metadata.
- On invalid YAML or validation failure, the daemon logs the error and keeps the last valid config.

## 2026-05-22 - Name Display After Sync

- `LABEL` is local-only desired metadata from `config.yaml`.
- `MESH NAME` is observed MeshCore state from the node/contact store.
- A config edit alone should not change the observed mesh name; a successful sync must apply
  `settings.name.desired`.

## 2026-05-22 - MeshCore CLI References

- Keep these references handy when validating command semantics:
  - https://docs.meshcore.io/cli_commands/
  - https://github.com/meshcore-dev/MeshCore/wiki/Repeater-&-Room-Server-CLI-Reference/83961784b86ef23a55ca7cf621bd9381efa9f535
- Important units/details confirmed from the wiki reference:
  - `set advert.interval {minutes}`
  - `set flood.advert.interval {hours}`
  - `set radio {freq},{bw},{sf},{cr}` requires reboot to apply
  - `region put` and `region save` are raw region-management commands, not `set <key> <value>`
    settings
  - For MeshCore v1.15.0 and newer, `region allowf` is no longer required after `region put`
- After successful `set name`, the DB node name is updated immediately to avoid stale display.

## 2026-05-22 - Desired vs Known Safety Model

- YAML is desired state.
- SQLite is known state: observations, command results, history, and schedule.
- The default policy is `audit`, not `enforce`; writes require explicit `policy: enforce` or
  `default_policy: enforce`.
- `mch plan` provides a read-only desired-vs-known view without touching the mesh.

## 2026-05-22 - Config File Write Access Removed

- `mchd` does not write `config.yaml` or `secrets.yaml`.
- Commissioning returns paste-ready snippets only.
- The systemd unit grants write access to runtime/database paths, not the operator-owned config
  directory.

## 2026-05-22 - Plan Command

- Added `mch plan` as a read-only desired-vs-known view.
- It uses the daemon's loaded config/secrets and known DB values.
- Output groups:
  - Planned writes: explicit/enherited `enforce`, known drift, concrete next `set` command.
  - Audit drift: known drift under `audit`, explicitly no write.
  - Needs observation: desired values where the DB has no known value yet.
