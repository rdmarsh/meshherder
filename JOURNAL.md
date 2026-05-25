# Project Journal

This journal records project decisions and lessons learned without preserving private bench
details, node identities, generated private keys, local hostnames, or operator passwords.
Use `BACKLOG.md` for active todo items, wishlist items, deferred ideas, and explicit non-goals.

## 2026-05-25 - Fleet Report API Foundation

- Released as `0.7.0`; exposing a new HTTP API and central reporting contract is a minor-version
  capability change, not an invisible patch to `0.6.1`.
- Added a single normalized read-only fleet/node report model with `schema_version: 1`.
- The model classifies node health, setting status (`ok`, `nominal`, `drift`, `unknown`,
  `ignored`) and next action (`none`, `read`, `write`, `report`, `ignore`), retaining collected
  facts such as `lat` and `lon` for later map presentation.
- Existing status, plan, node-detail, and issues RPC payloads are compatibility projections from
  the model; display-only CLI `status` and `plan` now request the fleet model directly.
- Added an optional HTTP JSON transport for read-only local consumers. It binds only to loopback
  addresses until authentication is designed and includes no write endpoints.
- Revised the public HTTP route vocabulary to match the existing read-only CLI commands:
  `/health`, `/status`, `/status/<node>`, `/issues`, `/plan`, and `/logs`. Removed the draft
  `/api/v1/...` and `/healthz` paths because the API is still under active development.
- Field use exposed the next API refinement: `/status` should become a small, pollable
  dashboard/map summary, including health and coordinates, while `/status/<node>` supplies
  clickable full details and `/logs` remains separately refreshed.
- Retained the YAML/secrets/observed-DB ownership boundary for future UI work. API-driven clients
  do not require storing credentials or private identities in SQLite; future configuration
  editing must provide authentication, reviewable diffs, auditing, and explicit protected
  import/export handling.
- Clarified that collected settings are factual observations but remain distinct from facts in
  the model because they also carry desired values, policy, drift classification, and actions.
- Identified avoidable mesh load when the same key is configured as both a fact and a setting:
  the current sync path reads it in both passes. Future work should read once per cycle and fan
  that observation out to both semantic views.
- Captured the future messenger direction: explicit send/receive configuration, channel
  selection, and operator-reviewed pending sender requests without confusing DM authorization
  with managed-node login permissions.

## 2026-05-25 - Verbose Forced Sync And Operational Logs

- Released as `0.7.1`: `mch sync --verbose <node>` streams per-command progress while a
  foreground sync waits on slow or unreachable mesh responses.
- Fact and setting read failures are now persisted as node activity so `mch logs <node>` explains
  a partial sync without requiring access to `journalctl`. The journal remains the daemon/service
  diagnostic stream; systemd process state is intentionally not mixed into mesh status.
- `TRIES` means enforced write attempts. A setting read timeout never reaches comparison/write
  and correctly does not increment that value.
- The observed `mch contacts acai` miss was not a case-sensitivity defect: contact-name matching
  already ignores case, but `acai` is not a contiguous substring of `Acacia`.
- Kept a full messaging inbox outside project scope; messenger work remains focused on operational
  commands, notifications, and explicit authorization review.

## 2026-05-24 - Status view improvements and passive last_seen

**`clock` bare-command fix**: `_collect_facts()` was sending `get clock` instead of bare
`clock`, causing nodes to return `??: clock`. Added `"clock"` to the bare-command tuple
alongside `ver`, `board`, and `neighbors`.

**last_seen vs last_connected now distinct**: `last_seen` updates passively via an
`ADVERTISEMENT` event subscription set up at daemon startup — any time a managed node
broadcasts an advert on the mesh, its `last_seen` is updated immediately without waiting
for the next sync. `last_connected` still only updates when the work queue successfully
exchanges commands with the node. Both fields now show as full datetime + relative time
in the node detail view (`2026-05-24 00:22:00 (  2m ago)`).

**Settings deception fixed**: `mch status <node>` detail view now merges desired config
settings (from both node and group config) with DB observations. New settings added to
config show immediately with their desired values and `-` for observed — clearly pending
rather than invisible. Previously, only settings that had been synced at least once
appeared, making `state: configured` look complete when new config had never been applied.

**Settings table column widths**: now computed from content display width using
`_wlen()` / `_wpad()` (via `unicodedata.east_asian_width`), so emoji-containing values
(e.g. `🦷 Quakers Hill`) no longer push subsequent columns out of alignment. The drift
indicator column header `~` is only shown when at least one row is actually drifted.

**Radio debug logging**: added `log.debug("set_radio: ...")` before every
`mc.commands.set_radio()` call in `hop_radio`, `restore_radio`, and `set_radio` in
`gateway.py`. Visible at DEBUG log level without cluttering production INFO logs.

## 2026-05-23 - Clock sync, settings vs operations, and powersaving fix

**Clock sync mechanism confirmed**: `send_cmd` (meshcore_py) embeds `int(time.time())` from the
host in every command packet. When a managed node receives `clock sync` over the mesh, it can use
that embedded timestamp to set its clock — so time accuracy comes from the Mac's NTP clock, not
the gateway node's own clock. The `clock sync` operation in `config.yaml` (every 6 hours) is the
correct approach.

**Settings vs operations**: Settings use `get <key>` / `set <key> <value>` with drift detection;
operations are fire-and-forget scheduled commands. Region commands (`region put`, `region save`)
must be operations because there is no `get region` read-back. `clock sync` and `reboot` are
genuine operations. See DESIGN.md for the full explanation.

**powersaving bug found and fixed**: `powersaving` as a setting was sending `get powersaving`
(read) and `set powersaving on` (write) — both wrong. The firmware CLI uses bare `powersaving`
to read (returns "on"/"off") and `powersaving on` / `powersaving off` to write. Fixed via
`_BARE_COMMAND_KEYS` in `queue.py`; the set is extensible as more non-standard keys are found.

**clkreboot open question**: commissioning ends with bare `reboot`. `clkreboot` would sync the
clock before rebooting. Unknown whether MeshCore persists time to flash across a reboot. Needs
live bench test — see BACKLOG Research section.

## 2026-05-23 - Bench Investigation of the set prv.key Timeout

After shipping the new-mode fallback, the next live commission failed at the same
point: `password walter76` replied OK, `set prv.key <real key>` timed out with no
reply after 30s. The fallback recovered the auth on retry, but each retry hit the
exact same wall — the wizard was now stuck in a loop where the fallback worked
correctly but couldn't get past `set prv.key`.

Built `scripts/bench_session_invalidation.py` to test hypotheses against the
USB-attached bricked node from luna's gateway over the mesh, with mchd stopped so
the script owned the serial port. Every step prints `[SEND]` / `[REPLY]` /
`[NO REPLY]` so the bench tape is unambiguous. The script never sends `reboot`,
so any staged radio/key changes never apply.

### Run 1: hypothesis "password change invalidates the admin session"

Phases: factory login → `set name PHASE_A` → `password <new>` → `set name PHASE_B`
(no re-login) → re-login → `set name PHASE_C`. Result: A, B, and C all replied
`OK`. The session is **not** invalidated by `password <new>`. Hypothesis refuted.

### Run 2: hypothesis "`set prv.key` is the slow command"

Refocused Phases: factory login → `set name PHASE_A` (control) → `set prv.key
<junk>` (90s timeout) → `password <new>` → re-login → `set prv.key <junk>` (90s).

Results:
- Phase A: `set name PHASE_A` → `OK` (instant).
- Phase B: `set prv.key 1111...2222...` → **`'Error, bad key'`** (instant).
- Phase C: same junk key after password change → **`'Error, bad key'`** (instant).

Two key inferences:
- The firmware *validates the scalar clamping* on the private key. Our junk had
  bit 0 set in the first byte, violating the standard curve25519 `&= 248` clamp,
  so the firmware rejected it instantly with `'Error, bad key'`. `src/mch/meshkey.py`
  produces properly clamped keys, so real wizard-generated keys pass validation.
- A *validated* `set prv.key` doesn't just respond fast and lazily defer work.
  The processing the firmware does for a valid key (probably curve25519 scalar
  multiplication on the ESP32 to derive the new public key, plus flash writes)
  takes long enough that the response misses the 30s timeout in
  `gateway.send_command` (`src/mch/gateway.py`, line 353).

Side observation from Run 2: the post-password re-login failed
(`login failed for bbcde403cef3: None`), but Phase C still got an admin reply
without it. Consistent with the existing model in `CLAUDE.md`: after first login
the gateway pubkey is on the node's ACL and subsequent commands work without
re-authentication.

### Conclusion and next step

The bug is not session-related. The bug is that the default 30s timeout is too
short for `set prv.key` (and almost certainly `set radio`, which failed the same
way on 2026-05-22). Next: extend the bench with Phase D that sends a *real*
generated private key with a long timeout so we can measure the actual response
time, then ship a calibrated per-command timeout in commissioning.

### Run 3: Phase D with a real generated key (180s timeout)

Sent a fresh `generate_private_key()` value with a 180s timeout. The firmware
replied within the window with:
`OK, reboot to apply! New pubkey: <hex>`. The pubkey matched the one derived
locally from the same private key, so `src/mch/meshkey.py` produces the format
the firmware expects and our `derive_public_key` agrees with the firmware's
own derivation. The response was real, not lost — the 30s default timeout was
just too short for the firmware to finish its work (curve25519 scalar mult to
derive the new public key + flash write to persist the staged identity).

### Fix

Per-command timeout in `_send_commission_command`: `set prv.key` and `set radio`
get 180s; everything else keeps the 30s default. Targeted and reversible.

### Reference: firmware source for `set prv.key`

Handler at `src/helpers/CommonCLI.cpp` (MeshCore main, ~line 710):

```cpp
} else if (memcmp(config, "prv.key ", 8) == 0) {
  uint8_t prv_key[PRV_KEY_SIZE];
  bool success = mesh::Utils::fromHex(prv_key, PRV_KEY_SIZE, &config[8]);
  if (success && mesh::LocalIdentity::validatePrivateKey(prv_key)) {
    mesh::LocalIdentity new_id;
    new_id.readFrom(prv_key, PRV_KEY_SIZE);
    _callbacks->saveIdentity(new_id);
    strcpy(reply, "OK, reboot to apply! New pubkey: ");
    mesh::Utils::toHex(&reply[33], new_id.pub_key, PUB_KEY_SIZE);
  } else {
    strcpy(reply, "Error, bad key");
  }
}
```

`validatePrivateKey` is what rejects malformed input fast; `readFrom` does the
expensive public-key derivation; `saveIdentity` writes to flash. Then the reply
goes out — the reply is always sent, it just takes time. There is no remote-vs-
serial gate on `set prv.key` (only `get prv.key` is serial-only).

### Live confirmation (0.1.2)

After deploying 0.1.2 with the 180s per-command timeout, `mch commission` on
luna completed end-to-end against the bricked bench node:

- `setting name`, `setting guest password`, `setting password` → all fast.
- `setting generated private key` → completed within 180s (previously timed out at 30s).
- `setting radio` → completed within 180s.
- `reboot` → fire-and-forget.
- `verifying identity (2/3)` → new pubkey visible after the second post-reboot scan.

Direct USB check after reboot: live pubkey, radio, and name all matched the
wizard's snippet. The bricked node is now fully commissioned. The "set prv.key
times out" failure mode is closed for v1.15-era firmware.

### Live confirmation: `commission replace` (same session)

To validate `_login_for_replace`, the just-commissioned `81d6bf19` was erased
via USB (`meshcore-cli -r -s /dev/cu.usbserial-0001 erase`, "Erase filesystem
(serial only)") and re-discovered as a factory-fresh node with a new pubkey
`c26d49f6d122...`. The operator pasted the original `81d6bf19` config/secrets
snippet into luna's config files, reloaded mchd, and ran `mch commission` →
replace → selected `81d6bf19`. The wizard hit the same 180s flow for the
slow commands; direct USB check after reboot confirmed the original pubkey
restored from the secrets `prv_key`.

### Side observations from the live runs

- **Wizard's `group: default` was rejected by hot-reload.** Luna's `config.yaml`
  defines `QH` only. Tracked in `BACKLOG.md` — wizard should read live config
  and offer existing groups.
- **`mch validate` defaults to `~/config.yaml` instead of the mchd path.** Real
  bug — caught the file divergence here. Tracked.
- **`busy: scheduled sync` UX during commission** — scheduled sync of the just-
  added node 81d6bf19 grabbed the operation lock; commission had to retry
  several times before the sync window closed. Tracked.
- **Duplicate label spotted via `mch status`** — `2fd9c7bbc93d qh-wold1`
  (configured) and `1fd9c7bb qh-wold1` (ghost) share the same label. Loader
  doesn't flag this. Tracked.
- **Post-replace name regression to "Heltec Repeater".** Replace mode does not
  send `set name`; on identity change the firmware appears to fall back to a
  hardware-default name. Tracked — decision pending whether replace should
  always push `set name <label>`.

## 2026-05-23 - New-Mode Commission Fallback to User Password

- After shipping the replace-mode fallback (entry below), a live `commission new` run on the
  same bricked node hit the same class of bug at `set prv.key`: the password change just
  before it appears to invalidate the admin session on v1.15-era firmware, and the next
  command times out waiting for a response.
- Verified by raw serial (`meshcore-cli -r -s /dev/cu.usbserial-0001 get freq` etc.) that
  the node was still on factory frequency and factory pubkey after the timeout — i.e. the
  state-changing radio/key writes had not been applied (because they only take effect on
  reboot, which never happened). Only the admin password had actually changed.
- Extended the fallback to new mode the same way as replace: factory password first, then
  the user-supplied admin password from the wizard form. The CLI generates `prv_key` once
  before the retry loop (`cli.py:1129`) and resends the same value, so the originally
  feared "operator loses the daemon-generated prv_key" failure mode is mitigated for the
  common in-session retry case. It can still bite if the wizard itself is killed mid-run.
- The operator's framing is the right one: the mesh is fragile by nature, so partial-state
  recovery is the *purpose* of the tool, not an edge case. Future work should keep this
  bias: prefer idempotent commands and fallback strategies over assuming a clean baseline.

### Bench observation (v1.15.0-dee3e26 on Heltec V3)

- Symptom: `password <new>` returns OK, the very next CLI command sent over the mesh times
  out with no response.
- Reproduced on both `set radio` (replace mode, 2026-05-22) and `set prv.key` (new mode,
  2026-05-23). Same firmware build.
- Hypothesis: the password change closes the current admin session in some firmware paths;
  the next command runs without admin auth and is dropped silently.
- Mitigation in current code: keep password/identity changes late in the command list and
  rely on idempotent retry-with-fallback rather than ordering tricks. Direct serial reset
  is the bench escape hatch when both fallbacks are exhausted.

## 2026-05-23 - Replacement Commission Retry After Partial Failure

- On 2026-05-22 a replacement commission on Heltec V3 / MeshCore v1.15 changed the admin
  password and private key, then timed out waiting for `set radio`. The next attempt could
  not log in because the factory password was already gone.
- The fix is stateless: on factory-login failure in replace mode, try the managed password
  from `secrets.yaml`. The mutation commands (`set guest.password`, `password`,
  `set prv.key`, `set radio`) are idempotent for the same target values, so re-sending the
  whole sequence is safe.
- Login is now per-mode, not shared in `handle_commission`. `_commission_new` keeps the
  factory-only path; `_commission_replace` uses `_login_for_replace` which announces the
  fallback via the progress callback so the operator sees what happened.

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
